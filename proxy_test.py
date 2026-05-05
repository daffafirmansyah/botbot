#!/usr/bin/env python3
"""
Proxy pool health check.

Tests every proxy in proxies.json against:
  1. ipify (quick liveness + exit-IP verification)
  2. claimyshare.io root (TLS handshake + WAF acceptance)

Run this before restarting the bot so you know which proxies are alive
and which exit IP each one gives you. Outputs a simple table and prints
a summary; exit code 0 if ALL proxies work, 1 otherwise.

Usage:
    python proxy_test.py            # test all proxies in proxies.json
    python proxy_test.py --quick    # skip claimyshare check (just ipify)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import sys
import time

try:
    from curl_cffi import requests as _cffi_requests  # type: ignore
    _HAS_CFFI = True
except ImportError:
    import requests as _cffi_requests  # type: ignore
    _HAS_CFFI = False

from core import load_proxies, get_proxy_for_account, IMPERSONATE_PROFILE


TIMEOUT_SEC = 15
IPIFY_URL = "https://api.ipify.org?format=json"
CLAIMYSHARE_URL = "https://claimyshare.io/"


def _test_proxy(proxy_url: str, quick: bool) -> dict:
    """Probe a single proxy; return dict of results."""
    result = {
        "proxy": proxy_url,
        "alive": False,
        "exit_ip": None,
        "ipify_ms": None,
        "claimy_status": None,
        "claimy_ms": None,
        "error": None,
    }

    proxies = {"http": proxy_url, "https": proxy_url}

    # --- Probe 1: ipify (liveness + exit IP) ---
    try:
        t0 = time.time()
        if _HAS_CFFI:
            resp = _cffi_requests.get(
                IPIFY_URL,
                timeout=TIMEOUT_SEC,
                proxies=proxies,
                impersonate=IMPERSONATE_PROFILE,
            )
        else:
            resp = _cffi_requests.get(
                IPIFY_URL, timeout=TIMEOUT_SEC, proxies=proxies
            )
        result["ipify_ms"] = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            try:
                result["exit_ip"] = resp.json().get("ip")
                result["alive"] = True
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        result["error"] = f"ipify: {type(e).__name__}: {e}"
        return result

    if not result["alive"]:
        return result

    if quick:
        return result

    # --- Probe 2: claimyshare.io (WAF / TLS handshake) ---
    try:
        t0 = time.time()
        if _HAS_CFFI:
            resp = _cffi_requests.get(
                CLAIMYSHARE_URL,
                timeout=TIMEOUT_SEC,
                proxies=proxies,
                impersonate=IMPERSONATE_PROFILE,
            )
        else:
            resp = _cffi_requests.get(
                CLAIMYSHARE_URL, timeout=TIMEOUT_SEC, proxies=proxies
            )
        result["claimy_ms"] = int((time.time() - t0) * 1000)
        result["claimy_status"] = resp.status_code
    except Exception as e:  # noqa: BLE001
        result["error"] = f"claimy: {type(e).__name__}: {e}"

    return result


def _format_proxy_short(proxy_url: str) -> str:
    """Mask password in the URL for display."""
    # http://user:pass@host:port -> user@host:port
    try:
        after_scheme = proxy_url.split("://", 1)[1]
        auth, hostport = after_scheme.split("@", 1)
        user = auth.split(":", 1)[0]
        return f"{user}@{hostport}"
    except Exception:  # noqa: BLE001
        return proxy_url[:40]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe every proxy in proxies.json for liveness."
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip the claimyshare.io probe (just ipify).",
    )
    parser.add_argument(
        "--account",
        metavar="NAME",
        help="Show which proxy would be assigned to this account "
             "(via get_proxy_for_account) and exit.",
    )
    args = parser.parse_args()

    proxies = load_proxies()
    if not proxies:
        print("[error] proxies.json is missing or empty; nothing to test.")
        return 1

    if args.account:
        assigned = get_proxy_for_account(args.account)
        if assigned is None:
            print(f"Account '{args.account}' -> direct connection (no pool).")
        else:
            print(f"Account '{args.account}' -> {_format_proxy_short(assigned)}")
        return 0

    print(f"Testing {len(proxies)} proxy IP(s) "
          f"({'quick' if args.quick else 'full'}) ...\n")

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(proxies)) as ex:
        futures = [
            ex.submit(_test_proxy, p, args.quick) for p in proxies
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    # Preserve pool ordering for the report (not arrival order).
    order = {p: i for i, p in enumerate(proxies)}
    results.sort(key=lambda r: order.get(r["proxy"], 999))

    # Header
    if args.quick:
        print(f"{'#':>3} {'proxy (user@host:port)':<38} "
              f"{'alive':<7} {'exit_ip':<17} {'ipify':>7}")
        print("-" * 80)
    else:
        print(f"{'#':>3} {'proxy (user@host:port)':<38} "
              f"{'alive':<7} {'exit_ip':<17} {'ipify':>7} "
              f"{'claimy':>7} {'status':>7}")
        print("-" * 98)

    alive_count = 0
    for i, r in enumerate(results, 1):
        alive_str = "YES" if r["alive"] else "NO"
        if r["alive"]:
            alive_count += 1
        exit_ip = r["exit_ip"] or "-"
        ipify = f"{r['ipify_ms']}ms" if r["ipify_ms"] else "-"
        proxy_disp = _format_proxy_short(r["proxy"])

        if args.quick:
            print(f"{i:>3} {proxy_disp:<38} {alive_str:<7} "
                  f"{exit_ip:<17} {ipify:>7}")
        else:
            claimy = f"{r['claimy_ms']}ms" if r["claimy_ms"] else "-"
            status = str(r["claimy_status"]) if r["claimy_status"] else "-"
            print(f"{i:>3} {proxy_disp:<38} {alive_str:<7} "
                  f"{exit_ip:<17} {ipify:>7} {claimy:>7} {status:>7}")

        if r["error"]:
            print(f"    error: {r['error']}")

    print()
    print(f"Summary: {alive_count}/{len(proxies)} proxies alive.")
    if alive_count < len(proxies):
        print("  Dead proxies will be skipped by the bot? NO -- current\n"
              "  implementation hard-assigns each account to one proxy.\n"
              "  If a proxy is dead, its assigned accounts fall silent.\n"
              "  Remove dead entries from proxies.json and restart.")
    if not args.quick:
        non_200 = [r for r in results if r["alive"] and r["claimy_status"] != 200]
        if non_200:
            print(f"  Warning: {len(non_200)} proxy/proxies returned non-200 "
                  f"from claimyshare.io -- may be WAF-blocked.")

    return 0 if alive_count == len(proxies) else 1


if __name__ == "__main__":
    sys.exit(main())
