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
import json
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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

# Lowered from 10s -> 3s -> 1s after observing the admin hot-wallet drains
# in ~3 minutes under bot competition. With Helius (HELIUS_API_KEY set in
# env, prepended in core.SOLANA_RPCS) the free tier comfortably handles
# 60 reqs/min (1s polling = 86k req/day, well under the 100k/day cap).
# Detection lag drops to ~0.5s avg vs 1.5s on 3s polling vs 5s on 10s.
# That extra 1s of head-start is the entire difference between catching
# a competitor-drained refill vs ABORTING with "hot wallet already drained".
# Without a Helius key, public RPC may rate-limit at 1s; revert to 3s if
# you see "rate-limited" or "503" entries from the Solana RPC layer.
POLL_INTERVAL_SEC = 1                    # how often we check the hot wallet balance
TOPUP_THRESHOLD_LAMPORTS = 100_000_000   # 0.1 SOL — only fire on real admin refills

# Accounts that should ALWAYS fire first regardless of balance, in the order
# listed. Put your "most important to claim no matter what" accounts here.
# Leave empty tuple () to revert to pure balance-DESC ordering. These fire
# even before the biggest-balance account, at positions 0, 1, 2, ... of the
# plan list.
PRIORITY_ACCOUNTS: tuple[str, ...] = ("daffa14",)
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
# Worker cap is adapted at startup (see main()) based on whether
# proxies.json is populated:
#   * pool populated  -> 64 (each IP only sees 64/N in-flight, safe)
#   * pool empty      -> 32 (single-VPS IP; 64 guarantees 429 storm)
# This default is the proxy-on number; main() tightens it when the pool
# is empty so a silently-missing proxies.json can't re-introduce the
# original self-DDoS pattern.
MAX_PARALLEL_WORKERS = 64            # cap concurrent in-flight POSTs (auto-tightened to 32 if no proxy)
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
# Tightened settings (May 2026): user wants near-real-time balance update so
# bot can detect balance increases (e.g. claimable rewards from completed
# tasks) within ~1.5 min instead of ~5 min. With proxy pool active, the
# extra /api/user load is distributed across rotating IPs and per-IP rate
# limit is no longer the bottleneck.
BALANCE_REFRESH_SPACING_SEC = 1.0    # delay between accounts inside one pass
BALANCE_REFRESH_GAP_SEC = 30         # extra rest after a full pass through all accounts
BALANCE_CACHE_MAX_AGE_SEC = 1800.0   # use cached value if fetched within last 30 min
# Only refresh an account if its cached value is older than this threshold.
# Lowered from 300s -> 60s so each sweep refreshes nearly all accounts,
# giving us a ~1.5 min worst-case staleness on balance increases. This is
# 5x more frequent than before but proxy distributes the load (each IP
# only sees ~1 req per minute on average).
BALANCE_REFRESH_AGE_THRESHOLD_SEC = 60.0
# How long after startup we wait before declaring the cache "ready". Until
# this point a top-up is allowed to fall back to live-fetch on cache miss.
BALANCE_CACHE_WARMUP_SEC = 90.0
# Adaptive auto-pause: if the refresher hits this many consecutive failures
# (every account in the current sweep returning None), it's a clear sign
# the IP is in a hard CloudFlare rate-limit state and continuing to hit
# /api/user is just keeping the WAF angry. Pause for the duration below
# so the IP can actually cool down.
BALANCE_REFRESH_FAIL_PAUSE_THRESHOLD = 10
BALANCE_REFRESH_PAUSE_SEC = 300       # 5 minutes of zero traffic on /api/user
# Sweep-level fail pause: if a single sweep has this many failures total
# (not necessarily consecutive), assume IP is in a sustained rate-limit
# state and pause. Catches the case where a 429 storm is interspersed
# with occasional successes (which would reset the consecutive counter).
BALANCE_REFRESH_SWEEP_FAIL_THRESHOLD = 15
# Persisted cache snapshot. Other tools (account_status.py) read this file
# instead of hitting /api/user themselves, so a status check while the bot
# is running doesn't double-burst the IP rate limit. Written after every
# full sweep of the refresher.
BALANCE_CACHE_SNAPSHOT_PATH = Path(__file__).resolve().parent / "balance_cache.json"

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

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of the whole cache.

        Used by save_to_disk; readers in other processes call back through
        the same shape (see load_balance_cache_snapshot below).
        """
        with self._lock:
            return {
                "saved_at": time.time(),
                "entries": {
                    name: {"balance": balance, "fetched_at": ts}
                    for name, (balance, ts) in self._cache.items()
                },
            }

    def save_to_disk(self, path: Path) -> None:
        """Write a snapshot to disk so other tools can read it without
        hitting /api/user themselves. Writes to a tmp file then renames
        atomically so a concurrent reader never sees a half-written file.
        """
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.snapshot(), separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:  # noqa: BLE001 - best-effort, never block the bot
            pass

    def load_from_disk(
        self, path: Path = None, max_age_sec: float = BALANCE_CACHE_MAX_AGE_SEC
    ) -> int:
        """Pre-populate cache from a disk snapshot written by a previous run.

        Used on startup to avoid the cold-start /api/user burst: when the bot
        restarts (especially after a 429-storm-triggered manual restart), the
        IP is still hot from CloudFlare's perspective, so hitting /api/user
        for all 64 accounts immediately just re-triggers the storm.

        Loading the previous snapshot lets the TTL-based refresh skip every
        account whose entry is still fresh, spreading the warmup over many
        sweeps instead of bursting on sweep 1. Only entries with a real
        balance (not None) and timestamp younger than max_age_sec are loaded.

        Returns the number of entries loaded.
        """
        snap_path = path if path is not None else BALANCE_CACHE_SNAPSHOT_PATH
        snap = load_balance_cache_snapshot(snap_path, max_age_sec=max_age_sec)
        if snap is None:
            return 0
        now = time.time()
        loaded = 0
        with self._lock:
            for name, (balance, ts) in snap.items():
                if not isinstance(balance, (int, float)):
                    continue
                if (now - ts) > max_age_sec:
                    continue
                self._cache[name] = (balance, ts)
                loaded += 1
        return loaded


def load_balance_cache_snapshot(
    path: Path = BALANCE_CACHE_SNAPSHOT_PATH,
    max_age_sec: float = BALANCE_CACHE_MAX_AGE_SEC,
) -> Optional[dict[str, tuple[Optional[float], float]]]:
    """Read a balance-cache snapshot written by a monitor.py instance.

    Returns a {name: (balance, fetched_at)} dict suitable for direct use,
    or None if the file is missing, malformed, or older than max_age_sec.
    Per-entry freshness is checked by the consumer.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    saved_at = float(data.get("saved_at") or 0.0)
    if saved_at <= 0 or (time.time() - saved_at) > max_age_sec:
        return None
    out: dict[str, tuple[Optional[float], float]] = {}
    for name, entry in (data.get("entries") or {}).items():
        if not isinstance(entry, dict):
            continue
        balance = entry.get("balance")
        ts = float(entry.get("fetched_at") or 0.0)
        out[name] = (balance, ts)
    return out


def _balance_refresher_loop(
    accounts: list[dict], cache: BalanceCache, log
) -> None:
    """Background loop: rotate through accounts, refreshing each balance.

    Smart refresh: skip accounts whose cache is still fresh (younger than
    BALANCE_REFRESH_AGE_THRESHOLD_SEC). Drastically reduces /api/user load
    once cache is warm. Per-account spacing keeps us under the per-IP rate
    window. After a full pass we sleep BALANCE_REFRESH_GAP_SEC extra.

    On refresh failure we PRESERVE the previous cached value (don't
    overwrite with None) so a transient 429 doesn't degrade the cache.
    Two pause triggers:
      - consecutive: 10 in-a-row fails (hard storm)
      - sweep-level: 15+ fails per sweep (sustained storm interspersed
        with occasional successes)
    """
    consecutive_fails = 0
    while not _stop:
        success_count = 0
        fail_count = 0
        skip_count = 0
        paused = False

        for acc in accounts:
            if _stop:
                return
            name = acc.get("name", "?")

            # Skip if cache is still fresh enough (TTL-based refresh).
            # cache.get returns None if missing OR older than the threshold,
            # so a non-None result means "we already have a recent value".
            if (
                cache.get(name, max_age_sec=BALANCE_REFRESH_AGE_THRESHOLD_SEC)
                is not None
            ):
                skip_count += 1
                continue

            try:
                # Peek at previous cached value (any age within MAX_AGE) so
                # we can detect a balance increase after the new fetch. None
                # means we have no comparable prior value.
                previous_balance = cache.get(
                    name, max_age_sec=BALANCE_CACHE_MAX_AGE_SEC
                )

                # single_attempt = no retry. A 429 here will be seen again
                # on next sweep, no urgency. Without this flag a rate-limit
                # storm would amplify (each fail = 4 API calls × 17s).
                msgs: list[str] = []
                balance = fetch_claimable_balance(
                    acc, lambda m: msgs.append(m), single_attempt=True
                )
                if balance is None:
                    # Preserve last good cached value: do NOT overwrite with None.
                    # If never cached, leave absent so cache.get returns None as
                    # before. The max_age_sec check still catches truly stale
                    # entries.
                    consecutive_fails += 1
                    fail_count += 1
                    last = msgs[-1] if msgs else "unknown error"
                    log(f"[balance-cache] {name}: refresh failed ({last[:120]})")
                else:
                    # Detect balance increase (new claimable rewards, e.g.
                    # task completion). Use a small epsilon to ignore
                    # floating-point noise. Only log meaningful increases
                    # (>= 1e-6 SOL = 0.000001) to avoid noise from rounding.
                    if (
                        previous_balance is not None
                        and balance > previous_balance + 1e-6
                    ):
                        delta = balance - previous_balance
                        log(
                            f"[balance-cache] [INCREASE] {name} "
                            f"+{delta:.6f} SOL "
                            f"(was {previous_balance:.6f}, now {balance:.6f})"
                        )
                    cache.update(name, balance)
                    consecutive_fails = 0
                    success_count += 1
            except Exception as e:  # noqa: BLE001
                consecutive_fails += 1
                fail_count += 1
                log(f"[balance-cache] {name}: exception {e}")

            # Auto-pause: too many failures in a row -> IP is in a hard
            # rate-limit state, continuing only keeps the WAF angry.
            if consecutive_fails >= BALANCE_REFRESH_FAIL_PAUSE_THRESHOLD:
                log(
                    f"[balance-cache] {consecutive_fails} consecutive "
                    f"failures -> pausing refresher for "
                    f"{BALANCE_REFRESH_PAUSE_SEC}s to let IP cool down."
                )
                for _ in range(BALANCE_REFRESH_PAUSE_SEC):
                    if _stop:
                        return
                    time.sleep(1)
                consecutive_fails = 0
                paused = True
                break  # restart the sweep from the top

            # Spacing between accounts within one pass.
            for _ in range(int(BALANCE_REFRESH_SPACING_SEC * 10)):
                if _stop:
                    return
                time.sleep(0.1)

        # End of one sweep iteration.
        if _stop:
            return
        if paused:
            # Don't write snapshot, don't sleep extra — the 5-min pause
            # already covered the cool-down. Loop back for a fresh sweep.
            continue

        # Sweep finished normally.
        if success_count > 0:
            cache.save_to_disk(BALANCE_CACHE_SNAPSHOT_PATH)
        stats = cache.stats()
        log(
            f"[balance-cache] sweep done | ok={success_count} "
            f"fail={fail_count} skip={skip_count} | size={stats['size']} "
            f"freshest={stats['freshest_age']:.0f}s "
            f"oldest={stats['oldest_age']:.0f}s | "
            f"sleeping {BALANCE_REFRESH_GAP_SEC}s before next pass."
        )

        # Sweep-level fail pause: catches storms interspersed with successes.
        # Distinct from consecutive_fails which only triggers on a hard run.
        if fail_count >= BALANCE_REFRESH_SWEEP_FAIL_THRESHOLD:
            log(
                f"[balance-cache] sweep had {fail_count} failures "
                f"(threshold {BALANCE_REFRESH_SWEEP_FAIL_THRESHOLD}) -> "
                f"pausing {BALANCE_REFRESH_PAUSE_SEC}s to let IP cool down."
            )
            for _ in range(BALANCE_REFRESH_PAUSE_SEC):
                if _stop:
                    return
                time.sleep(1)
            consecutive_fails = 0
            continue  # skip the regular gap, pause already covered it

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
    """Return every account that's outside its own 5s same-account spacing.

    Fire-all policy: we no longer skip the 24h post-success cooldown here.
    Reasoning: the operator prefers paying one wasted API call per cooldowned
    account over risking the rare case where state.json's last_success_at is
    wrong (e.g. the account never actually got the SOL on-chain) and we'd
    silently miss a live top-up. Server returns EXIT_COOLDOWN cheaply for
    real cooldowns via is_cooldown_message(), so the cost is tiny.

    PER_ACCOUNT_SPACING_SEC (5s) stays: it prevents the same account from
    firing twice inside a single top-up burst (which *would* just 429).
    """
    eligible: list[dict] = []
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
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
    balance or fall back to live fetch. Fire-all policy: we do NOT skip
    dust accounts anymore -- the cache balance can be stale (e.g. the user
    completed a task 10 seconds ago and the refresher hasn't swept them
    yet), and skipping them silently means we miss a live top-up for that
    account. Let core.attempt_withdraw's own min-threshold guard handle
    real zero-balance accounts; that costs zero API calls (early return).

    Returns (kept, hits, misses, dust_skipped). `dust_skipped` is retained
    in the signature for log compatibility but is always 0 under fire-all.
    """
    kept: list[tuple[dict, Optional[float]]] = []
    hits = 0
    misses = 0
    dust = 0  # kept for log format compat; always 0 under fire-all policy

    for acc in eligible:
        name = acc.get("name", "?")
        if cache is None:
            kept.append((acc, None))  # legacy live-fetch path
            misses += 1
            continue
        cached = cache.get(name)
        if cached is None:
            # Miss. live-fetch path handles it inside attempt_withdraw.
            kept.append((acc, None))
            misses += 1
            continue
        # Fire-all: pass every cached balance through as override, even if
        # it's below MIN_WITHDRAW_SOL. core.attempt_withdraw's threshold
        # guard returns EXIT_COOLDOWN cheaply for real dust, and a stale
        # "dust" cache entry gets a chance to prove itself wrong.
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

    # Priority order:
    #   1. PRIORITY_ACCOUNTS (tuple at top of file) always fire first, in
    #      the order listed. Use for "claim this one no matter what".
    #   2. Then biggest-balance accounts -- they're the highest reward if
    #      the hot wallet drains mid-burst.
    #   3. Unknown-balance entries (override is None -> live fetch) last,
    #      because they also pay a 0-17s rate-limit retry tax on the first
    #      /api/user call.
    # Python sorts tuples left-to-right, so (bucket, inner) cleanly
    # partitions without needing two passes.
    def _priority_key(item: tuple[dict, Optional[float]]) -> tuple[int, float]:
        acc, override = item
        name = acc.get("name", "")
        if name in PRIORITY_ACCOUNTS:
            return (0, float(PRIORITY_ACCOUNTS.index(name)))
        bal = override if override is not None else -1.0
        return (1, -bal)

    plan.sort(key=_priority_key)

    stagger = max(PARALLEL_STAGGER_MS, 0) / 1000.0
    total_dispatch = stagger * (len(plan) - 1)
    # Always show first 5 of the plan so daffa14 (PRIORITY_ACCOUNTS[0])
    # is visible even if its balance is unknown/zero. Mark priority
    # accounts with a "*" prefix for at-a-glance verification.
    top_previews = []
    for acc, ov in plan[:5]:
        marker = "*" if acc["name"] in PRIORITY_ACCOUNTS else ""
        ov_str = f"{ov:.4f}" if ov is not None else "?"
        top_previews.append(f"{marker}{acc['name']}({ov_str})")
    log(
        f"[parallel] firing {len(plan)} account(s) "
        f"(cache hits={hits} misses={misses} dust-skip={dust}; "
        f"workers={len(plan)} [coverage-first: 1 thread per account, no queueing], "
        f"stagger={PARALLEL_STAGGER_MS}ms => dispatch window {total_dispatch:.1f}s; "
        f"priority top5: {top_previews})."
    )
    state_lock = threading.Lock()
    # COVERAGE-FIRST sizing: one worker per plan entry so every account
    # gets dispatched immediately. The previous min(MAX_PARALLEL_WORKERS,
    # len(plan)) cap meant the bottom (len(plan) - cap) accounts sat in
    # the executor queue while the first batch chewed through infinite
    # 429 retries -- by the time a slot freed up, the hot wallet was
    # already drained and the queued accounts never fired at all.
    #
    # Trade-off: this temporarily uses more concurrent /api/withdraw
    # connections than MAX_PARALLEL_WORKERS suggests when no proxy is
    # active. CloudFlare will 429-storm the burst, but core.py's infinite
    # retry policy absorbs that and at least every account gets one shot
    # before the wallet drains. Dust entries (~20/64) short-circuit at
    # the threshold guard inside attempt_withdraw with no API call, so
    # the real concurrent POST count is closer to len(real_eligible).
    workers = len(plan)

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


def _log_skip_reasons(accounts: list[dict], state: dict, now: float, log) -> None:
    """
    Under the fire-all policy the only real skip reason at dispatch time
    is the 5s per-account spacing window (anti-double-fire within one
    burst). We still log 24h-cooldown accounts as informational so the
    operator can see who's "almost certainly going to bounce with
    EXIT_COOLDOWN" in this burst, but the bot no longer skips them.

    Called only at fire time so the main heartbeat loop doesn't spam the
    log every 10s with identical lines.
    """
    for acc in accounts:
        name = acc.get("name", "?")
        entry = get_account_state(state, name)
        cd_remaining = _seconds_until_cooldown_ends(entry["last_success_at"], now)
        if cd_remaining > 0:
            log(
                f"[{name}] [info] within 24h cooldown window, "
                f"{cd_remaining/3600.0:.1f}h remaining "
                f"(last success {entry['last_success_at']}); "
                "firing anyway per fire-all policy."
            )
        spacing_remaining = PER_ACCOUNT_SPACING_SEC - (
            now - float(entry["last_attempt_ts"])
        )
        if spacing_remaining > 0:
            log(
                f"[{name}] [skip] per-account rate-limit window, "
                f"{spacing_remaining:.1f}s remaining."
            )


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
    _log_skip_reasons(accounts, state, now, log)
    eligible = _eligible_accounts(accounts, state, now)
    delta = current_hot - prev_hot
    skipped = len(accounts) - len(eligible)
    log(
        f"[topup] hot wallet {prev_hot/1e9:.9f} -> {current_hot/1e9:.9f} SOL "
        f"(+{delta/1e9:.9f}); firing {len(eligible)} of {len(accounts)} account(s) "
        f"(fire-all policy; skipped={skipped} via 5s per-account spacing only)."
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

    # Proxy pool status. We report it up-front so a silent fall-back to
    # direct-connect (e.g. proxies.json missing after a deploy) can't
    # hide as a mystery rate-limit regression later.
    #
    # NOTE on workers: _process_topup_parallel now uses workers=len(plan)
    # (one thread per account, no queueing) so MAX_PARALLEL_WORKERS is
    # only relevant as a soft hint. Coverage > burst-gentleness for the
    # snipe use case -- if we cap workers, accounts queued behind the
    # cap never fire when first-batch workers loop on infinite 429
    # retries until the hot wallet drains.
    from core import load_proxies, get_proxy_for_account
    pool = load_proxies()
    if pool:
        # Show distribution: how many accounts land on each proxy slot.
        dist: dict[int, int] = {}
        for acc in accounts:
            p = get_proxy_for_account(acc["name"])
            idx = pool.index(p) if p in pool else -1
            dist[idx] = dist.get(idx, 0) + 1
        dist_str = ", ".join(
            f"proxy#{k}:{v}" for k, v in sorted(dist.items())
        )
        log(
            f"[proxy-pool] {len(pool)} proxy IP(s) active; "
            f"{len(accounts)} account(s) distributed ({dist_str}); "
            f"fire mode=coverage-first (workers=len(plan))."
        )
    else:
        log(
            "[proxy-pool] no proxies.json or empty list; "
            "all accounts use direct connection from VPS IP. "
            "fire mode=coverage-first (workers=len(plan)); expect "
            "CloudFlare 429 storm on burst -- absorbed by infinite "
            "retry policy in core.py."
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
        # Pre-populate from previous run's disk snapshot. Avoids the cold-start
        # /api/user burst: with a warm cache the TTL-based refresher skips
        # most accounts on sweep 1, spreading the load instead of hammering
        # the IP just as the WAF score is still high from the previous storm.
        loaded = cache.load_from_disk()
        if loaded > 0:
            log(
                f"[balance-cache] loaded {loaded} entries from disk snapshot "
                f"(cold-start burst avoided)."
            )
        else:
            log(
                "[balance-cache] no usable disk snapshot; cold start with "
                "empty cache (first sweep will hit /api/user for all accounts)."
            )
        _start_balance_refresher(accounts, cache, log)
        log(
            f"[balance-cache] refresher started (spacing={BALANCE_REFRESH_SPACING_SEC}s, "
            f"gap={BALANCE_REFRESH_GAP_SEC}s, max_age={BALANCE_CACHE_MAX_AGE_SEC:.0f}s, "
            f"refresh_threshold={BALANCE_REFRESH_AGE_THRESHOLD_SEC:.0f}s, "
            f"warmup={BALANCE_CACHE_WARMUP_SEC:.0f}s, "
            f"sweep_fail_pause={BALANCE_REFRESH_SWEEP_FAIL_THRESHOLD})."
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
