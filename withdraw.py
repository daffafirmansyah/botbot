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
    EXIT_API_ERROR,
    EXIT_COOLDOWN,
    EXIT_NETWORK,
    EXIT_OK,
    attempt_withdraw,
    load_accounts,
    make_logger,
)

# Parallel firing: send all account POSTs concurrently. Set False to fall
# back to a sequential loop with INTER_ACCOUNT_SPACING_SEC between requests.
PARALLEL_FIRE = True
MAX_PARALLEL_WORKERS = 20
# Stagger the parallel dispatch so account #N waits N * PARALLEL_STAGGER_MS
# before its first request fires. 0 = pure burst (all reqs in same ms,
# maximum sniping speed but high 429-storm risk — relies on aggressive retry
# in core.py to recover the misses). Raise to 200/500/2000 if you start
# seeing IP-level blocks (403 / connection refused, NOT just 429).
PARALLEL_STAGGER_MS = 0

# Sequential fallback only.
INTER_ACCOUNT_SPACING_SEC = 5


def _fire_one(acc: dict, log, start_delay_sec: float = 0.0) -> int:
    if start_delay_sec > 0:
        time.sleep(start_delay_sec)
    log(f"[{acc['name']}] [fire] starting withdraw.")
    exit_code, _parsed, _status = attempt_withdraw(acc, log, verify_onchain=False)
    return exit_code


def _run_parallel(accounts: list[dict], log) -> list[int]:
    stagger = max(PARALLEL_STAGGER_MS, 0) / 1000.0
    total_dispatch = stagger * (len(accounts) - 1)
    log(
        f"[parallel] firing {len(accounts)} account(s) "
        f"(max workers={MAX_PARALLEL_WORKERS}, "
        f"stagger={PARALLEL_STAGGER_MS}ms => dispatch window {total_dispatch:.1f}s)."
    )
    workers = min(MAX_PARALLEL_WORKERS, len(accounts))
    results: list[int] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wd") as ex:
        futures = [
            ex.submit(_fire_one, acc, log, i * stagger)
            for i, acc in enumerate(accounts)
        ]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                log(f"[error] worker thread crashed: {e}")
                results.append(EXIT_API_ERROR)
    return results


def _run_sequential(accounts: list[dict], log) -> list[int]:
    results: list[int] = []
    for i, acc in enumerate(accounts):
        log(
            f"[{acc['name']}] attempt {i + 1}/{len(accounts)} "
            f"| wallet={acc['wallet_address']} amount_sol={acc['amount_sol']}"
        )
        results.append(_fire_one(acc, log))
        if i < len(accounts) - 1:
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
