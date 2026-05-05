"""
Claimyshare auto-withdraw — one-shot mode.

Iterates over every account in config.json and sends ONE POST per account.
By default fires them in parallel via a thread pool to maximize the chance
of catching the current hot-wallet refill before it drains.

For the "attempt whenever hot wallet is topped up" behavior, use
monitor.py instead.

Exit code reflects the best outcome across accounts:
  0 if at least one account succeeded,
  2 if all attempts were cooldown (nothing to do right now),
  3 otherwise (see withdraw.log for details).
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import (
    DAILY_COOLDOWN_SEC,
    EXIT_API_ERROR,
    EXIT_COOLDOWN,
    EXIT_NETWORK,
    EXIT_OK,
    MIN_WITHDRAW_SOL,
    attempt_withdraw,
    get_account_state,
    iso_to_unix,
    load_accounts,
    load_state,
    make_logger,
)

# Avoid a circular import: monitor.py imports withdraw-adjacent helpers from
# core, but its balance-cache reader is what we want for smart-filter so we
# pull only that symbol here. monitor.py itself does NOT import withdraw.py.
from monitor import load_balance_cache_snapshot

# Parallel firing: send all account POSTs concurrently. Set False to fall
# back to a sequential loop with INTER_ACCOUNT_SPACING_SEC between requests.
PARALLEL_FIRE = True
# Bumped from 32 -> 64 when the proxy pool went live (monitor.py uses 64
# too for the same reason): 64 concurrent requests spread across 10 exit
# IPs is only ~6-7 in-flight per IP, so CloudFlare sees normal traffic.
# If you ever drop the proxy pool, dial this back down to match.
MAX_PARALLEL_WORKERS = 64
# Stagger the parallel dispatch so account #N waits N * PARALLEL_STAGGER_MS
# before its first request fires. 0 = pure burst (all reqs in same ms,
# maximum sniping speed but high 429-storm risk — relies on aggressive retry
# in core.py to recover the misses). Raise to 200/500/2000 if you start
# seeing IP-level blocks (403 / connection refused, NOT just 429).
# 2ms mirrors monitor.py: still effectively a burst but avoids the
# exact-same-timestamp fingerprint that some WAFs flag.
PARALLEL_STAGGER_MS = 2

# Sequential fallback only.
INTER_ACCOUNT_SPACING_SEC = 5


# ---------------------------------------------------------------------------
# Smart filter: skip accounts that the server would reject anyway
# ---------------------------------------------------------------------------


def _filter_smart(
    accounts: list[dict], log
) -> tuple[list[dict], dict[str, int]]:
    """
    Pre-filter accounts BEFORE we burn an API call on them:
      * skip if inside DAILY_COOLDOWN_SEC window since last success
        (state.json last_success_at) -- server would return cooldown.
      * skip if monitor.py's cache says balance < MIN_WITHDRAW_SOL
        (no balance to withdraw, would hit core.py's threshold guard).

    Accounts with no cache entry are kept (unknown -> let server decide).
    Accounts with no state entry are kept (never attempted -> try).

    Returns (eligible_accounts, skip_counters).
    """
    state = load_state()
    # load_balance_cache_snapshot returns dict[name, (balance, fetched_at)]
    # already fresh-filtered via max_age_sec. None if missing/stale/malformed.
    snapshot = load_balance_cache_snapshot() or {}
    now = time.time()

    eligible: list[dict] = []
    skipped = {"cooldown": 0, "dust": 0, "no_balance": 0}

    for acc in accounts:
        name = acc["name"]

        # 1. Cooldown check (state.json)
        entry = get_account_state(state, name)
        lsa = entry.get("last_success_at")
        if lsa:
            cooldown_end = iso_to_unix(lsa) + DAILY_COOLDOWN_SEC
            remaining = cooldown_end - now
            if remaining > 0:
                hrs = remaining / 3600.0
                log(
                    f"[{name}] [skip] in 24h cooldown, "
                    f"{hrs:.1f}h remaining (last success {lsa})."
                )
                skipped["cooldown"] += 1
                continue

        # 2. Balance check (balance_cache.json, populated by monitor.py)
        # snapshot[name] is (balance_or_None, fetched_at_unix) or missing.
        bal_entry = snapshot.get(name)
        if bal_entry is not None:
            balance, _ts = bal_entry
            if isinstance(balance, (int, float)):
                if balance < MIN_WITHDRAW_SOL:
                    log(
                        f"[{name}] [skip] balance {balance:.6f} SOL below "
                        f"threshold {MIN_WITHDRAW_SOL} SOL."
                    )
                    skipped["dust"] += 1
                    continue
            else:
                # cache entry present but balance missing/None -> keep,
                # server will tell us the real answer.
                skipped["no_balance"] += 1
        else:
            # no cache entry at all -> keep, live fetch path.
            skipped["no_balance"] += 1

        eligible.append(acc)

    log(
        f"[smart-filter] {len(eligible)}/{len(accounts)} eligible "
        f"(skipped: cooldown={skipped['cooldown']} "
        f"dust={skipped['dust']} unknown_balance={skipped['no_balance']})."
    )
    return eligible, skipped


def _fire_one(
    acc: dict,
    log,
    start_delay_sec: float = 0.0,
    amount_override: float | None = None,
) -> int:
    if start_delay_sec > 0:
        time.sleep(start_delay_sec)
    log(f"[{acc['name']}] [fire] starting withdraw.")
    exit_code, _parsed, _status = attempt_withdraw(
        acc,
        log,
        verify_onchain=False,
        amount_sol_override=amount_override,
    )
    return exit_code


def _build_plan(
    accounts: list[dict], log
) -> list[tuple[dict, float | None]]:
    """
    Build (account, amount_override) pairs from monitor.py's balance cache.

    Accounts present in the cache get their balance passed through as
    amount_sol_override so attempt_withdraw skips the live /api/user call
    (0-17s rate-limit retry tax per account). Accounts missing from the
    cache fall through to the live fetch path -- safer than skipping them
    entirely when the cache is stale or incomplete.

    Result is sorted by override DESC so the biggest-balance accounts
    enter the thread pool first and are the first 32 in flight. Unknown
    (override=None) accounts go last because they also pay live-fetch tax.
    """
    # load_balance_cache_snapshot returns dict[name, (balance, fetched_at)]
    # with stale entries already filtered out. None if file missing/malformed.
    snapshot = load_balance_cache_snapshot() or {}

    plan: list[tuple[dict, float | None]] = []
    cache_hits = 0
    cache_misses = 0
    for acc in accounts:
        name = acc["name"]
        bal_entry = snapshot.get(name)
        override: float | None = None
        if bal_entry is not None:
            balance, _ts = bal_entry
            if isinstance(balance, (int, float)):
                override = float(balance)
                cache_hits += 1
        if override is None:
            cache_misses += 1
        plan.append((acc, override))

    def _priority_key(item: tuple[dict, float | None]) -> float:
        _acc, override = item
        return -(override if override is not None else -1.0)

    plan.sort(key=_priority_key)

    log(
        f"[plan] cache hits={cache_hits} misses={cache_misses} "
        f"(misses will live-fetch /api/user before /api/withdraw)."
    )
    return plan


def _run_parallel(accounts: list[dict], log) -> list[int]:
    plan = _build_plan(accounts, log)

    stagger = max(PARALLEL_STAGGER_MS, 0) / 1000.0
    total_dispatch = stagger * (len(plan) - 1)
    top_previews = [
        f"{acc['name']}({ov:.4f})" for acc, ov in plan[:5]
        if ov is not None
    ]
    log(
        f"[parallel] firing {len(plan)} account(s) "
        f"(max workers={MAX_PARALLEL_WORKERS}, "
        f"stagger={PARALLEL_STAGGER_MS}ms => dispatch window {total_dispatch:.1f}s; "
        f"priority top5: {top_previews})."
    )
    workers = min(MAX_PARALLEL_WORKERS, len(plan))
    results: list[int] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wd") as ex:
        futures = [
            ex.submit(_fire_one, acc, log, i * stagger, override)
            for i, (acc, override) in enumerate(plan)
        ]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                log(f"[error] worker thread crashed: {e}")
                results.append(EXIT_API_ERROR)
    return results


def _run_sequential(accounts: list[dict], log) -> list[int]:
    plan = _build_plan(accounts, log)
    results: list[int] = []
    for i, (acc, override) in enumerate(plan):
        log(
            f"[{acc['name']}] attempt {i + 1}/{len(plan)} "
            f"| wallet={acc['wallet_address']} "
            f"amount={'cache=' + f'{override:.6f}' if override is not None else 'live-fetch'}"
        )
        results.append(_fire_one(acc, log, amount_override=override))
        if i < len(plan) - 1:
            time.sleep(INTER_ACCOUNT_SPACING_SEC)
    return results


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Claimyshare auto-withdraw (one-shot). Fires every account "
                    "unless --only / --exclude is used."
    )
    p.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="Fire only the given account name(s). Case-sensitive, exact match.",
    )
    p.add_argument(
        "--exclude",
        nargs="+",
        metavar="NAME",
        help="Skip the given account name(s). Case-sensitive, exact match.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List all available account names and exit (no withdraw fired).",
    )
    p.add_argument(
        "--smart",
        action="store_true",
        help="Enable smart-filter (pre-skip cooldown + dust accounts). "
             "DEFAULT IS FIRE-ALL: under competition the operator prefers "
             "paying wasted API calls over missing accounts whose cache is "
             "stale. Use --smart only when you explicitly want to cut API "
             "volume (e.g. long-running hourly cron, not post-topup retries).",
    )
    # Back-compat: --force used to mean 'bypass smart-filter' when the default
    # was filter-on. Now filter is off by default, so --force is a no-op we
    # keep around so old scripts don't break.
    p.add_argument(
        "--force",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; no-op under fire-all default.
    )
    return p.parse_args()


def _filter_accounts(
    accounts: list[dict], only: list[str] | None, exclude: list[str] | None
) -> list[dict]:
    available = {a["name"]: a for a in accounts}

    if only:
        unknown = [n for n in only if n not in available]
        if unknown:
            sys.exit(f"[error] --only contains unknown account(s): {unknown}. "
                     f"Use --list to see available names.")
        # Preserve the order from --only so the user controls dispatch order.
        accounts = [available[n] for n in only]

    if exclude:
        unknown = [n for n in exclude if n not in available]
        if unknown:
            print(f"[warn] --exclude contains unknown account(s): {unknown} "
                  f"(ignored).", file=sys.stderr)
        accounts = [a for a in accounts if a["name"] not in set(exclude)]

    return accounts


def main() -> int:
    args = _parse_args()
    accounts = load_accounts()

    if args.list:
        for a in accounts:
            print(a["name"])
        return EXIT_OK

    accounts = _filter_accounts(accounts, args.only, args.exclude)
    if not accounts:
        print("[error] no accounts left after filtering.", file=sys.stderr)
        return EXIT_API_ERROR

    log = make_logger("withdraw.log")

    # Fire-all is the default. Operator can opt into smart-filter with --smart
    # when they explicitly want to cut API volume (e.g. long cron).
    if args.smart:
        accounts, _skip_counters = _filter_smart(accounts, log)
        if not accounts:
            log("[smart-filter] nothing to fire after filtering. "
                "Drop --smart to fire every account.")
            return EXIT_COOLDOWN
    else:
        log(
            f"[fire-all] firing every account ({len(accounts)} total); "
            "cooldown/dust skips are delegated to core.attempt_withdraw's "
            "own threshold + cooldown guards. Pass --smart for pre-filtering."
        )

    log(
        f"one-shot start | accounts={[a['name'] for a in accounts]} "
        f"mode={'parallel' if PARALLEL_FIRE else 'sequential'}"
    )

    if PARALLEL_FIRE:
        results = _run_parallel(accounts, log)
    else:
        results = _run_sequential(accounts, log)

    ok = sum(1 for c in results if c == EXIT_OK)
    cd = sum(1 for c in results if c == EXIT_COOLDOWN)
    err = sum(1 for c in results if c not in (EXIT_OK, EXIT_COOLDOWN))
    log(f"summary | ok={ok} cooldown={cd} error={err} total={len(results)}")

    if ok > 0:
        return EXIT_OK
    if cd == len(results) and len(results) > 0:
        return EXIT_COOLDOWN
    if any(c == EXIT_NETWORK for c in results):
        return EXIT_NETWORK
    return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
