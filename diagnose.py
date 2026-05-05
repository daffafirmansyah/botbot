"""
diagnose.py - Quick health probe of every claimyshare endpoint we touch.

Helps answer: is the whole backend down, or only the /api/tasks/complete +
X verification path? Read-only — never POSTs anything destructive.

Endpoints probed:
  GET  /api/user     -> balance fetch
  GET  /api/tasks    -> task list + breakdown by verification type
  GET  https://x.com/i/api/1.1/account/verify_credentials.json
                     -> sanity check that the Twitter side is healthy too
                        (read-only, just confirms cookies still log us in)

Usage:
  python diagnose.py                    # picks the first account in config
  python diagnose.py --name kanao11     # pick a specific account
  python diagnose.py --probe-complete   # ALSO POST /api/tasks/complete on
                                        # ONE non-X task per account to see
                                        # whether the backend can credit
                                        # at all (uses one task; harmless
                                        # if it would have been claimed
                                        # anyway).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

import core
import tasks


def _pick_account(name: str | None) -> dict:
    accounts = core.load_accounts()
    if name:
        for a in accounts:
            if a.get("name") == name:
                return a
        sys.exit(f"[error] account {name!r} not in config.json")
    return accounts[0]


def _make_logger(prefix: str):
    return lambda msg: print(f"  [{prefix}] {msg}")


def probe_user(acc: dict) -> None:
    print("=" * 70)
    print("[1/3] GET /api/user  (balance read, claimyshare health)")
    print("=" * 70)
    log = _make_logger("user")
    balance = core.fetch_claimable_balance(acc, log)
    if balance is None:
        print("  STATUS: FAIL  (read-only endpoint did not respond cleanly)")
        return
    print(f"  STATUS: OK    balanceSolTask = {balance:.9f} SOL")


def probe_tasks(acc: dict) -> list[dict]:
    print()
    print("=" * 70)
    print("[2/3] GET /api/tasks  (task list)")
    print("=" * 70)
    log = _make_logger("tasks")
    try:
        all_tasks = tasks.fetch_tasks(
            acc["bearer_token"], acc["cookie"], log=log
        )
    except Exception as e:  # noqa: BLE001
        print(f"  STATUS: FAIL  {e}")
        return []

    print(f"  STATUS: OK    total tasks = {len(all_tasks)}")
    print()

    # Per-task breakdown (compact one-liners).
    print("  Tasks (id, done, type, reward, title):")
    for t in all_tasks:
        tid = t.get("id")
        title = (t.get("title") or "")[:40]
        vtype = t.get("verificationType") or "?"
        done = bool(t.get("completed") or t.get("done"))
        reward = t.get("rewardSol") or t.get("rewardCys") or "-"
        print(
            f"    id={str(tid):>4} done={'Y' if done else 'N'} "
            f"type={vtype:<20} reward={str(reward):<6} title={title!r}"
        )

    # Aggregate counts.
    print()
    types = Counter(t.get("verificationType") or "unknown" for t in all_tasks)
    print("  Verification type breakdown:")
    for vt, n in types.most_common():
        print(f"    {vt}: {n}")

    return all_tasks


def probe_complete(acc: dict, all_tasks: list[dict]) -> None:
    print()
    print("=" * 70)
    print("[3/3] POST /api/tasks/complete  (one non-X task as a probe)")
    print("=" * 70)

    # Find the first eligible non-X task that isn't already done. That way,
    # we learn whether /complete works at all without touching X-verified
    # tasks (which we already know report 'service-down').
    candidates = [
        t for t in all_tasks
        if not (t.get("completed") or t.get("done"))
        and not str(t.get("title", "")).lower().startswith(("follow", "like"))
    ]
    if not candidates:
        print("  SKIP  no non-X candidate tasks left (everything is "
              "already done or only follow/like remain).")
        return

    target = candidates[0]
    tid = target.get("id")
    title = target.get("title")
    vtype = target.get("verificationType")
    print(f"  probing taskId={tid} '{title}' (type={vtype})...")

    status, body = tasks.complete_task(
        acc["bearer_token"], acc["cookie"], int(tid)
    )
    outcome = tasks._classify_response(status, body)
    print(f"  STATUS: {status}")
    print(f"  OUTCOME: {outcome}")
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error") or "")
    if msg:
        print(f"  MESSAGE: {msg!r}")

    print()
    if outcome == "ok":
        print("  ==> Non-X /complete WORKS. Backend can credit rewards; "
              "the issue is isolated to X verification.")
    elif outcome == "service-down":
        print("  ==> Non-X /complete ALSO returns service-down. The whole "
              "/complete endpoint (or its dependencies) is down.")
    elif outcome in ("throttled", "error"):
        print(f"  ==> Inconclusive ({outcome}). Could be rate-limit or a "
              "different failure mode; try again in a minute.")
    elif outcome == "already-done":
        print("  ==> Server says already-done. /complete works, the "
              "task was just claimed before. Endpoint healthy.")
    else:
        print(f"  ==> Got '{outcome}'. See raw status/message above.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", help="account name (default: first in config).")
    p.add_argument(
        "--probe-complete",
        action="store_true",
        help="also POST /api/tasks/complete on one non-X task to test "
             "whether the endpoint is fully down or only X-verified "
             "tasks fail.",
    )
    args = p.parse_args()

    acc = _pick_account(args.name)
    print(f"# account: {acc['name']}")
    print(f"# wallet : {acc['wallet_address'][:10]}..{acc['wallet_address'][-4:]}")
    print()

    probe_user(acc)
    all_tasks = probe_tasks(acc)
    if args.probe_complete and all_tasks:
        probe_complete(acc, all_tasks)
    elif all_tasks:
        print()
        print("(skipping /complete probe; pass --probe-complete to enable)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
