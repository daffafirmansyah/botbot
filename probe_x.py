"""
probe_x.py — validate X (twitter.com) cookies in x_accounts.tsv.

For every row in x_accounts.tsv whose auth_token and ct0 are filled,
hit a lightweight authenticated X endpoint to check if the cookie pair
is still valid. This does NOT follow or like anything — it's purely
a health check.

Useful as:
  1. A post-setup sanity check ("did I paste the cookies correctly?")
  2. A foundation piece of Phase 2 (x_auto.py will reuse the same
     request shape for follow/like).
  3. A way to see which claimyshare account is linked to which X
     handle (the endpoint returns screen_name).

Examples:
  python probe_x.py                 # probe every filled account
  python probe_x.py --name adella   # probe one account only

Output per row:
  [OK]       <name>   -> @<x_handle>         (cookies valid)
  [INVALID]  <name>   http=<code> body=...   (cookies expired / wrong)
  [ERROR]    <name>   network: ...           (could not reach X)
  [SKIP]     <name>   placeholder             (row not filled yet)

Exit code:
  0 if every filled account probed successfully, 1 otherwise.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
X_ACCOUNTS_PATH = SCRIPT_DIR / "x_accounts.tsv"

# Public bearer from x.com's web bundle. This is the SAME value every
# browser uses when you load x.com — it's not a secret. It only identifies
# the "web app" client; real auth still comes from the auth_token cookie.
X_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Lightweight authenticated endpoint that returns the current user's
# settings (including screen_name). 200 = valid cookies, 401/403 = not.
SETTINGS_URL = "https://x.com/i/api/1.1/account/settings.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT_SEC = 15

# Delay between probes to avoid tripping X's anti-bot heuristics.
# Probing is read-only and low-risk, so a light random delay is enough.
PROBE_DELAY_MIN_SEC = 0.8
PROBE_DELAY_MAX_SEC = 2.0


def _is_placeholder(acc: dict) -> bool:
    """Row still has REPLACE_WITH_* values — user hasn't filled it in."""
    return (
        "REPLACE_WITH" in (acc.get("auth_token") or "")
        or "REPLACE_WITH" in (acc.get("ct0") or "")
        or not acc.get("auth_token")
        or not acc.get("ct0")
    )


def _load_x_accounts() -> list[dict]:
    if not X_ACCOUNTS_PATH.exists():
        sys.exit(f"[error] {X_ACCOUNTS_PATH} not found.")
    with X_ACCOUNTS_PATH.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _probe_one(auth_token: str, ct0: str) -> dict:
    """
    Hit /1.1/account/settings.json with the given cookies. Returns a dict
    with 'status' set to 'ok', 'invalid', or 'error'.
    """
    headers = {
        "authorization": f"Bearer {X_WEB_BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "user-agent": USER_AGENT,
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://x.com/",
    }
    try:
        resp = requests.get(
            SETTINGS_URL, headers=headers, timeout=HTTP_TIMEOUT_SEC
        )
    except requests.RequestException as e:
        return {"status": "error", "details": f"network: {e}"}

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:200]}

    if (
        resp.status_code == 200
        and isinstance(body, dict)
        and body.get("screen_name")
    ):
        return {
            "status": "ok",
            "screen_name": body["screen_name"],
            "protected": body.get("protected", False),
        }

    return {
        "status": "invalid",
        "http": resp.status_code,
        "body": body,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate X cookies in x_accounts.tsv."
    )
    parser.add_argument(
        "--name",
        help="probe only this account (default: all filled rows).",
    )
    args = parser.parse_args()

    accounts = _load_x_accounts()

    if args.name:
        accounts = [a for a in accounts if a.get("name") == args.name]
        if not accounts:
            sys.exit(f"[error] account {args.name!r} not in x_accounts.tsv.")

    filled = [a for a in accounts if not _is_placeholder(a)]
    skipped = len(accounts) - len(filled)

    print(
        f"probing {len(filled)} account(s) "
        f"({skipped} placeholder/empty skipped)."
    )
    print()

    stats = {"ok": 0, "invalid": 0, "error": 0}

    for i, acc in enumerate(filled):
        name = acc.get("name", "?")
        if i > 0:
            time.sleep(random.uniform(PROBE_DELAY_MIN_SEC, PROBE_DELAY_MAX_SEC))

        result = _probe_one(acc["auth_token"], acc["ct0"])
        status = result["status"]
        stats[status] += 1

        if status == "ok":
            lock = " [protected]" if result.get("protected") else ""
            print(f"  [OK]       {name:20}  ->  @{result['screen_name']}{lock}")
        elif status == "invalid":
            http = result.get("http", "?")
            body = result.get("body", "?")
            # Keep the body short so the output stays readable.
            body_str = str(body)
            if len(body_str) > 120:
                body_str = body_str[:117] + "..."
            print(f"  [INVALID]  {name:20}  http={http} body={body_str}")
        else:
            print(f"  [ERROR]    {name:20}  {result.get('details')}")

    print()
    print(
        f"SUMMARY: ok={stats['ok']}  invalid={stats['invalid']}  "
        f"error={stats['error']}  skipped={skipped}"
    )

    return 0 if stats["invalid"] + stats["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
