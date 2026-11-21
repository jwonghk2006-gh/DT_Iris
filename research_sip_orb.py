"""Published test: 5-minute ORB on "Stocks in Play" (Zarattini, Barbon & Aziz 2024,
"A Profitable Day Trading Strategy For The U.S. Equity Market", SSRN 4729284).

PRE-DECLARED on 2026-11-20, rules taken from the paper's text BEFORE any data was fetched.
Do not change them after seeing results; any variation is a new test, written down first.

Rules (paper, sections 2.1 and 4):
  Universe each day: opening price > $5; average volume over the previous 14 days >= 1,000,000
    shares; 14-day ATR > $0.50. (Our universe: Alpaca common stocks on main exchanges,
    including delisted ones - data_alpaca/universe_all.csv.)
  Relative volume = volume of the first 5 minutes (09:30-09:35) / average first-5-minute volume
    of the previous 14 days. Keep >= 100%, trade the top 20 by relative volume.
  Direction = the 5-minute candle: close > open -> buy stop at the 5-minute high;
    close < open -> sell stop at the 5-minute low; open == close (doji) -> no trade.
  Entry order works from 09:35 until the close (the paper gives no expiry).
  Stop loss 10% of the 14-day ATR from the executed entry price. No target. Exit at the close.
  Sizing: 1% of the capital allocated to the position lost at the stop, leverage at most 4x
    (so a very tight stop risks less than 1%). Capital is split over 20 slots.
  Paper's costs: $0.0035/share commission, no slippage mentioned.

Our implementation choices (fixed now):
  - ATR = Wilder 14-day on split-adjusted daily bars, scaled to that day's raw prices.
  - Fills: stop entry fills at max(trigger, bar open) (+ slippage); stop exit at min(stop, bar open)
    (- slippage); if entry and stop are inside the same 1-minute bar, the stop is assumed hit.
  - Exit at the close of the 15:59 bar.
  - Two cost versions:  A "paper"     = $0.0035/share, no slippage (replication)
                        B "realistic" = $0.005/share + 1 cent slippage on every fill (THE TEST)

PASS only if ALL hold for version B on the research period (before the locked holdout):
  1. mean R per trade > 0 and the t-stat of daily mean R >= 2.0
  2. mean R per trade > 0 from 2024-01-01 on (after the paper's 2016-2023 sample)
  3. the 20-slot portfolio's annualised return > 0
Version A, the years and the long/short split are printed for description only.

    python research_sip_orb.py              # download what is missing, then test
"""
from __future__ import annotations

import os

os.environ.setdefault("DT_DATA", "alpaca")

import json
import time

import numpy as np
import pandas as pd

import dt_alpaca as al
import dt_bars as bars
import dt_config as cfg
import dt_data as data
import dt_gappers as gap

OR_DIR = cfg.DATA_DIR / "or5"            # one parquet per day: first-5-minute bar of every candidate
FULL_DIR = cfg.DATA_DIR / "sip_bars"     # full-day minute bars of the selected stocks (kept apart from bars/)
SEL_FILE = cfg.DATA_DIR / "sip_selected.csv"
RES = cfg.RESULTS_DIR
MIN_PRICE, MIN_ADV, MIN_ATR, MIN_RVOL, TOP_N = 5.0, 1_000_000, 0.50, 1.0, 20
STOP_ATR, MAX_LEV, RISK = 0.10, 4.0, 0.01
COSTS = {"A_paper": (0.0035, 0.0), "B_realistic": (0.005, 0.01)}
PER_CALL = 300


# ------------------------------------------------------------------ daily features (known before the open)
def daily_table() -> pd.DataFrame:
    uni = pd.read_csv(gap.UNIVERSE_FILE, keep_default_na=False)
    frames = []
    for sym in uni[uni["keep"]]["symbol"]:
        d = gap.load_daily(sym)
        if d is None or len(d) < 20 or "raw_close" not in d:
            continue
        f = pd.DataFrame(index=pd.Index(d.index, name="date"))
        f["adv14"] = d["volume"].rolling(14).mean().shift(1)
        k = (d["raw_close"] / d["close"]).shift(1)                      # adjusted -> raw price units of yesterday
        atr = bars.atr(d[["open", "high", "low", "close"]].assign(volume=0), 14).shift(1)
        f["atr14"] = atr * k
        f["prev_close"] = d["raw_close"].shift(1)
        f["symbol"] = sym
        frames.append(f.dropna())
    t = pd.concat(frames).reset_index()
    t["cand"] = (t["adv14"] >= MIN_ADV) & (t["atr14"] > MIN_ATR) & (t["prev_close"] > MIN_PRICE * 0.8)
    return t


def or_fetch_plan(t: pd.DataFrame) -> dict:
    """{day: [symbols]}: candidates, plus the 20 sessions before a symbol's candidate days (for the average)."""
    t = t.sort_values(["symbol", "date"])
    ext = t.groupby("symbol")["cand"].transform(lambda s: s[::-1].astype(int).rolling(21, min_periods=1).max()[::-1])
    t = t[ext > 0]
    return {d: sorted(g["symbol"]) for d, g in t.groupby("date")}


def fetch_or(plan: dict) -> None:
    OR_DIR.mkdir(parents=True, exist_ok=True)
    todo = [d for d in sorted(plan) if not (OR_DIR / f"{d}.parquet").exists()]
    print(f"opening 5 minutes: {len(plan) - len(todo)} days on disk, {len(todo)} to fetch "
          f"(~{sum(len(plan[d]) for d in todo) / max(len(todo), 1):.0f} symbols a day)", flush=True)
    t0 = time.time()
    for n, d in enumerate(todo, 1):
        s0 = pd.Timestamp(f"{d} 09:30", tz=cfg.TZ)
        s1 = pd.Timestamp(f"{d} 09:34:59", tz=cfg.TZ)
        rows = []
        syms = plan[d]
        for k in range(0, len(syms), PER_CALL):
            got = al.bars_multi(syms[k:k + PER_CALL], "1Min", s0, s1, "raw")
            for sym, bs in got.items():
                if not bs:
                    continue
                b = pd.DataFrame(bs).sort_values("t")
                rows.append({"symbol": sym, "open": b["o"].iloc[0], "high": b["h"].max(), "low": b["l"].min(),
                             "close": b["c"].iloc[-1], "volume": b["v"].sum(), "first": b["t"].iloc[0]})
        pd.DataFrame(rows).to_parquet(OR_DIR / f"{d}.parquet")
        if n % 50 == 0 or n == len(todo):
            rate = n / (time.time() - t0)
            print(f"  or5: {n}/{len(todo)} days, ~{(len(todo) - n) / rate / 60:.0f} min left", flush=True)


# ------------------------------------------------------------------ selection
def select(t: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for p in sorted(OR_DIR.glob("*.parquet")):
        o = pd.read_parquet(p)
        if len(o):
            o["date"] = pd.Timestamp(p.stem).date()
            frames.append(o)
    o = pd.concat(frames).sort_values(["symbol", "date"])
    o["avg14"] = o.groupby("symbol")["volume"].transform(lambda v: v.rolling(14, min_periods=10).mean().shift(1))
    # the 14 previous days must be consecutive sessions of the market, not just of this symbol's rows: close enough,
    # because a symbol is fetched on every session of its candidate window
    o["rvol"] = o["volume"] / o["avg14"]
    o = o.merge(t[["date", "symbol", "adv14", "atr14", "cand"]], on=["date", "symbol"], how="inner")
    o = o[o["cand"] & (o["open"] > MIN_PRICE) & (o["rvol"] >= MIN_RVOL) & (o["close"] != o["open"])]
    o["side"] = np.where(o["close"] > o["open"], "long", "short")
    sel = o.sort_values("rvol", ascending=False).groupby("date").head(TOP_N).sort_values(["date", "rvol"],
                                                                                         ascending=[True, False])
    sel = sel[["date", "symbol", "side", "open", "high", "low", "close", "volume", "rvol", "adv14", "atr14"]]
    sel.to_csv(SEL_FILE, index=False)
    return sel


def _full_path(sym: str, d) -> "os.PathLike":
    return FULL_DIR / sym / f"{d}.parquet"


def fetch_full(sel: pd.DataFrame) -> None:
    need = sel[[not data._path(r.symbol, r.date).exists() and not _full_path(r.symbol, r.date).exists()
                for r in sel.itertuples()]]
    days = sorted(need["date"].unique())
    print(f"full-day bars: {len(sel) - len(need)} symbol-days on disk, {len(need)} to fetch on {len(days)} days", flush=True)
    t0 = time.time()
    for n, d in enumerate(days, 1):
        syms = sorted(need[need["date"] == d]["symbol"])
        s0 = pd.Timestamp(f"{d} 09:30", tz=cfg.TZ)
        s1 = pd.Timestamp(f"{d} 15:59:59", tz=cfg.TZ)
        got = al.bars_multi(syms, "1Min", s0, s1, "raw")
        for sym in syms:
            if got.get(sym):
                df = bars.normalize(al._to_frame(got[sym], daily=False))
                p = _full_path(sym, d)
                p.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(p)
        if n % 100 == 0 or n == len(days):
            rate = n / (time.time() - t0)
            print(f"  full: {n}/{len(days)} days, ~{(len(days) - n) / rate / 60:.0f} min left", flush=True)


def load_full(sym: str, d) -> pd.DataFrame | None:
    for p in (_full_path(sym, d), data._path(sym, d)):
        if p.exists():
            return pd.read_parquet(p)
    return None


# ------------------------------------------------------------------ simulation
def simulate(r, df: pd.DataFrame, comm: float, slip: float) -> dict | None:
    t = df.index.tz_convert(cfg.TZ)
    hhmm = t.hour * 100 + t.minute
    df = df[(hhmm >= 935) & (hhmm <= 1559)]
    if df.empty:
        return None
    sg = 1 if r.side == "long" else -1
    trig = r.high if sg > 0 else r.low
    dist = STOP_ATR * r.atr14
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    entry = stop = None
    exit_px, why = None, "close"
    for i in range(len(df)):
        if entry is None:
            if (h[i] >= trig) if sg > 0 else (l[i] <= trig):
                entry = (max(trig, o[i]) if sg > 0 else min(trig, o[i])) + sg * slip
                stop = entry - sg * dist
                if (l[i] <= stop) if sg > 0 else (h[i] >= stop):          # entry and stop in one bar: assume stopped
                    exit_px, why = stop - sg * slip, "stop"
                    break
            continue
        if (l[i] <= stop) if sg > 0 else (h[i] >= stop):
            exit_px, why = (min(stop, o[i]) if sg > 0 else max(stop, o[i])) - sg * slip, "stop"
            break
    if entry is None:
        return None
    if exit_px is None:
        exit_px = c[-1] - sg * slip
    r_mult = (sg * (exit_px - entry) - 2 * comm) / dist
    risk_frac = dist / entry
    ret_alloc = r_mult * min(RISK, MAX_LEV * risk_frac)               # return on the capital of this slot
    return {"date": r.date, "symbol": r.symbol, "side": r.side, "entry": entry, "exit": exit_px, "why": why,
            "r": r_mult, "ret_alloc": ret_alloc, "rvol": r.rvol, "price": entry, "stop_dist": dist,
            "cost_r": (2 * comm + 2 * slip) / dist}


def tstat(x: pd.Series) -> float:
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else float("nan")


def report(tr: pd.DataFrame, label: str, all_days: list) -> dict:
    daily_r = tr.groupby("date")["r"].mean()
    port = tr.groupby("date")["ret_alloc"].sum().div(TOP_N).reindex(all_days, fill_value=0.0)
    yrs = len(port) / 252
    ann = float((1 + port).prod() ** (1 / yrs) - 1) if yrs > 0 else float("nan")
    sharpe = float(port.mean() / port.std() * np.sqrt(252)) if port.std() > 0 else float("nan")
    eq = (1 + port).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    out = {"trades": len(tr), "win_%": round((tr.r > 0).mean() * 100, 1), "mean_R": round(tr.r.mean(), 4),
           "t_days": round(tstat(daily_r), 2), "ann_return_%": round(ann * 100, 1), "sharpe": round(sharpe, 2),
           "max_dd_%": round(mdd * 100, 1)}
    print(f"{label:28s} {out}")
    return out


def main() -> None:
    cut = pd.Timestamp(json.loads((cfg.DATA_DIR / "manifest.json").read_text())["holdout_start"]).date()
    print("daily features ...", flush=True)
    t = daily_table()
    plan = or_fetch_plan(t)
    fetch_or(plan)
    sel = select(t)
    print(f"selected: {len(sel)} symbol-days on {sel['date'].nunique()} days "
          f"({(sel.side == 'long').sum()} long, {(sel.side == 'short').sum()} short)", flush=True)
    fetch_full(sel)

    research = sel[sel["date"] < cut]                                 # the holdout is not simulated
    all_days = sorted(d for d in plan if d < cut and d >= min(research["date"]))
    results = {}
    for name, (comm, slip) in COSTS.items():
        rows = []
        for r in research.itertuples():
            df = load_full(r.symbol, r.date)
            if df is not None:
                x = simulate(r, df, comm, slip)
                if x:
                    rows.append(x)
        results[name] = pd.DataFrame(rows)
        results[name].to_csv(RES / f"sip_orb_trades_{name}.csv", index=False)

    print(f"\nresearch period {min(all_days)} .. {max(all_days)}  (holdout from {cut} untouched)")
    print("\n=== all years ===")
    summ = {n: report(tr, n, all_days) for n, tr in results.items()}
    B = results["B_realistic"]
    B_y = pd.to_datetime(B["date"])
    print("\n=== version B by year ===")
    by_year = {}
    for y in sorted(B_y.dt.year.unique()):
        by_year[y] = report(B[B_y.dt.year == y], f"B {y}", [d for d in all_days if d.year == y])
    print("\n=== version A by year (paper costs) ===")
    A = results["A_paper"]
    A_y = pd.to_datetime(A["date"])
    for y in sorted(A_y.dt.year.unique()):
        report(A[A_y.dt.year == y], f"A {y}", [d for d in all_days if d.year == y])
    print("\n=== version B by side ===")
    for sd in ("long", "short"):
        report(B[B.side == sd], f"B {sd}", all_days)
    post = B[B_y >= pd.Timestamp("2024-01-01")]
    post_days = [d for d in all_days if d >= pd.Timestamp("2024-01-01").date()]
    print()
    post_s = report(post, "B 2024 onward", post_days)
    s = summ["B_realistic"]
    c1 = s["mean_R"] > 0 and s["t_days"] >= 2.0
    c2 = post_s["mean_R"] > 0
    c3 = s["ann_return_%"] > 0
    print(f"\nPASS CHECK (version B): 1) mean R>0 and t>=2: {c1}   2) 2024+ mean R>0: {c2}   3) portfolio return>0: {c3}")
    print("RESULT:", "PASS" if c1 and c2 and c3 else "FAIL")
    stop_share = (B["why"] == "stop").mean()
    print(f"\nversion B: stopped out on {stop_share:.0%} of trades; median stop distance ${B['stop_dist'].median():.3f}; "
          f"median cost per trade {B['cost_r'].median():.2f}R (version A: {A['cost_r'].median():.2f}R)")


if __name__ == "__main__":
    main()
