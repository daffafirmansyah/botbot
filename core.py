"""
Shared helpers for the claimyshare withdraw / monitor scripts.

Keeps all HTTP, RPC and state-persistence logic in one place so
`withdraw.py` (one-shot) and `monitor.py` (watch loop) stay thin.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

# ---------------------------------------------------------------------------
# TLS impersonation
# ---------------------------------------------------------------------------
# CloudFlare WAF fingerprints the TLS handshake of `requests` (OpenSSL +
# Python's default cipher order) and frequently slow-modes it. curl_cffi
# replays a real Chrome TLS handshake byte-for-byte, so the WAF treats us
# like a normal browser. We use it ONLY for claimyshare API calls; Solana
# public RPCs are unaffected and stay on plain `requests`.
#
# If curl_cffi is missing we transparently fall back to `requests` so the
# bot still works (just without the impersonation benefit).
try:
    from curl_cffi import requests as _cffi_requests  # type: ignore
    _TLS_IMPERSONATION_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep missing
    _cffi_requests = None  # type: ignore[assignment]
    _TLS_IMPERSONATION_AVAILABLE = False

# Browser profile to impersonate. Newer profile = more recent fingerprint.
# Keep this in sync with what curl_cffi advertises as a stable target.
# Override by setting CLAIMY_IMPERSONATE in the environment if needed.
IMPERSONATE_PROFILE = os.environ.get("CLAIMY_IMPERSONATE", "chrome120")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = "https://claimyshare.io/api/withdraw"
USER_API_URL = "https://claimyshare.io/api/user"

# Solana RPC endpoints, tried in order. First success wins; the last-known-good
# endpoint is remembered and preferred on subsequent calls, so a transient
# outage on the primary doesn't add latency to every poll afterwards.
#
# Only free, no-API-key endpoints that were verified live are listed here.
# Many older public endpoints (Ankr, drpc.org, onfinality, extrnode, rpcpool,
# helius) now require signup + key, rate-limit aggressively on the anonymous
# tier, or return 401/403 — do NOT re-add them without testing.
#
# To add a private endpoint with an API key (recommended if you run the bot
# 24/7), just append its URL to the list, e.g.:
#     "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY",
#     "https://solana-mainnet.g.alchemy.com/v2/YOUR_KEY",
SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",   # Solana Labs official (primary)
    "https://solana-rpc.publicnode.com",     # PublicNode (anycast) fallback
]
# Backward-compat alias: some code may still reference SOLANA_RPC.
SOLANA_RPC = SOLANA_RPCS[0]

HOT_WALLET = "8MrX8pJ6VkCsmMjrn4jTrp9DFACrytKVz6T23vDpqGgy"

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"

# Exit codes (used by withdraw.py; monitor.py uses them internally).
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_COOLDOWN = 2
EXIT_API_ERROR = 3
EXIT_NETWORK = 4

# Protocol / business constants observed from live traffic and on-chain.
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_REQS = 3
# Daily cooldown between successful withdraws — use slightly under 24h so we
# don't miss the earliest valid slot.
DAILY_COOLDOWN_SEC = 23 * 3600 + 55 * 60  # 23h55m

# Auto-withdraw mode: skip withdraws for accounts whose claimable balance
# (balanceSolTask from /api/user) is below this. Prevents burning rate-limit
# budget on dust or on accounts already drained in the current cycle.
MIN_WITHDRAW_SOL = 0.0005

# ----- Retry policy for transient failures -----
# AGGRESSIVE / SNIPE mode: retry 429 and 5xx as fast as possible. We
# explicitly IGNORE the server's Retry-After header (which says 30s) and
# retry every ~2 seconds instead, on the bet that:
#   - the rate-limit is a sliding window where some slots reopen sooner;
#   - or that server-side 5xx are transient and recover in seconds.
#
# !! WARNING: This is louder traffic and can trigger anti-abuse / IP ban.
# Increase the wait constants below if you start seeing IP-level blocks
# (everything returning 403 / connection refused) instead of 429.
#
# 24h daily cooldown (200-OK + "too many withdrawal" message) is STILL
# never retried — that's a server-enforced lock and retrying is futile.
MAX_RETRIES_RATE_LIMIT = math.inf      # infinite retries on 429
MAX_RETRIES_SERVER_ERROR = math.inf    # infinite retries on 5xx
RETRY_429_FALLBACK_SEC = 2             # used when server omits Retry-After
RETRY_429_MAX_WAIT_SEC = 2             # CAP on actual wait — overrides server's
                                       # Retry-After so we don't sit idle 30s
RETRY_429_COOLDOWN_THRESHOLD_SEC = 3600  # if server says Retry-After > 1h,
                                         # bail out (it's a real lock, not a
                                         # short rate-limit window)
SERVER_ERROR_BACKOFF_SEC = (2, 2, 2, 2)  # flat 2s wait, no escalation
RETRY_JITTER_SEC = 1                     # small jitter to desync parallel
                                         # workers when they all retry

Logger = Callable[[str], None]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(log_filename: str) -> Logger:
    """
    Return a thread-safe log(msg) function that prints + appends to
    SCRIPT_DIR/log_filename. Safe to call from multiple worker threads
    (parallel withdraw firing).
    """
    log_path = SCRIPT_DIR / log_filename
    lock = threading.RLock()

    def log(line: str) -> None:
        stamped = f"[{utc_now_iso()}] {line}"
        with lock:
            print(stamped, flush=True)
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(stamped + "\n")
            except OSError as e:
                print(f"[warn] could not write log file {log_path}: {e}", file=sys.stderr)

    return log


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# amount_sol is intentionally NOT here: it's optional (default "auto" => fetch
# balanceSolTask from /api/user at withdraw time). Validated separately in
# load_accounts().
REQUIRED_ACCOUNT_FIELDS = ("bearer_token", "cookie", "wallet_address")


def _normalize_amount_sol(raw, acc_name: str):
    """
    Accepts:
      - missing / None / "" / "auto" (any case) -> returns "auto" sentinel
      - positive int or float -> returns float
    Anything else aborts via sys.exit(EXIT_CONFIG).
    """
    if raw is None:
        return "auto"
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "auto"):
            return "auto"
        # Allow numeric string like "0.0034" for convenience (e.g. imported TSV).
        try:
            v = float(s)
        except ValueError:
            print(
                f"[error] account {acc_name!r}: amount_sol={raw!r} is not "
                f"a number or \"auto\".",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)
        if v <= 0:
            print(
                f"[error] account {acc_name!r}: amount_sol must be > 0 "
                f"(got {v}).",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)
        return v
    if isinstance(raw, (int, float)):
        if raw <= 0:
            # 0 / negative is treated as "auto" intent for convenience.
            return "auto"
        return float(raw)
    print(
        f"[error] account {acc_name!r}: amount_sol has unsupported type "
        f"{type(raw).__name__}.",
        file=sys.stderr,
    )
    sys.exit(EXIT_CONFIG)


def _read_config_file() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"[error] config.json not found at {CONFIG_PATH}.\n"
            "Copy config.example.json -> config.json and fill it in.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    try:
        # utf-8-sig tolerates a BOM if Notepad / PowerShell created the file.
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"[error] config.json is not valid JSON: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def load_accounts() -> list[dict]:
    """
    Return a list of account dicts.

    Supports two schemas in config.json:

    1) Multi-account (preferred):
         {"accounts": [ {"name": "acc1", "bearer_token": ..., ...}, ... ]}

    2) Legacy single-account (kept for backward compat):
         {"bearer_token": ..., "cookie": ..., "wallet_address": ..., "amount_sol": ...}
       This is wrapped as [{"name": "default", ...}].

    Each account MUST have all of REQUIRED_ACCOUNT_FIELDS. The "name" field
    is required in the multi-account schema (auto-assigned in legacy mode);
    names must be unique and are used as keys in state.json.
    """
    data = _read_config_file()

    # Legacy single-account.
    if "accounts" not in data:
        missing = [k for k in REQUIRED_ACCOUNT_FIELDS if not data.get(k)]
        if missing:
            print(
                f"[error] config.json missing fields: {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)
        acc = dict(data)
        acc["name"] = acc.get("name") or "default"
        acc["amount_sol"] = _normalize_amount_sol(acc.get("amount_sol"), acc["name"])
        return [acc]

    # Multi-account.
    raw_accounts = data.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        print(
            "[error] config.json 'accounts' must be a non-empty list.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    seen_names: set[str] = set()
    normalized: list[dict] = []
    for idx, acc in enumerate(raw_accounts):
        if not isinstance(acc, dict):
            print(f"[error] accounts[{idx}] is not an object.", file=sys.stderr)
            sys.exit(EXIT_CONFIG)
        name = acc.get("name") or f"acc{idx + 1}"
        if name in seen_names:
            print(
                f"[error] duplicate account name {name!r} in config.json.",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)
        seen_names.add(name)

        missing = [k for k in REQUIRED_ACCOUNT_FIELDS if not acc.get(k)]
        if missing:
            print(
                f"[error] account {name!r} missing fields: {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)

        cleaned = dict(acc)
        cleaned["name"] = name
        cleaned["amount_sol"] = _normalize_amount_sol(acc.get("amount_sol"), name)
        normalized.append(cleaned)

    return normalized


def load_config() -> dict:
    """Backward-compat wrapper: returns the first account. Deprecated."""
    return load_accounts()[0]


# ---------------------------------------------------------------------------
# Solana RPC helpers
# ---------------------------------------------------------------------------

# Index of the RPC endpoint that last succeeded. Used so that after a
# fallover we stick with the working endpoint instead of hitting the dead
# primary on every subsequent poll. Reset to 0 on process restart.
_last_good_rpc_idx = 0


def _rpc(method: str, params: list, timeout: int = 10) -> dict | None:
    """
    Call a Solana JSON-RPC method with automatic fallover across
    SOLANA_RPCS. Returns the parsed response on first success, or None
    if every endpoint fails / times out.

    Starts from the last-known-good endpoint to avoid wasting time on a
    primary that's currently down, then round-robins through the rest.
    """
    global _last_good_rpc_idx
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    n = len(SOLANA_RPCS)
    if n == 0:
        return None
    # Try last-known-good first, then the rest in order.
    order = [(_last_good_rpc_idx + i) % n for i in range(n)]
    for idx in order:
        url = SOLANA_RPCS[idx]
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            # Solana RPC returns HTTP 200 even for JSON-RPC errors, so
            # guard against {"error": ...} responses before declaring victory.
            if isinstance(data, dict) and "error" in data and "result" not in data:
                continue
            _last_good_rpc_idx = idx
            return data
        except Exception:
            continue
    return None


def get_balance_lamports(address: str) -> int | None:
    data = _rpc("getBalance", [address])
    if not data or "result" not in data:
        return None
    try:
        return int(data["result"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def bootstrap_last_success_iso(user_wallet: str, log: Logger) -> str | None:
    """
    Scan the user wallet's recent signatures for the most recent incoming
    transfer from HOT_WALLET. Returns an ISO UTC timestamp, or None if no
    such transfer is found.

    Used on first startup so the monitor knows the real daily cooldown
    window before it attempts anything.
    """
    sigs_data = _rpc("getSignaturesForAddress", [user_wallet, {"limit": 25}])
    if not sigs_data or not sigs_data.get("result"):
        log("[bootstrap] could not fetch signatures; assuming no prior success.")
        return None

    for entry in sigs_data["result"]:
        if entry.get("err"):
            continue
        sig = entry["signature"]
        tx_data = _rpc(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not tx_data or not tx_data.get("result"):
            continue
        tx = tx_data["result"]
        try:
            keys = tx["transaction"]["message"]["accountKeys"]
            signer_keys = [k for k in keys if k.get("signer")]
            if not signer_keys:
                continue
            signer = signer_keys[0]["pubkey"]
            if signer != HOT_WALLET:
                continue
            # Confirm destination is user_wallet.
            instructions = tx["transaction"]["message"]["instructions"]
            is_payout = any(
                ix.get("program") == "system"
                and ix.get("parsed", {}).get("type") == "transfer"
                and ix["parsed"]["info"].get("destination") == user_wallet
                for ix in instructions
            )
            if not is_payout:
                continue
            block_time = tx.get("blockTime")
            if not block_time:
                continue
            iso = datetime.fromtimestamp(block_time, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            log(f"[bootstrap] last payout from hot wallet at {iso} (sig {sig[:16]}...)")
            return iso
        except (KeyError, IndexError, TypeError):
            continue

    log("[bootstrap] no prior payout from hot wallet found in recent signatures.")
    return None


def iso_to_unix(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
#
# Schema (v2, multi-account):
#   {
#     "last_hot_balance_lamports": <int>,
#     "accounts": {
#       "<account_name>": {
#         "last_success_at": "<ISO UTC>" | null,
#         "last_attempt_ts": <unix seconds>
#       },
#       ...
#     }
#   }

DEFAULT_ACCOUNT_STATE: dict = {
    "last_success_at": None,
    "last_attempt_ts": 0,
}

DEFAULT_STATE: dict = {
    "last_hot_balance_lamports": 0,
    "accounts": {},
}


def _migrate_legacy_state(data: dict) -> dict:
    """Detect v1 single-account state and migrate under the 'default' key."""
    if "accounts" in data:
        return data
    migrated = {
        "last_hot_balance_lamports": int(data.get("last_hot_balance_lamports", 0)),
        "accounts": {
            "default": {
                "last_success_at": data.get("last_success_at"),
                "last_attempt_ts": float(data.get("last_attempt_ts", 0)),
            }
        },
    }
    return migrated


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_hot_balance_lamports": 0, "accounts": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data = _migrate_legacy_state(data)
        if "last_hot_balance_lamports" not in data:
            data["last_hot_balance_lamports"] = 0
        if "accounts" not in data or not isinstance(data["accounts"], dict):
            data["accounts"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return {"last_hot_balance_lamports": 0, "accounts": {}}


def get_account_state(state: dict, name: str) -> dict:
    """Return (and lazily create) the per-account state entry."""
    accounts = state.setdefault("accounts", {})
    entry = accounts.get(name)
    if entry is None:
        entry = dict(DEFAULT_ACCOUNT_STATE)
        accounts[name] = entry
    else:
        # Fill in any missing defaults (robust to older files).
        for k, v in DEFAULT_ACCOUNT_STATE.items():
            entry.setdefault(k, v)
    return entry


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as e:
        print(f"[warn] could not write state.json: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def build_headers(bearer: str, cookie: str) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {bearer}",
        "content-type": "application/json",
        "cookie": cookie,
        "origin": "https://claimyshare.io",
        "referer": "https://claimyshare.io/withdraw",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def is_cooldown_message(parsed: dict | None) -> bool:
    if not isinstance(parsed, dict):
        return False
    msg = str(parsed.get("message", "")).lower()
    return "too many withdrawal" in msg


# Headers that curl_cffi sets automatically as part of the impersonate
# profile. We strip them from caller-provided headers so the TLS handshake
# fingerprint and the HTTP-level identity stay in sync (a Chrome-120 TLS
# handshake paired with a Chrome-147 user-agent is a WAF red flag).
_HEADERS_OWNED_BY_IMPERSONATE = (
    "user-agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "accept-encoding",
)


def _strip_owned_headers(headers: dict) -> dict:
    """Return a copy of `headers` with curl_cffi-managed keys removed."""
    out = {}
    for k, v in headers.items():
        if k.lower() in _HEADERS_OWNED_BY_IMPERSONATE:
            continue
        out[k] = v
    return out


def claimyshare_get(url: str, *, headers: dict, timeout: int) -> Any:
    """GET against claimyshare with TLS impersonation when available.

    Returns a response object that quacks like requests.Response (has
    `.status_code`, `.headers`, `.json()`, `.text`). Use this for ANY
    claimyshare.io endpoint so the WAF dodge stays consistent across
    monitor.py / withdraw.py / tasks.py.
    """
    if _TLS_IMPERSONATION_AVAILABLE and _cffi_requests is not None:
        return _cffi_requests.get(
            url,
            headers=_strip_owned_headers(headers),
            timeout=timeout,
            impersonate=IMPERSONATE_PROFILE,
        )
    return requests.get(url, headers=headers, timeout=timeout)


def claimyshare_post(url: str, *, headers: dict, json: dict, timeout: int) -> Any:
    """POST against claimyshare with TLS impersonation when available."""
    if _TLS_IMPERSONATION_AVAILABLE and _cffi_requests is not None:
        return _cffi_requests.post(
            url,
            headers=_strip_owned_headers(headers),
            json=json,
            timeout=timeout,
            impersonate=IMPERSONATE_PROFILE,
        )
    return requests.post(url, headers=headers, json=json, timeout=timeout)


# Retry budget for fetch_claimable_balance. This call sits on the hot path
# of monitor.py / withdraw.py during a snipe so we keep the schedule SHORT
# (total ~17s worst case) — better to fail fast and let the outer caller
# retry the whole withdraw than to hold up a top-up window for 3 minutes.
# For non-snipe contexts (diagnose.py, manual scripts) the schedule is
# still long enough to clear a transient per-IP rate-limit hiccup.
BALANCE_FETCH_RETRY_WAITS_SEC = (2, 5, 10)
BALANCE_FETCH_MAX_RETRIES = len(BALANCE_FETCH_RETRY_WAITS_SEC)


def fetch_claimable_balance(cfg: dict, log: Logger) -> float | None:
    """
    GET /api/user with this account's credentials and return the
    `balanceSolTask` field as a float (SOL).

    Retries 429 / 5xx with a short progressive backoff so a single
    rate-limit hiccup doesn't spuriously skip a top-up. Returns None only
    after exhausting retries or on a permanent failure (network, non-2xx,
    non-JSON, missing field). Callers should treat None as "cannot
    proceed".
    """
    name = cfg.get("name", "?")
    headers = build_headers(cfg["bearer_token"], cfg["cookie"])
    # Balance fetch is a read from the app root, not the /withdraw page.
    headers["referer"] = "https://claimyshare.io/"

    last_status = 0
    for attempt in range(1, BALANCE_FETCH_MAX_RETRIES + 2):  # initial + retries
        try:
            resp = claimyshare_get(USER_API_URL, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001 - both requests & curl_cffi raise
            log(f"[{name}] [balance] network error: {e}")
            return None

        last_status = resp.status_code
        if resp.status_code == 200:
            break

        retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
        if not retryable or attempt > BALANCE_FETCH_MAX_RETRIES:
            log(f"[{name}] [balance] unexpected status {resp.status_code}.")
            return None

        wait = BALANCE_FETCH_RETRY_WAITS_SEC[attempt - 1]
        log(
            f"[{name}] [balance] status={resp.status_code} "
            f"(attempt {attempt}/{BALANCE_FETCH_MAX_RETRIES}); sleeping "
            f"{wait}s then retrying."
        )
        time.sleep(wait)
    else:
        # Loop fell through without `break` -> last attempt was non-200.
        log(f"[{name}] [balance] gave up after {BALANCE_FETCH_MAX_RETRIES + 1} "
            f"attempt(s); last status={last_status}.")
        return None

    try:
        parsed = resp.json()
    except ValueError:
        log(f"[{name}] [balance] non-JSON response.")
        return None

    if not isinstance(parsed, dict):
        log(f"[{name}] [balance] response body is not a JSON object.")
        return None

    val = parsed.get("balanceSolTask")
    if val is None:
        log(f"[{name}] [balance] 'balanceSolTask' missing in /api/user response.")
        return None

    try:
        return float(val)
    except (TypeError, ValueError):
        log(f"[{name}] [balance] 'balanceSolTask' not numeric: {val!r}.")
        return None


# ---------------------------------------------------------------------------
# Core withdraw attempt
# ---------------------------------------------------------------------------

def attempt_withdraw(
    cfg: dict,
    log: Logger,
    verify_onchain: bool = True,
) -> tuple[int, dict | None, int]:
    """
    Send exactly one POST to /api/withdraw for a single account config.

    Args:
        cfg: account dict with bearer_token, cookie, wallet_address.
             amount_sol is optional:
               - "auto" (or missing) -> GET /api/user, withdraw balanceSolTask.
               - positive number -> withdraw exactly that.
             Optional "name" field is used purely for log context.
        log: logger callable.
        verify_onchain: if True (default), sleep 30s after a 2xx success and
             confirm the balance delta on-chain. Set False when iterating
             multiple accounts on a single top-up event to stay snappy.

    Returns (exit_code, parsed_body_or_none, http_status_or_0).
    """
    wallet = cfg["wallet_address"]
    name = cfg.get("name", "?")

    # Resolve the amount: "auto" => fetch balanceSolTask from /api/user.
    raw_amount = cfg.get("amount_sol", "auto")
    if isinstance(raw_amount, str) and raw_amount.strip().lower() == "auto":
        balance = fetch_claimable_balance(cfg, log)
        if balance is None:
            log(f"[{name}] [error] could not fetch claimable balance; skipping.")
            return EXIT_API_ERROR, None, 0
        if balance < MIN_WITHDRAW_SOL:
            log(
                f"[{name}] [skip] claimable balance {balance:.9f} SOL "
                f"below threshold {MIN_WITHDRAW_SOL} SOL; nothing to withdraw."
            )
            return EXIT_COOLDOWN, None, 0
        amount = balance
        log(f"[{name}] [auto] claimable balance = {amount:.9f} SOL; withdrawing that.")
    else:
        amount = float(raw_amount)

    pre: int | None = None
    if verify_onchain:
        pre = get_balance_lamports(wallet)
        if pre is not None:
            log(f"[{name}] pre-balance on-chain: {pre / 1e9:.9f} SOL")

    headers = build_headers(cfg["bearer_token"], cfg["cookie"])
    body = {"amountSol": amount, "walletAddress": wallet}

    # ----- Retry loop -----
    # 429 (per-JWT rate-limit) and 5xx (server unavailable) are transient and
    # retryable. A 200-OK with "too many withdrawal" message is the 24h daily
    # cooldown and is NOT retried.
    rate_limit_retries_left = MAX_RETRIES_RATE_LIMIT
    server_error_retries_left = MAX_RETRIES_SERVER_ERROR
    attempt_num = 0

    while True:
        attempt_num += 1

        try:
            resp = claimyshare_post(
                API_URL, headers=headers, json=body, timeout=30
            )
        except Exception as e:  # noqa: BLE001 - requests/curl_cffi siblings
            log(f"[{name}] [error] network error during POST: {e}")
            return EXIT_NETWORK, None, 0

        status = resp.status_code
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None

        log(
            f"[{name}] response status={status} "
            f"body={parsed if parsed is not None else resp.text!r}"
        )

        # ----- 429: per-JWT rate-limit, retry after Retry-After -----
        if status == 429:
            retry_after_raw = resp.headers.get("retry-after", "")
            try:
                retry_after = int(retry_after_raw) if retry_after_raw else RETRY_429_FALLBACK_SEC
            except ValueError:
                retry_after = RETRY_429_FALLBACK_SEC

            # Long Retry-After (server explicitly says "wait hours") => not a
            # short rate-limit, treat as cooldown and bail.
            if retry_after > RETRY_429_COOLDOWN_THRESHOLD_SEC:
                log(
                    f"[{name}] [cooldown] 429 retry-after={retry_after}s "
                    f"exceeds {RETRY_429_COOLDOWN_THRESHOLD_SEC}s; "
                    f"treating as cooldown."
                )
                return EXIT_COOLDOWN, parsed, status

            # Aggressive mode: cap the wait at RETRY_429_MAX_WAIT_SEC so we
            # don't sit idle for the full 30s the server politely asks for.
            effective_wait = min(retry_after, RETRY_429_MAX_WAIT_SEC)

            if rate_limit_retries_left > 0:
                wait = effective_wait + random.uniform(0, RETRY_JITTER_SEC)
                rate_limit_retries_left -= 1
                left_str = (
                    "unlimited"
                    if rate_limit_retries_left == math.inf
                    else f"{int(rate_limit_retries_left)}"
                )
                log(
                    f"[{name}] [retry] 429 rate-limit; sleeping {wait:.1f}s "
                    f"then retry (attempt {attempt_num + 1}, "
                    f"{left_str} retries left)."
                )
                time.sleep(wait)
                continue

            log(f"[{name}] [cooldown] 429 retries exhausted; giving up.")
            return EXIT_COOLDOWN, parsed, status

        # ----- 200-ish + cooldown message: 24h daily cooldown, NO retry -----
        if is_cooldown_message(parsed):
            log(f"[{name}] [cooldown] daily cooldown message detected; not retrying.")
            return EXIT_COOLDOWN, parsed, status

        # ----- 5xx server unavailable: retry with escalating backoff -----
        if 500 <= status < 600:
            if server_error_retries_left > 0:
                # `attempt_num - 1` is how many 5xx-driven retries we've already
                # done (clamped to backoff array length so it plateaus).
                idx = min(attempt_num - 1, len(SERVER_ERROR_BACKOFF_SEC) - 1)
                base = SERVER_ERROR_BACKOFF_SEC[idx]
                wait = base + random.uniform(0, RETRY_JITTER_SEC)
                server_error_retries_left -= 1
                left_str = (
                    "unlimited"
                    if server_error_retries_left == math.inf
                    else f"{int(server_error_retries_left)}"
                )
                log(
                    f"[{name}] [retry] {status} server error; sleeping "
                    f"{wait:.1f}s then retry (attempt {attempt_num + 1}, "
                    f"{left_str} retries left)."
                )
                time.sleep(wait)
                continue

            log(f"[{name}] [error] {status} server error; retries exhausted.")
            return EXIT_API_ERROR, parsed, status

        # ----- 2xx success path -----
        if 200 <= status < 300 and isinstance(parsed, dict):
            if parsed.get("success", True):
                if verify_onchain:
                    log(f"[{name}] [ok] API success. verifying on-chain in 30s...")
                    time.sleep(30)
                    post = get_balance_lamports(wallet)
                    if post is not None and pre is not None:
                        delta = (post - pre) / 1e9
                        log(
                            f"[{name}] post-balance: {post / 1e9:.9f} SOL | "
                            f"delta: {delta:+.9f} SOL"
                        )
                        if delta > 0:
                            log(f"[{name}] [ok] on-chain delta confirms withdraw landed.")
                        else:
                            log(
                                f"[{name}] [warn] API success but on-chain delta "
                                "is zero. Withdraw may still be queued."
                            )
                else:
                    log(f"[{name}] [ok] API success (on-chain verify skipped).")
                return EXIT_OK, parsed, status

        log(f"[{name}] [error] unexpected response (treated as failure).")
        return EXIT_API_ERROR, parsed, status
