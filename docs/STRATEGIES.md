# DT_Iris strategies

Plain-language version of `dt_config.STRATEGIES` and `dt_strategies.py`. Every number is
a first draft taken from the course notes and the June 2026 review: a hypothesis to test.

**Common to all four**
- Long watch: price at least $5 (course notes say $1; raised 2026-11-20). Short watch: price at least $25.
- New entries only before 12:00 ET. Everything closed at 15:00 ET. Nothing held overnight.
- Entry is a stop-limit just beyond the trigger bar; stop is a stop order; target is a limit.
- R = entry minus stop. Risk per trade = 1% of the account (course notes). At most 3 trades
  and 2 open positions a day; no new trades after -3R in a day.
- Stop moves to break-even at +1R.
- Short rules are exactly the long rules turned upside down.

## Power Ranger: opening range breakout (1-minute bars)

Buy (Green Power Ranger):
1. Quality gap up: premarket change at least +4%.
2. Opening range = first 5 minutes.
3. Skip if a bar in the range breaks below the FIRST bar's low (review: quality drops).
   Optionally (`require_failed_counter_bar`) insist on the "perfect" version: an early red
   bar that fails to go lower.
4. Skip if the range is wider than 1.5 or narrower than 0.05 daily ATR (when daily ATR is known).
5. Entry: 1 tick above the range high (HOD). Stop: 1 tick below the range low (LOD), or VWAP
   with `stop_mode = "vwap"`. Target 2R. The order waits 30 minutes, then is cancelled.

## Cupid: pullback to the 9 EMA / VWAP (2-minute bars)

Buy:
1. Uptrend: the trigger bar closes above the 9 EMA and above VWAP, and the EMA is rising.
2. A move up of at least 2 intraday ATR to a swing high.
3. Then a sequential pullback of 2 to 8 bars: no higher highs, each bar overlapping the
   previous one by less than 50% (smooth).
4. Pullback depth 20% to 50% of the move (review: deeper than 50% was a false signal).
5. The pullback touches or comes within 0.3 ATR of the 9 EMA or VWAP.
6. Trigger bar: green, body at least 0.15 ATR (review: tiny weak bars should not trigger),
   volume at least 1x average.
7. Entry 1 tick above the trigger bar. Stop 1 tick below the pullback low.
   Target 1.5R, or 2R when trigger volume is at least 2x average.

## Tug-Of-War: chop on the 9 EMA, then the break (2-minute bars)

From the notes: "Follow the 9EMA line, once it breaks up, buy."
1. A box of 6 to 20 bars no taller than 1.2 intraday ATR.
2. Price crossed the 9 EMA at least twice inside the box, and the EMA is nearly flat.
3. A green bar closes above the box high and above the EMA.
4. Entry 1 tick above that bar. Stop 1 tick below the box low. Target 2R.

## Roller Coaster: waterfall reversal (2-minute bars, counter-trend)

Buy:
1. A waterfall over the last 8 bars: at least 3 wide red bars (range at least 1.8x ATR).
2. Extended: the drop covers at least 4 ATR and price is at least 2 ATR below VWAP.
3. Reversal bar at the bottom: a narrow-body green bar (body at most 35% of the range) or a
   long lower tail (at least 50% of the range).
4. Entry 1 tick above the reversal bar. Stop 1 tick below it.
5. Target: 2R, but never beyond the 50% retracement of the waterfall; skip if that leaves
   less than 1R.
6. Up to 2 entries per side per day ("usually whippy and requires a 2nd entry").

## What is not coded yet (manual judgement in the course)

- Gap "shock value": gapping over wide red bars, over multiple pivots, into a void.
- Room to the next resistance or support (at least 2R).
- Relative strength or weakness against SPY and QQQ.
- 45-degree pullback angle.
These matter in the course and are the next things to add.
