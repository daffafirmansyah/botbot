"""
connectivity_check.py - Minimal probe to diagnose persistent 429s.

When the bot has been getting 429 on /api/user for an hour or more, the
question is: is this a transient CloudFlare cooldown (will clear) or a
persistent IP block (won't clear without changing IP)? This script makes
exactly THREE requests, total, so it can answer that question without
making the rate-limit situation worse.

Probes:
  1. Solana RPC                  - baseline, is outbound HTTPS healthy?
  2. claimyshare.io home page    - is the domain reachable at all from
                                   this IP, or is the WHOLE domain blocked?
  3. /api/user (one account)     - what status / body is the WAF actually
                                   returning right now?

Read-only. Never POSTs anything.

Usage:
  python connectivity_check.py
  python connectivity_check.py --name kanao11    # pick a specific account
"""
from __future__ import annotations

import argparse
import sys

import requests

import core


CLAIMYSHARE_HOME = "https://claimyshare.io/"
USER_API = "https://claimyshare.io/api/user"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def _probe_solana() -> tuple[int, str]:
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
            timeout=10,
        )
        return r.status_code, r.text[:80]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _probe_home() -> tuple[int, str]:
    """No auth. If this is 429, our IP is blocked at the domain level."""
    try:
        r = requests.get(CLAIMYSHARE_HOME, timeout=15, allow_redirects=False)
        return r.status_code, r.text[:80]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _probe_user_api(acc: dict) -> tuple[int, str]:
    headers = core.build_headers(acc["bearer_token"], acc["cookie"])
    headers["referer"] = "https://claimyshare.io/"
    try:
        resp = core.claimyshare_get(USER_API, headers=headers, timeout=15)
        body = ""
        try:
            body = resp.text[:160]
        except Exception:  # noqa: BLE001
            pass
        return resp.status_code, body
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _interpret(solana: int, home: int, api: int) -> None:
    print()
    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)

    if solana != 200:
        print("  Solana RPC failed -> VPS network is broken. Not a rate-limit")
        print("  issue. Check basic connectivity first (ping, DNS).")
        return

    if home == 0:
        print("  Outbound to claimyshare.io fails entirely (network error).")
        print("  Could be DNS, CloudFlare blocking the TCP handshake, or a")
        print("  routing problem. Try `curl -v https://claimyshare.io/` from")
        print("  the VPS shell to see the underlying error.")
        return
    if home == 429:
        print("  Home page itself is 429. CloudFlare is blocking your IP for")
        print("  the WHOLE claimyshare.io domain, not just the API. This is")
        print("  an extended cooldown (1-24h typical for repeat offenders).")
        print()
        print("  Options:")
        print("    A. Wait 6-24h. Cooldowns DO eventually expire.")
        print("    B. Get a new outbound IP:")
        print("       - VPS provider: reboot / re-deploy may rotate IP")
        print("       - or use a proxy (paid) to route claimyshare traffic")
        print("    C. Reduce future traffic so this doesn't happen again:")
        print("       - don't run multiple test scripts simultaneously")
        print("       - increase BALANCE_REFRESH_SPACING_SEC if 64+ accounts")
        return
    if home in (403, 503):
        print("  Home page returned {0}. CloudFlare challenge / forbidden.".format(home))
        print("  Same playbook as 429 above. New IP or wait.")
        return
    if home >= 500:
        print(f"  Home page is {home}: server-side issue at claimyshare. Not")
        print("  your fault. Wait and retry.")
        return
    if home != 200:
        print(f"  Home page returned {home}. Unusual; inspect body above.")

    # Home is OK or 2xx-ish. Now interpret API result.
    if api == 0:
        print("  Home OK, but /api/user network errored. Could be a TLS")
        print("  fingerprint mismatch in curl_cffi. Try setting the")
        print("  CLAIMY_IMPERSONATE env var to a different chrome profile,")
        print("  or temporarily disable TLS impersonation in core.py.")
        return
    if api == 200:
        print("  /api/user is OK. Rate limit has cleared. Bot should resume")
        print("  successfully on the next refresher sweep.")
        return
    if api == 401:
        print("  /api/user returns 401. Cookies/bearer for this account are")
        print("  expired. Refresh credentials via refresh_creds.py.")
        return
    if api == 403:
        print("  /api/user returns 403. Either auth was rejected or WAF is")
        print("  challenging. If only this account does it -> auth issue.")
        print("  If every account does it -> WAF, treat like 429.")
        return
    if api == 429:
        print("  Home OK but /api/user is 429. The rate limit is path-")
        print("  specific (the WAF singled out /api/user). This usually")
        print("  clears in 5-30 minutes of zero traffic. Stop ALL scripts")
        print("  hitting /api/user, wait the full window, then resume.")
        return
    if api >= 500:
        print(f"  /api/user is {api}: server-side. Wait and retry.")
        return
    print(f"  /api/user returned {api}. Inspect body above for the message.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", help="account name (default: first in config).")
    args = p.parse_args()

    accounts = core.load_accounts()
    acc = None
    if args.name:
        for a in accounts:
            if a.get("name") == args.name:
                acc = a
                break
        if acc is None:
            sys.exit(f"[error] account {args.name!r} not in config.json")
    else:
        acc = accounts[0] if accounts else None
    if acc is None:
        sys.exit("[error] no accounts in config.json")

    print(f"# probing connectivity (account: {acc['name']})")
    print()

    print("[1/3] Solana RPC ...")
    s_status, s_body = _probe_solana()
    print(f"      status={s_status} body={s_body!r}")
    print()

    print("[2/3] claimyshare.io home (no auth) ...")
    h_status, h_body = _probe_home()
    print(f"      status={h_status} body={h_body!r}")
    print()

    print(f"[3/3] /api/user with account {acc['name']!r} ...")
    a_status, a_body = _probe_user_api(acc)
    print(f"      status={a_status} body={a_body!r}")

    _interpret(s_status, h_status, a_status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
