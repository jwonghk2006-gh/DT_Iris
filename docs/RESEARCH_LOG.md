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
