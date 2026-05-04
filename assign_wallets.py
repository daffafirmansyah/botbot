#!/usr/bin/env python3
"""
One-shot helper to (re)assign withdraw `wallet_address` for every account in
both config.json (runtime) AND accounts.tsv (bulk-import source-of-truth)
from a fixed wallet pool. Updating both keeps the two files in sync so a
later `add_account.py --bulk accounts.tsv` won't revert to old wallets.

Rules:
  - Each "normal" wallet (POOL) is shared by AT MOST 2 accounts.
  - The DAFFA14_WALLET is reserved for the single account whose name is
    exactly "daffa14" (case-insensitive). That account gets that wallet and
    NO other account is allowed to use it.
  - Existing wallet_address values are OVERWRITTEN.

Safety:
  - Dry-run by default. Prints the full assignment plan.
  - Refuses to run if there are duplicate wallets in the pool (sanity).
  - Refuses to run if there are more "normal" accounts than capacity
    (33 wallets * 2 = 66) unless --truncate is passed.
  - Writes timestamped backups of config.json AND accounts.tsv before
    modifying.
  - Pass --apply to actually write changes.
  - Pass --no-tsv to skip the accounts.tsv update.
  - Pass --prune-empty-creds to also drop accounts.tsv rows whose
    bearer_token OR cookie field is blank (rows never used in production).

Usage on VPS:
  cd ~/botbot
  source ~/venv/bin/activate
  python assign_wallets.py            # dry-run, shows plan
  python assign_wallets.py --apply    # commits changes (after backup)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Reuse the same broken-line merger that add_account.py uses, so we read
# accounts.tsv exactly the way bulk import does.
try:
    from add_account import _merge_broken_rows
except ImportError as exc:  # pragma: no cover - defensive
    sys.exit(f"[FATAL] cannot import add_account._merge_broken_rows: {exc}")

# ---------------------------------------------------------------------------
# Wallet pool (DO NOT EDIT casually — this is the source of truth)
# ---------------------------------------------------------------------------

POOL: list[str] = [
    "5SL7WhS9X6UKmxEp54gH4DAnr7yRv1ByGTTwXwSnS1zN",  # 1
    "6djtYferEYsAYFLjbLvDYeHqJQ88YmPEWpdDbTd1Az8a",  # 2
    "G6EmJfXc2b5mrYEFvW6YxVAWZM1QQGHNcPk9mem7RYv",   # 3
    "5QuFwZTpmj7sTV5Bb5ACizocWb725TTTmQrHYYt8orCr",  # 4
    "GETws4gcaZ7P3vYQP7jAQFf7Apkr1yRb14NrVMr67bMS",  # 5
    "EPkso2bBUGZDekWZTBHvc5srR7FHA8X6MELNgdHiMkif",  # 6
    "645eTm22jmAbj5kT8n3z44kKePkVqowLb1EPUY3kpKB",   # 7
    "5deJ8q8qSaZyEhDLEr1ntWJnmuxJJ1XkdRQmN2B9Bwnp",  # 8
    "9By2yaE99kQpwUP3xJ7PkyNnNGSvmvCF4pAqfoSAzB2p",  # 9
    "4C2pYCLUavu8NDXLSJziQpDy4DKJFnUySmdZTbpRGDJV",  # 10
    "EDp9rF6ZXWhn5hf9kEVcH9xZJLoDttes39XXAkv5wks1",  # 11
    "EDQCUMMutdvjxLZBYREn9yHoasWjpNgnaV7u6KFnLraH",  # 12
    "AduRXx4NnaXz4w2wZUgxZzdyZoyBhKiuoYDRDdqi9fez",  # 13
    "9kLn9LXM3f8rhh2JV4EH3fHe3YMji5NN6THKFKKm5qnp",  # 14
    "BBWeUVMidH41bMH1gNpLS1SrADDcKJvs9ufua3Mr4PNq",  # 15
    "8Ek3y15mJFUCqdicyFzNjMH2BwoUGsBXN4dVHaqWKLup",  # 16
    "GdK3xB3DHkpkmJAYT7GkLqSQNxaow4ZaZLrb9T1QfUG9",  # 17
    "6AaE9XSpcizMh2dp1QUzyHNQjW1BwrHYajXrENAPDUqN",  # 18
    "8WHdtfWPRrMr1WdRBedXZY81AgBCrDd1FzGDuGFH4pYK",  # 19
    "4zG1KXjFNNHzbhdzAJgGEoJZRw577xsuvBtrsjZYeq3M",  # 20
    "ZYKopX2umkyWj5gqicdUuHx933JUMAC5EGQaSnuNxJW",   # 21
    "Ep1HDWrhhHtkG5wXr5GAU36fbQsELtwSDhqdiMd8wPtb",  # 22
    "Bda94gpReTmvZW8QwsuCmU5Z46cFPn3y1UorcvhuepzS",  # 23
    "HTqUwt5oVUZ8zpB7HpvUoVWaXTxM8wfaQ3r8zq1cJedt",  # 24
    "4X4Ey3ggwz8DpXwQehkmowETGm5BZXJRB4KbWf8i5D18",  # 25
    "DCKKYx2MB3jTHSib7mPe5yMcNoVoS5CSwVmXDygpGLde",  # 26
    "DZBxAr6Nmhxs5LxUna2qEswckbhcdf4LfdM9tUwwQFi1",  # 27
    "HzU7VKxuFhqNJ2msBVjAw7ASZRKKo2kCbvdZRnTrX5Co",  # 28
    "FZqCVrTJixXk5kxSVjJGBcPHo3v2YYxdQcL4rHrBXsm",   # 29
    "89GTihNSKyfPeBwXfhnQh9mVnhFkE8AR3MtTRJDcPzhw",  # 30
    "93LNxeXJmVBXdU2SnNHUMS9j7gHJm8NHhT9kPc7GLP8C",  # 31
    "E1xQv1j4QSTMDnUWccs7WenAsRSLVyAQRm38kXCT4YVi",  # 32
    "3gCvbgQi46XJ98gQCh22euehPFKjksTAPDT3kUWxNCSM",  # 33
]

DAFFA14_WALLET = "678rCoX51RMCJZq9i46zKP7LovPyb6AMw2J5W4rDKmnw"
DAFFA14_NAME_LC = "daffa14"          # case-insensitive exact-match account name
ACCOUNTS_PER_WALLET = 2

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
ACCOUNTS_TSV_PATH = SCRIPT_DIR / "accounts.tsv"
TSV_REQUIRED_FIELDS = ("name", "bearer_token", "cookie", "wallet_address", "amount_sol")


# ---------------------------------------------------------------------------

def short(addr: str) -> str:
    return f"{addr[:4]}..{addr[-4:]}" if len(addr) > 10 else addr


def validate_pool() -> None:
    seen: set[str] = set()
    for w in POOL:
        if w in seen:
            sys.exit(f"[FATAL] duplicate wallet in POOL: {w}")
        seen.add(w)
    if DAFFA14_WALLET in seen:
        sys.exit(f"[FATAL] DAFFA14_WALLET also present in POOL: {DAFFA14_WALLET}")


def load_config() -> tuple[dict, list[dict]]:
    if not CONFIG_PATH.exists():
        sys.exit(f"[FATAL] config.json not found at {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    accs = cfg.get("accounts", [])
    if not isinstance(accs, list) or not accs:
        sys.exit("[FATAL] config.json has no 'accounts' array or it is empty")
    return cfg, accs


def build_plan(accounts: list[dict], truncate: bool) -> list[tuple[dict, str, str]]:
    """
    Returns list of (account_obj, new_wallet, reason) preserving config order.
    Raises SystemExit on capacity error unless truncate=True.
    """
    daffa_idx: list[int] = [
        i for i, a in enumerate(accounts)
        if str(a.get("name", "")).strip().lower() == DAFFA14_NAME_LC
    ]
    if len(daffa_idx) > 1:
        sys.exit(f"[FATAL] multiple accounts named '{DAFFA14_NAME_LC}': indices {daffa_idx}")

    # Capacity check for non-daffa14 accounts.
    normal_indices = [i for i in range(len(accounts)) if i not in daffa_idx]
    capacity = len(POOL) * ACCOUNTS_PER_WALLET
    if len(normal_indices) > capacity:
        msg = (
            f"[FATAL] {len(normal_indices)} normal accounts but pool capacity "
            f"is {capacity} ({len(POOL)} wallets * {ACCOUNTS_PER_WALLET}). "
            f"Re-run with --truncate to leave the extra accounts unchanged."
        )
        if not truncate:
            sys.exit(msg)
        print(f"[warn] {msg.replace('[FATAL] ', '')}")

    plan: list[tuple[dict, str, str]] = []

    # daffa14 special-case (if present).
    for i in daffa_idx:
        plan.append((accounts[i], DAFFA14_WALLET, "daffa14 reserved"))

    # Normal accounts: 2 per wallet, in config order.
    slot = 0
    for i in normal_indices:
        if slot >= capacity:
            print(f"[skip] account #{i} '{accounts[i].get('name','?')}' "
                  f"-> over capacity, leaving wallet_address unchanged")
            continue
        wallet = POOL[slot // ACCOUNTS_PER_WALLET]
        seat = (slot % ACCOUNTS_PER_WALLET) + 1
        plan.append((accounts[i], wallet,
                     f"pool[{slot // ACCOUNTS_PER_WALLET + 1}/33] seat {seat}/2"))
        slot += 1

    return plan


def show_plan(plan: list[tuple[dict, str, str]]) -> None:
    name_w = max(len(str(a.get("name", "?"))) for a, _, _ in plan)
    name_w = max(name_w, 4)
    changes = 0
    print(f"\n  {'#':>3}  {'name':<{name_w}}  {'old':<10}  ->  {'new':<10}  reason")
    print("  " + "-" * (name_w + 50))
    for i, (acc, new_w, reason) in enumerate(plan):
        name = str(acc.get("name", "?"))
        old = acc.get("wallet_address", "")
        old_s = short(old) if old else "(empty)"
        new_s = short(new_w)
        flag = "  " if old == new_w else "* "
        if old != new_w:
            changes += 1
        print(f"  {i:>3}  {name:<{name_w}}  {old_s:<10}  ->  {new_s:<10}  "
              f"{flag}{reason}")
    print(f"\n  total entries: {len(plan)}    changes: {changes}    "
          f"unchanged: {len(plan) - changes}")


def assignment_summary(plan: list[tuple[dict, str, str]]) -> None:
    counts: dict[str, list[str]] = {}
    for acc, wallet, _ in plan:
        counts.setdefault(wallet, []).append(str(acc.get("name", "?")))

    print("\n  Per-wallet usage:")
    for w in [DAFFA14_WALLET] + POOL:
        users = counts.get(w, [])
        tag = "daffa14*" if w == DAFFA14_WALLET else f"pool{POOL.index(w)+1:>2}"
        marker = "" if len(users) <= ACCOUNTS_PER_WALLET and (
            w != DAFFA14_WALLET or len(users) <= 1
        ) else "  <-- OVERFILLED"
        print(f"    {tag}  {short(w)}  {len(users)} acc(s)  "
              f"[{', '.join(users) if users else '(unused)'}]{marker}")


def apply_changes(cfg: dict, plan: list[tuple[dict, str, str]]) -> None:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG_PATH.with_suffix(f".json.bak.{ts}")
    shutil.copy2(CONFIG_PATH, backup)
    print(f"\n[ok] backup written: {backup.name}")

    for acc, new_wallet, _ in plan:
        acc["wallet_address"] = new_wallet

    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"[ok] {CONFIG_PATH.name} updated ({len(plan)} entries touched)")


# ---------------------------------------------------------------------------
# accounts.tsv update
# ---------------------------------------------------------------------------

def _read_tsv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    """Return (fieldnames, rows, delimiter). Mirrors add_account.bulk_import."""
    text = path.read_text(encoding="utf-8-sig")
    if "\t" in text:
        delimiter = "\t"
    elif ";" in text:
        delimiter = ";"
    else:
        delimiter = ","

    raw_lines = text.splitlines()
    if not raw_lines:
        sys.exit(f"[FATAL] {path.name} is empty")

    header_line = raw_lines[0]
    data_lines, _absorbed = _merge_broken_rows(
        raw_lines[1:], delimiter, len(TSV_REQUIRED_FIELDS)
    )
    merged = [header_line] + data_lines

    reader = csv.DictReader(merged, delimiter=delimiter)
    fieldnames = list(reader.fieldnames or [])
    missing = [f for f in TSV_REQUIRED_FIELDS if f not in fieldnames]
    if missing:
        sys.exit(f"[FATAL] {path.name} missing required columns: {missing}")
    rows = [dict(r) for r in reader]
    return fieldnames, rows, delimiter


def _write_tsv_rows(
    path: Path, fieldnames: list[str], rows: list[dict[str, str]], delimiter: str
) -> None:
    """Write rows back. csv.writer will quote any value containing the
    delimiter, a quote, or a newline so cookies with embedded newlines
    survive a round-trip and stay parseable by add_account.bulk_import."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=fieldnames,
        delimiter=delimiter,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        # csv.DictWriter writes None as the empty string by default; coerce
        # explicitly so the output is deterministic.
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    path.write_text(buf.getvalue(), encoding="utf-8")


def _row_has_blank_creds(row: dict[str, str]) -> bool:
    """True when bearer_token OR cookie is blank/whitespace-only."""
    bearer = str(row.get("bearer_token", "") or "").strip()
    cookie = str(row.get("cookie", "") or "").strip()
    return not bearer or not cookie


def preview_tsv(prune_empty: bool) -> None:
    """Print read-only stats about accounts.tsv during dry-run."""
    if not ACCOUNTS_TSV_PATH.exists():
        return
    try:
        _, rows, _ = _read_tsv_rows(ACCOUNTS_TSV_PATH)
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover - defensive
        print(f"[warn] accounts.tsv preview failed: {e}")
        return

    blank = [str(r.get("name", "")).strip() or "(no-name)"
             for r in rows if _row_has_blank_creds(r)]
    print(f"[info] accounts.tsv: {len(rows)} row(s), "
          f"{len(blank)} with blank bearer/cookie")
    if blank:
        action = "WILL BE PRUNED" if prune_empty else "kept (use --prune-empty-creds to drop)"
        preview = ", ".join(blank[:10]) + (f", ...(+{len(blank)-10})" if len(blank) > 10 else "")
        print(f"[info] blank-creds rows {action}: {preview}")


def apply_tsv_changes(
    plan: list[tuple[dict, str, str]], prune_empty: bool = False
) -> None:
    if not ACCOUNTS_TSV_PATH.exists():
        print(f"[warn] {ACCOUNTS_TSV_PATH.name} not found; skipping TSV update.")
        return

    fieldnames, rows, delim = _read_tsv_rows(ACCOUNTS_TSV_PATH)

    pruned_names: list[str] = []
    if prune_empty:
        kept: list[dict[str, str]] = []
        for row in rows:
            if _row_has_blank_creds(row):
                pruned_names.append(str(row.get("name", "")).strip() or "(no-name)")
            else:
                kept.append(row)
        rows = kept

    name_to_wallet: dict[str, str] = {}
    for acc, new_wallet, _ in plan:
        nm = str(acc.get("name", "")).strip()
        if nm:
            name_to_wallet[nm] = new_wallet

    tsv_names = {str(r.get("name", "")).strip() for r in rows if r.get("name")}
    plan_names = set(name_to_wallet.keys())
    only_in_tsv = sorted(tsv_names - plan_names)
    only_in_plan = sorted(plan_names - tsv_names)
    if only_in_tsv:
        print(f"[warn] in {ACCOUNTS_TSV_PATH.name} but NOT in config.json: "
              f"{only_in_tsv}  (these rows keep their current wallet_address)")
    if only_in_plan:
        print(f"[warn] in config.json but NOT in {ACCOUNTS_TSV_PATH.name}: "
              f"{only_in_plan}  (no TSV row to update)")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ACCOUNTS_TSV_PATH.with_suffix(f".tsv.bak.{ts}")
    shutil.copy2(ACCOUNTS_TSV_PATH, backup)
    print(f"[ok] backup written: {backup.name}")

    touched = 0
    for row in rows:
        nm = str(row.get("name", "")).strip()
        if nm in name_to_wallet:
            new_w = name_to_wallet[nm]
            if row.get("wallet_address", "") != new_w:
                row["wallet_address"] = new_w
                touched += 1
            else:
                row["wallet_address"] = new_w  # idempotent overwrite

    _write_tsv_rows(ACCOUNTS_TSV_PATH, fieldnames, rows, delim)
    if pruned_names:
        preview = ", ".join(pruned_names[:10]) + (
            f", ...(+{len(pruned_names)-10})" if len(pruned_names) > 10 else "")
        print(f"[ok] pruned {len(pruned_names)} row(s) with blank bearer/cookie: {preview}")
    print(f"[ok] {ACCOUNTS_TSV_PATH.name} updated ({len(rows)} row(s) kept, "
          f"{touched} wallet field(s) changed)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="actually write changes (default: dry-run)")
    p.add_argument("--truncate", action="store_true",
                   help="allow more accounts than capacity; extras stay unchanged")
    p.add_argument("--no-tsv", action="store_true",
                   help="skip updating accounts.tsv (only touch config.json)")
    p.add_argument("--prune-empty-creds", action="store_true",
                   help="drop accounts.tsv rows whose bearer_token or cookie is blank")
    args = p.parse_args()

    validate_pool()
    cfg, accounts = load_config()

    print(f"[info] config.json:  {len(accounts)} account(s)")
    if not args.no_tsv:
        preview_tsv(prune_empty=args.prune_empty_creds)
    print(f"[info] pool: {len(POOL)} wallets * {ACCOUNTS_PER_WALLET} = "
          f"{len(POOL) * ACCOUNTS_PER_WALLET} normal slots + 1 daffa14 slot")

    plan = build_plan(accounts, truncate=args.truncate)
    show_plan(plan)
    assignment_summary(plan)

    if not args.apply:
        print("\n[dry-run] no changes written. Re-run with --apply to commit.")
        if args.no_tsv:
            print("[dry-run] --no-tsv set: accounts.tsv would be SKIPPED")
        else:
            extra = " (+ prune blank-creds rows)" if args.prune_empty_creds else ""
            print(f"[dry-run] --apply will also update accounts.tsv{extra} "
                  "(use --no-tsv to skip)")
        return

    apply_changes(cfg, plan)
    if not args.no_tsv:
        apply_tsv_changes(plan, prune_empty=args.prune_empty_creds)
    else:
        print("[info] --no-tsv set: accounts.tsv left untouched.")

    print("\n[done] verify with:")
    print("  python -c \"import json; "
          "[print(a['name'], a['wallet_address'][:8]+'..') "
          "for a in json.load(open('config.json'))['accounts']]\"")
    if not args.no_tsv:
        print("  awk -F'\\t' 'NR>1{print $1, substr($4,1,8)\"..\"}' accounts.tsv | head")


if __name__ == "__main__":
    main()
