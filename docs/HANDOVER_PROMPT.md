# Hand-over prompt for a new AI session

Copy everything inside the box into a new Claude (or other AI) session opened in your
DT_Iris folder (e.g. `Documents\DT_Iris`). Update the "Where it stands" part when things change.

```
You are continuing work on DT_Iris, my intraday US-stock trading research system in
the DT_Iris folder this session is opened in (Windows, Python venv in .venv, Windows PowerShell 5.1:
no "&&", one command per line). I am based in Singapore; US market hours are 21:30-04:00 or
22:30-05:00 Singapore. You are not a financial adviser; I want honest, data-based views.

Read these first, in order:
1. docs/RESEARCH_LOG.md  - every test so far and its result
2. README.md             - files and design
3. USER_MANUAL.md        - daily routine; section 7 = commands, checks, common problems
4. docs/STRATEGIES.md    - the strategy rules in words
5. dt_config.py          - every setting

Where it stands (2026-11-21):
- Broker/data: Longbridge OpenAPI (longport 4.3.7) for live quotes and orders; Alpaca free
  plan for 5-year research data (data_alpaca/, consolidated volume, includes delisted stocks).
- The pre-market scanner runs automatically: Windows task "DT_Iris pre-market scan",
  Mon-Fri 20:45 Singapore, waits until 09:00 ET, locks data/watchlists/<date>.csv and posts to
  the Telegram group "DT_Iris Notification". Log: logs/scan.log.
- Six strategies (Power Ranger, Cupid, Tug-Of-War, Roller Coaster, ORB Desk A/B) all failed
  to beat random entries over 5 years. A pre-declared gap-drift test failed. The published
  "Stocks in Play" 5-minute ORB failed, also with tick data (break-even before costs, -0.17R
  after). Conclusion: no intraday gap / opening-range edge in 5 years.
- research_dip_buy.py (daily RSI(2) dips in top-500 uptrending stocks, 1-10 day holds) PASSED
  its pre-declared bar: +0.49%/trade net, beat 99/99 random. BUT the 10-slot portfolio made
  +7.6%/yr (Sharpe 0.45) vs SPY +18.8% (Sharpe 1.15): worse than holding the index. Next
  candidates: holdout check (pass bar first) and a SPY-hedged version.
- 2026-11-23: the scanner now makes a GAP list on the gap RELATIVE to SPY (stock gap - beta x SPY
  gap; Power Ranger, Roller Coaster) and an IN-PLAY list on pre-market relative volume (Cupid,
  Tug-Of-War); the runner adds intraday RS vs SPY to alerts. Tested 2026-11-24 (research_rs.py):
  FAIL for all three; relative gap and the RS filter reduce losses but leave no edge.
  Follow-up research_rs_lead.py (enter WITH RS at 10:00-11:00) also FAIL: -58.8R validate, 5/99.
- Paper trading runs for fun/learning only (first day 2026-11-20: 2 trades, +0.79R).
- The runner (dt_runner.py) is NOT scheduled and must not trade real money. Live order path
  never tested on the Longbridge demo account. Known risks: stop watched locally (no broker
  stop if the PC dies); dt_executor._exit() does not wait for the target-cancel confirmation.
- Holdout: data_alpaca/manifest.json, from 2026-09-25 (40 sessions), NEVER used. Use it
  once only, for a rule set that already passed, with the pass bar written first.

Rules for working with me:
- Write any new test's rules and pass bar at the top of its script BEFORE running it; run it
  once; do not tune until it passes (I lost money with an over-fitted grid-search system, AQS).
- A result counts only if it beats the random-entry noise check and survives realistic costs
  (1 cent slippage per fill + $0.005/share commission).
- Run test_dt.py after code changes (31 tests). Never print or copy secrets from .env.
- Do not place orders or start the runner in lbpaper/live mode without my explicit OK.
- Longbridge quirk: before the open, quote().prev_close is TWO sessions old; use
  pre_market_quote.prev_close (already split-adjusted). The scanner does this since 2026-11-20.

My next question is:
```
