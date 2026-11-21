"""Gap-drift test: do gap stocks keep moving in the gap's direction during the day?

PRE-DECLARED on 2026-11-20, before any result was looked at. Do not change these rules
after seeing the output; any new idea is a NEW test, written down first.

Sample   : every symbol-day in data_alpaca/gappers.csv (gap >= 4%, long price >= $5,
           short price >= $25, 20-day avg volume >= 500k, top 20 per side per day),
           research period only: days before the locked holdout (manifest holdout_start).
Rule     : trade in the gap direction (buy gap-ups, short gap-downs).
           Entry = open of the 09:35 bar (after the first 5 minutes).
           Exit  = close of the 14:59 bar (the 15:00 flatten). No stop, no target.
Costs    : 1 cent slippage on entry and on exit, plus $0.005/share commission each way,
           expressed as a % of the entry price.
Market   : SPY's return over the same 09:35-15:00 window is subtracted (in the trade's
           direction), so a bull or bear market is not mistaken for drift.
Statistic: average the trades of each day first (gappers on one day move together),
           then a t-statistic across days.

PASS only if ALL of these hold for the primary rule (both sides together, net of costs,
market-adjusted):
  1. mean daily return > 0 with t-stat >= 2.0
  2. positive in at least 4 of the 5 years 2022-2026 (2021 has only 6 weeks)
  3. positive for longs and for shorts separately (so it is not just one side riding the market)
Everything else printed (09:31 / 10:00 entries, gap-size and price buckets) is description
only and must not be used to pick a "best" version.

    python research_gap_drift.py
"""
from __future__ import annotations

import os

os.environ.setdefault("DT_DATA", "alpaca")

import json

import numpy as np
import pandas as pd

import dt_config as cfg
import dt_data as data
import dt_gappers as gap

SLIP, COMM = 0.01, 0.005
ENTRIES = {"09:31": "09:31", "09:35": "09:35", "10:00": "10:00"}   # 09:35 is the primary
PRIMARY = "09:35"
EXIT = "14:59"


def minute_prices(df: pd.DataFrame) -> pd.Series:
    t = df.index.tz_convert(cfg.TZ)
    return pd.Series(df["open"].values, index=t.strftime("%H:%M")), pd.Series(df["close"].values, index=t.strftime("%H:%M"))


def entry_exit(df: pd.DataFrame, entry: str) -> tuple[float, float] | None:
    opens, closes = minute_prices(df)
    hhmm = opens.index
    e = [k for k in hhmm if entry <= k <= _plus(entry, 5)]          # first bar within 5 minutes of the entry time
    x = [k for k in hhmm if _plus(EXIT, -10) <= k <= EXIT]          # last bar within 10 minutes before the exit
    if not e or not x:
        return None
    return float(opens[e[0]]), float(closes[x[-1]])


def _plus(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def spy_returns(days: list) -> dict:
    """{(day, entry): SPY return entry->exit}. Fetched from Alpaca once, cached in data_alpaca/spy_windows.csv."""
    path = cfg.DATA_DIR / "spy_windows.csv"
    if path.exists():
        s = pd.read_csv(path)
        have = set(s["date"])
        if all(str(d) in have for d in days):
            return {(r.date, r.entry): r.ret for r in s.itertuples()}
    import dt_alpaca as al
    rows = []
    start = pd.Timestamp(min(days), tz=cfg.TZ)
    end = pd.Timestamp(max(days), tz=cfg.TZ) + pd.Timedelta(days=1)
    print("fetching SPY 1-minute bars from Alpaca ...", flush=True)
    got = al.bars_multi(["SPY"], "1Min", start, end, "raw").get("SPY", [])
    df = al._to_frame(got, daily=False).set_index("time")
    for d, g in df.groupby(df.index.tz_convert(cfg.TZ).date):
        for name, et in ENTRIES.items():
            ex = entry_exit(g, et)
            if ex:
                rows.append({"date": str(d), "entry": name, "ret": ex[1] / ex[0] - 1})
    s = pd.DataFrame(rows)
    s.to_csv(path, index=False)
    return {(r.date, r.entry): r.ret for r in s.itertuples()}


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else float("nan")


def summary(t: pd.DataFrame, col: str) -> dict:
    daily = t.groupby("date")[col].mean()
    return {"trades": len(t), "days": len(daily), "mean_trade_%": round(t[col].mean() * 100, 3),
            "mean_day_%": round(daily.mean() * 100, 3), "t_days": round(tstat(daily), 2),
            "win_%": round((t[col] > 0).mean() * 100, 1)}


def main() -> None:
    m = json.loads((cfg.DATA_DIR / "manifest.json").read_text())
    cut = pd.Timestamp(m["holdout_start"]).date()
    g = gap.load_gappers()
    g = g[g["date"] < cut].copy()
    print(f"research sample: {len(g)} gap symbol-days, {g['date'].min()} .. {g['date'].max()} (holdout from {cut} untouched)")
    rows = []
    for r in g.itertuples():
        p = data._path(r.symbol, r.date)
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        sg = 1 if r.side == "long" else -1
        for name, et in ENTRIES.items():
            ex = entry_exit(df, et)
            if not ex:
                continue
            e, x = ex
            gross = sg * (x / e - 1)
            cost = (2 * SLIP + 2 * COMM) / e
            rows.append({"date": str(r.date), "year": r.date.year, "symbol": r.symbol, "side": r.side, "entry": name,
                         "gap_pct": r.gap_pct, "price": e, "gross": gross, "net": gross - cost})
    t = pd.DataFrame(rows)
    spy = spy_returns(sorted(g["date"].unique()))
    sg = np.where(t["side"] == "long", 1, -1)
    t["spy"] = [spy.get((d, e), np.nan) for d, e in zip(t["date"], t["entry"])]
    t["net_mkt"] = t["net"] - sg * t["spy"]
    t["gross_mkt"] = t["gross"] - sg * t["spy"]
    t.to_csv(cfg.RESULTS_DIR.mkdir(parents=True, exist_ok=True) or cfg.RESULTS_DIR / "gap_drift_trades.csv", index=False)

    p = t[t["entry"] == PRIMARY]
    print("\n=== PRIMARY: entry 09:35 open, exit 15:00, gap direction ===")
    for col, label in (("gross", "before costs"), ("net", "after costs"), ("gross_mkt", "before costs, minus SPY"),
                       ("net_mkt", "after costs, minus SPY  <- the test")):
        print(f"{label:38s} {summary(p, col)}")
    print("\nby year (after costs, minus SPY):")
    yrs = {y: summary(gy, "net_mkt") for y, gy in p.groupby("year")}
    for y, s in yrs.items():
        print(f"  {y}: {s}")
    print("\nby side (after costs, minus SPY):")
    sides = {sd: summary(gs, "net_mkt") for sd, gs in p.groupby("side")}
    for sd, s in sides.items():
        print(f"  {sd:5s}: {s}")

    overall = summary(p, "net_mkt")
    full_years = [y for y in yrs if y >= 2022]
    pos_years = sum(yrs[y]["mean_day_%"] > 0 for y in full_years)
    c1 = overall["mean_day_%"] > 0 and overall["t_days"] >= 2.0
    c2 = pos_years >= 4
    c3 = all(sides.get(sd, {"mean_day_%": -1})["mean_day_%"] > 0 for sd in ("long", "short"))
    print(f"\nPASS CHECK: 1) mean>0 and t>=2: {c1}   2) positive years {pos_years}/{len(full_years)} (need 4): {c2}"
          f"   3) longs and shorts both positive: {c3}")
    print("RESULT:", "PASS" if c1 and c2 and c3 else "FAIL")

    print("\n--- description only (not used to pick a version) ---")
    for name in ENTRIES:
        if name != PRIMARY:
            print(f"entry {name}: net minus SPY {summary(t[t.entry == name], 'net_mkt')}")
    p = p.assign(gap_bucket=pd.cut(p["gap_pct"].abs(), [4, 6, 10, 20, 1000], right=False),
                 price_bucket=pd.cut(p["price"], [0, 10, 20, 50, 1e9], right=False))
    print("by gap size:")
    for b, gb in p.groupby("gap_bucket", observed=True):
        print(f"  {str(b):12s} {summary(gb, 'net_mkt')}")
    print("by entry price:")
    for b, gb in p.groupby("price_bucket", observed=True):
        print(f"  {str(b):14s} {summary(gb, 'net_mkt')}")


if __name__ == "__main__":
    main()
