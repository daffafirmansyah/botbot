#!/usr/bin/env python3
"""
One-shot helper to (re)assign withdraw `wallet_address` for every account in
config.json from a fixed wallet pool.

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
  - Writes a timestamped backup of config.json before modifying.
  - Pass --apply to actually write changes.

Usage on VPS:
  cd ~/botbot
  source ~/venv/bin/activate
  python assign_wallets.py            # dry-run, shows plan
  python assign_wallets.py --apply    # commits changes (after backup)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

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

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="actually write changes (default: dry-run)")
    p.add_argument("--truncate", action="store_true",
                   help="allow more accounts than capacity; extras stay unchanged")
    args = p.parse_args()

    validate_pool()
    cfg, accounts = load_config()

    print(f"[info] config.json: {len(accounts)} account(s)")
    print(f"[info] pool: {len(POOL)} wallets * {ACCOUNTS_PER_WALLET} = "
          f"{len(POOL) * ACCOUNTS_PER_WALLET} normal slots + 1 daffa14 slot")

    plan = build_plan(accounts, truncate=args.truncate)
    show_plan(plan)
    assignment_summary(plan)

    if not args.apply:
        print("\n[dry-run] no changes written. Re-run with --apply to commit.")
        return

    apply_changes(cfg, plan)
    print("\n[done] verify with:")
    print("  python -c \"import json; "
          "[print(a['name'], a['wallet_address'][:8]+'..') "
          "for a in json.load(open('config.json'))['accounts']]\"")


if __name__ == "__main__":
    main()
