# DT_Iris research log

What was built, what was tested, and what came out. Newest decisions at the bottom.
R = the distance from entry to stop; +1R = a winner the size of the planned loss
(1% of the account in the backtests). Results include costs unless said otherwise.

## 2026-11-17 to 19: DT_Iris built (Longbridge)

- Four course setups coded (Power Ranger, Cupid, Tug-Of-War, Roller Coaster) with the same
  fill rules for backtest and paper (`dt_sim.py`), a random-entry noise check and a locked holdout.
- Longbridge history limit: 400 different symbols per quota period, so only about 10 months of
  gap days (Nov 2025 - Sep 2026) could be downloaded.

## 2026-11-20: ORB Desk v2 port and first tests (Longbridge, ~10 months)

- The TradingView "ORB Desk v2" rules (`..\Collin\ORB_Desk_v2`) were ported as research
  strategies `orb_retest_a` (enter after the retest) and `orb_retest_b` (enter after the 2-bar
  swing confirmation). They are `enabled: False`: the runner never trades them.
- Result: A 26 trades, -21R, worse than all 99 random-entry runs. B 2 trades. Verdict: do not trade.

## 2026-11-20: automatic pre-market scanner

- `dt_scanner.py --scheduled`, run by the Windows task **"DT_Iris pre-market scan"**
  (Mon-Fri 20:45 Singapore; it waits until 09:00 New York, so US daylight saving needs no change).
- Scans the whole Longbridge universe (about 5,840 common stocks) in about 5 seconds, ranks gaps,
  saves and LOCKS `data\watchlists\<date>.csv` (+ `<date>.json`), sends the list to Telegram.
- Long-side minimum price raised from the course's $1 to **$5** (`LONG_MIN_PRICE`), to keep out
  $1-2 small caps.

## 2026-11-20: 5-year history from Alpaca

- `dt_alpaca.py` (free Basic plan, market data only) downloaded Nov 2021 - Nov 2026 into
  `data_alpaca\`: 6,032 stocks including 1,050 that were later delisted, 24,567 gap stock-days,
  their 1-minute bars. Consolidated volume (all exchanges), unlike Longbridge's Nasdaq-only feed.
- Its own holdout: **2026-09-25 onward (40 sessions) is locked and has NOT been used.**
- Backtest it with `dt_backtest.py --data alpaca`; results in `results_alpaca\`.

### Results, 5 years (Nov 2021 - Sep 2026), each with 99 random-entry runs

| Strategy | Trades | Win % | Total R | Random avg | Beat random |
|---|---|---|---|---|---|
| Power Ranger | 2,250 | 36% | -73.5R | -76.3R | 51/99 (same as random) |
| Cupid | 70 | 43% | +2.0R | +7.4R | 28/99 |
| Tug-Of-War | 2,752 | 33% | -235.7R | +23.7R | 0/99 (worse than random) |
| Roller Coaster | 39 | 23% | -13.0R | -9.5R | 24/99 |
| ORB Desk A | 843 | 25% | -246.2R | -93.2R | 0/99 (worse than random) |
| ORB Desk B | 46 | 28% | -5.0R | -0.3R | 28/99 |

Power Ranger made +42R in 2022-23 and lost -113R in 2024-26. None has an edge.

## 2026-11-20: gap-drift test (pre-declared, `research_gap_drift.py`)

- Question: do gap stocks keep moving in the gap direction from 09:35 to 15:00?
- 23,360 trades: -0.04%/day before costs (zero), **-0.18%/day after costs and minus SPY (t -2.7)**,
  positive in 1 of 5 years. **FAIL.** Gap-ups fade slightly; gap-downs show no reliable move.
- Shorting gap-ups was checked on the research years only: +0.105%/day before costs (t 1.2),
  **-0.07%/day after costs**. Not worth the holdout; the holdout was kept locked.

## 2026-11-20: published "Stocks in Play" 5-minute ORB (pre-declared, `research_sip_orb.py`)

- Rules from Zarattini, Barbon & Aziz (2024), SSRN 4729284: price > $5, 14-day average volume
  >= 1M, 14-day ATR > $0.50, first-5-minute relative volume >= 100%, top 20; trade the break of
  the 5-minute high (up candle) or low (down candle); stop 10% of ATR; exit at the close;
  1% risk, max 4x leverage. The paper reports Sharpe 2.81, 2016-2023, commission only.
- Pass bar (fixed beforehand), on realistic costs ($0.005/share + 1 cent slippage per fill):
  mean R > 0 with t >= 2; positive from 2024 on; portfolio annual return > 0.
- RESULT: see the section added below when the run finished.

## 2026-11-20: live scanner bugs found and fixed

- **Previous close:** before the open, Longbridge `quote().prev_close` is the close of the session
  BEFORE yesterday. Every live gap had been measured against the wrong day (PDD showed +8.8%,
  real -1.8%). Fixed: the scanner uses `pre_market_quote.prev_close` (yesterday's close,
  split-adjusted).
- **Splits:** SNDK 8-for-1 on 2026-11-20 showed as a -88.6% gap. The scanner now detects a split
  (yesterday's raw close vs the split-adjusted one differ by >20%) and notes it on the list.
- **Funds:** Longbridge abbreviates iShares as "ISHRS" and Vanguard as "VG"; about 40 funds
  (EWT, EWJ, VOO...) were in the universe. Added to the name filter; universe now 5,842 stocks.
  The 5-year gap list was checked and had no funds.
- **Telegram:** the old token was incomplete and the chat id pointed at the bot itself. Fixed;
  alerts now go to the group **"DT_Iris Notification"**.

### Result of the "Stocks in Play" ORB test (finished 2026-11-20 22:36)

- 19,751 trades on 1,232 research days (25,280 selected stock-days; the rest never triggered).
- **Version B (realistic costs): -0.52R per trade, win 8%, t -23.8, negative every year,
  2024 onward -0.58R. Version A (paper costs): -0.33R. RESULT: FAIL** as pre-declared.
- 91% of trades hit the stop. Median stop distance $0.149; costs 0.20R per trade (B), 0.05R (A).
- **Caveat that decides it:** with a stop that close, the entry minute often also reaches the
  stop, and 1-minute bars cannot show the order. The pre-declared rule assumes the stop was hit
  (about half of all trades). Sensitivity on a 4,000-trade sample (description only, not a
  new pass bar):

  | Entry-minute assumption | A (paper costs) | B (realistic) |
  |---|---|---|
  | stop hit if the minute touches it (pre-declared) | -0.30R | -0.51R |
  | stop hit only if the minute closes beyond it | +0.29R | +0.11R |
  | stop ignored in the entry minute | +0.19R | -0.00R |

- Conclusion: FAIL as declared, but the true answer sits inside the entry minute. Resolving it
  needs tick (trade-by-trade) data for the entry minute of each trade. The holdout is still
  locked; nothing is traded.

### Tick check of the "Stocks in Play" ORB (`research_sip_orb_ticks.py`, finished 2026-11-21 ~00:00 SGT)

- The 10,662 entry minutes where 1-minute bars could not tell the order were replayed with Alpaca
  SIP ticks (regular trades only). In 93% of them the stop really was hit after the entry.
- **Version B (realistic costs): -0.17R per trade, t -5.7, annual -29%, negative every year;
  2024 onward -0.20R. Version A (paper costs): +0.01R, t 0.3, Sharpe 0.16 (paper: 2.81).**
- **RESULT: FAIL.** With exact entry minutes the strategy is break-even before slippage and loses
  after realistic costs. Together with the six course strategies and the gap-drift test, no
  intraday gap / opening-range edge was found in 5 years of data. Holdout still unused.

## 2026-11-20/21: first paper session (for learning, not evidence)

- `dt_runner.py --mode paper` ran the four course strategies on the 22-stock watchlist.
  BJ Tug-Of-War short -1.12R (-$20.40, stopped); ANT Tug-Of-War short +1.92R (+$64.36, target).
  Day +0.79R, +$43.96 after costs. The whole chain worked: watchlist, setup, entry, stop/target,
  journal, Telegram. One day says nothing about edge (Tug-Of-War: -236R over 5 years).
- Paper runs continue for fun / learning only.

## 2026-11-21: dip-buying in large uptrending stocks (pre-declared, `research_dip_buy.py`)

- Daily bars, top 500 stocks by 50-day dollar volume, raw price >= $10, close above its
  200-day average, RSI(2) < 10 at the close; buy next open; sell the open after the first close
  above the 5-day average (max 10 days). No stop. Costs $0.005/share + 1 cent per fill.
  Random control: random uptrend stock from the same day's top 500, same entry day and hold.
- Pass bar (fixed first): mean > 0 with t >= 2; beat >= 95/99 random; positive from 2024;
  10-slot portfolio return > 0. Research period: signals 2022-07-21 to 2026-09-23.
- **RESULT: PASS (all four).** 28,811 trades, win 64%, **+0.49% net per trade** (t 5.1),
  average hold 3.9 days, positive every year. Random uptrend stocks held the same days made
  +0.34% (range +0.28 to +0.42%): **beat 99/99**. Costs are only ~0.02% per trade.
- **But read it carefully:**
  - Most of the +0.49% is just being long stocks in a rising market; the dip selection adds
    about **+0.15% per trade** over random.
  - The 10-slot portfolio made **+7.6% a year, max drawdown -25%, Sharpe 0.45**. Holding SPY
    over the same days made **+18.8%, drawdown -19%, Sharpe 1.15**. As a stand-alone
    strategy it was WORSE than buying the index.
  - Trades overlap in time (3-4 day holds), so the t-stat is somewhat flattering.
  - The research years were a strong bull market for large caps.
- First rule set to show a real selection effect. Not yet a reason to trade it.
  Possible next steps (each needs its pass bar written first): the holdout check (40 sessions,
  once); a market-hedged version (long the dips, short SPY) that measures the edge on its own.

## 2026-11-23: relative strength / weakness and a "stocks in play" list (live scanner only)

User's point: a gap only means something against the market, and only Power Ranger (and Roller
Coaster) are gap setups; Cupid and Tug-Of-War need busy stocks, not gaps. Iris's notes agree:
Power Ranger = "quality gap **or** very good relative strength/weakness"; Cupid = gap is
"watchlist context, not the trigger" and asks "does it have relative strength or weakness?".

Built (`dt_rs.py`, `dt_scanner.py`, `dt_runner.py`, settings in `dt_config.py`):
- **relative gap = stock gap - beta x SPY gap**, beta from 60 daily returns, clamped 0.3-3.0.
  `GAP_BASIS = "relative"`: the gap list needs |relative gap| >= 4% and its sign sets the side
  (strong -> long, weak -> short). `"raw"` restores the old rule.
- **Sector**: the ETF (of 15: XLK, SMH, XLF, KRE, XLE, XLV, XBI, ...) whose daily returns
  correlated best with the stock over 60 days (>= 0.5), and the gap relative to it. Information only.
- **In-play list** for Cupid and Tug-Of-War: price >= $5, 20-day average volume >= 1M, premarket
  volume >= 50k shares and >= 5% of a normal day (`pm_rvol`), top 20 by pm_rvol, side = sign of
  the relative gap. The watchlist CSV has a `lists` column; the runner lets each list feed only
  its strategies (`LIST_STRATEGIES`).
- **Intraday RS**: the runner also follows SPY and adds "RS +x% vs SPY" (stock % since the open
  minus beta x SPY %) to every setup alert and signal. Information only, not a filter yet.

First look, 2026-11-23 08:31 ET, SPY +2.5% pre-market after -3.6% on Friday: 216 stocks with a
raw gap >= 4% dropped out (their gap was mostly the market); SKM (flat, -5.1% vs SPY) and HDB
joined the short side; in play: ZS, KYMR, CRWD, ETSY.

**NOT tested.** Every backtest above used raw gaps and the gap list for all strategies. Before
trusting the relative rules, write a pre-declared test on the Alpaca 5 years (relative-gap list
vs raw-gap list for Power Ranger; in-play list for Cupid / Tug-Of-War, which needs new minute
downloads for non-gap stocks), pass bar first.

## 2026-11-24: relative-strength and in-play lists tested (pre-declared, `research_rs.py`) - FAIL

Rules, variants, split and pass bar were written at the top of `research_rs.py` before any run.
Selection on TRAIN (2021-11 to 2024-06), judged once on VALIDATE (2024-07 to 2026-09).
Strategy rules and costs unchanged; only the stock list (and an optional intraday RS filter)
varied. New data: pre-market volume 04:00-09:00 for 2,297 liquid stocks (Alpaca hourly bars),
SPY 1-minute bars, and minutes for 54,051 list stock-days. Full numbers:
`results_alpaca/rs_test/report.json`, log `logs/rs_run.log`.

| Strategy | Chosen on train | Validate trades | Validate total R | Baseline (raw-4) validate | Beat random | Verdict |
|---|---|---|---|---|---|---|
| Power Ranger | rel gap >= 6%, RS filter off | 912 | -10.7R | -59.1R (1,145 trades) | 91/99 (need 95) | FAIL |
| Cupid | (no variant reached 100 train trades) raw-4 | 34 | +9.9R | same | 88/99 | FAIL (too few trades) |
| Tug-Of-War | raw-4 + RS filter | 1,157 | -52.5R | -156.8R (1,397 trades) | 0/99 | FAIL |

What it says (validate numbers are description only, not used to choose):
- **Relative gap helps Power Ranger, but not enough.** Every relative-gap variant lost less than
  its raw-gap twin from 5% up (rel-6 -10.7R vs raw-6 -34.4R; rel-5 -37.4R vs raw-5 -65.9R), and
  rel-6 beat 91 of 99 random entries. Still a loss after costs, so no edge to trade.
- **The intraday RS filter (trade only in the direction of RS vs SPY) roughly thirds
  Tug-Of-War's losses** (-52.5R vs -156.8R), but Tug-Of-War stays far worse than random entries
  on the same stocks: its entries are worse than chance.
- **Hint for a NEW test (not a result):** random entries in the RS direction on the RS-filtered
  Tug-Of-War days made +49.8R on average (1,157 trades, about +0.04R a trade). Trading WITH
  relative strength may carry a small edge on its own. Found after looking, so it must be a new
  pre-declared test before anyone trusts it.
- **In-play list (pre-market volume) did not help Tug-Of-War** (every inplay variant lost more per
  trade on train than raw-4). Cupid fires 0-36 times in 2.5 years on ANY list: its own setup rules
  (mainly the "<50% candle overlap") are the bottleneck, not the stock list.
- Holdout NOT used (nothing passed). As pre-declared, the live scanner keeps the user's settings
  (relative gap 4%, in-play 5%), marked unproven; the runner stays paper-only.

## 2026-11-24: RS continuation (trade with relative strength vs SPY) - FAIL

Follow-up to the hint above. Rules, 27 variants and pass bar written at the top of
`research_rs_lead.py` before any run; same data, split and costs as research_rs.py.
Rule: on raw-4 / rel-4 / inplay-5% lists, at 10:00, 10:30 or 11:00 take the list's side only if
RS since the open agrees by >= 0/1/2%; stop-entry 1 tick beyond that bar, stop at the day's
extreme (0.2-1.0 x ATR), target 2R, live limits (3 trades/day, 2 open), biggest |RS| first.
Random controls enter only AFTER the check time (no look-ahead). Full numbers:
`results_alpaca/rs_lead/report.json`, log `logs/rs_lead_run.log`.

- Chosen on train: raw-4, 10:00, RS >= 1% (train 1,162 trades, +7.7R, +0.007R/trade).
- Validate: 1,094 trades, **-58.8R** (-0.054R/trade); beat 5/99 random entries (random mean -34.7R).
  Checks: trades & profit FAIL, random FAIL, better than no-RS reference (-102.6R) PASS -> **FAIL**.
- Only 1 of 27 variants made money on validate (raw-4 11:00 RS>=2%: +4.5R, +0.004R/trade), and
  that one lost -72.1R on train. Nothing is positive on both periods.
- What holds up across all 27: requiring RS agreement **always loses less than not requiring it**
  (e.g. raw-4 10:30: -8.5R vs -102.4R). RS is a useful FILTER that removes bad trades, but
  trading with RS is not an edge on its own after costs.
- The earlier +0.04R hint was the look-ahead in the controls (they could enter before RS was known).
- Holdout NOT used. Nothing goes live; runner stays paper-only.

## 2026-11-25: live opening-candle bug found in the 24 Nov paper session (fixed)

The paper run on 24 Nov made 20 Power Ranger setups at 09:34; in 19 the stop sat at or just beyond the
PREVIOUS close (ADI short stop 378.87, real 09:30-09:34 high 337.57). The first candle pushed by
Longbridge reached back to the previous close; Longbridge's candle history for the same minutes is clean,
and the runner keeps the first copy of a minute. Effect: 10-20% stops, tiny sizes, unreachable targets,
both position slots blocked all day (the +0.31R paper result is not meaningful).
Fix: `dt_runner.verified_bar` - for the first 15 minutes every pushed candle is replaced by the history
candle for that minute (differences logged as `bar_check`); guard test added (25/25). Replay of 24 Nov on
clean bars: 10 setups instead of 20, same 2 trades (SNY +1.15R, ADI +0.43R). Cause of the bad push is
inferred (likely extended-hours trades folded into the first candle); tonight's `bar_check` lines will show.

## 2026-11-25: 09:00 list vs the open, and a scanner fault (fixed)

Compared the locked 09:00 lists (20, 23, 24 Nov) with the real gaps at the open (Longbridge daily bars):
- 50 of 68 picks (74%) still had a >= 4% gap in their direction at the open (20 Nov 12/22, 23 Nov 21/21,
  24 Nov 17/25 on the relative gap); median change 09:00 -> open 1.9 points. Failures were mostly borderline
  gaps (-4.0 to -4.9%) or thin names (TTI +12.1% -> +0.8%, CUE +8.1% -> +1.8%).
- Overlap with the open-based backtest list was only 8/22, 10/21, 12/25. Causes: (1) SCANNER FAULT -
  Longbridge's quote reply silently omitted 267 of 5,842 symbols on 23 Nov (AMD +12.4%, TSM +20.9%,
  DELL +35.7%, MRVL, TSLA, GOOGL, AMZN, META ...) and ~21 on 20 Nov; (2) almost no pre-market trading on the
  Nasdaq feed at 09:00 (DOCN, A, ONTO, TECK ...); (3) gaps that formed after 09:00 (SMTC, AEHR, ICHR);
  (4) the relative-gap rule (by design); (5) different top-20 ranking (pre-market volume vs dollar volume).
- Fix: `dt_scanner._quote_batch` re-asks for any symbol missing from a reply (2 retries) and logs what is
  still missing; guard test added (26/26). Live check: 5,815 rows, only 6 dotted share classes missing.
- Open question (not done): the backtests measure the gap at the open, the live list at 09:00. A 09:25
  refresh or a backtest on 09:00 data would close that gap.

## 2026-11-25: full code review of the live path - 5 fixes before the first lbpaper session

Reviewed broker, executor, runner, simulator, bars, scanner, store, recorder and config line by line.
Fixed (31 guard tests, all pass):
1. Runner robustness: any exception in the live loop ended the runner (and the locally watched stops).
   `ErrorGuard` now logs each failing step, alerts Telegram (at most once per 5 min per kind) and carries
   on; a crash outside the loop sends a "CRASHED - check positions" alert.
2. Live contexts had no daily ATR / 20-day volume, so Power Ranger's range-width check (0.05-1.5 x ATR)
   never ran live (and ORB could never fire). `daily_stats` fills both; values match gappers.csv exactly.
   24 Nov through the live path on clean bars: 6 Power Ranger setups instead of 20; same 2 trades.
3. The opening-candle check now waits once per minute (not per symbol) and paces history calls to ~8/s.
4. Early-close days (`EARLY_CLOSE_DAYS`: 2026-11-27, 2026-12-24): entries stop 11:45, flat 12:45, runner
   ends 12:50 - in setups, paper sim, order manager and runner. Before: a 15:00 flatten after a 13:00 close.
5. Order manager: partial exit fills now keep their P&L; a cancel is confirmed (final status) before the
   exit is sent, so a target filling during the cancel cannot close the position twice.
Not fixed (small): `run_live` saves the mode it ran in as the default; dotted share classes (BRK.B) are
never quoted; Power Ranger's opening range counts 5 bars, not 5 minutes, for stocks with an empty minute.
Correction: lbpaper sizes on min(ACCOUNT_SIZE, net assets) = $10,000, so about $100 risk a trade.
