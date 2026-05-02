"""
Claimyshare auto-withdraw — one-shot mode.

Sends exactly ONE POST to /api/withdraw per invocation. Designed to be
run ad-hoc or from Windows Task Scheduler. For the "attempt whenever
hot wallet is topped up" behavior, use monitor.py instead.

See README.md for setup and context.
"""

from __future__ import annotations

import sys

from core import attempt_withdraw, load_config, make_logger


def main() -> int:
    cfg = load_config()
    log = make_logger("withdraw.log")

    log(f"attempt start | wallet={cfg['wallet_address']} amount_sol={cfg['amount_sol']}")
    exit_code, _parsed, _status = attempt_withdraw(cfg, log)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
