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

import signal
import sys
import time
from typing import Optional

from core import (
    DAILY_COOLDOWN_SEC,
    EXIT_COOLDOWN,
    EXIT_OK,
    HOT_WALLET,
    RATE_LIMIT_WINDOW_SEC,
    attempt_withdraw,
    bootstrap_last_success_iso,
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

POLL_INTERVAL_SEC = 30               # how often we check the hot wallet balance
TOPUP_THRESHOLD_LAMPORTS = 500_000   # 0.0005 SOL — ignore dust / tx fees
# Per-account: stay under the site's 3 req / 60 s per-JWT rate limit.
PER_ACCOUNT_SPACING_SEC = RATE_LIMIT_WINDOW_SEC // 2 + 5  # ~35s
# Between two different accounts on a single top-up event: keep just
# enough spacing to avoid sub-second bursts from the same IP.
INTER_ACCOUNT_SPACING_SEC = 5
# Stop iterating if hot wallet drops below this mid-sequence — the remaining
# accounts will almost certainly fail and we'd just burn rate-limit budget.
HOT_WALLET_FLOOR_LAMPORTS = 200_000  # ~0.0002 SOL

_stop = False


def _handle_sigint(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    print("\n[monitor] stop requested, finishing current iteration...", flush=True)


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
    """On first-ever run, fill last_success_at for each account from chain."""
    for acc in accounts:
        entry = get_account_state(state, acc["name"])
        if entry["last_success_at"] is not None:
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


def _process_topup(
    accounts: list[dict],
    state: dict,
    current_hot: int,
    prev_hot: int,
    log,
) -> None:
    """Iterate eligible accounts on a single top-up event."""
    now = time.time()
    eligible = _eligible_accounts(accounts, state, now)
    delta = current_hot - prev_hot
    log(
        f"[topup] hot wallet {prev_hot/1e9:.9f} -> {current_hot/1e9:.9f} SOL "
        f"(+{delta/1e9:.9f}); {len(eligible)} of {len(accounts)} account(s) eligible."
    )
    if not eligible:
        return

    for i, acc in enumerate(eligible):
        if _stop:
            break

        # Mid-sequence hot-wallet floor guard.
        if i > 0:
            current_hot_now = get_balance_lamports(HOT_WALLET)
            if current_hot_now is not None and current_hot_now < HOT_WALLET_FLOOR_LAMPORTS:
                log(
                    f"[topup] hot wallet drained to "
                    f"{current_hot_now/1e9:.9f} SOL; aborting remaining "
                    f"{len(eligible) - i} account(s)."
                )
                break

        entry = get_account_state(state, acc["name"])
        log(f"[{acc['name']}] [fire] {i + 1}/{len(eligible)} starting withdraw.")
        entry["last_attempt_ts"] = time.time()
        exit_code, _parsed, _status = attempt_withdraw(acc, log, verify_onchain=False)

        if exit_code == EXIT_OK:
            entry["last_success_at"] = utc_now_iso()
            log(
                f"[{acc['name']}] [ok] success; next attempt earliest in "
                f"{_human_duration(DAILY_COOLDOWN_SEC)}."
            )
        elif exit_code == EXIT_COOLDOWN:
            # Could be either 60s rate limit or 24h daily; assume daily to be safe.
            entry["last_success_at"] = utc_now_iso()
            log(f"[{acc['name']}] [cooldown] server refused; assuming 24h from now.")
        else:
            log(f"[{acc['name']}] [error] failed (exit={exit_code}); keep cooldown unchanged.")

        save_state(state)

        if i < len(eligible) - 1 and not _stop:
            _sleep_with_stop(INTER_ACCOUNT_SPACING_SEC)


def main() -> int:
    accounts = load_accounts()
    log = make_logger("monitor.log")
    signal.signal(signal.SIGINT, _handle_sigint)

    log(
        f"monitor started | accounts={len(accounts)} "
        f"poll={POLL_INTERVAL_SEC}s topup>={TOPUP_THRESHOLD_LAMPORTS/1e9:.6f} SOL"
    )

    state = load_state()
    _bootstrap_accounts(accounts, state, log)
    _log_startup_status(accounts, state, log)

    last_balance = int(state.get("last_hot_balance_lamports", 0))
    # For log-throttling only.
    last_logged_balance = last_balance

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
                _process_topup(accounts, state, current, last_balance, log)
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
