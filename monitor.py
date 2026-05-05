"""
Claimyshare auto-withdraw — watch-loop mode (multi-account).

Polls the site's hot wallet (8MrX...) on Solana mainnet. When the
balance goes up by more than TOPUP_THRESHOLD_LAMPORTS (i.e. the admin
topped it up so payouts can flow), this script iterates over every
account in config.json and fires ONE withdraw per eligible account,
spaced INTER_ACCOUNT_SPACING_SEC apart.

Eligibility per account:
  * not inside its observed ~24h daily cooldown, and
  * at least PER_ACCOUNT_SPACING_SEC has passed since that account's
    previous attempt (per-JWT rate limit is 3 req / 60 s).

State (last hot balance + per-account last success / attempt times) is
persisted to state.json so restarts don't re-fire attempts.

Run:
    python monitor.py

Ctrl+C stops cleanly.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from core import (
    DAILY_COOLDOWN_SEC,
    EXIT_COOLDOWN,
    EXIT_OK,
    HOT_WALLET,
    MIN_WITHDRAW_SOL,
    attempt_withdraw,
    bootstrap_last_success_iso,
    fetch_claimable_balance,
    get_account_state,
    get_balance_lamports,
    iso_to_unix,
    load_accounts,
    load_state,
    make_logger,
    save_state,
    utc_now_iso,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

POLL_INTERVAL_SEC = 10                   # how often we check the hot wallet balance
TOPUP_THRESHOLD_LAMPORTS = 100_000_000   # 0.1 SOL — only fire on real admin refills
# Per-account spacing between attempts from the SAME account across different
# topup events. Aligned with withdraw.py's INTER_ACCOUNT_SPACING_SEC (5s);
# core.py's 429 retry logic absorbs any burst that exceeds the site's
# 3 req / 60 s per-JWT limit.
PER_ACCOUNT_SPACING_SEC = 5

# Parallel firing: fire all eligible accounts simultaneously when a top-up
# is detected, instead of sequential with INTER_ACCOUNT_SPACING_SEC between
# them. Drastically increases hit rate when hot wallet drains fast.
# Trade-off: makes the burst pattern from one IP more visible to WAF.
PARALLEL_FIRE = True
MAX_PARALLEL_WORKERS = 32            # cap concurrent in-flight POSTs
# Stagger the parallel dispatch so account #N waits N * PARALLEL_STAGGER_MS
# before its first request fires. 0 = pure burst (all reqs in same ms,
# maximum sniping speed but high 429-storm risk — relies on aggressive retry
# in core.py to recover the misses). Raise to 200/500/2000 if you start
# seeing IP-level blocks (403 / connection refused, NOT just 429).
# 2ms = still effectively a burst (64 accounts dispatched over ~126ms) but
# avoids the exact-same-timestamp fingerprint that some WAFs flag.
PARALLEL_STAGGER_MS = 2

# Sequential fallback (only used if PARALLEL_FIRE = False):
INTER_ACCOUNT_SPACING_SEC = 5

# Stop firing if hot wallet drops below this — in parallel mode this is
# checked once before kicking off the batch; in sequential mode it's
# checked between accounts.
HOT_WALLET_FLOOR_LAMPORTS = 200_000  # ~0.0002 SOL

# Log a short "alive" line every N seconds even when nothing interesting is
# happening. Without this, a quiet hot wallet produces zero log output and
# the bot looks dead to an outside observer. Set to 0 to disable.
HEARTBEAT_INTERVAL_SEC = 300  # 5 minutes

# --- Balance pre-cache ---
# A background thread continuously refreshes each account's claimable balance
# from /api/user and stores it in an in-memory cache. When a top-up arrives,
# _process_topup hands the cached value to attempt_withdraw via
# amount_sol_override, skipping the live fetch (and its 0-17s rate-limit
# retry tax) entirely. This is what makes the snipe "instant".
#
# Trade-off: balance can be slightly stale. claimyshare's balanceSolTask
# only changes when the user completes a task or successfully withdraws,
# both of which are events WE control, so staleness is bounded by our own
# refresh cadence. Top-ups themselves do NOT change balanceSolTask (the
# top-up refills claimyshare's hot wallet, not the user's claimable amount),
# so a top-up never invalidates a cached value.
BALANCE_PRECACHE_ENABLED = True
BALANCE_REFRESH_SPACING_SEC = 1.5    # delay between accounts inside one pass
BALANCE_REFRESH_GAP_SEC = 30         # extra rest after a full pass through all accounts
BALANCE_CACHE_MAX_AGE_SEC = 600.0    # use cached value if fetched within last 10 min
# How long after startup we wait before declaring the cache "ready". Until
# this point a top-up is allowed to fall back to live-fetch on cache miss.
BALANCE_CACHE_WARMUP_SEC = 90.0

_stop = False


def _handle_sigint(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    print("\n[monitor] stop requested, finishing current iteration...", flush=True)


# ---------------------------------------------------------------------------
# Balance cache
# ---------------------------------------------------------------------------

class BalanceCache:
    """Thread-safe per-account cache of /api/user `balanceSolTask`.

    A background thread populates this from claimyshare every
    BALANCE_REFRESH_SPACING_SEC; consumers (the top-up firing path) read it
    via .get(name) and pass the value to attempt_withdraw via
    amount_sol_override so they don't have to pay the live-fetch tax
    inside the snipe window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # name -> (balance_or_None, fetched_at_unix)
        self._cache: dict[str, tuple[Optional[float], float]] = {}
        self._started_at = time.time()

    def update(self, name: str, balance: Optional[float]) -> None:
        with self._lock:
            self._cache[name] = (balance, time.time())

    def get(
        self, name: str, max_age_sec: float = BALANCE_CACHE_MAX_AGE_SEC
    ) -> Optional[float]:
        """Return cached balance if present and fresh; else None."""
        with self._lock:
            entry = self._cache.get(name)
            if entry is None:
                return None
            balance, ts = entry
            if time.time() - ts > max_age_sec:
                return None
            return balance

    def is_warmed_up(self) -> bool:
        """True after BALANCE_CACHE_WARMUP_SEC since startup.

        Top-up handler uses this to decide whether a cache miss should fall
        back to live-fetch (still in warmup) vs treat the miss as a real
        skip (cache is supposed to be populated by now).
        """
        return (time.time() - self._started_at) >= BALANCE_CACHE_WARMUP_SEC

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            ages = [now - ts for _, ts in self._cache.values()]
            return {
                "size": len(self._cache),
                "freshest_age": min(ages) if ages else 0.0,
                "oldest_age": max(ages) if ages else 0.0,
            }


def _balance_refresher_loop(
    accounts: list[dict], cache: BalanceCache, log
) -> None:
    """Background loop: rotate through accounts, refreshing each balance.

    Per-account spacing keeps us under the per-IP rate window so the
    refresh itself doesn't trigger 429s. After a full pass we sleep
    BALANCE_REFRESH_GAP_SEC extra so total cycle time stays generous.
    """
    while not _stop:
        for acc in accounts:
            if _stop:
                return
            name = acc.get("name", "?")
            try:
                # Silent on success, escalate on errors only.
                msgs: list[str] = []
                balance = fetch_claimable_balance(
                    acc, lambda m: msgs.append(m)
                )
                cache.update(name, balance)
                if balance is None:
                    # First failure for an account is worth knowing about;
                    # downgrade to one-line summary to avoid log spam.
                    last = msgs[-1] if msgs else "unknown error"
                    log(f"[balance-cache] {name}: refresh failed ({last[:120]})")
            except Exception as e:  # noqa: BLE001
                log(f"[balance-cache] {name}: exception {e}")
            # Spacing between accounts within one pass.
            for _ in range(int(BALANCE_REFRESH_SPACING_SEC * 10)):
                if _stop:
                    return
                time.sleep(0.1)
        # Extra rest after a full sweep.
        if _stop:
            return
        stats = cache.stats()
        log(
            f"[balance-cache] full sweep done | size={stats['size']} "
            f"freshest={stats['freshest_age']:.0f}s oldest={stats['oldest_age']:.0f}s | "
            f"sleeping {BALANCE_REFRESH_GAP_SEC}s before next pass."
        )
        for _ in range(BALANCE_REFRESH_GAP_SEC):
            if _stop:
                return
            time.sleep(1)


def _start_balance_refresher(
    accounts: list[dict], cache: BalanceCache, log
) -> threading.Thread:
    t = threading.Thread(
        target=_balance_refresher_loop,
        args=(accounts, cache, log),
        name="balance-refresher",
        daemon=True,
    )
    t.start()
    return t


def _human_duration(sec: float) -> str:
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _seconds_until_cooldown_ends(last_success_iso: Optional[str], now: float) -> float:
    if not last_success_iso:
        return 0.0
    remaining = (iso_to_unix(last_success_iso) + DAILY_COOLDOWN_SEC) - now
    return max(0.0, remaining)


def _eligible_accounts(accounts: list[dict], state: dict, now: float) -> list[dict]:
    """Return accounts that are neither in daily cooldown nor in their own rate-limit window."""
    eligible: list[dict] = []
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
        if _seconds_until_cooldown_ends(entry["last_success_at"], now) > 0:
            continue
        if now - float(entry["last_attempt_ts"]) < PER_ACCOUNT_SPACING_SEC:
            continue
        eligible.append(acc)
    return eligible


def _bootstrap_accounts(accounts: list[dict], state: dict, log) -> None:
    """
    On first-ever run, fill last_success_at for each account from chain.

    Accounts that SHARE a wallet_address with any other account are SKIPPED:
    bootstrap can only see the on-chain destination, so a single incoming tx
    to that wallet would be (incorrectly) attributed to every account using
    it, poisoning the cooldown state. We leave last_success_at = None for
    those accounts (treating them as "ready") and rely on per-attempt state
    updates once the bot actually fires.

    Only accounts whose wallet_address is unique across the list get the
    chain-scan treatment.
    """
    # Count how many accounts use each destination wallet.
    wallet_count: dict[str, int] = {}
    for acc in accounts:
        w = acc.get("wallet_address", "")
        wallet_count[w] = wallet_count.get(w, 0) + 1

    shared_wallets: dict[str, list[str]] = {}
    for acc in accounts:
        w = acc.get("wallet_address", "")
        if wallet_count.get(w, 0) > 1:
            shared_wallets.setdefault(w, []).append(acc["name"])

    # Report shared-wallet groups once, up front, and heal any stale
    # last_success_at left over from earlier (pre-fix) bootstrap runs.
    for wallet, names in shared_wallets.items():
        preview = ", ".join(names[:3])
        if len(names) > 3:
            preview += f", ... +{len(names) - 3} more"
        log(
            f"[bootstrap-skip] wallet {wallet[:8]}...{wallet[-4:]} is shared "
            f"by {len(names)} account(s) [{preview}]; on-chain scan can't "
            "distinguish them, leaving cooldown untracked until first fire."
        )

        # Heal: if none of the accounts in this group have ever been fired
        # by monitor.py (last_attempt_ts == 0 for all), any existing
        # last_success_at on them is definitely a bootstrap artifact from
        # a previous (pre-fix) run — safe to clear. If ANY account has a
        # real attempt history, we leave the group alone to avoid wiping
        # legitimate per-account cooldowns.
        entries = [get_account_state(state, n) for n in names]
        never_fired = all(
            float(e.get("last_attempt_ts", 0.0) or 0.0) == 0.0 for e in entries
        )
        poisoned = [
            n for n, e in zip(names, entries)
            if e.get("last_success_at") is not None
        ]
        if never_fired and poisoned:
            log(
                f"[bootstrap-heal] clearing stale last_success_at on "
                f"{len(poisoned)} account(s) under {wallet[:8]}...{wallet[-4:]} "
                "(bootstrap artifact from a previous run)."
            )
            for entry in entries:
                entry["last_success_at"] = None

    # Scan chain only for accounts with a unique destination wallet.
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
        if entry["last_success_at"] is not None:
            continue
        if wallet_count.get(acc.get("wallet_address", ""), 0) > 1:
            continue
        log(f"[{acc['name']}] [bootstrap] scanning chain for last hot-wallet payout...")
        discovered = bootstrap_last_success_iso(acc["wallet_address"], log)
        if discovered:
            entry["last_success_at"] = discovered
    save_state(state)


def _log_startup_status(accounts: list[dict], state: dict, log) -> None:
    now = time.time()
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
        lsa = entry["last_success_at"]
        if lsa:
            cd = _seconds_until_cooldown_ends(lsa, now)
            status = (
                f"ready ({_human_duration(-cd)} past cooldown)"
                if cd <= 0
                else f"cooldown ends in {_human_duration(cd)}"
            )
            log(f"[{acc['name']}] last success {lsa}, {status}")
        else:
            log(f"[{acc['name']}] no prior success; will attempt on first top-up.")


def _record_attempt_outcome(
    state: dict,
    acc: dict,
    exit_code: int,
    log,
) -> None:
    """Update per-account state based on a finished attempt."""
    entry = get_account_state(state, acc["name"])
    if exit_code == EXIT_OK:
        entry["last_success_at"] = utc_now_iso()
        log(
            f"[{acc['name']}] [ok] success; next attempt earliest in "
            f"{_human_duration(DAILY_COOLDOWN_SEC)}."
        )
    elif exit_code == EXIT_COOLDOWN:
        # Could be either 60 s rate limit or 24 h daily; assume daily to be safe.
        entry["last_success_at"] = utc_now_iso()
        log(f"[{acc['name']}] [cooldown] server refused; assuming 24h from now.")
    else:
        log(f"[{acc['name']}] [error] failed (exit={exit_code}); keep cooldown unchanged.")


def _fire_one_threaded(
    acc: dict,
    state: dict,
    state_lock: threading.Lock,
    log,
    start_delay_sec: float = 0.0,
    amount_sol_override: Optional[float] = None,
) -> tuple[dict, int]:
    """Worker thread body: optional initial delay (for staggered dispatch),
    then mark attempt, fire, update state safely. Pass-through of
    amount_sol_override lets the snipe path skip the live balance fetch.
    """
    if start_delay_sec > 0:
        time.sleep(start_delay_sec)
    with state_lock:
        entry = get_account_state(state, acc["name"])
        entry["last_attempt_ts"] = time.time()
    log(f"[{acc['name']}] [fire] starting withdraw.")
    exit_code, _parsed, _status = attempt_withdraw(
        acc, log, verify_onchain=False, amount_sol_override=amount_sol_override
    )
    with state_lock:
        _record_attempt_outcome(state, acc, exit_code, log)
    return acc, exit_code


def _resolve_overrides(
    eligible: list[dict],
    cache: Optional[BalanceCache],
    log,
) -> tuple[list[tuple[dict, Optional[float]]], int, int, int]:
    """For each eligible account decide whether the snipe will use a cached
    balance, fall back to live fetch, or skip outright (cache says dust).

    Returns (kept, hits, misses, dust_skipped) where `kept` is the list of
    (acc, override) tuples to actually fire. Accounts in dust state are
    pre-filtered here so they never even occupy a worker slot.
    """
    kept: list[tuple[dict, Optional[float]]] = []
    hits = 0
    misses = 0
    dust = 0

    for acc in eligible:
        name = acc.get("name", "?")
        if cache is None:
            kept.append((acc, None))  # legacy live-fetch path
            misses += 1
            continue
        cached = cache.get(name)
        if cached is None:
            # Miss. If we're still warming up, give the live-fetch path a
            # chance; if we're fully warm, this likely means a real auth or
            # network failure earlier — let the worker log it via live-fetch.
            kept.append((acc, None))
            misses += 1
            continue
        if cached < MIN_WITHDRAW_SOL:
            dust += 1
            log(
                f"[{name}] [pre-skip] cached balance {cached:.9f} SOL below "
                f"threshold {MIN_WITHDRAW_SOL}; not firing."
            )
            continue
        kept.append((acc, cached))
        hits += 1

    return kept, hits, misses, dust


def _process_topup_parallel(
    eligible: list[dict],
    state: dict,
    log,
    cache: Optional[BalanceCache] = None,
) -> None:
    plan, hits, misses, dust = _resolve_overrides(eligible, cache, log)
    if not plan:
        log(f"[parallel] no accounts to fire (cache hits={hits} misses={misses} dust-skip={dust}).")
        return

    stagger = max(PARALLEL_STAGGER_MS, 0) / 1000.0
    total_dispatch = stagger * (len(plan) - 1)
    log(
        f"[parallel] firing {len(plan)} account(s) "
        f"(cache hits={hits} misses={misses} dust-skip={dust}; "
        f"max workers={MAX_PARALLEL_WORKERS}, "
        f"stagger={PARALLEL_STAGGER_MS}ms => dispatch window {total_dispatch:.1f}s)."
    )
    state_lock = threading.Lock()
    workers = min(MAX_PARALLEL_WORKERS, len(plan))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wd") as ex:
        futures = [
            ex.submit(
                _fire_one_threaded, acc, state, state_lock, log,
                i * stagger, override,
            )
            for i, (acc, override) in enumerate(plan)
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                log(f"[error] worker thread crashed: {e}")

    save_state(state)
    log(f"[parallel] all {len(plan)} attempts complete.")


def _process_topup_sequential(
    eligible: list[dict],
    state: dict,
    log,
    cache: Optional[BalanceCache] = None,
) -> None:
    plan, hits, misses, dust = _resolve_overrides(eligible, cache, log)
    if not plan:
        log(f"[sequential] no accounts to fire (cache hits={hits} misses={misses} dust-skip={dust}).")
        return
    log(
        f"[sequential] firing {len(plan)} account(s) "
        f"(cache hits={hits} misses={misses} dust-skip={dust})."
    )

    for i, (acc, override) in enumerate(plan):
        if _stop:
            break

        # Mid-sequence hot-wallet floor guard.
        if i > 0:
            current_hot_now = get_balance_lamports(HOT_WALLET)
            if current_hot_now is not None and current_hot_now < HOT_WALLET_FLOOR_LAMPORTS:
                log(
                    f"[topup] hot wallet drained to "
                    f"{current_hot_now/1e9:.9f} SOL; aborting remaining "
                    f"{len(plan) - i} account(s)."
                )
                break

        entry = get_account_state(state, acc["name"])
        log(f"[{acc['name']}] [fire] {i + 1}/{len(plan)} starting withdraw.")
        entry["last_attempt_ts"] = time.time()
        exit_code, _parsed, _status = attempt_withdraw(
            acc, log, verify_onchain=False, amount_sol_override=override
        )
        _record_attempt_outcome(state, acc, exit_code, log)
        save_state(state)

        if i < len(plan) - 1 and not _stop:
            _sleep_with_stop(INTER_ACCOUNT_SPACING_SEC)


def _process_topup(
    accounts: list[dict],
    state: dict,
    current_hot: int,
    prev_hot: int,
    log,
    cache: Optional[BalanceCache] = None,
) -> None:
    """Dispatch eligible accounts on a single top-up event."""
    now = time.time()
    eligible = _eligible_accounts(accounts, state, now)
    delta = current_hot - prev_hot
    log(
        f"[topup] hot wallet {prev_hot/1e9:.9f} -> {current_hot/1e9:.9f} SOL "
        f"(+{delta/1e9:.9f}); {len(eligible)} of {len(accounts)} account(s) eligible."
    )
    if not eligible:
        return

    # Pre-flight floor check (parallel mode can't check mid-burst).
    pre_check = get_balance_lamports(HOT_WALLET)
    if pre_check is not None and pre_check < HOT_WALLET_FLOOR_LAMPORTS:
        log(
            f"[topup] hot wallet already drained to {pre_check/1e9:.9f} SOL "
            "before we could fire; aborting batch."
        )
        return

    if PARALLEL_FIRE:
        _process_topup_parallel(eligible, state, log, cache)
    else:
        _process_topup_sequential(eligible, state, log, cache)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Claimyshare watch-loop auto-withdraw.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python monitor.py                      # normal run (no chain bootstrap)\n"
            "  python monitor.py --bootstrap          # seed last_success_at from on-chain history (unique-wallet setups only)\n"
            "  python monitor.py --reset-cooldowns    # clear all cooldowns on startup\n"
        ),
    )
    p.add_argument(
        "--reset-cooldowns",
        action="store_true",
        help=(
            "Clear last_success_at and last_attempt_ts for every account on "
            "startup, then skip the bootstrap chain scan. Use this when "
            "accounts share a destination wallet and bootstrap assigned the "
            "same (usually wrong) cooldown to all of them."
        ),
    )
    p.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Run the on-chain scan to seed last_success_at for accounts "
            "with no prior history. DEFAULT IS OFF because most setups "
            "have multiple accounts sharing one destination wallet, where "
            "the scan can't tell which account got paid and would assign "
            "the same (often wrong) cooldown to everyone. The shared-wallet "
            "auto-skip in _bootstrap_accounts() already protects against "
            "that, but turning bootstrap off entirely is simpler and avoids "
            "any chain RPC noise on startup. Pass this flag if you have "
            "unique wallets per account and want pre-populated cooldowns."
        ),
    )
    return p.parse_args()


def _reset_cooldowns(accounts: list[dict], state: dict, log) -> None:
    """Clear all per-account cooldown fields. Called by --reset-cooldowns."""
    cleared = 0
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
        had_success = entry.get("last_success_at") is not None
        entry["last_success_at"] = None
        entry["last_attempt_ts"] = 0.0
        if had_success:
            cleared += 1
    save_state(state)
    log(
        f"[reset] cleared cooldown for {cleared} of {len(accounts)} account(s). "
        "All accounts will be eligible on the next top-up."
    )


def main() -> int:
    args = _parse_args()
    accounts = load_accounts()
    log = make_logger("monitor.log")
    signal.signal(signal.SIGINT, _handle_sigint)

    log(
        f"monitor started | accounts={len(accounts)} "
        f"poll={POLL_INTERVAL_SEC}s topup>={TOPUP_THRESHOLD_LAMPORTS/1e9:.6f} SOL "
        f"reset_cooldowns={args.reset_cooldowns} bootstrap={args.bootstrap} "
        f"precache={BALANCE_PRECACHE_ENABLED}"
    )

    state = load_state()

    if args.reset_cooldowns:
        _reset_cooldowns(accounts, state, log)
        # --reset-cooldowns implies skip-bootstrap (explicit "forget history").
    elif args.bootstrap:
        _bootstrap_accounts(accounts, state, log)
    else:
        log("[bootstrap] skipped (default — use --bootstrap to opt in).")

    _log_startup_status(accounts, state, log)

    # Start the balance pre-cache refresher. Daemon thread, so it dies with
    # the main loop. First snipe inside BALANCE_CACHE_WARMUP_SEC after
    # startup may still hit live-fetch for cache misses.
    cache: Optional[BalanceCache] = None
    if BALANCE_PRECACHE_ENABLED:
        cache = BalanceCache()
        _start_balance_refresher(accounts, cache, log)
        log(
            f"[balance-cache] refresher started (spacing={BALANCE_REFRESH_SPACING_SEC}s, "
            f"gap={BALANCE_REFRESH_GAP_SEC}s, max_age={BALANCE_CACHE_MAX_AGE_SEC:.0f}s, "
            f"warmup={BALANCE_CACHE_WARMUP_SEC:.0f}s)."
        )

    last_balance = int(state.get("last_hot_balance_lamports", 0))
    # For log-throttling only.
    last_logged_balance = last_balance
    # For the periodic "alive" heartbeat.
    last_heartbeat_ts = time.time()

    while not _stop:
        current = get_balance_lamports(HOT_WALLET)
        now = time.time()

        if current is None:
            log("[warn] RPC balance read failed; sleeping and retrying.")
            _sleep_with_stop(POLL_INTERVAL_SEC)
            continue

        # Prime on the very first successful read.
        if last_balance == 0:
            last_balance = current
            last_logged_balance = current
            state["last_hot_balance_lamports"] = current
            save_state(state)
            log(f"initial hot wallet balance: {current/1e9:.9f} SOL")

        delta = current - last_balance
        topup_detected = delta >= TOPUP_THRESHOLD_LAMPORTS

        if topup_detected:
            eligible_count = len(_eligible_accounts(accounts, state, now))
            if eligible_count > 0:
                _process_topup(accounts, state, current, last_balance, log, cache)
            else:
                # Top-up but everyone is on cooldown — log once, then move on.
                log(
                    f"[topup-skip] {last_balance/1e9:.9f} -> {current/1e9:.9f} SOL "
                    f"(+{delta/1e9:.9f}); all accounts in cooldown."
                )
            # Always advance the baseline so we don't re-fire on the same refill.
            last_balance = current
            last_logged_balance = current

        else:
            # Non-topup: only occasionally log balance drift.
            if abs(current - last_logged_balance) >= TOPUP_THRESHOLD_LAMPORTS:
                log(
                    f"balance {last_logged_balance/1e9:.9f} -> "
                    f"{current/1e9:.9f} SOL (no topup)."
                )
                last_logged_balance = current
            last_balance = current

        # Persist state each loop.
        state["last_hot_balance_lamports"] = last_balance
        save_state(state)

        # Periodic "alive" heartbeat so a quiet hot wallet doesn't look dead.
        if (
            HEARTBEAT_INTERVAL_SEC > 0
            and now - last_heartbeat_ts >= HEARTBEAT_INTERVAL_SEC
        ):
            eligible_now = _eligible_accounts(accounts, state, now)
            log(
                f"[heartbeat] alive | hot_wallet={current/1e9:.9f} SOL | "
                f"eligible={len(eligible_now)}/{len(accounts)} | "
                f"next poll in {POLL_INTERVAL_SEC}s"
            )
            last_heartbeat_ts = now

        _sleep_with_stop(POLL_INTERVAL_SEC)

    log("[monitor] stopped.")
    return 0


def _sleep_with_stop(seconds: int) -> None:
    """Sleep in small chunks so Ctrl+C is responsive."""
    end = time.time() + seconds
    while time.time() < end and not _stop:
        time.sleep(min(1.0, end - time.time()))


if __name__ == "__main__":
    sys.exit(main())
