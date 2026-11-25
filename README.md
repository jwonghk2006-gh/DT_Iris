# DT_Iris (Day Trading Iris)

Intraday US-stock trading system for the Iris Yuan setups: **Power Ranger (opening range
breakout), Cupid, Tug-Of-War and Roller Coaster**. It runs on the Longbridge OpenAPI and
is built on the same plan as `..\Binance\AT_Binance`: one config file, honest backtests
with a noise check, a locked holdout, paper before live, and one file that sends orders,
protected by hard gates.

Started 2026-11-17. The research workspace it came from is `..\Iris` (see `Iris\HANDOVER.md`).

## Run order

```
.venv\Scripts\python.exe test_dt.py                       guard tests (32), run after every change
.venv\Scripts\python.exe dt_data.py --import-iris         old Iris IBKR minute data -> data/bars
.venv\Scripts\python.exe dt_gappers.py --months 12          every 4%+ gap day of the last year (+ minutes, quota-limited)
.venv\Scripts\python.exe dt_record.py                     EVERY EVENING after 16:05 ET: save today's gappers' minutes (free)
.venv\Scripts\python.exe dt_data.py --symbols SPY,ARM --days 30    Longbridge minute data for chosen symbols
.venv\Scripts\python.exe dt_backtest.py --controls 99     backtest + noise check
.venv\Scripts\python.exe dt_scanner.py                    pre-market watchlist, whole universe (needs keys; scheduled daily via run_scan.bat)
.venv\Scripts\python.exe dt_runner.py --mode paper        live session, simulated fills
.venv\Scripts\python.exe dt_runner.py --replay 2026-06-03 replay stored bars through the live engine
run_dashboard.bat                                         dashboard at http://localhost:8518
```

## Files

| file | what it does |
|---|---|
| `dt_config.py` | every setting: filters, strategy numbers, time rules, risk, costs, live caps |
| `dt_bars.py` | 1-min -> n-min bars (finished bars only), 9 EMA, VWAP, ATR, day context |
| `dt_strategies.py` | the four setups; shorts are the long rules on mirrored prices |
| `dt_sim.py` | fill rules shared by the backtester and paper trading |
| `dt_backtest.py` | walks every symbol-day; random-entry noise check |
| `dt_data.py` | minute-bar store, Iris CSV import, Longbridge download, holdout lock |
| `dt_record.py` | nightly: today's gappers + their full-day minute bars, outside the history quota |
| `dt_gappers.py` | historical gap days (universe -> daily bars -> top 20 gappers per side per day -> minutes) |
| `dt_rs.py` | relative strength vs SPY: beta, relative gap, best-matching sector ETF, intraday RS line |
| `dt_scanner.py` | pre-market gap + stocks-in-play scan of the whole universe (~5,900 stocks, ~5 s) -> `data/watchlists/<date>.csv`; `--scheduled` waits for 09:00 ET and locks it |
| `dt_runner.py` | live loop (Longbridge candle + quote push) and `--replay` |
| `dt_executor.py` | order manager for the Longbridge demo / live account |
| `dt_broker.py` | **the only file that sends orders**; keys and gates |
| `dt_store.py` | trades, signals, activity, runner state, kill switch (`store/`) |
| `dt_telegram.py` | alerts |
| `dashboard.py` | Streamlit dashboard |
| `test_dt.py` | guard tests |
| `dt_alpaca.py` | 5-year research history from Alpaca (market data only) -> `data_alpaca/` |
| `research_gap_drift.py` | pre-declared test: do gap stocks drift in the gap direction? (failed) |
| `research_sip_orb.py` | pre-declared test of the published "Stocks in Play" 5-minute ORB (failed) |
| `research_sip_orb_ticks.py` | the same ORB with the entry minute replayed from tick data (failed) |
| `research_dip_buy.py` | pre-declared daily dip-buying test in top-500 uptrending stocks (passed; weaker than SPY) |
| `SETUP_NEW_PC.md` | installing DT_Iris on another computer from GitHub |
| `run_scan.bat` | launched by the Windows task "DT_Iris pre-market scan" |
| `docs/STRATEGIES.md` | the rules in words, with what each number means |
| `docs/RESEARCH_LOG.md` | every test run so far and its result - read this first |
| `docs/HANDOVER_PROMPT.md` | paste into a new AI session to continue the work |
| `USER_MANUAL.md` | how to use it, step by step; section 7 = commands and checks on your own |

## Design decisions (and the Iris bugs they fix)

- **Bars:** Longbridge pushes 1-minute candles with an `is_confirmed` flag. Only confirmed
  candles are used, and 2-minute bars are built from them by `BarBuilder`, which never
  releases an unfinished bar. Iris's monitor treated 5-second bars as 1-minute bars.
- **Same code everywhere:** detectors only read bars up to "now" and the indicators are
  causal, so the backtester, `--replay` and the live runner find the same setups.
  `test_backtest_equals_replay` checks it.
- **Fill rules** are explicit and pessimistic (see `dt_sim.py`): stop-limit entries with a
  limit, slippage, commission, stop first when stop and target share a bar, no target on
  the entry bar, 12:00 ET entry cutoff, 15:00 ET flatten, break-even at +1R, daily limits.
- **Noise check:** random entries on the same symbol-days, same side, stop size and target.
  A strategy is only interesting when it beats them (p <= 0.05).
- **Holdout:** once there are 120+ trading days, the most recent 40 are locked and the
  backtester refuses them unless `--holdout` is passed. Write a pre-registration first.
- **Research sample:** `dt_gappers.py` finds every day a main-exchange stock (no ETF/fund)
  opened 4%+ away from the previous close with 500k+ average volume, keeps the top 20 per
  side per day like the live scanner, and downloads those days' minutes. In the backtest a
  gap-up day only trades longs and a gap-down day only shorts, like the live watchlist.
  Limits: today's listings only (delisted names missing), ranked by average rather than
  premarket volume, a few ADRs slip through, Longbridge volume is partial.
- **Longbridge limits (found 2026-11-17):** minute/daily HISTORY (`history_candlesticks_by_date`)
  is limited to **400 distinct symbols per quota period**; symbols already used can be asked
  again (list in `data/history_symbols.txt`). The latest-bars call (`candlesticks`) is not
  limited: up to 1,000 daily bars (back to 2022) or the last 1,000 minutes (one full day) for
  any symbol. So the gap scan covers the whole market, historical minutes only exist for the
  400 quota symbols, and `dt_record.py` collects every new day's gappers for free.
  The US data package is Nasdaq-only LV1 (`US_QBBO_OpenAPI`): volume is about 55-60% of
  consolidated volume.
- **Timestamps:** Longbridge returns naive datetimes in the PC's local time zone (Singapore);
  `dt_broker.to_utc` converts them. Checked against IBKR minute bars: prices match to the cent.
- **Credentials:** Longbridge has one App Key + App Secret per developer app; the demo and
  live accounts differ only by their Access Token. `.env` holds
  `LONGPORT_APP_KEY`, `LONGPORT_APP_SECRET`, `LONGPORT_PAPER_ACCESS_TOKEN`, `LONGPORT_LIVE_ACCESS_TOKEN`.
- **Stops:** Longbridge has no bracket order. The target rests at the broker as a limit;
  the stop is watched by the runner (`STOP_MODE = "local"`) until
  Settings -> "stop-order probe" proves a LIT sell below market waits on the demo account.
  If the runner or PC stops, **the stop is not protected**. Do not leave a live position
  unattended.

## Status (2026-11-21)

- **No intraday strategy has an edge.** Over 5 years of Alpaca data none of the six rule sets
  beats random entries, gap stocks show no drift, and the published 5-minute ORB fails after
  costs (also with tick data). Details: `docs/RESEARCH_LOG.md`.
- **Daily dip-buying** (`research_dip_buy.py`) passed its pre-declared test (+0.49% per trade,
  beat 99/99 random) but as a portfolio made less than holding SPY. Not traded.
- Paper runs of the intraday runner continue for learning only; real money stays off.
- The pre-market scanner runs automatically (Windows task, 09:00 ET) and posts to the
  Telegram group "DT_Iris Notification".
- **The Longbridge order path has not yet run against the demo account.**
- Data: Longbridge ~10 months of gap days in `data/`; Alpaca 5 years in `data_alpaca/`
  (holdout from 2026-09-25, unused).
- Strategy numbers are first drafts from the course notes; Tug-Of-War and Roller Coaster
  had no written rules in Iris and were drafted from `By Iris Yuan.docx`.
- The 2026-06-03 ASTC "Cupid Sell" from the Iris review is rejected here because price was
  above VWAP (the rulebook requires a downtrend). Decide whether that rule should stay.
- No ATR rules were written in the notes; ATR is used for opening-range width, Cupid leg
  size and pullback tolerance, Tug-Of-War box height and Roller Coaster "extended".

## Not built yet

Pivot / resistance "room to 2R" check, relative strength vs SPY/QQQ, Northern Light (swing),
a walk-forward stage, and a sector filter.

## 5-year research data (Alpaca)

`python dt_alpaca.py --check`, then `python dt_alpaca.py --years 5` downloads gap days and their
1-minute bars from Alpaca (market data only, free plan, keys `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`
/ `ALPACA_BASE_URL` in `.env`) into `data_alpaca/`, with its own gappers list and holdout.
Includes delisted stocks and uses consolidated volume. Backtest it with
`python dt_backtest.py --data alpaca --strategies ...`; results go to `results_alpaca/`.
Live trading and the scanner are not affected.
