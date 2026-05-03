"""
Quick latency + reliability benchmark for the Solana RPC endpoints
configured in core.SOLANA_RPCS. Run this from the machine you intend to
host monitor.py on (e.g. the VPS) so the numbers reflect reality for
that network path.

Usage:
    python rpc_benchmark.py           # 10 rounds per endpoint, default
    python rpc_benchmark.py --rounds 30
"""
from __future__ import annotations

import argparse
import statistics
import time

import requests

from core import SOLANA_RPCS, HOT_WALLET


def ping_once(url: str, timeout: float = 5.0) -> float | None:
    """Return round-trip seconds, or None on failure."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [HOT_WALLET]}
    start = time.perf_counter()
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data and "result" not in data:
            return None
        return time.perf_counter() - start
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark Solana RPC endpoints.")
    p.add_argument("--rounds", type=int, default=10, help="Requests per endpoint (default 10)")
    p.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout seconds (default 5.0)")
    args = p.parse_args()

    print(f"Benchmarking {len(SOLANA_RPCS)} endpoint(s), {args.rounds} round(s) each, {args.timeout}s timeout.\n")
    results: list[tuple[str, list[float], int]] = []

    for url in SOLANA_RPCS:
        latencies: list[float] = []
        failures = 0
        for _ in range(args.rounds):
            dt = ping_once(url, args.timeout)
            if dt is None:
                failures += 1
            else:
                latencies.append(dt * 1000.0)  # ms
            time.sleep(0.1)  # don't hammer
        results.append((url, latencies, failures))

    print(f"{'Endpoint':<55} {'OK':>4} {'FAIL':>4} {'min':>6} {'avg':>6} {'p95':>6} {'max':>6}")
    print("-" * 95)
    for url, lats, fails in results:
        if lats:
            mn = min(lats)
            avg = statistics.mean(lats)
            p95 = statistics.quantiles(lats, n=20)[18] if len(lats) >= 2 else mn
            mx = max(lats)
            print(f"{url:<55} {len(lats):>4} {fails:>4} {mn:>5.0f}ms {avg:>5.0f}ms {p95:>5.0f}ms {mx:>5.0f}ms")
        else:
            print(f"{url:<55} {len(lats):>4} {fails:>4} {'-':>6} {'-':>6} {'-':>6} {'-':>6}  (all failed)")

    # Rank by avg latency among those with failures < 50%.
    ranked = sorted(
        [(url, lats, fails) for url, lats, fails in results if lats and fails < args.rounds / 2],
        key=lambda x: statistics.mean(x[1]),
    )
    if ranked:
        print("\nSuggested SOLANA_RPCS order (fastest first, ignoring endpoints with >=50% fail rate):\n")
        for url, _, _ in ranked:
            print(f'    "{url}",')

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
