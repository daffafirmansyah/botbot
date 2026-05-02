"""
Claimyshare auto-withdraw — watch-loop mode.

Polls the site's hot wallet (8MrX...) on Solana mainnet. When the
balance goes up by more than a configurable threshold (i.e. the admin
topped it up so payouts can flow), this script fires ONE withdraw
request for our account — but only if:

  * we are not inside the observed ~24h daily cooldown, and
  * at least RATE_LIMIT_WINDOW_SEC has passed since the previous
    attempt (to stay under the 3 req / 60s site rate limit).

State (last known balance, last success time, last attempt time) is
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
    get_balance_lamports,
    iso_to_unix,
    load_config,
    load_state,
    make_logger,
    save_state,
    utc_now_iso,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

POLL_INTERVAL_SEC = 30          # how often we check the hot wallet balance
TOPUP_THRESHOLD_LAMPORTS = 500_000   # 0.0005 SOL — ignore dust / tx fees
ATTEMPT_SPACING_SEC = RATE_LIMIT_WINDOW_SEC // 2 + 5  # ~35s between our POSTs


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


def main() -> int:
    cfg = load_config()
    log = make_logger("monitor.log")
    signal.signal(signal.SIGINT, _handle_sigint)

    state = load_state()

    # First-run bootstrap: discover the real last-success time from chain,
    # so we don't fire inside an existing cooldown.
    if state.get("last_success_at") is None:
        log("[bootstrap] no prior success recorded; scanning on-chain history...")
        discovered = bootstrap_last_success_iso(cfg["wallet_address"], log)
        if discovered:
            state["last_success_at"] = discovered
            save_state(state)

    log(
        f"monitor started | poll={POLL_INTERVAL_SEC}s "
        f"topup>={TOPUP_THRESHOLD_LAMPORTS/1e9:.6f} SOL "
        f"hot_wallet={HOT_WALLET}"
    )
    if state["last_success_at"]:
        cd = _seconds_until_cooldown_ends(state["last_success_at"], time.time())
        log(f"last success at {state['last_success_at']}; daily cooldown ends in {_human_duration(cd)}")
    else:
        log("no recorded last success; will attempt on first detected top-up.")

    last_balance = int(state.get("last_hot_balance_lamports", 0))
    last_success_iso: Optional[str] = state.get("last_success_at")
    last_attempt_ts: float = float(state.get("last_attempt_ts", 0))
    # Prevents noisy "balance changed" log spam when nothing interesting happens.
    last_logged_balance = last_balance

    while not _stop:
        current = get_balance_lamports(HOT_WALLET)
        now = time.time()

        if current is None:
            log("[warn] RPC balance read failed; sleeping and retrying.")
            _sleep_with_stop(POLL_INTERVAL_SEC)
            continue

        # Prime last_balance on very first successful read.
        if last_balance == 0:
            last_balance = current
            last_logged_balance = current
            state["last_hot_balance_lamports"] = current
            save_state(state)
            log(f"initial hot wallet balance: {current/1e9:.9f} SOL")

        delta = current - last_balance
        topup_detected = delta >= TOPUP_THRESHOLD_LAMPORTS

        cooldown_remaining = _seconds_until_cooldown_ends(last_success_iso, now)
        in_daily_cooldown = cooldown_remaining > 0
        since_last_attempt = now - last_attempt_ts
        in_rate_limit = since_last_attempt < ATTEMPT_SPACING_SEC

        if topup_detected and not in_daily_cooldown and not in_rate_limit:
            log(
                f"[topup] {last_balance/1e9:.9f} -> {current/1e9:.9f} SOL "
                f"(+{delta/1e9:.9f}). attempting withdraw."
            )
            last_attempt_ts = now
            exit_code, parsed, status = attempt_withdraw(cfg, log)

            if exit_code == EXIT_OK:
                last_success_iso = utc_now_iso()
                log(
                    f"[ok] withdraw succeeded at {last_success_iso}; "
                    f"next attempt earliest in {_human_duration(DAILY_COOLDOWN_SEC)}."
                )
            elif exit_code == EXIT_COOLDOWN:
                # Server refused with cooldown message. We don't know if it's
                # the 60s rate limit or the 24h daily cooldown, so conservatively
                # treat as daily cooldown.
                last_success_iso = utc_now_iso()
                log("[cooldown] server refused; assuming 24h cooldown from now.")
            else:
                log(f"[error] attempt failed (exit={exit_code}). will retry on next top-up.")
                # Keep last_success_iso unchanged so we don't lock ourselves out.

            # Always bump the last-known balance to "now" so we only react to
            # the NEXT top-up, not this one again.
            last_balance = current
            last_logged_balance = current

        else:
            # Small logging: only print if something interesting happened.
            if abs(current - last_logged_balance) >= TOPUP_THRESHOLD_LAMPORTS:
                reason = []
                if not topup_detected:
                    reason.append("no topup")
                if in_daily_cooldown:
                    reason.append(f"daily cd {_human_duration(cooldown_remaining)}")
                if in_rate_limit:
                    reason.append(
                        f"rate cd {_human_duration(ATTEMPT_SPACING_SEC - since_last_attempt)}"
                    )
                log(
                    f"balance {last_logged_balance/1e9:.9f} -> {current/1e9:.9f} SOL; "
                    f"skipping ({', '.join(reason) or 'no action'})."
                )
                last_logged_balance = current

            # Update tracked balance even on non-attempt so we don't re-fire on
            # stale deltas once cooldowns expire.
            last_balance = current

        # Persist state every loop iteration.
        state["last_hot_balance_lamports"] = last_balance
        state["last_success_at"] = last_success_iso
        state["last_attempt_ts"] = last_attempt_ts
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
