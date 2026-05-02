"""
Claimyshare auto-withdraw — one-shot mode.

Iterates over every account in config.json, sends ONE POST per account,
with a short fixed spacing between accounts. Designed to be run ad-hoc
or from a scheduler. For the "attempt whenever hot wallet is topped
up" behavior use monitor.py instead.

Exit code reflects the best outcome across accounts:
  0 if at least one account succeeded,
  2 if all attempts were cooldown (nothing to do right now),
  3 otherwise (see withdraw.log for details).
"""

from __future__ import annotations

import sys
import time

from core import (
    EXIT_API_ERROR,
    EXIT_COOLDOWN,
    EXIT_NETWORK,
    EXIT_OK,
    attempt_withdraw,
    load_accounts,
    make_logger,
)

# Spacing between accounts — the site's per-user rate limit is per-JWT
# (3 req / 60 s), so different accounts don't share a bucket. Keep this
# above ~1s to avoid sub-second bursts from the same IP.
INTER_ACCOUNT_SPACING_SEC = 5


def main() -> int:
    accounts = load_accounts()
    log = make_logger("withdraw.log")

    log(f"one-shot start | accounts={[a['name'] for a in accounts]}")

    results: list[int] = []
    for i, acc in enumerate(accounts):
        log(
            f"[{acc['name']}] attempt {i + 1}/{len(accounts)} "
            f"| wallet={acc['wallet_address']} amount_sol={acc['amount_sol']}"
        )
        # verify_onchain=False to keep one-shot snappy; we do a final
        # on-chain summary only for successful ones if desired.
        exit_code, _parsed, _status = attempt_withdraw(acc, log, verify_onchain=False)
        results.append(exit_code)

        if i < len(accounts) - 1:
            time.sleep(INTER_ACCOUNT_SPACING_SEC)

    ok = sum(1 for c in results if c == EXIT_OK)
    cd = sum(1 for c in results if c == EXIT_COOLDOWN)
    err = sum(1 for c in results if c not in (EXIT_OK, EXIT_COOLDOWN))
    log(f"summary | ok={ok} cooldown={cd} error={err} total={len(results)}")

    if ok > 0:
        return EXIT_OK
    if cd == len(results):
        return EXIT_COOLDOWN
    if any(c == EXIT_NETWORK for c in results):
        return EXIT_NETWORK
    return EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
