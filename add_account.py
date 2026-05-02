"""
Helper to add accounts to config.json without hand-editing JSON.

Two modes:

  Interactive (default):
      python add_account.py
      -> prompts for name, bearer, cookie, wallet, amount per account,
         loops until you stop.

  Bulk import from TSV:
      python add_account.py --bulk accounts.tsv
      -> reads a tab-separated file and imports every row.

  List existing accounts (without leaking bearer / cookie):
      python add_account.py --list

Bulk file format (header row required, any column order):

    name<TAB>bearer_token<TAB>cookie<TAB>wallet_address<TAB>amount_sol
    acc1<TAB>eyJhbG...<TAB>GAESA=...<TAB>24Kgco...<TAB>0.0033999998
    acc2<TAB>eyJhbG...<TAB>GAESA=...<TAB>24Kgco...<TAB>0.0033999998

Notes:
  * Existing config.json (legacy single-account or multi-account) is
    preserved and migrated; new entries are appended.
  * Duplicate names are rejected — pick unique names per account.
  * Bearer pasted with "Bearer " prefix is auto-stripped.
  * Wallet and amount default to the last-added values to speed up bulk
    interactive entry where most accounts share the same wallet.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

REQUIRED_FIELDS = ("name", "bearer_token", "cookie", "wallet_address", "amount_sol")


# ---------------------------------------------------------------------------
# config.json read / write
# ---------------------------------------------------------------------------

def load_config_data() -> dict:
    """Return the raw config dict, normalized to {'accounts': [...]}."""
    if not CONFIG_PATH.exists():
        return {"accounts": []}

    try:
        # utf-8-sig tolerates a BOM if Notepad / PowerShell created the file.
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        sys.exit(f"[error] existing config.json is invalid JSON: {e}")

    if "accounts" in data:
        if not isinstance(data["accounts"], list):
            sys.exit("[error] existing config.json 'accounts' is not a list.")
        return data

    # Legacy single-account schema -> wrap.
    if any(k in data for k in ("bearer_token", "cookie", "wallet_address", "amount_sol")):
        legacy = dict(data)
        legacy["name"] = legacy.get("name") or "default"
        return {"accounts": [legacy]}

    return {"accounts": []}


def save_config_data(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def existing_names(data: dict) -> set[str]:
    return {a.get("name") for a in data.get("accounts", []) if a.get("name")}


def normalize_bearer(raw: str) -> str:
    s = raw.strip()
    # Tolerate "Bearer xxx" or "bearer: xxx" pastes.
    for prefix in ("Bearer ", "bearer ", "BEARER "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    if s.startswith("Bearer:") or s.startswith("bearer:"):
        s = s.split(":", 1)[1].strip()
    return s


def parse_amount(raw: str) -> float:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty amount")
    return float(raw)


def validate_entry(entry: dict, taken_names: set[str]) -> Optional[str]:
    """Return error message if invalid, None if OK."""
    for f in REQUIRED_FIELDS:
        v = entry.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            return f"missing field {f!r}"
    if entry["name"] in taken_names:
        return f"duplicate name {entry['name']!r}"
    if not isinstance(entry["amount_sol"], (int, float)) or entry["amount_sol"] <= 0:
        return f"invalid amount_sol: {entry['amount_sol']!r}"
    return None


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------

def _prompt(label: str, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"  {label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw or not required:
            return raw
        print("    (required)")


def _suggest_next_name(taken: set[str]) -> str:
    i = 1
    while f"acc{i}" in taken:
        i += 1
    return f"acc{i}"


def interactive_loop(data: dict) -> int:
    print(f"\nclaimyshare-withdraw add-account (interactive)")
    print(f"Existing accounts: {len(data['accounts'])}")
    print("Press Ctrl+C any time to stop. config.json is saved after every add.\n")

    last_wallet: str | None = None
    last_amount: str | None = None
    if data["accounts"]:
        last = data["accounts"][-1]
        last_wallet = last.get("wallet_address")
        last_amount = str(last.get("amount_sol")) if last.get("amount_sol") is not None else None

    added = 0
    try:
        while True:
            taken = existing_names(data)
            print(f"--- new account #{len(data['accounts']) + 1} ---")
            name = _prompt("name", default=_suggest_next_name(taken))
            if name in taken:
                print(f"  ! name {name!r} already in use, try another.")
                continue
            bearer = normalize_bearer(_prompt("bearer (paste JWT, no 'Bearer ' prefix needed)"))
            cookie = _prompt("cookie (paste full value e.g. GAESA=...)")
            wallet = _prompt("wallet_address (Solana)", default=last_wallet)
            amount_str = _prompt("amount_sol", default=last_amount or "0.0033999998")

            try:
                amount = parse_amount(amount_str)
            except ValueError as e:
                print(f"  ! invalid amount: {e}; try again.")
                continue

            entry = {
                "name": name,
                "bearer_token": bearer,
                "cookie": cookie,
                "wallet_address": wallet,
                "amount_sol": amount,
            }
            err = validate_entry(entry, taken)
            if err:
                print(f"  ! rejected: {err}; try again.")
                continue

            data["accounts"].append(entry)
            save_config_data(data)
            added += 1
            last_wallet = wallet
            last_amount = amount_str
            print(f"  + saved. total accounts: {len(data['accounts'])}\n")

            cont = input("Add another? [Y/n]: ").strip().lower()
            if cont in ("n", "no"):
                break
    except (KeyboardInterrupt, EOFError):
        print("\n[interrupted]")

    print(f"\nDone. {added} new account(s) added. Total: {len(data['accounts'])}.")
    return 0


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def _shorten_wallet(w: str) -> str:
    return f"{w[:8]}...{w[-4:]}" if len(w) > 16 else w


def _bearer_fingerprint(b: str) -> str:
    """Show last 6 chars + total length so you can tell tokens apart
    when rotating, without printing the actual JWT."""
    if not b:
        return "(empty)"
    return f"...{b[-6:]} ({len(b)}c)"


def list_accounts(data: dict) -> int:
    accounts = data.get("accounts", [])
    if not accounts:
        print("No accounts in config.json yet.")
        print(f"  config path: {CONFIG_PATH}")
        return 0

    print(f"\nTotal accounts: {len(accounts)}  (config: {CONFIG_PATH})\n")
    header = f"  {'#':>3}  {'name':<12}  {'wallet':<22}  {'amount':<13}  {'bearer':<18}  {'cookie':<6}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, a in enumerate(accounts, 1):
        name = a.get("name", "?")
        wallet = _shorten_wallet(a.get("wallet_address", ""))
        amount = a.get("amount_sol", "?")
        bearer = _bearer_fingerprint(a.get("bearer_token", ""))
        cookie = "set" if a.get("cookie") else "MISSING"
        print(
            f"  {i:>3}  {name:<12}  {wallet:<22}  {amount!s:<13}  {bearer:<18}  {cookie:<6}"
        )
    print()

    # Quick health summary.
    missing_bearer = sum(1 for a in accounts if not a.get("bearer_token"))
    missing_cookie = sum(1 for a in accounts if not a.get("cookie"))
    if missing_bearer or missing_cookie:
        print(
            f"  WARN: {missing_bearer} account(s) missing bearer, "
            f"{missing_cookie} missing cookie."
        )
    return 0


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------

def bulk_import(data: dict, tsv_path: Path) -> int:
    if not tsv_path.exists():
        sys.exit(f"[error] bulk file not found: {tsv_path}")

    # utf-8-sig auto-strips a BOM if PowerShell / Notepad / Excel added one.
    text = tsv_path.read_text(encoding="utf-8-sig")
    # Auto-detect delimiter: tab > comma > semicolon.
    if "\t" in text:
        delimiter = "\t"
    elif ";" in text:
        delimiter = ";"
    else:
        delimiter = ","

    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if reader.fieldnames is None:
        sys.exit("[error] bulk file appears empty or has no header row.")
    header_set = {h.strip() for h in reader.fieldnames}
    missing = [f for f in REQUIRED_FIELDS if f not in header_set]
    if missing:
        sys.exit(
            f"[error] bulk file missing required header(s): {', '.join(missing)}.\n"
            f"  Header found: {', '.join(reader.fieldnames)}"
        )

    added = 0
    skipped = 0
    for row_num, row in enumerate(reader, start=2):  # row_num counts data rows after header
        taken = existing_names(data)
        try:
            entry = {
                "name": row["name"].strip(),
                "bearer_token": normalize_bearer(row["bearer_token"]),
                "cookie": row["cookie"].strip(),
                "wallet_address": row["wallet_address"].strip(),
                "amount_sol": parse_amount(row["amount_sol"]),
            }
        except (KeyError, ValueError) as e:
            print(f"  ! row {row_num}: {e}; skipped.")
            skipped += 1
            continue

        err = validate_entry(entry, taken)
        if err:
            print(f"  ! row {row_num} ({entry.get('name', '?')}): {err}; skipped.")
            skipped += 1
            continue

        data["accounts"].append(entry)
        added += 1
        if added % 10 == 0:
            save_config_data(data)
            print(f"  ... {added} added so far (saved).")

    save_config_data(data)
    print(f"\nDone. {added} added, {skipped} skipped. Total accounts: {len(data['accounts'])}.")
    return 0 if added > 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add account(s) to config.json (interactive or bulk TSV)."
    )
    ap.add_argument(
        "--bulk",
        type=Path,
        help="Path to a TSV/CSV file with header row (name, bearer_token, cookie, wallet_address, amount_sol).",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="List existing accounts in config.json without printing bearer / cookie values.",
    )
    ns = ap.parse_args()

    data = load_config_data()
    if ns.list:
        return list_accounts(data)
    if ns.bulk:
        return bulk_import(data, ns.bulk)
    return interactive_loop(data)


if __name__ == "__main__":
    sys.exit(main())
