"""
Shared helpers for the claimyshare withdraw / monitor scripts.

Keeps all HTTP, RPC and state-persistence logic in one place so
`withdraw.py` (one-shot) and `monitor.py` (watch loop) stay thin.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = "https://claimyshare.io/api/withdraw"
SOLANA_RPC = "https://solana-rpc.publicnode.com"
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

Logger = Callable[[str], None]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_logger(log_filename: str) -> Logger:
    """Return a log(msg) function that prints + appends to SCRIPT_DIR/log_filename."""
    log_path = SCRIPT_DIR / log_filename

    def log(line: str) -> None:
        stamped = f"[{utc_now_iso()}] {line}"
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

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            f"[error] config.json not found at {CONFIG_PATH}.\n"
            "Copy config.example.json -> config.json and fill it in.",
            file=sys.stderr,
        )
        sys.exit(EXIT_CONFIG)

    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[error] config.json is not valid JSON: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)

    required = ("bearer_token", "cookie", "wallet_address", "amount_sol")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        print(f"[error] config.json missing fields: {', '.join(missing)}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    return cfg


# ---------------------------------------------------------------------------
# Solana RPC helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, timeout: int = 15) -> dict | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(SOLANA_RPC, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
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

DEFAULT_STATE: dict = {
    "last_hot_balance_lamports": 0,
    "last_success_at": None,   # ISO UTC or None
    "last_attempt_ts": 0,      # unix seconds
}


def load_state() -> dict:
    if not STATE_PATH.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        # Fill in any missing keys with defaults.
        merged = dict(DEFAULT_STATE)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
        return merged
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)


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


# ---------------------------------------------------------------------------
# Core withdraw attempt
# ---------------------------------------------------------------------------

def attempt_withdraw(cfg: dict, log: Logger) -> tuple[int, dict | None, int]:
    """
    Send exactly one POST to /api/withdraw.

    Returns (exit_code, parsed_body_or_none, http_status_or_0).

    Exit codes follow the same convention as withdraw.py:
      EXIT_OK, EXIT_COOLDOWN, EXIT_API_ERROR, EXIT_NETWORK.
    """
    wallet = cfg["wallet_address"]
    amount = float(cfg["amount_sol"])

    pre = get_balance_lamports(wallet)
    if pre is not None:
        log(f"pre-balance on-chain (user): {pre / 1e9:.9f} SOL")

    headers = build_headers(cfg["bearer_token"], cfg["cookie"])
    body = {"amountSol": amount, "walletAddress": wallet}

    try:
        resp = requests.post(API_URL, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        log(f"[error] network error during POST: {e}")
        return EXIT_NETWORK, None, 0

    status = resp.status_code
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None

    log(f"response status={status} body={parsed if parsed is not None else resp.text!r}")

    if status == 429:
        retry_after = resp.headers.get("retry-after")
        log(f"[cooldown] 429 (retry-after={retry_after}s).")
        return EXIT_COOLDOWN, parsed, status

    if is_cooldown_message(parsed):
        log("[cooldown] cooldown message detected.")
        return EXIT_COOLDOWN, parsed, status

    if 200 <= status < 300 and isinstance(parsed, dict):
        if parsed.get("success", True):
            log("[ok] API returned success. verifying on-chain in 30s...")
            time.sleep(30)
            post = get_balance_lamports(wallet)
            if post is not None and pre is not None:
                delta = (post - pre) / 1e9
                log(
                    f"post-balance: {post / 1e9:.9f} SOL | delta: {delta:+.9f} SOL"
                )
                if delta > 0:
                    log("[ok] on-chain delta confirms withdraw landed.")
                else:
                    log(
                        "[warn] API success but on-chain delta is zero. "
                        "Withdraw may still be queued; re-check later."
                    )
            return EXIT_OK, parsed, status

    log("[error] unexpected response (treated as failure).")
    return EXIT_API_ERROR, parsed, status
