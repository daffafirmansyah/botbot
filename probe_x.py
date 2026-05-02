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

# Two-stage probe:
#   1. GET HOME_URL with cookies. If we end up authenticated (no redirect
#      to login), the cookies are valid — this is the canonical signal.
#   2. Best-effort SETTINGS_URL fetch to grab the linked X screen_name
#      so the user can sanity-check the mapping. If this stage fails it
#      does NOT mark the account invalid — stage 1 is authoritative.
HOME_URL = "https://x.com/home"
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


def _check_home(auth_token: str, ct0: str) -> dict:
    """
    Stage 1: GET https://x.com/home with the cookies and check the result.
    A logged-in session lands on /home (200). A logged-out session is
    redirected to /login or /i/flow/login (3xx).
    """
    cookies = {"auth_token": auth_token, "ct0": ct0}
    headers = {
        "user-agent": USER_AGENT,
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8"
        ),
        "accept-language": "en-US,en;q=0.9",
        "upgrade-insecure-requests": "1",
    }
    try:
        resp = requests.get(
            HOME_URL,
            cookies=cookies,
            headers=headers,
            allow_redirects=False,
            timeout=HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        return {"status": "error", "details": f"network: {e}"}

    # 3xx -> not logged in (redirected to login flow).
    if 300 <= resp.status_code < 400:
        loc = (resp.headers.get("location") or "").lower()
        if "login" in loc or "flow" in loc or "i/flow" in loc:
            return {"status": "invalid", "details": f"redirected to {loc}"}
        # Some unrelated redirect — treat as logged in (e.g. 302 to /).
        return {"status": "ok"}

    if resp.status_code == 200:
        return {"status": "ok"}

    # 401, 403, 5xx etc.
    return {
        "status": "invalid",
        "http": resp.status_code,
        "details": f"unexpected status {resp.status_code}",
    }


def _try_screen_name(auth_token: str, ct0: str) -> str | None:
    """
    Stage 2 (best effort): try to fetch the X screen_name linked to these
    cookies via the legacy settings endpoint. Returns None if the call
    fails for any reason — this stage is purely informational.
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
        "referer": "https://x.com/home",
        "origin": "https://x.com",
    }
    try:
        resp = requests.get(
            SETTINGS_URL, headers=headers, timeout=HTTP_TIMEOUT_SEC
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        return body.get("screen_name") if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _probe_one(auth_token: str, ct0: str) -> dict:
    """
    Stage 1 (authoritative) + Stage 2 (informational).

    Returns a dict with 'status' set to 'ok', 'invalid', or 'error'.
    On 'ok', 'screen_name' is included if Stage 2 succeeded.
    """
    home = _check_home(auth_token, ct0)
    if home["status"] != "ok":
        return home

    screen_name = _try_screen_name(auth_token, ct0)
    return {
        "status": "ok",
        "screen_name": screen_name,  # may be None
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
            sn = result.get("screen_name")
            handle = f"@{sn}" if sn else "(screen_name unavailable)"
            print(f"  [OK]       {name:20}  ->  {handle}")
        elif status == "invalid":
            http = result.get("http", "?")
            details = result.get("details", "?")
            print(f"  [INVALID]  {name:20}  http={http} {details}")
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
