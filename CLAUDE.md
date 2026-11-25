# DT_Iris: notes for Claude (loaded automatically in every new session in this folder)

DT_Iris (DT = Day Trading) is the user's US-stock day-trading research and paper-trading
system, a rebuild of the course "Iris" setups on the AT_Binance pattern. Windows 11, Python venv
in `.venv`, Windows PowerShell 5.1 (no `&&`). The user is in Singapore. Not financial advice:
give honest, data-based views.

## Read first, before answering anything about the project
1. `docs/RESEARCH_LOG.md`: every test and its result (newest at the bottom)
2. `docs/HANDOVER_PROMPT.md`: "Where it stands" and the working rules
3. `README.md` (files and design), `USER_MANUAL.md` (daily routine; section 7 = checks and problems)
4. `docs/STRATEGIES.md` and `dt_config.py`: the rules and every setting

## Automatic jobs (Windows Task Scheduler; check with `Get-ScheduledTask -TaskName "DT_Iris*"`)
| Task | When (Singapore) | What | Log |
|---|---|---|---|
| DT_Iris pre-market scan | Mon-Fri 20:45, then waits until **09:00 New York** (DST-safe) | `run_scan.bat` -> `dt_scanner.py --scheduled`: scans, locks `data/watchlists/<date>.csv`, posts to Telegram | `logs/scan.log` |
| DT_Iris nightly recorder | Tue-Sat 06:00 (after the US close) | `run_record.bat` -> `dt_record.py`: saves that day's gappers' 1-minute bars to `data/bars`, appends `data/gappers.csv` | `logs/record.log` |
Since 2026-11-23 the scan builds TWO lists (column `lists`): **gap** (relative gap = stock gap − beta × SPY gap,
|≥4%|, feeds Power Ranger / Roller Coaster) and **in_play** (pre-market volume ≥ 5% of a normal day, feeds Cupid /
Tug-Of-War). `dt_rs.py` holds the relative-strength maths; `GAP_BASIS = "raw"` in dt_config restores the old rule.
Tested 2026-11-24 (`research_rs.py`, 5 years): FAIL for Power Ranger, Cupid, Tug-Of-War - they lose less but
still lose; paper only. The follow-up "trade with RS" test (`research_rs_lead.py`) also FAILED
(-58.8R on validate): RS works as a loss-reducing filter, not as an edge. See RESEARCH_LOG.
The dashboard is `run_dashboard.bat` -> http://localhost:8518 (Streamlit). The runner (`dt_runner.py`) is NOT scheduled,
except a ONE-OFF task "DT_Iris lbpaper runner (tonight)" the user approved for 25 Nov 2026 22:10 SGT
(`run_runner_lbpaper.bat`, log `logs/runner.log`; first ever orders to the Longbridge demo account; task deletes itself).

## Hard rules
- Never print, copy or send values from `.env`; only check which names are set.
- Do not place orders, or start the runner in `lbpaper`/`live` mode, without the user's explicit OK.
  Live needs `DT_LIVE_ENABLED=yes` plus the gates in `dt_broker.py` (the only file that sends orders).
- Ask before creating or changing scheduled tasks or other persistent config.
- New tests: write the rules and pass bar at the top of the script BEFORE running, run once, no
  tuning until it passes (the user lost money with an over-fitted system). A result counts only if it
  beats the random-entry noise check after costs (1 cent slippage per fill + $0.005/share).
- Holdouts (`data/manifest.json`, `data_alpaca/manifest.json`) stay locked; use once, pass bar first.
- Run `.venv\Scripts\python.exe test_dt.py` after code changes.

## Longbridge gotchas
- Timestamps come back naive in the PC's local time (SGT): convert with `dt_broker.to_utc`, never `tz_localize("UTC")`.
- History quota: 400 distinct symbols per period (error 301607). `candlesticks()` (latest 1,000 bars) is quota-free.
- Before the open, `quote().prev_close` is two sessions old; use `pre_market_quote.prev_close`.
- US data package is Nasdaq-only: volume is about 55-60% of consolidated volume.
- The FIRST pushed 1-minute candle of a session can reach back to the previous close (seen 2026-11-24:
  Power Ranger stops 10-20% wide). `dt_runner.verified_bar` replaces pushed candles in the first 15 minutes
  with the candle history (logged as `bar_check` in activity). Check `bar_check` lines after a live session.
- `quote()` can silently leave symbols out of its reply (23 Nov: 267 of 5,842, incl. AMD, TSM, DELL).
  `dt_scanner._quote_batch` re-asks for missing ones; scan.log lists any still missing (6 dotted share classes
  such as BRK.B / BF.B always are: lb_symbol does not map them).

## Dashboard look (the user's choice)
Light blue sidebar `#dcecf8`, very light grey page `#f4f5f7`, default text 1.5x with a slider,
sidebar text slightly larger than the page, modest heading sizes. The user did not like beige or very large text.

Keep this file and `docs/HANDOVER_PROMPT.md` "Where it stands" up to date when things change.
