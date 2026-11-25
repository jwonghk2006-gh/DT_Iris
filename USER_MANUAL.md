# DT_Iris user manual

DT_Iris finds the Iris day-trading setups (Power Ranger, Cupid, Tug-Of-War, Roller Coaster)
on US stocks, tests them on past minute data, and trades them: first on pretend money,
then on the Longbridge demo account, and only later, behind safety checks, with real money.

## 1. One-time setup

1. Open `DT_Iris\.env` in Notepad and fill in (no spaces, no quotes):
   ```
   LONGPORT_APP_KEY=...            from open.longbridge.com -> Developer
   LONGPORT_APP_SECRET=...         same page
   LONGPORT_PAPER_ACCESS_TOKEN=... the token shown while the DEMO account is selected
   LONGPORT_LIVE_ACCESS_TOKEN=     leave empty until you go live
   ```
   The App Key and App Secret are the same for demo and live. Only the token changes.
   Save as **`.env`**, not `.env.txt`: in Notepad choose *Save as type: All files*.
2. Telegram (done 2026-11-20): `DT_TELEGRAM_BOT_TOKEN` is the FULL token from BotFather
   (`123456789:AAH...`, about 46 characters). `DT_TELEGRAM_CHAT_ID` is the group
   "DT_Iris Notification" (a negative number). Never the bot's own number. See section 7.
3. Alpaca (done 2026-11-20, research data only): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
   `ALPACA_BASE_URL`. Free Basic plan is enough.
4. Double-click `run_dashboard.bat`. The dashboard opens at `http://localhost:8518`.
5. Settings -> **Test lbpaper connection**. You should see `"ok": true` and your demo balance.

## 2. Daily routine (Singapore evening)

US market hours are 09:30-16:00 New York: 21:30-04:00 Singapore in US summer time,
22:30-05:00 in US winter time.

| When (New York) | What | Where |
|---|---|---|
| 09:00 | **Automatic**: Windows task "DT_Iris pre-market scan" (starts 20:45 Singapore Mon-Fri, waits for 09:00 ET) scans the whole Longbridge universe and locks the watchlist; Telegram gets the list. Log in `logs\scan.log`. **Scan now** on the Scanner page only if it was missed | Scanner |
| 09:20 | Check the watchlist. Optional: start the runner in **`paper`** mode for learning (no real money). Never `lbpaper`/`live` while no strategy has passed testing (see `docs/RESEARCH_LOG.md`) | Runner |
| 09:30-12:00 | Setups appear in the Runner page and on Telegram; trades are taken in the chosen mode | Runner, Home |
| 12:00 | No new entries | automatic |
| 15:00 | Everything is closed | automatic |
| after 16:05 | `dt_record.py` saves today's gappers for research (free). **Automatic**: Windows task "DT_Iris nightly recorder", 06:00 Singapore Tue-Sat; log in `logs\record.log` | scheduled |
| after | Mark each trade valid / false signal / broke plan | Trades |

The runner window must stay open and the PC awake until 15:00 New York time.

## 3. Modes

| Mode | Orders go to | Needs |
|---|---|---|
| `paper` | nowhere: fills simulated with the backtest rules | market data (any token) |
| `lbpaper` | Longbridge demo account | `LONGPORT_PAPER_ACCESS_TOKEN` |
| `live` | Longbridge real account | live token, `DT_LIVE_ENABLED=yes`, 10 paper sessions, typed LIVE |

Suggested order: a week of `paper`, then a week of `lbpaper`, and compare the two. They
should take the same trades; differences show slippage and order handling. Only then think
about `live`, with the small caps in `dt_config.py` ($2,000 a position, $50 risk a trade,
$150 daily loss, long only).

## 4. Safety

- **Kill switch** (Runner page): no new entries and waiting setups dropped. Open trades
  keep their target, and the runner still watches their stop.
- The **stop is watched by the runner**, because Longbridge has no bracket order. If the PC
  sleeps or the runner window closes, an open trade has only its target order. Before
  relying on the broker for stops, run Settings -> *stop-order probe* on the demo account
  during market hours.
- Exits are never blocked by the gates; only new entries are.
- The runner stops at 15:05 ET and sends a summary. If a position is still open it says so
  loudly: close it in the Longbridge app.
- Only one runner can run at a time.

## 5. Backtester and what the numbers mean

- **Total R / Avg R**: profit in units of risk. +10R at 1% risk is about +10%.
- **Noise check**: the same number of trades entered at random times, same stop size and
  target. "Beat 95 of 99" (p = 0.05) or better means the setups add something. Worse means
  the result could be luck.
- Fewer than about 30 trades says almost nothing. With 4 days of data the first run made
  9 trades. Download more days (Data page) before drawing conclusions.
- Holdout: once you have 120+ trading days, the last 40 are locked. Use them ONCE, to check
  a frozen rule set, after writing down beforehand what result counts as a pass.

## 6. Changing the rules

All numbers are in `dt_config.py`, explained in `docs/STRATEGIES.md`. After a change, run
`test_dt.py`, re-run the backtest, and compare it with the noise check. Change a few things
at a time: every tweak that only helps on past data makes the backtest more optimistic.

Before testing a new idea, write the rule and the pass bar at the top of the script (see
`research_gap_drift.py`), THEN run it once. Do not adjust and re-run until it passes: that is
how a backtest learns the past and fails live.

## 7. On your own: commands and checks

Open **Windows PowerShell** and go to the folder first. PowerShell 5.1 does not accept `&&`,
so run one command per line:

```
cd $HOME\Documents\DT_Iris
```

| To do | Command |
|---|---|
| Guard tests (after any change) | `.venv\Scripts\python.exe test_dt.py` (expect `32/32 passed`) |
| Dashboard | double-click `run_dashboard.bat`, open `http://localhost:8518` |
| Scan now (if the scheduled scan was missed; before 09:30 ET) | `.venv\Scripts\python.exe dt_scanner.py` |
| Scan only some symbols | `.venv\Scripts\python.exe dt_scanner.py --symbols PL,LULU` |
| Telegram test | `.venv\Scripts\python.exe dt_telegram.py "test"` (expect `sent: True`) |
| Alpaca keys test | `.venv\Scripts\python.exe dt_alpaca.py --check` |
| Top up the 5-year data with new days | `.venv\Scripts\python.exe dt_alpaca.py --years 5` (only fetches what is missing) |
| Backtest one strategy on 5 years | `.venv\Scripts\python.exe dt_backtest.py --data alpaca --strategies power_ranger --controls 99` |
| Backtest on Longbridge data | `.venv\Scripts\python.exe dt_backtest.py --strategies power_ranger --controls 99` |
| Gap-drift test | `.venv\Scripts\python.exe research_gap_drift.py` |
| Published ORB test | `.venv\Scripts\python.exe research_sip_orb.py` |
| Dip-buying test (daily bars, ~2 min) | `.venv\Scripts\python.exe research_dip_buy.py` |
| Paper session (for learning) | `.venv\Scripts\python.exe dt_runner.py --mode paper`, or Runner page in `paper` mode, before 09:30 ET |

A 5-year backtest takes 10-40 minutes per strategy; the noise check uses all CPU cores.

### Is the scheduled scan working?

1. Task Scheduler -> Task Scheduler Library -> **DT_Iris pre-market scan**: Last Run Result
   `0x0` = finished OK; `0x41301` / 267009 = running (it waits from 20:45 until 09:00 ET).
2. `logs\scan.log`, last lines: `N long, M short from 5842 symbols ...` and `exit code 0`.
   "too late for a pre-market scan" = the task started after 09:25 ET. "no premarket quotes"
   = US holiday. "weekend" = Saturday/Sunday in New York.
3. The group "DT_Iris Notification" gets the list a few seconds after 09:00 ET.
4. The PC must be ON or ASLEEP at 20:45 Singapore (the task can wake it), not shut down.
5. Stop the task: `schtasks /Change /TN "DT_Iris pre-market scan" /DISABLE`
   (`/ENABLE` to resume, `/Delete ... /F` to remove).

### Common problems

| Symptom | Cause and fix |
|---|---|
| `sent: False` from the Telegram test | Token incomplete (must contain `:`) or chat id wrong. 403 "can't send messages to the bot" = chat id is the bot's own number. Send any message in the group, then read the group id from `https://api.telegram.org/bot<TOKEN>/getUpdates` (`"chat":{"id":-...`) |
| Alerts stop after changing group settings | Telegram turned the group into a "supergroup" with a NEW id (starts with -100). Read it again from getUpdates and update `DT_TELEGRAM_CHAT_ID` |
| A gap of -80% or +700% on the list | A split. The scanner marks it "split today" and uses the adjusted close; if not marked, add the symbol to `EXCLUDE_SYMBOLS` in `dt_config.py` for that day |
| An ETF/fund on the list | Add a word from its Longbridge name to `EXCLUDE_WORDS` in `dt_gappers.py`, then rebuild: `.venv\Scripts\python.exe -c "import dt_gappers as g; g.build_universe(refresh=True)"` |
| `Alpaca refused the request (403)` | Keys wrong, or a request for data newer than 15 minutes (the free plan forbids that) |
| Dashboard shows an error after an edit | Read the red box; run `test_dt.py`; the previous version of each file is NOT kept automatically, so keep a copy before editing |

### Rules worth keeping

- The **holdout** (`data_alpaca\manifest.json`, from 2026-09-25) is used ONCE, for a rule set
  that already passed on the research years, with the pass bar written down first. Using it
  is permanent: after that the last 40 days are no longer a clean test.
- A result counts only if it beats the random-entry noise check AND survives realistic costs
  (1 cent slippage each way plus commission).
- Paper (`paper`, then `lbpaper`) for at least 10 sessions before any `live` trading, with the
  small live caps in `dt_config.py`. Live shorts stay off (`ALLOW_SHORT_LIVE = False`).
- Known live risk: the stop is watched by the runner on this PC. If the PC or runner stops,
  an open position has no stop at the broker. Never leave a live position unattended.
