# claimyshare-withdraw

Minimal auto-withdraw for one or more of your own accounts on
`claimyshare.io`. Fires one POST per account per eligible window, with
on-chain confirmation, persistent state, and clean cooldown handling.

Two modes:

| Mode | Entry point | What it does | When to use |
| --- | --- | --- | --- |
| **One-shot** | `python withdraw.py` | Iterates every account in `config.json`, fires one POST per account (15 s spacing), exits. | Run manually, or schedule once per day. |
| **Monitor** | `python monitor.py` | Watches the site's hot wallet (`8MrX...`) on-chain. The moment it's topped up, fires one withdraw per eligible account. Keeps running. | "Be first after the admin refill" strategy. |

Pick **one** mode — running both concurrently wastes per-account
rate-limit budget.

## What this script does NOT do

- It does **not** log in with Twitter / OAuth automatically. You must
  extract `bearer_token` and `cookie` from each logged-in browser
  session manually, even when running multiple accounts.
- It does **not** retry-spam. Hitting the API more than 3 times in 60
  seconds is rate-limited with `429 Too Many Requests` and bypasses
  nothing.
- It does **not** let you bypass the ~24-hour cooldown between
  successful withdraws on a given account.
- It does **not** rotate IPs / proxies between accounts. Run on one
  VPS and they all share the outbound IP.

Anything beyond this scope is out of scope by design.

## Why once per run

From real traffic on this account:
- `/api/withdraw` is rate-limited to **3 req / 60 s** (`ratelimit-policy: 3;w=60`).
- Successful withdraws are spaced **~24 hours** apart (on-chain evidence).
- When either limit is hit the server always returns the same body:
  ```json
  {"message": "Too many withdrawal requests, please try again later"}
  ```

So the correct strategy is: one attempt, ideally at a time you know
the daily cooldown has expired. If the server still refuses, stop and
try again tomorrow — faster retries cannot succeed.

## Setup

Requires Python 3.10+.

```powershell
cd C:\Users\daffa\CascadeProjects\claimyshare-withdraw
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create your config from the template:

```powershell
copy config.example.json config.json
```

`config.json` supports multiple accounts:

```json
{
  "accounts": [
    { "name": "acc1", "bearer_token": "...", "cookie": "...", "wallet_address": "...", "amount_sol": 0.0033999998 },
    { "name": "acc2", "bearer_token": "...", "cookie": "...", "wallet_address": "...", "amount_sol": 0.0033999998 }
  ]
}
```

For a single account, the legacy flat schema (top-level
`bearer_token` / `cookie` / `wallet_address` / `amount_sol`) is still
accepted and auto-wrapped under the name `default`.

Fill each account with values pulled from your browser:

| Field | Where to get it |
| --- | --- |
| `name` | Any unique label per account, e.g. `acc1`, `acc2`. Used as a key in `state.json` and in log lines. |
| `bearer_token` | In DevTools → Network → withdraw request → Request Headers → `authorization`. Copy only the part after `Bearer ` (no `Bearer ` prefix). Token expires ~30 days after issuance. |
| `cookie` | Same Network request → Request Headers → `cookie`. Copy the **entire** value (e.g. `GAESA=...`). |
| `wallet_address` | Solana wallet where withdrawals should land. |
| `amount_sol` | Amount the UI lets you withdraw. The UI currently posts e.g. `0.0033999998`. |

There is no shortcut for multi-account: you must extract
`bearer_token` + `cookie` from each account's logged-in browser session
separately. Twitter / OAuth auto-login is **not** supported by design.

`config.json` is gitignored — don't commit it.

## Run: one-shot mode

```powershell
python withdraw.py
```

Iterates every account in `config.json` once, with a 15 s gap between
accounts (to avoid sub-second bursts that look like a bot). Each
account gets exactly one POST.

Expected outputs (per account):

- `[acc1] [ok] API success ...` → withdraw accepted by the API.
- `[acc1] [cooldown] ...` → that account is in the 24 h window or rate limit; skipped this round.
- `[acc1] [error] ...` → inspect `withdraw.log`; token/cookie may be expired.

A `summary` line is printed at the end with `ok=N cooldown=M error=K`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | At least one account succeeded |
| 1 | Missing or invalid `config.json` |
| 2 | All accounts in cooldown / rate limit |
| 3 | Other API error on every account (check log) |
| 4 | Network errors |

## Run: monitor mode ("auto on top-up", multi-account)

```powershell
python monitor.py
```

What happens on start:

1. **Per-account bootstrap.** For each account in `config.json` whose
   `last_success_at` is unknown, the script scans that account's
   wallet history on-chain for the most recent incoming transfer from
   `8MrX...` and uses it as the cooldown anchor.
2. It polls the hot wallet balance every **30 seconds**.
3. When balance jumps by **≥ 0.0005 SOL**, it builds the list of
   eligible accounts (not in their own 24 h cooldown, not within the
   per-account rate-limit window) and fires one POST per eligible
   account, spaced **15 s** apart.
4. If hot wallet drains below ~0.0002 SOL mid-sequence, the remaining
   accounts are skipped (they would fail anyway).
5. After each success, that account is locked out for ~23 h 55 m.
6. State is persisted per-account in `state.json`, so Ctrl+C → restart
   is safe.

Example log fragment:

```
[2026-05-02T13:59:12Z] monitor started | accounts=3 poll=30s topup>=0.000500 SOL
[2026-05-02T13:59:13Z] [acc1] [bootstrap] scanning chain for last hot-wallet payout...
[2026-05-02T13:59:14Z] [acc1] last success 2026-05-02T12:52:24Z, cooldown ends in 22h53m10s
[2026-05-02T13:59:14Z] [acc2] no prior success; will attempt on first top-up.
[2026-05-02T14:00:42Z] initial hot wallet balance: 0.001174207 SOL
[2026-05-03T14:20:15Z] [topup] hot wallet 0.001174207 -> 0.152000000 SOL (+0.150825793); 2 of 3 account(s) eligible.
[2026-05-03T14:20:15Z] [acc2] [fire] 1/2 starting withdraw.
[2026-05-03T14:20:16Z] [acc2] response status=200 body={'success': True, ...}
[2026-05-03T14:20:16Z] [acc2] [ok] success; next attempt earliest in 23h55m00s.
[2026-05-03T14:20:31Z] [acc3] [fire] 2/2 starting withdraw.
[2026-05-03T14:20:32Z] [acc3] response status=200 body={'success': True, ...}
[2026-05-03T14:20:32Z] [acc3] [ok] success; next attempt earliest in 23h55m00s.
```

Tunables (top of `monitor.py`):

| Name | Default | Meaning |
| --- | --- | --- |
| `POLL_INTERVAL_SEC` | 30 | How often to hit Solana RPC for the hot wallet balance. |
| `TOPUP_THRESHOLD_LAMPORTS` | 500,000 (0.0005 SOL) | Ignore dust / tx-fee noise; only react to real refills. |
| `PER_ACCOUNT_SPACING_SEC` | ~35 | Min gap between two attempts on the **same** account (3/60 s rate limit). |
| `INTER_ACCOUNT_SPACING_SEC` | 15 | Gap between two **different** accounts firing on the same top-up. |
| `HOT_WALLET_FLOOR_LAMPORTS` | 200,000 (0.0002 SOL) | If hot wallet drops below this mid-sequence, skip remaining accounts. |

### Keep it running

The script runs in the foreground. To keep it alive after you close the
terminal, pick one:

- **Task Scheduler** (start at logon):
  ```powershell
  $py = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw\.venv\Scripts\python.exe"
  $script = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw\monitor.py"
  $wd = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw"

  $action  = New-ScheduledTaskAction  -Execute $py -Argument "`"$script`"" -WorkingDirectory $wd
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
      -DontStopIfGoingOnBatteries -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 5)

  Register-ScheduledTask -TaskName "claimyshare-monitor" `
      -Action $action -Trigger $trigger -Settings $settings `
      -Description "claimyshare.io hot-wallet monitor + auto-withdraw."
  ```
  Start immediately: `Start-ScheduledTask -TaskName "claimyshare-monitor"`
  Stop / remove: `Unregister-ScheduledTask -TaskName "claimyshare-monitor" -Confirm:$false`

- **Manual**: just `python monitor.py` in a PowerShell window and leave it.

## One-shot mode: schedule once per day

If you'd rather skip the live monitor and just have Windows run
`withdraw.py` once per day, your last successful on-chain withdraw
timestamp is the ideal trigger time: schedule a few minutes **after**
it so the 24h window is comfortably past.

Register a daily task via PowerShell (run the shell **as Administrator**
and adjust the `-At` time to match your pattern):

```powershell
$py = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw\.venv\Scripts\python.exe"
$script = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw\withdraw.py"
$wd = "C:\Users\daffa\CascadeProjects\claimyshare-withdraw"

$action  = New-ScheduledTaskAction  -Execute $py -Argument "`"$script`"" -WorkingDirectory $wd
$trigger = New-ScheduledTaskTrigger -Daily -At 20:40   # 20:40 WIB = 13:40 UTC, adjust as needed
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "claimyshare-withdraw" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily claimyshare.io withdraw (single request)."
```

Remove it later with:

```powershell
Unregister-ScheduledTask -TaskName "claimyshare-withdraw" -Confirm:$false
```

## Token / cookie rotation

- The JWT is valid ~30 days from issuance. When `withdraw.py` starts
  returning non-cooldown errors, re-login on `claimyshare.io` in your
  browser and replace both `bearer_token` and `cookie` in `config.json`.
- If you suspect your token was exposed, log out on the site (which
  should invalidate the session server-side), then log back in.

## Safety notes — read before trusting the site further

- The site's hot wallet (`8MrX8pJ6VkCsmMjrn4jTrp9DFACrytKVz6T23vDpqGgy`)
  pays out small amounts (observed 0.0025 – 0.006 SOL / day per account
  for this user) and simultaneously rotates SOL out to many other
  addresses. That matches a working-for-now reward site, but it also
  matches an operation that could stop paying at any time.
- **Do not deposit SOL / USDC to this site.** Any "unlock withdrawal
  by paying fee" prompt is a scam pattern, full stop.
- **Multi-account is supported but risky.** Reward sites commonly
  detect sybil patterns and forfeit all related balances at once. See
  the next section for what to do (and not do) if you go that route.
- Cross-check every payout on-chain:
  `https://solscan.io/account/<your_wallet>`. If the API reports
  success but nothing lands on-chain for 10+ minutes, treat the success
  as suspect.

## Multi-account risks (read once, take seriously)

If you run multiple accounts withdrawing to the **same** wallet (the
current default in `config.example.json`), you are creating a strong
sybil signature on-chain: anyone — including the site's backend — can
see N distinct user accounts paying out to one address and freeze /
forfeit balances. Mitigations you can adopt yourself:

- **Different `wallet_address` per account.** Spread payouts so they
  don't all land on one wallet. The `INTER_ACCOUNT_SPACING_SEC` gap
  doesn't help if the destination wallet is identical.
- **Don't let all accounts withdraw at the same minute every day.**
  This bot already only fires on top-ups, so timing varies with the
  admin's refill cadence — but if there are very few refills per day,
  the pattern is still tight.
- **Same VPS = same outbound IP for all accounts.** If you rotate IPs
  (HTTP proxy per account), this script does **not** support that and
  I will not add it — the further you go down that road, the more this
  is unambiguous evasion of anti-sybil controls.

Nothing here removes the basic risk that a) the site rugs everyone, or
b) the site detects sybil patterns and forfeits all linked balances at
once. Multi-account compounds that risk; it doesn't distribute it.

## Files

| File | Purpose |
| --- | --- |
| `core.py` | Shared helpers: HTTP POST, Solana RPC, state persistence, account loading. |
| `withdraw.py` | One-shot entry point. Iterates accounts, one POST each. |
| `monitor.py` | Watch-loop entry point. Polls hot wallet, fires eligible accounts on top-up. |
| `config.example.json` | Template. Copy to `config.json` and fill. |
| `config.json` | Your real credentials, multi-account (gitignored). |
| `state.json` | Per-account persisted state (gitignored). |
| `withdraw.log` | Append-only audit of one-shot runs. |
| `monitor.log` | Append-only audit of monitor runs. |
| `requirements.txt` | Python deps. Just `requests`. |
