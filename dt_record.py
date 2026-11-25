"""Nightly recorder: saves TODAY's gappers' 1-minute bars, without using the history quota.

    python dt_record.py              # run any time after 20:00 New York (08:00/09:00 Singapore next morning)
    python dt_record.py --dry-run    # only show today's gappers

Why: Longbridge's minute HISTORY is limited to 400 distinct symbols per quota period,
but `candlesticks()` (the latest 1,000 bars) is not limited, and 1,000 minutes cover
one whole day including pre- and post-market (04:00-20:00 = 960 minutes).

So every evening this script:
1. asks for quotes of the whole universe (batches of 400, quick) -> today's open and
   previous close -> today's gap %,
2. keeps the same gappers the live scanner and dt_gappers would (4%+, price, average
   volume from data/daily, top MAX_WATCHLIST per side),
3. saves their full day of minute bars to data/bars and appends them to data/gappers.csv.
The research sample then grows by up to ~40 symbol-days every trading day, for free.
Also saves SPY and QQQ, the benchmarks.
"""
from __future__ import annotations

import argparse
import time

import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_data as data
import dt_gappers as gap


def todays_gappers(day) -> pd.DataFrame:
    import dt_broker as broker
    ctx = broker.quote_context()
    syms = gap.tradable_symbols()
    rows = []
    for k in range(0, len(syms), 400):
        for q in ctx.quote([broker.lb_symbol(s) for s in syms[k:k + 400]]):
            if not q.open or not q.prev_close:
                continue
            if broker.to_utc(q.timestamp).tz_convert(cfg.TZ).date() != day:
                continue                                   # no trade today (halted, holiday)
            o, pc = float(q.open), float(q.prev_close)
            if o <= 0 or pc <= 0:
                continue
            rows.append({"symbol": broker.plain(q.symbol), "open": o, "prev_close": pc, "gap_pct": (o / pc - 1) * 100})
        time.sleep(gap.PAUSE)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["gap_pct"].abs() >= cfg.MIN_GAP_PCT].copy()
    df["side"] = df["gap_pct"].map(lambda g: "long" if g > 0 else "short")
    df = df[((df.side == "long") & (df.open >= cfg.LONG_MIN_PRICE)) | ((df.side == "short") & (df.open >= cfg.SHORT_MIN_PRICE))]
    out = []
    for r in df.itertuples():
        d = gap.load_daily(r.symbol)
        if d is None:
            gap.fetch_daily(r.symbol, day - pd.Timedelta(days=60), day)   # quota-free
            d = gap.load_daily(r.symbol)
        if d is None:
            continue
        f = gap.daily_features(d[d.index < day])
        if len(f) < 10:
            continue
        last = f.iloc[-1]
        vol = d[d.index < day]["volume"].tail(20)
        adv = float(vol.mean())
        if adv < cfg.MIN_AVG_DAILY_VOLUME:
            continue
        dv = float((d[d.index < day]["volume"] * d[d.index < day]["close"]).tail(20).mean())
        atr = float(bars.atr(d[d.index < day].assign(volume=0), cfg.DAILY_ATR_LEN).iloc[-1])
        g_adj = (r.open / last.close - 1) * 100          # vs split-adjusted close: a split is not a gap
        if abs(g_adj) < cfg.MIN_GAP_PCT or (g_adj > 0) != (r.side == "long"):
            continue
        out.append({"date": day, "symbol": r.symbol, "side": r.side, "gap_pct": round(g_adj, 2),
                    "open_adj": r.open, "prev_close_adj": last.close, "atr14_adj": atr, "adv20": adv,
                    "dollar_vol20": dv, "score": abs(r.gap_pct) * dv})
    g = pd.DataFrame(out)
    if g.empty:
        return g
    return (g.sort_values("score", ascending=False).groupby("side", group_keys=False).head(cfg.MAX_WATCHLIST)
             .reset_index(drop=True))


def save_today_minutes(sym: str, day) -> int:
    import dt_broker as broker
    from longport.openapi import AdjustType, Period, TradeSessions
    cs = broker.quote_context().candlesticks(broker.lb_symbol(sym), Period.Min_1, 1000, AdjustType.NoAdjust,
                                            TradeSessions.All)
    df = pd.DataFrame([{"time": broker.to_utc(c.timestamp), "open": float(c.open), "high": float(c.high),
                        "low": float(c.low), "close": float(c.close), "volume": float(c.volume)} for c in cs])
    if df.empty:
        return 0
    df = data.fix_bar_start(bars.normalize(df))
    df = df[df.index.tz_convert(cfg.TZ).date == day]
    if bars.regular(df).shape[0] < 300:
        return -len(df)                        # incomplete day: not saved
    old = data._path(sym, day)
    if old.exists() and len(pd.read_parquet(old)) >= len(df):
        return len(df)                         # a fuller copy is already saved (run late: the
                                               # 1,000-bar window has lost the early pre-market)
    data.save_day(sym, day, df)
    return len(df)


def main(dry: bool) -> None:
    import dt_broker as broker
    now = pd.Timestamp.now(tz=cfg.TZ)
    spy = broker.quote_context().quote(["SPY.US"])[0]
    day = broker.to_utc(spy.timestamp).tz_convert(cfg.TZ).date()     # the latest US session
    if day == now.date() and now.hour < 16:
        print("Today's US session is still open: run after 16:05 New York "
              "(04:05 / 05:05 Singapore next morning).")
        return
    if now.date() > day and now.weekday() < 5 and now.hour >= 4:
        print(f"Note: the next session's pre-market has started, so {day}'s early pre-market "
              "bars may be cut off. Best run between 16:05 New York and 04:00 the next day.")
    g = todays_gappers(day)
    print(f"{day}: {len(g)} gappers ({(g.side == 'long').sum() if len(g) else 0} long, "
          f"{(g.side == 'short').sum() if len(g) else 0} short)")
    if len(g):
        print(g[["symbol", "side", "gap_pct", "adv20"]].to_string(index=False))
    if dry or g.empty:
        return
    saved = 0
    for sym in list(g["symbol"]) + list(cfg.BENCHMARKS):
        try:
            n = save_today_minutes(sym, day)
            saved += n > 0
            if n <= 0:
                print(f"  {sym}: incomplete ({-n} bars), not saved")
        except Exception as exc:
            print(f"  {sym}: {type(exc).__name__}: {exc}")
        time.sleep(gap.PAUSE)
    old = gap.load_gappers()
    allg = pd.concat([old[old["date"] != day] if len(old) else old, g], ignore_index=True)
    allg.to_csv(gap.GAPPERS_FILE, index=False)
    print(f"saved {saved} symbol-days of minutes; gappers.csv now has {len(allg)} rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
