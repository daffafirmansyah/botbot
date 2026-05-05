"""
account_status.py - Show every account's current state at a glance.

Per-account info:
  - name
  - wallet_address (used for withdraw destination)
  - claimable balance (live from /api/user, with retry)
  - cooldown status (from state.json — last successful withdraw)
  - auth status (OK / 401 / 429 / network)

Accounts are grouped by wallet_address so you can see which wallets are
shared (claimyshare's wallet pool, max 2 accounts per wallet by default).

Usage:
  python account_status.py                # full check (live balance + state)
  python account_status.py --no-balance   # skip live fetch; only config+state
  python account_status.py --only adella,bolvi  # restrict to specific names
  python account_status.py --json         # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from typing import Optional

import core


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Spacing between live balance fetches. With 64 accounts -> 128s total.
# Keeps us under per-IP rate limit while still giving a fresh view.
SPACING_SEC = 2.0


# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------

def _classify_failure(msgs: list[str]) -> str:
    """Compress all log messages into a single short status keyword."""
    joined = " ".join(msgs).lower()
    if "status 401" in joined or "401" in joined:
        return "AUTH-EXPIRED"
    if "status 403" in joined:
        return "FORBIDDEN"
    if "status 429" in joined or "gave up" in joined:
        return "RATE-LIMITED"
    if "network error" in joined:
        return "NETWORK"
    if "non-json" in joined:
        return "BAD-RESPONSE"
    return "FAIL"


def _fetch_one(acc: dict) -> tuple[Optional[float], str]:
    msgs: list[str] = []
    balance = core.fetch_claimable_balance(acc, lambda m: msgs.append(m))
    if balance is None:
        return None, _classify_failure(msgs)
    return balance, "OK"


def _cooldown_status(state: dict, name: str) -> str:
    entry = core.get_account_state(state, name)
    last = entry.get("last_success_at")
    if not last:
        return "no-history"
    try:
        last_unix = core.iso_to_unix(last)
    except Exception:  # noqa: BLE001
        return f"invalid-date={last}"
    remaining = (last_unix + core.DAILY_COOLDOWN_SEC) - time.time()
    if remaining <= 0:
        return "ready"
    h, r = divmod(int(remaining), 3600)
    m, _ = divmod(r, 60)
    if h:
        return f"cooldown {h}h{m:02d}m"
    return f"cooldown {m}m"


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def _short_wallet(wallet: str) -> str:
    if len(wallet) <= 16:
        return wallet
    return f"{wallet[:8]}..{wallet[-6:]}"


def _print_grouped(rows: list[dict]) -> None:
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_wallet[r["wallet"]].append(r)

    # Stable wallet ordering: by first account name
    wallet_order = sorted(by_wallet.keys(), key=lambda w: by_wallet[w][0]["name"])

    print()
    for wallet in wallet_order:
        group = by_wallet[wallet]
        wlabel = _short_wallet(wallet) if wallet else "<NO WALLET>"
        total_bal = sum((r["balance"] or 0.0) for r in group)
        print(f"-- wallet {wlabel} ({len(group)} acc, total {total_bal:.6f} SOL) --")
        for r in group:
            name = r["name"]
            bal = r["balance"]
            bal_s = f"{bal:.6f}" if bal is not None else "  ?  "
            status = r["status"]
            cd = r["cooldown"]
            mark = "OK" if status == "OK" else status
            ready = ""
            if status == "OK" and bal is not None:
                if bal < core.MIN_WITHDRAW_SOL:
                    ready = "[dust]"
                elif cd in ("ready", "no-history"):
                    ready = "[READY]"
                else:
                    ready = f"[{cd}]"
            else:
                ready = f"[{cd}]"
            print(f"  {name:20s} bal={bal_s} SOL  {mark:14s}  {ready}")


def _print_summary(rows: list[dict]) -> None:
    total = len(rows)
    ok_rows = [r for r in rows if r["status"] == "OK"]
    bad_auth = [r for r in rows if r["status"] in ("AUTH-EXPIRED", "FORBIDDEN")]
    rate_limited = [r for r in rows if r["status"] == "RATE-LIMITED"]
    other_fail = [
        r for r in rows
        if r["status"] not in ("OK", "AUTH-EXPIRED", "FORBIDDEN", "RATE-LIMITED")
    ]

    # READY = will fire on next top-up. That's both:
    #   * "ready"      -> already withdrew before, cooldown is over.
    #   * "no-history" -> never withdrew yet, no cooldown to wait for.
    # Only accounts whose cooldown string starts with "cooldown " are
    # actually locked.
    READY_STATES = ("ready", "no-history")
    ready = [
        r for r in ok_rows
        if (r["balance"] or 0) >= core.MIN_WITHDRAW_SOL
        and r["cooldown"] in READY_STATES
    ]
    cooldown = [
        r for r in ok_rows
        if (r["balance"] or 0) >= core.MIN_WITHDRAW_SOL
        and r["cooldown"] not in READY_STATES
    ]
    dust = [
        r for r in ok_rows
        if (r["balance"] or 0) < core.MIN_WITHDRAW_SOL
    ]
    total_claimable = sum((r["balance"] or 0.0) for r in ready)
    total_locked = sum((r["balance"] or 0.0) for r in cooldown)
    total_dust = sum((r["balance"] or 0.0) for r in dust)
    unique_wallets = len({r["wallet"] for r in rows if r["wallet"]})
    empty_wallets = len([r for r in rows if not r["wallet"]])

    print()
    print("=" * 70)
    print(f"  Total accounts:                {total}")
    print(f"  Unique wallets:                {unique_wallets}")
    if empty_wallets:
        print(f"  Accounts with EMPTY wallet:    {empty_wallets}  <-- broken!")
    print()
    print(f"  READY to withdraw:             {len(ready):3d}  | "
          f"sum balance: {total_claimable:.6f} SOL")
    print(f"  In cooldown (already claimed): {len(cooldown):3d}  | "
          f"sum balance: {total_locked:.6f} SOL")
    print(f"  Dust (< {core.MIN_WITHDRAW_SOL}):           {len(dust):3d}  | "
          f"sum balance: {total_dust:.6f} SOL")
    print()
    if bad_auth:
        print(f"  AUTH EXPIRED (401/403):        {len(bad_auth)}")
        for r in bad_auth:
            print(f"      - {r['name']}")
    if rate_limited:
        print(f"  RATE LIMITED (429 exhausted):  {len(rate_limited)}")
        for r in rate_limited:
            print(f"      - {r['name']}")
    if other_fail:
        print(f"  OTHER FAILURES:                {len(other_fail)}")
        for r in other_fail:
            print(f"      - {r['name']}: {r['status']}")
    print("=" * 70)
    if len(ready) > 0:
        print(f"\n  >> Next top-up will fire {len(ready)} account(s) "
              f"for ~{total_claimable:.6f} SOL claimable. <<")
    if bad_auth:
        print(f"\n  >> Refresh creds for {len(bad_auth)} expired account(s) "
              f"before next top-up. <<")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--no-balance", action="store_true",
                   help="skip live balance fetch (only show config + cooldown).")
    p.add_argument("--only", default="",
                   help="comma-separated account names to restrict to.")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of pretty output.")
    args = p.parse_args()

    accounts = core.load_accounts()
    state = core.load_state()

    only_set = {n.strip() for n in args.only.split(",") if n.strip()} if args.only else None
    if only_set:
        accounts = [a for a in accounts if a["name"] in only_set]
        if not accounts:
            sys.exit(f"[error] no accounts matched --only={args.only}")

    rows: list[dict] = []
    skip_fetch = args.no_balance

    if not skip_fetch and not args.json:
        print(f"# fetching balance for {len(accounts)} account(s) "
              f"({SPACING_SEC}s spacing -> ~{len(accounts) * SPACING_SEC:.0f}s)...",
              flush=True)

    for i, acc in enumerate(accounts):
        name = acc["name"]
        wallet = acc.get("wallet_address", "") or ""
        cd = _cooldown_status(state, name)

        if skip_fetch:
            balance, status = None, "SKIPPED"
        else:
            balance, status = _fetch_one(acc)
            if i < len(accounts) - 1:
                time.sleep(SPACING_SEC)

        rows.append({
            "name": name,
            "wallet": wallet,
            "balance": balance,
            "status": status,
            "cooldown": cd,
        })

        if not args.json and not skip_fetch:
            # Live progress so the user sees something happening.
            bal_s = f"{balance:.6f}" if balance is not None else "  ?  "
            mark = "OK" if status == "OK" else status
            print(f"  [{i+1:3d}/{len(accounts)}] {name:20s} bal={bal_s} {mark}",
                  flush=True)

    if args.json:
        print(json.dumps({
            "accounts": rows,
            "min_withdraw_sol": core.MIN_WITHDRAW_SOL,
        }, indent=2))
        return 0

    _print_grouped(rows)
    _print_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
