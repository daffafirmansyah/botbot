"""
Claimyshare auto social-tasks — one-shot mode.

For every account in config.json:
  1. GET  /api/tasks                  -> list of available tasks
  2. Filter to follow/like task types  -> skip everything else (Register, etc.)
  3. POST /api/tasks/complete          -> {"taskId": <id>}, one at a time
  4. Wait TASK_INTER_DELAY_SEC seconds between tasks so the backend has time
     to verify the follow/like against X's API. Hitting too fast triggers
     "still verifying" / rate-limit responses.

This script ASSUMES you have already followed/liked the targets manually on
X (twitter.com). The bot only claims the reward; it does NOT do the actual
follow/like for you. Tasks the backend cannot verify (e.g. you haven't
followed yet) are logged and skipped — re-run later to pick them up.

Examples:
  python tasks.py                    # all accounts, sequential
  python tasks.py --parallel         # all accounts, parallel + stagger
  python tasks.py --name acc1        # only one account
  python tasks.py --dry-run          # list eligible tasks, don't POST

Exit code:
  0 if at least one task was completed across all accounts,
  3 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from core import (
    EXIT_API_ERROR,
    EXIT_OK,
    build_headers,
    load_accounts,
    make_logger,
)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

TASKS_LIST_URL = "https://claimyshare.io/api/tasks"
TASKS_COMPLETE_URL = "https://claimyshare.io/api/tasks/complete"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Backend verifies follow/like against X's API after we POST. If we fire too
# fast it returns "still verifying" / rate-limits us. 8s is the safe baseline
# observed manually. Lower at your own risk.
TASK_INTER_DELAY_SEC = 8

# Only attempt tasks whose title starts with one of these (case-insensitive).
# Anything else (Register, Share, etc.) is skipped silently.
TASK_TITLE_PREFIXES = ("follow", "like")

# Per-request HTTP timeout.
HTTP_TIMEOUT_SEC = 20

# Parallel mode: fire multiple accounts at once. Each account still walks
# its own task list sequentially with TASK_INTER_DELAY_SEC between tasks.
MAX_PARALLEL_WORKERS = 8
PARALLEL_STAGGER_MS = 500   # delay between account workers starting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tasks_headers(bearer: str, cookie: str) -> dict:
    """build_headers() defaults referer to /withdraw; override to /tasks."""
    h = build_headers(bearer, cookie)
    h["referer"] = "https://claimyshare.io/tasks"
    return h


def _is_eligible(task: dict) -> bool:
    """True if the task is a follow/like type and not already completed."""
    title = str(task.get("title") or "").strip().lower()
    if not title.startswith(TASK_TITLE_PREFIXES):
        return False
    # Some APIs expose a `completed` / `done` flag — respect it if present.
    if task.get("completed") is True:
        return False
    if task.get("done") is True:
        return False
    return True


def _fmt_reward(parsed: dict | None) -> str:
    """Compact one-line reward summary from the complete-task response."""
    if not isinstance(parsed, dict):
        return ""
    sol = parsed.get("rewardSol", 0)
    cys = parsed.get("rewardCys", 0)
    box = parsed.get("twitterBox") or {}
    box_cys = box.get("rewardCys", 0) if isinstance(box, dict) else 0
    parts = []
    if sol:
        parts.append(f"+{sol} SOL")
    if cys:
        parts.append(f"+{cys} CYS")
    if box_cys:
        parts.append(f"+{box_cys} CYS (box)")
    return " ".join(parts) if parts else "(no reward fields)"


def fetch_tasks(bearer: str, cookie: str) -> list[dict]:
    """GET /api/tasks for one account. Raises on network / non-200."""
    resp = requests.get(
        TASKS_LIST_URL,
        headers=_tasks_headers(bearer, cookie),
        timeout=HTTP_TIMEOUT_SEC,
    )
    resp.raise_for_status()
    data = resp.json()
    # Some APIs wrap the list under a key; be lenient.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tasks", "data", "result"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    raise ValueError(f"unexpected /api/tasks response shape: {type(data).__name__}")


def complete_task(
    bearer: str, cookie: str, task_id: int
) -> tuple[int, dict | None]:
    """POST /api/tasks/complete with {taskId}. Returns (status, parsed_body)."""
    body = {"taskId": task_id}
    try:
        resp = requests.post(
            TASKS_COMPLETE_URL,
            headers=_tasks_headers(bearer, cookie),
            json=body,
            timeout=HTTP_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        return 0, {"error": f"network: {e}"}
    try:
        parsed = resp.json()
    except ValueError:
        parsed = {"raw": resp.text[:300]}
    return resp.status_code, parsed


# ---------------------------------------------------------------------------
# Per-account worker
# ---------------------------------------------------------------------------


def process_account(acc: dict, log, dry_run: bool) -> dict:
    """Walk one account's task list and POST complete on each eligible task."""
    name = acc.get("name", "?")
    bearer = acc["bearer_token"]
    cookie = acc["cookie"]

    log(f"[{name}] fetching tasks list...")
    try:
        tasks = fetch_tasks(bearer, cookie)
    except Exception as e:  # noqa: BLE001
        log(f"[{name}] [error] failed to fetch tasks: {e}")
        return {"name": name, "ok": 0, "fail": 0, "skipped": 0, "reward_sol": 0.0}

    eligible = [t for t in tasks if _is_eligible(t)]
    other = len(tasks) - len(eligible)
    log(
        f"[{name}] {len(tasks)} task(s) total | "
        f"{len(eligible)} eligible (follow/like) | "
        f"{other} skipped (other types or already done)."
    )

    if not eligible:
        return {"name": name, "ok": 0, "fail": 0, "skipped": other, "reward_sol": 0.0}

    ok = 0
    fail = 0
    reward_sol_total = 0.0

    for i, task in enumerate(eligible):
        tid = task.get("id")
        title = task.get("title", "?")
        expected = task.get("rewardSol", "?")

        if i > 0:
            log(f"[{name}] sleeping {TASK_INTER_DELAY_SEC}s before next task...")
            time.sleep(TASK_INTER_DELAY_SEC)

        if dry_run:
            log(f"[{name}] [dry-run] would POST taskId={tid} ('{title}', "
                f"expected +{expected} SOL).")
            continue

        log(f"[{name}] posting taskId={tid} ('{title}')...")
        status, body = complete_task(bearer, cookie, tid)

        if 200 <= status < 300 and isinstance(body, dict) \
                and str(body.get("message", "")).lower() == "task completed":
            ok += 1
            try:
                reward_sol_total += float(body.get("rewardSol") or 0)
            except (TypeError, ValueError):
                pass
            log(f"[{name}] [ok] task {tid} '{title}' -> {_fmt_reward(body)}")
        else:
            fail += 1
            # Most common reasons: backend says you haven't followed yet, or
            # task is mid-verification. We do NOT retry — user re-runs later.
            log(f"[{name}] [skip] task {tid} '{title}' status={status} body={body}")

    return {
        "name": name,
        "ok": ok,
        "fail": fail,
        "skipped": other,
        "reward_sol": reward_sol_total,
    }


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _run_sequential(accounts: list[dict], log, dry_run: bool) -> list[dict]:
    results: list[dict] = []
    for i, acc in enumerate(accounts):
        log(f"=== account {i + 1}/{len(accounts)}: {acc.get('name', '?')} ===")
        results.append(process_account(acc, log, dry_run))
    return results


def _run_parallel(accounts: list[dict], log, dry_run: bool) -> list[dict]:
    stagger = max(PARALLEL_STAGGER_MS, 0) / 1000.0
    total_dispatch = stagger * (len(accounts) - 1)
    log(
        f"[parallel] firing {len(accounts)} account(s) "
        f"(max workers={MAX_PARALLEL_WORKERS}, "
        f"stagger={PARALLEL_STAGGER_MS}ms => dispatch window {total_dispatch:.1f}s)."
    )
    workers = min(MAX_PARALLEL_WORKERS, len(accounts))
    results: list[dict] = []

    def _delayed(acc: dict, delay: float) -> dict:
        if delay > 0:
            time.sleep(delay)
        return process_account(acc, log, dry_run)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="task") as ex:
        futures = [
            ex.submit(_delayed, acc, i * stagger)
            for i, acc in enumerate(accounts)
        ]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                log(f"[error] worker thread crashed: {e}")
                results.append({
                    "name": "?", "ok": 0, "fail": 0, "skipped": 0, "reward_sol": 0.0,
                })
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-complete claimyshare follow/like tasks for all accounts.",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help="run accounts in parallel (default: sequential).",
    )
    p.add_argument(
        "--name",
        help="run only the account with this name (default: all accounts).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + filter tasks but do NOT POST /api/tasks/complete.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    accounts = load_accounts()

    if args.name:
        match = [a for a in accounts if a.get("name") == args.name]
        if not match:
            print(f"[error] account {args.name!r} not found in config.json.",
                  file=sys.stderr)
            return EXIT_API_ERROR
        accounts = match

    log = make_logger("tasks.log")
    log(
        f"tasks one-shot start | accounts={[a['name'] for a in accounts]} "
        f"mode={'parallel' if args.parallel else 'sequential'} "
        f"dry_run={args.dry_run}"
    )

    if args.parallel and len(accounts) > 1:
        results = _run_parallel(accounts, log, args.dry_run)
    else:
        results = _run_sequential(accounts, log, args.dry_run)

    # ----- Summary -----
    total_ok = sum(r["ok"] for r in results)
    total_fail = sum(r["fail"] for r in results)
    total_skipped = sum(r["skipped"] for r in results)
    total_reward = sum(r["reward_sol"] for r in results)

    log("=== summary ===")
    for r in results:
        log(
            f"  {r['name']}: ok={r['ok']} fail={r['fail']} "
            f"other_skipped={r['skipped']} reward=+{r['reward_sol']:.6f} SOL"
        )
    log(
        f"TOTAL: ok={total_ok} fail={total_fail} "
        f"other_skipped={total_skipped} reward=+{total_reward:.6f} SOL "
        f"across {len(results)} account(s)."
    )

    return EXIT_OK if total_ok > 0 else EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
