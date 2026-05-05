"""
Claimyshare auto social-tasks — one-shot mode (Phase 1).

For every account in config.json:
  1. GET  /api/tasks                  -> list of available tasks
  2. Filter to follow/like task types  -> skip everything else (Register, etc.)
  3. POST /api/tasks/complete          -> {"taskId": <id>}, one at a time
  4. Classify the response by message content:
        - "Task completed"           -> reward claimed (success)
        - "You are not following"    -> needs real X follow first
        - "You haven't liked"        -> needs real X like first
        - "already completed"        -> silent no-op
        - other                      -> error, log full body
  5. Tasks that need a real X action are written to pending_x.json so the
     Phase 2 module (x_auto.py — TODO) can pick them up later.

IMPORTANT: this script does NOT do the actual follow/like on X. If a task
requires a real follow you haven't done yet, claimyshare will reject the
claim — bot will skip it and write the target to pending_x.json. Run again
after you've followed (or after Phase 2 has done it for you).

Examples:
  python tasks.py                    # all accounts, sequential
  python tasks.py --parallel         # all accounts, parallel + stagger
  python tasks.py --name acc1        # only one account
  python tasks.py --dry-run          # list eligible tasks, don't POST

Outputs:
  tasks.log         human-readable log
  pending_x.json    machine-readable list of pending follow/like targets

Exit code:
  0 if at least one task was completed across all accounts,
  3 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable

from core import (
    EXIT_API_ERROR,
    EXIT_OK,
    SCRIPT_DIR,
    build_headers,
    claimyshare_get,
    claimyshare_post,
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
# fast it returns "still verifying" / rate-limits us. 15s is conservative —
# gives X enough time to propagate state. Lower at your own risk.
TASK_INTER_DELAY_SEC = 15

# Only attempt tasks whose title starts with one of these (case-insensitive).
# Anything else (Register, Share, etc.) is skipped silently.
TASK_TITLE_PREFIXES = ("follow", "like")

# Per-request HTTP timeout.
HTTP_TIMEOUT_SEC = 20

# Retry policy for `fetch_tasks` when claimyshare returns 429 / 5xx. The
# discovery step is the gate to everything else, so giving up on the first
# rate-limit hit aborts the whole account. Mirrors the retry shape used in
# core.attempt_withdraw, but with finite retries because we do NOT want to
# block the caller indefinitely on a read.
FETCH_TASKS_MAX_RETRIES = 5
FETCH_TASKS_RETRY_BASE_SEC = 3
FETCH_TASKS_RETRY_MAX_SEC = 8

# Parallel mode: fire multiple accounts at once. Each account still walks
# its own task list sequentially with TASK_INTER_DELAY_SEC between tasks.
# Conservative defaults: 4 parallel workers + 2s stagger spread "burst" of
# 64 accounts over ~2 minutes instead of seconds, lowering WAF / 429 risk.
MAX_PARALLEL_WORKERS = 4
PARALLEL_STAGGER_MS = 2000   # delay between account workers starting

# Output file for tasks that need a real follow/like on X. Phase 2 (x_auto.py)
# will consume this. Gitignored — never commit.
PENDING_X_PATH = SCRIPT_DIR / "pending_x.json"

# Response classification — keywords matched (case-insensitive) against the
# response 'message' field. Order doesn't matter; first match wins.
_OK_KEYWORDS = ("task completed",)
_NEED_FOLLOW_KEYWORDS = (
    "not following this account",
    "please follow and try",
)
_NEED_LIKE_KEYWORDS = (
    "haven't liked",
    "have not liked",
    "please like and try",
)
_ALREADY_DONE_KEYWORDS = (
    "already completed",
    "already claimed",
)
_THROTTLED_KEYWORDS = (
    "still verifying",
    "still syncing",
    "try again later",
    "try again in a few",
    "too many requests",
)

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


def _classify_response(status: int, body: dict | None) -> str:
    """
    Classify the /api/tasks/complete response into a coarse outcome.

    Returns one of:
      "ok"           -> reward claimed
      "need-follow"  -> backend says we haven't followed the target on X yet
      "need-like"    -> backend says we haven't liked the tweet on X yet
      "already-done" -> task was already completed earlier
      "throttled"    -> verification still in progress / soft rate limit
      "error"        -> anything else, including network failures
    """
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("error") or "").lower()

    if 200 <= status < 300 and any(k in msg for k in _OK_KEYWORDS):
        return "ok"
    if any(k in msg for k in _NEED_FOLLOW_KEYWORDS):
        return "need-follow"
    if any(k in msg for k in _NEED_LIKE_KEYWORDS):
        return "need-like"
    if any(k in msg for k in _ALREADY_DONE_KEYWORDS):
        return "already-done"
    if status == 429 or any(k in msg for k in _THROTTLED_KEYWORDS):
        return "throttled"
    return "error"


def _build_pending_entry(task: dict, outcome: str) -> dict | None:
    """
    Build a pending_x.json entry from a task object the bot couldn't claim
    yet because it needs a real X action. Returns None if the task doesn't
    have enough info to act on.
    """
    if outcome not in ("need-follow", "need-like"):
        return None

    action = "follow" if outcome == "need-follow" else "like"
    # verificationTarget is the canonical pointer:
    #   - for follow: "@handle"
    #   - for like:   tweet ID (numeric string)
    # Fall back to parsing `url` if verificationTarget is missing.
    target = task.get("verificationTarget")
    url = task.get("url") or ""
    if not target and url:
        if action == "follow":
            # https://x.com/<handle>  ->  @<handle>
            tail = url.rstrip("/").split("/")[-1]
            target = f"@{tail}" if tail else None
        else:
            # https://x.com/<user>/status/<id>?s=20  ->  <id>
            parts = url.split("/status/")
            if len(parts) == 2:
                target = parts[1].split("?")[0].split("/")[0] or None

    if not target:
        return None

    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "action": action,
        "target": target,
        "url": url or None,
        "reward_sol": task.get("rewardSol"),
        "verification_type": task.get("verificationType"),
    }


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


def fetch_tasks(
    bearer: str, cookie: str, log: Callable[[str], None] | None = None
) -> list[dict]:
    """GET /api/tasks for one account, retrying on 429 / 5xx.

    Routed through `core.claimyshare_get` so the request shares the same
    Chrome-120 TLS fingerprint as monitor.py / withdraw.py.

    Raises RuntimeError if every retry is exhausted with a non-2xx status,
    or ValueError if the response is shaped unexpectedly. Network errors
    bubble up from `claimyshare_get` as plain Exceptions.
    """
    last_status = 0
    last_body = ""
    for attempt in range(1, FETCH_TASKS_MAX_RETRIES + 2):  # initial + retries
        resp = claimyshare_get(
            TASKS_LIST_URL,
            headers=_tasks_headers(bearer, cookie),
            timeout=HTTP_TIMEOUT_SEC,
        )
        status = resp.status_code
        last_status = status

        if status == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("tasks", "data", "result"):
                    v = data.get(key)
                    if isinstance(v, list):
                        return v
            raise ValueError(
                f"unexpected /api/tasks response shape: {type(data).__name__}"
            )

        # Non-2xx: capture body for the eventual error message.
        try:
            last_body = resp.text[:200]
        except Exception:  # noqa: BLE001 - response body fetch is best-effort
            last_body = ""

        # Decide whether this status is worth retrying. 429 (rate limit) and
        # 5xx (server) are transient; 4xx-other is usually fatal (auth, etc.).
        retryable = status == 429 or 500 <= status < 600
        if not retryable or attempt > FETCH_TASKS_MAX_RETRIES:
            break

        # Honor Retry-After if the server provides a small one; otherwise
        # use a short cap so we don't sit idle.
        retry_after_raw = ""
        try:
            retry_after_raw = resp.headers.get("retry-after", "") or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            retry_after = int(retry_after_raw) if retry_after_raw else FETCH_TASKS_RETRY_BASE_SEC
        except (TypeError, ValueError):
            retry_after = FETCH_TASKS_RETRY_BASE_SEC
        wait = min(max(retry_after, 1), FETCH_TASKS_RETRY_MAX_SEC)

        if log is not None:
            log(
                f"[fetch-tasks] status={status} (attempt {attempt}/"
                f"{FETCH_TASKS_MAX_RETRIES}); sleeping {wait}s then retrying."
            )
        time.sleep(wait)

    raise RuntimeError(
        f"GET /api/tasks failed after {FETCH_TASKS_MAX_RETRIES + 1} attempt(s): "
        f"status={last_status} body={last_body!r}"
    )


def complete_task(
    bearer: str, cookie: str, task_id: int
) -> tuple[int, dict | None]:
    """POST /api/tasks/complete with {taskId}. Returns (status, parsed_body).

    Uses `core.claimyshare_post` for TLS-impersonated traffic, falling back
    to plain `requests` if curl_cffi is unavailable.
    """
    body = {"taskId": task_id}
    try:
        resp = claimyshare_post(
            TASKS_COMPLETE_URL,
            headers=_tasks_headers(bearer, cookie),
            json=body,
            timeout=HTTP_TIMEOUT_SEC,
        )
    except Exception as e:  # noqa: BLE001 - requests/curl_cffi siblings
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
    """
    Walk one account's task list and POST complete on each eligible task.

    Returns a per-account result dict with counters and a `pending_x` list
    of follow/like targets the user still needs to perform on X (consumed
    later by Phase 2).
    """
    name = acc.get("name", "?")
    bearer = acc["bearer_token"]
    cookie = acc["cookie"]

    empty_result = {
        "name": name,
        "ok": 0,
        "need_follow": 0,
        "need_like": 0,
        "already_done": 0,
        "throttled": 0,
        "error": 0,
        "skipped_other": 0,
        "reward_sol": 0.0,
        "pending_x": [],
    }

    log(f"[{name}] fetching tasks list...")
    try:
        tasks = fetch_tasks(
            bearer, cookie, log=lambda msg: log(f"[{name}] {msg}")
        )
    except Exception as e:  # noqa: BLE001
        log(f"[{name}] [error] failed to fetch tasks: {e}")
        empty_result["error"] = 1
        return empty_result

    eligible = [t for t in tasks if _is_eligible(t)]
    other = len(tasks) - len(eligible)
    log(
        f"[{name}] {len(tasks)} task(s) total | "
        f"{len(eligible)} eligible (follow/like) | "
        f"{other} skipped (other types or already done)."
    )

    result = dict(empty_result)
    result["skipped_other"] = other

    if not eligible:
        return result

    for i, task in enumerate(eligible):
        tid = task.get("id")
        title = task.get("title", "?")
        expected = task.get("rewardSol", "?")

        # Only sleep between real POSTs; dry-run finishes instantly.
        if i > 0 and not dry_run:
            log(f"[{name}] sleeping {TASK_INTER_DELAY_SEC}s before next task...")
            time.sleep(TASK_INTER_DELAY_SEC)

        if dry_run:
            log(
                f"[{name}] [dry-run] would POST taskId={tid} ('{title}', "
                f"expected +{expected} SOL, verify={task.get('verificationType')})."
            )
            continue

        log(f"[{name}] posting taskId={tid} ('{title}')...")
        status, body = complete_task(bearer, cookie, tid)
        outcome = _classify_response(status, body)

        if outcome == "ok":
            result["ok"] += 1
            try:
                result["reward_sol"] += float(
                    (body or {}).get("rewardSol") or 0
                )
            except (TypeError, ValueError):
                pass
            log(f"[{name}] [ok] task {tid} '{title}' -> {_fmt_reward(body)}")

        elif outcome == "need-follow":
            result["need_follow"] += 1
            entry = _build_pending_entry(task, outcome)
            if entry:
                result["pending_x"].append(entry)
            target = (entry or {}).get("target", task.get("verificationTarget", "?"))
            log(f"[{name}] [need-follow] task {tid} '{title}' -> follow {target} on X")

        elif outcome == "need-like":
            result["need_like"] += 1
            entry = _build_pending_entry(task, outcome)
            if entry:
                result["pending_x"].append(entry)
            target = (entry or {}).get("target", task.get("verificationTarget", "?"))
            log(f"[{name}] [need-like] task {tid} '{title}' -> like tweet {target} on X")

        elif outcome == "already-done":
            result["already_done"] += 1
            log(f"[{name}] [already-done] task {tid} '{title}' (server says claimed before)")

        elif outcome == "throttled":
            result["throttled"] += 1
            log(
                f"[{name}] [throttled] task {tid} '{title}' "
                f"status={status} body={body} — re-run later."
            )

        else:  # "error"
            result["error"] += 1
            log(
                f"[{name}] [error] task {tid} '{title}' "
                f"status={status} body={body}"
            )

    return result


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


def _write_pending_x(results: list[dict], log) -> None:
    """
    Aggregate pending_x entries from every account result and write them to
    PENDING_X_PATH for Phase 2 (x_auto.py) to consume. Skips writing if no
    pending actions exist (also removes any stale file).
    """
    payload_accounts: dict[str, list] = {}
    for r in results:
        pending = r.get("pending_x") or []
        if pending:
            payload_accounts[r["name"]] = pending

    if not payload_accounts:
        # Nothing pending — clean up any stale file so Phase 2 doesn't act on
        # outdated targets.
        if PENDING_X_PATH.exists():
            try:
                PENDING_X_PATH.unlink()
                log(f"[pending-x] removed stale {PENDING_X_PATH.name} (nothing pending).")
            except OSError as e:
                log(f"[pending-x] could not remove stale {PENDING_X_PATH.name}: {e}")
        return

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "accounts": payload_accounts,
    }
    try:
        PENDING_X_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        total = sum(len(v) for v in payload_accounts.values())
        log(
            f"[pending-x] wrote {total} pending action(s) across "
            f"{len(payload_accounts)} account(s) to {PENDING_X_PATH.name}."
        )
    except OSError as e:
        log(f"[pending-x] FAILED to write {PENDING_X_PATH.name}: {e}")


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

    # ----- Per-account summary -----
    log("=== summary ===")
    for r in results:
        log(
            f"  {r['name']}: ok={r['ok']} "
            f"need-follow={r['need_follow']} need-like={r['need_like']} "
            f"already-done={r['already_done']} throttled={r['throttled']} "
            f"error={r['error']} other-skipped={r['skipped_other']} "
            f"reward=+{r['reward_sol']:.6f} SOL"
        )

    # ----- Aggregate totals -----
    total_ok = sum(r["ok"] for r in results)
    total_need_follow = sum(r["need_follow"] for r in results)
    total_need_like = sum(r["need_like"] for r in results)
    total_already = sum(r["already_done"] for r in results)
    total_throttled = sum(r["throttled"] for r in results)
    total_error = sum(r["error"] for r in results)
    total_reward = sum(r["reward_sol"] for r in results)

    log(
        f"TOTAL across {len(results)} account(s): "
        f"ok={total_ok} need-follow={total_need_follow} "
        f"need-like={total_need_like} already-done={total_already} "
        f"throttled={total_throttled} error={total_error} "
        f"reward=+{total_reward:.6f} SOL"
    )

    # ----- Print pending actions banner (human-friendly) -----
    if (total_need_follow + total_need_like) > 0:
        log("=== pending X actions (need real follow/like) ===")
        for r in results:
            for p in r.get("pending_x") or []:
                log(f"  {r['name']}: {p['action']} {p['target']} "
                    f"(task {p['task_id']}, +{p.get('reward_sol', 0)} SOL)")

    # ----- Write Phase-2 input file -----
    if not args.dry_run:
        _write_pending_x(results, log)

    return EXIT_OK if total_ok > 0 else EXIT_API_ERROR


if __name__ == "__main__":
    sys.exit(main())
