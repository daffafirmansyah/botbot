# claimyshare-withdraw

Minimal auto-withdraw for your own single account on `claimyshare.io`.

Two modes, both single-account, both for your own credentials:

| Mode | Entry point | What it does | When to use |
| --- | --- | --- | --- |
| **One-shot** | `python withdraw.py` | Fires exactly one POST to `/api/withdraw`, logs the response, verifies on-chain. Exits. | Run manually, or schedule via Windows Task Scheduler once per day. |
| **Monitor** | `python monitor.py` | Watches the site's hot wallet (`8MrX...`) on-chain. The moment it's topped up, fires one withdraw. Keeps running. | "Be first after the admin refill" strategy. |

Pick **one** mode — running both concurrently wastes the 3 req / 60 s
rate-limit budget.

## What this script does NOT do

- It does **not** log in with Twitter / OAuth automatically.
- It does **not** operate on multiple accounts.
- It does **not** retry-spam. Hitting the API more than 3 times in 60
  seconds is rate-limited with `429 Too Many Requests` and bypasses
  nothing.
- It does **not** let you bypass the ~24-hour cooldown between
  successful withdraws.

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

Then open `config.json` and fill in:

| Field | Where to get it |
| --- | --- |
| `bearer_token` | In DevTools → Network → withdraw request → Request Headers → `authorization`. Copy only the part after `Bearer ` (no `Bearer ` prefix). Token expires ~30 days after issuance. |
| `cookie` | Same Network request → Request Headers → `cookie`. Copy the **entire** value (e.g. `GAESA=...`). |
| `wallet_address` | Your own Solana wallet where withdrawals should land. |
| `amount_sol` | Amount the UI lets you withdraw. The UI currently posts e.g. `0.0033999998`. |

`config.json` is gitignored — don't commit it.

## Run: one-shot mode

```powershell
python withdraw.py
```

Expected outputs:

- `[ok] API returned success.` + an on-chain delta → withdraw landed.
- `[cooldown] ...` → you're still inside the 24h window or rate limit. Try tomorrow.
- `[error] unexpected response ...` → inspect `withdraw.log`; token/cookie may be expired.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | API success (and usually confirmed on-chain) |
| 1 | Missing or invalid `config.json` |
| 2 | Cooldown / rate limit (expected) |
| 3 | Other API error (check log) |
| 4 | Network / transport error |

## Run: monitor mode ("auto on top-up")

```powershell
python monitor.py
```

What happens on start:

1. On the very first run, the script scans your wallet's on-chain history
   for the most recent incoming transfer from `8MrX...` and treats it
   as the "last successful withdraw" — so it won't fire inside an
   existing 24h cooldown.
2. It polls the hot wallet balance every **30 seconds** via Solana RPC.
3. When balance jumps by **≥ 0.0005 SOL** and you're outside cooldown,
   it fires one withdraw POST, logs the response, and updates state.
4. After a success, it waits ~23h55m before it's allowed to try again.
5. State is persisted in `state.json`, so Ctrl+C → restart is safe.

All activity is appended to `monitor.log`:

```
[2026-05-02T13:59:12Z] monitor started | poll=30s topup>=0.000500 SOL hot_wallet=8MrX...
[2026-05-02T13:59:12Z] last success at 2026-05-02T13:32:24Z; daily cooldown ends in 23h33m12s
[2026-05-02T14:00:42Z] initial hot wallet balance: 0.001174207 SOL
[2026-05-03T14:20:15Z] [topup] 0.001174207 -> 0.152000000 SOL (+0.150825793). attempting withdraw.
[2026-05-03T14:20:17Z] response status=200 body={'success': True, ...}
[2026-05-03T14:20:47Z] [ok] on-chain delta confirms withdraw landed.
[2026-05-03T14:20:47Z] [ok] withdraw succeeded at 2026-05-03T14:20:47Z; next attempt earliest in 23h55m00s
```

Tunables are near the top of `monitor.py`:

| Name | Default | Meaning |
| --- | --- | --- |
| `POLL_INTERVAL_SEC` | 30 | How often to hit Solana RPC for the hot wallet balance. |
| `TOPUP_THRESHOLD_LAMPORTS` | 500,000 (0.0005 SOL) | Ignore dust / tx-fee noise; only react to real refills. |
| `ATTEMPT_SPACING_SEC` | ~35 | Minimum gap between our POSTs (stays under 3/60 s). |

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
- **Do not run multiple accounts.** Reward sites commonly detect sybil
  patterns and forfeit all related balances at once. This script is
  deliberately single-account.
- Cross-check every payout on-chain:
  `https://solscan.io/account/<your_wallet>`. If the API reports
  success but nothing lands on-chain for 10+ minutes, treat the success
  as suspect.

## Files

| File | Purpose |
| --- | --- |
| `core.py` | Shared helpers: HTTP POST, Solana RPC, state persistence. |
| `withdraw.py` | One-shot entry point. Sends one POST, parses, logs. |
| `monitor.py` | Watch-loop entry point. Polls hot wallet, fires on top-up. |
| `config.example.json` | Template. Copy to `config.json` and fill. |
| `config.json` | Your real credentials (gitignored). |
| `state.json` | Monitor-mode persisted state (gitignored). |
| `withdraw.log` | Append-only audit of one-shot runs. |
| `monitor.log` | Append-only audit of monitor runs. |
| `requirements.txt` | Python deps. Just `requests`. |
