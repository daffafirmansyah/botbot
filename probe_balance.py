"""
Probe claimyshare.io API to find the "claimable balance" endpoint.

Read-only: only sends GETs, never writes state. Uses the first account
in config.json and the same bearer/cookie as the withdraw flow.

Usage:
    python probe_balance.py

What it does:
  1. Loads config.json, picks the first account.
  2. Tries a list of common GET paths under claimyshare.io with
     1.5 s spacing (stays well under the 3 req / 60 s limit).
  3. Prints status, body preview, and highlights any response that
     mentions balance-like keywords (balance, amount, available,
     claimable, saldo, sol, lamport, wallet, earn, point).
  4. At the end, prints a short summary of candidate endpoints.

Once you see a promising endpoint, paste its URL and full response
body back and I'll wire it into the withdraw flow for auto-amount.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import requests

from core import build_headers, load_accounts

# Candidate GET paths. Both /api/* and a few root paths in case the site
# puts some endpoints outside /api/.
CANDIDATES: list[str] = [
    "/api/me",
    "/api/user",
    "/api/user/me",
    "/api/user/profile",
    "/api/user/balance",
    "/api/user/wallet",
    "/api/user/claimable",
    "/api/profile",
    "/api/balance",
    "/api/wallet",
    "/api/claim",
    "/api/claim/info",
    "/api/claim/available",
    "/api/withdraw",
    "/api/withdraw/info",
    "/api/withdraw/balance",
    "/api/withdraw/available",
    "/api/account",
    "/api/dashboard",
    "/api/earnings",
    "/api/points",
    "/api/tasks",
    "/api/status",
]

BASE = "https://claimyshare.io"
REQ_TIMEOUT = 15
SPACING_SEC = 1.5

BALANCE_KEYWORDS = (
    "balance",
    "amount",
    "available",
    "claimable",
    "saldo",
    "sol",
    "lamport",
    "wallet",
    "earn",
    "point",
    "credit",
)


def _looks_like_balance(body: Any) -> bool:
    try:
        text = json.dumps(body).lower() if isinstance(body, (dict, list)) else str(body).lower()
    except (TypeError, ValueError):
        text = str(body).lower()
    return any(k in text for k in BALANCE_KEYWORDS)


def _preview(body: Any, limit: int = 600) -> str:
    if isinstance(body, (dict, list)):
        try:
            s = json.dumps(body, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(body)
    else:
        s = str(body)
    if len(s) > limit:
        s = s[:limit] + f"\n  ... (truncated, total {len(s)} chars)"
    return s


def probe_one(acc: dict, path: str) -> tuple[int, Any]:
    url = f"{BASE}{path}" if path.startswith("/") else path
    headers = build_headers(acc["bearer_token"], acc["cookie"])
    # Override referer to root since balance checks usually happen from /, not /withdraw.
    headers["referer"] = f"{BASE}/"
    try:
        resp = requests.get(url, headers=headers, timeout=REQ_TIMEOUT)
    except requests.RequestException as e:
        return -1, f"{type(e).__name__}: {e}"

    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:1000]
    return resp.status_code, body


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        sys.exit("[error] no accounts in config.json. Add one with add_account.py first.")

    acc = accounts[0]
    print(f"Probing {BASE} with account {acc['name']!r} — {len(CANDIDATES)} paths, "
          f"{SPACING_SEC}s spacing.\n")

    hits: list[tuple[str, Any]] = []
    other_200s: list[str] = []

    for i, path in enumerate(CANDIDATES, 1):
        status, body = probe_one(acc, path)
        relevant = status == 200 and _looks_like_balance(body)
        marker = ""
        if relevant:
            marker = "  <-- RELEVANT (mentions balance-ish keywords)"
            hits.append((path, body))
        elif status == 200:
            marker = "  (200 but no balance keywords)"
            other_200s.append(path)

        print(f"[{i:>2}/{len(CANDIDATES)}] GET {path:<30} -> {status}{marker}")

        if status == 200:
            print("  body:")
            for line in _preview(body).splitlines():
                print(f"    {line}")
            print()

        if i < len(CANDIDATES):
            time.sleep(SPACING_SEC)

    print("=" * 60)
    print(f"Probe complete. {len(hits)} relevant hit(s), "
          f"{len(other_200s)} other 200 response(s).")
    if hits:
        print("\nLikely balance endpoint(s):")
        for path, _ in hits:
            print(f"  - GET {BASE}{path}")
        print("\nCopy the FULL response body of the best match and paste it "
              "back so I can see which field holds the balance.")
    elif other_200s:
        print("\nNo endpoint flagged 'balance-like', but these returned 200:")
        for p in other_200s:
            print(f"  - {p}")
        print("\nOne of these might still be it — inspect the bodies above.")
    else:
        print("\nNo 200 responses. Either auth failed, or none of these paths exist.")
        print("Next step: capture via DevTools instead (F12 -> Network -> reload).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
