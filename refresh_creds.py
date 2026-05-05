"""
refresh_creds.py - Sync credentials from accounts.tsv -> config.json
                   for accounts that ALREADY exist (in-place update).

Why this exists:
  add_account.py --bulk only APPENDS; it rejects duplicate names. When you
  refresh expired bearer / cookie values for accounts that are already in
  config.json, you need this in-place updater instead.

Behavior:
  - Reads accounts.tsv (same format as `add_account.py --bulk`).
  - For each row whose `name` already exists in config.json, updates
    bearer_token, cookie, wallet_address, and amount_sol IN PLACE.
  - Rows whose name is NOT in config.json are SKIPPED with a warning
    (use add_account.py for new accounts).
  - Writes a timestamped backup of config.json before saving.

Usage:
  python refresh_creds.py                    # apply changes
  python refresh_creds.py --dry-run          # preview only, no write
  python refresh_creds.py --only adella,bolvi  # restrict to specific names
  python refresh_creds.py --tsv path/to/file.tsv   # custom TSV path
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
DEFAULT_TSV_PATH = SCRIPT_DIR / "accounts.tsv"

REQUIRED_FIELDS = ("name", "bearer_token", "cookie", "wallet_address", "amount_sol")
UPDATABLE_FIELDS = ("bearer_token", "cookie", "wallet_address", "amount_sol")


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"[error] config.json not found at {CONFIG_PATH}")
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[error] config.json is not valid JSON: {e}")


def _backup_config() -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = CONFIG_PATH.with_suffix(f".backup.{ts}.json")
    shutil.copy2(CONFIG_PATH, backup)
    return backup


def _save_config(data: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _read_tsv(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"[error] TSV not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    delimiter = "\t" if "\t" in text else ("," if "," in text else ";")
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if reader.fieldnames is None:
        sys.exit(f"[error] {path}: empty or no header.")
    missing = [f for f in REQUIRED_FIELDS if f not in {h.strip() for h in reader.fieldnames}]
    if missing:
        sys.exit(f"[error] {path}: missing header(s) {missing}")
    rows: list[dict] = []
    for i, row in enumerate(reader, start=2):
        if any(row.get(f) is None for f in REQUIRED_FIELDS):
            print(f"  ! tsv row {i}: malformed row (missing column), skipped.")
            continue
        rows.append({f: (row[f] or "").strip() for f in REQUIRED_FIELDS})
    return rows


# ---------------------------------------------------------------------------
# Diff & update
# ---------------------------------------------------------------------------

def _normalize_amount(raw: str):
    """Mirror core._normalize_amount_sol minus the error-printing path."""
    s = (raw or "").strip()
    if s == "" or s.lower() == "auto":
        return "auto"
    try:
        v = float(s)
    except ValueError:
        return raw  # leave as-is so caller can flag
    return v if v > 0 else "auto"


def _short(v: str, n: int = 8) -> str:
    s = str(v or "")
    if len(s) <= n * 2 + 3:
        return s
    return f"{s[:n]}..{s[-n:]}"


def diff_account(existing: dict, fresh: dict) -> dict:
    """Return {field: (old, new)} for fields that actually changed."""
    changes: dict[str, tuple] = {}
    for f in UPDATABLE_FIELDS:
        old = existing.get(f, "")
        new_raw = fresh.get(f, "")
        if f == "amount_sol":
            new = _normalize_amount(new_raw)
        else:
            new = (new_raw or "").strip()
        if str(old) != str(new):
            changes[f] = (old, new)
    return changes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tsv", type=Path, default=DEFAULT_TSV_PATH,
                   help="Path to TSV (default: accounts.tsv).")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes without writing.")
    p.add_argument("--only", default="",
                   help="Comma-separated account names to restrict to.")
    args = p.parse_args()

    only_set = {n.strip() for n in args.only.split(",") if n.strip()} if args.only else None

    data = _load_config()
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        sys.exit("[error] config.json has no 'accounts' list (legacy single-account schema?).")

    by_name = {a.get("name"): a for a in accounts if isinstance(a, dict)}
    rows = _read_tsv(args.tsv)

    print(f"# config.json: {len(by_name)} account(s)")
    print(f"# tsv:          {len(rows)} row(s)")
    if only_set:
        print(f"# filtering to: {sorted(only_set)}")
    print()

    updated_names: list[str] = []
    unchanged_names: list[str] = []
    missing_in_config: list[str] = []
    skipped_filter = 0

    for row in rows:
        name = row["name"]
        if only_set and name not in only_set:
            skipped_filter += 1
            continue
        target = by_name.get(name)
        if target is None:
            missing_in_config.append(name)
            continue
        changes = diff_account(target, row)
        if not changes:
            unchanged_names.append(name)
            continue

        # Print diff per account.
        print(f"== {name} ==")
        for field, (old, new) in changes.items():
            if field == "amount_sol":
                print(f"   {field}: {old!r} -> {new!r}")
            else:
                print(f"   {field}: {_short(old)} -> {_short(new)}")
        if not args.dry_run:
            for field, (_old, new) in changes.items():
                target[field] = new
            updated_names.append(name)

    # ---- Summary ----
    print()
    print("=" * 60)
    print(f"updated:           {len(updated_names)}")
    if updated_names:
        print(f"  {', '.join(updated_names)}")
    print(f"unchanged:         {len(unchanged_names)}")
    print(f"missing in config: {len(missing_in_config)}")
    if missing_in_config:
        print(f"  {', '.join(missing_in_config)}")
        print("  (use `python add_account.py --bulk accounts.tsv` for new accounts)")
    if only_set:
        print(f"skipped by --only: {skipped_filter}")

    if args.dry_run:
        print()
        print("DRY RUN -- no changes written. Re-run without --dry-run to apply.")
        return 0

    if updated_names:
        backup = _backup_config()
        print(f"\nbackup -> {backup.name}")
        _save_config(data)
        print(f"saved  -> {CONFIG_PATH.name}")
    else:
        print("\nNothing to write.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
