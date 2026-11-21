"""Dip-buying in large, uptrending stocks (daily bars): pre-declared test.

PRE-DECLARED on 2026-11-21, before any run. Rules, costs and pass bar below are fixed; the
script is run once on the research period. The holdout (data_alpaca/manifest.json,
from 2026-09-25) is cut off before anything is computed and is NOT used.

Idea: a classic short-term mean-reversion setup (Connors-style RSI(2) pullback). Unlike the
intraday tests, it holds for days and the moves are several %, so 1-2 cents of costs are small.

Data: Alpaca split-adjusted daily bars (data_alpaca/daily, Oct 2021 on, includes stocks that
were later delisted). Dividends are not added (small bias against the strategy and the random
controls alike).

Universe, each day t (only data up to t is used):
  - raw (unadjusted) close >= $10 and at least 200 days of history
  - top 500 by 50-day average dollar volume (close x volume)
Signal at the close of day t:
  - close > 200-day simple average (uptrend)
  - RSI(2) < 10 (Wilder smoothing, alpha = 1/2)
Trade:
  - buy at the open of day t+1
  - sell at the open of the day after the first close above the 5-day simple average
    (checked from day t+1 on); if none by the close of day t+10, sell at the open of day t+11
  - no stop. If the stock's data ends first, sell at its last close.
  - costs: $0.005/share commission + 1 cent slippage per fill, on the raw price
    (round trip = 2 x 0.015 / raw entry price)
Random control (99 runs): for every real trade, a random stock from the same day's universe that
is also above its 200-day average (no RSI condition), bought at the same open and held the same
number of days. This keeps market timing and exposure equal and tests only the dip selection.
Portfolio: 10 slots, 10% of equity each at entry, lowest RSI(2) first when signals exceed free
slots, one position per stock.

PASS only if ALL hold on the research period (signals from the first day with 200 days of
history up to 2026-09-24):
  1. mean net return per trade > 0 and t-stat of the daily mean >= 2.0 (trades grouped by entry day)
  2. mean net return per trade beats at least 95 of the 99 random controls
  3. mean net return per trade > 0 for trades entered from 2024-01-01 on
  4. the 10-slot portfolio's annualised return > 0
SPY buy-and-hold over the same days is shown for comparison only (not a pass condition).

    python research_dip_buy.py
"""
from __future__ import annotations

import os

os.environ.setdefault("DT_DATA", "alpaca")

import json

import numpy as np
import pandas as pd

import dt_alpaca as al
import dt_config as cfg

DAILY = cfg.DATA_DIR / "daily"
RES = cfg.RESULTS_DIR
TOP_N, MIN_PRICE, RSI_MAX, MAX_HOLD, SLOTS = 500, 10.0, 10.0, 10, 10
COMM, SLIP = 0.005, 0.01
N_CONTROLS = 99
SEED = 7


def load_panel(cut: pd.Timestamp) -> dict[str, pd.DataFrame]:
    keep = set(pd.read_csv(cfg.DATA_DIR / "universe_all.csv").query("keep == True")["symbol"])
    cols = {k: {} for k in ("open", "close", "volume", "raw_open", "raw_close")}
    for p in sorted(DAILY.glob("*.parquet")):
        sym = p.stem
        if sym not in keep:
            continue
        d = pd.read_parquet(p)
        d.index = pd.to_datetime(d.index)
        d = d[d.index < cut]
        if len(d) < 200 or "raw_close" not in d:
            continue
        for k in cols:
            cols[k][sym] = d[k]
    return {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}


def rsi2(close: pd.DataFrame) -> pd.DataFrame:
    ch = close.diff()
    up = ch.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-ch.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def spy_daily(start, end) -> pd.Series:
    p = cfg.DATA_DIR / "spy_daily.parquet"
    if not p.exists():
        rows = al.bars_multi(["SPY"], "1Day", pd.Timestamp("2021-10-01", tz=cfg.TZ),
                             pd.Timestamp.now(tz=cfg.TZ).normalize() - pd.Timedelta(days=1), "all")["SPY"]
        df = al._to_frame(rows, daily=True)
        df.to_parquet(p)
    s = pd.read_parquet(p)["close"]
    s.index = pd.to_datetime(s.index)
    return s[(s.index >= start) & (s.index <= end)]


def day_t(daily_means: pd.Series) -> float:
    return float(daily_means.mean() / (daily_means.std(ddof=1) / np.sqrt(len(daily_means)))) if len(daily_means) > 2 else 0.0


def main() -> None:
    cut = pd.Timestamp(json.loads((cfg.DATA_DIR / "manifest.json").read_text())["holdout_start"])
    P = load_panel(cut)
    close, opn, vol, rclose, ropen = P["close"], P["open"], P["volume"], P["raw_close"], P["raw_open"]
    dates, syms = close.index, close.columns
    print(f"panel: {len(syms)} stocks x {len(dates)} days, {dates[0]:%Y-%m-%d} .. {dates[-1]:%Y-%m-%d} "
          f"(holdout from {cut:%Y-%m-%d} cut off)", flush=True)

    sma200 = close.rolling(200, min_periods=200).mean()
    sma5 = close.rolling(5, min_periods=5).mean()
    dv50 = (close * vol).rolling(50, min_periods=50).mean()
    elig = (rclose >= MIN_PRICE) & sma200.notna() & dv50.notna()
    rank = dv50.where(elig).rank(axis=1, ascending=False)
    univ = (rank <= TOP_N) & (close > sma200)                  # universe AND uptrend
    r2 = rsi2(close)
    sig = univ & (r2 < RSI_MAX)

    C, O, RO, S5 = close.to_numpy(), opn.to_numpy(), ropen.to_numpy(), sma5.to_numpy()
    R2 = r2.to_numpy()
    nD = len(dates)

    def last_valid(j: int, frm: int) -> int:
        k = frm
        while k > 0 and np.isnan(C[k, j]):
            k -= 1
        return k

    trades = []
    for t, j in zip(*np.nonzero(sig.to_numpy())):
        e = t + 1
        if e >= nD or np.isnan(O[e, j]) or np.isnan(RO[e, j]):
            continue
        x_idx, how = None, "time"
        for k in range(e, min(t + MAX_HOLD, nD - 1) + 1):
            if np.isnan(C[k, j]):                              # data ended (delisted / halted)
                k2 = last_valid(j, k)
                x_idx, how = k2, "data_end"
                break
            if not np.isnan(S5[k, j]) and C[k, j] > S5[k, j]:
                x_idx, how = k + 1, "sma5"
                break
        if x_idx is None:
            x_idx = t + MAX_HOLD + 1
        if how == "data_end":
            px = C[x_idx, j]
        elif x_idx >= nD:                                      # research period ends: last close
            x_idx = nD - 1
            px, how = C[last_valid(j, x_idx), j], "period_end"
        elif np.isnan(O[x_idx, j]):
            px, how = C[last_valid(j, x_idx - 1), j], "data_end"
        else:
            px = O[x_idx, j]
        cost = 2 * (COMM + SLIP) / RO[e, j]
        trades.append({"signal_date": dates[t], "entry_date": dates[e], "exit_date": dates[x_idx], "symbol": syms[j],
                       "t": t, "j": j, "e": e, "x": x_idx, "rsi2": R2[t, j], "entry": O[e, j], "exit": px, "how": how,
                       "gross": px / O[e, j] - 1, "cost": cost})
    T = pd.DataFrame(trades)
    T["net"] = T["gross"] - T["cost"]
    T["hold"] = T["x"] - T["e"]
    RES.mkdir(parents=True, exist_ok=True)
    T.drop(columns=["t", "j", "e", "x"]).to_csv(RES / "dip_buy_trades.csv", index=False)

    # ---- random controls
    rng = np.random.default_rng(SEED)
    U = univ.to_numpy()
    cand = {t: np.nonzero(U[t])[0] for t in np.unique(T["t"])}
    ctrl_means = []
    for _ in range(N_CONTROLS):
        rets = []
        for r in T.itertuples():
            c = cand[r.t]
            j = c[rng.integers(len(c))]
            e, x = r.e, min(r.e + r.hold, nD - 1)
            if np.isnan(O[e, j]) or np.isnan(RO[e, j]):
                continue
            px = O[x, j] if not np.isnan(O[x, j]) else C[last_valid(j, x), j]
            rets.append(px / O[e, j] - 1 - 2 * (COMM + SLIP) / RO[e, j])
        ctrl_means.append(np.nanmean(rets))
    ctrl_means = np.array(ctrl_means)

    # ---- portfolio (10 slots)
    by_entry = {e: g.sort_values("rsi2") for e, g in T.groupby("e")}
    equity, cash, pos, curve = 1.0, 1.0, {}, []
    first = int(T["e"].min())
    for d in range(first, nD):
        for j in [j for j, p in pos.items() if p["x"] <= d]:   # exits at the open
            p = pos.pop(j)
            cash += p["sh"] * p["px"] - p["sh"] * p["px"] * (COMM + SLIP) / p["rpx"]
        if d in by_entry:
            for r in by_entry[d].itertuples():
                if len(pos) >= SLOTS or r.j in pos:
                    continue
                alloc = equity / SLOTS
                if alloc > cash:
                    break
                sh = alloc / r.entry
                cash -= alloc + alloc * (COMM + SLIP) / RO[d, r.j]
                pos[r.j] = {"sh": sh, "x": r.x, "px": r.exit, "rpx": RO[r.x, r.j] if not np.isnan(RO[r.x, r.j]) else RO[d, r.j]}
        mark = sum(p["sh"] * (C[d, j] if not np.isnan(C[d, j]) else p["px"]) for j, p in pos.items())
        equity = cash + mark
        curve.append((dates[d], equity))
    for j, p in pos.items():
        cash += p["sh"] * p["px"]
    eq = pd.Series(dict(curve))
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    dr = eq.pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(252)
    eq.to_csv(RES / "dip_buy_equity.csv", header=["equity"])
    spy = spy_daily(eq.index[0], eq.index[-1])
    spy_ann = (spy.iloc[-1] / spy.iloc[0]) ** (1 / yrs) - 1
    spy_dd = (spy / spy.cummax() - 1).min()
    sdr = spy.pct_change().dropna()
    spy_sh = sdr.mean() / sdr.std() * np.sqrt(252)

    # ---- report
    def summ(x: pd.DataFrame, name: str) -> dict:
        dm = x.groupby("entry_date")["net"].mean()
        out = {"trades": len(x), "win": (x["net"] > 0).mean(), "mean_net_%": x["net"].mean() * 100,
               "mean_gross_%": x["gross"].mean() * 100, "t_days": day_t(dm), "avg_hold": x["hold"].mean()}
        print(f"{name:<14} trades {out['trades']:>5}  win {out['win']:.0%}  gross {out['mean_gross_%']:+.3f}%  "
              f"net {out['mean_net_%']:+.3f}%  t {out['t_days']:+.1f}  avg hold {out['avg_hold']:.1f} days")
        return out

    print(f"\nresearch period: signals {T.signal_date.min():%Y-%m-%d} .. {T.signal_date.max():%Y-%m-%d}")
    print(f"cost per round trip: median {T.cost.median() * 100:.3f}%  exits: {T.how.value_counts().to_dict()}\n")
    allS = summ(T, "all")
    yr = T["entry_date"].dt.year
    for y in sorted(yr.unique()):
        summ(T[yr == y], f"  {y}")
    post = summ(T[T["entry_date"] >= "2024-01-01"], "2024 onward")
    beat = int((allS["mean_net_%"] / 100 > ctrl_means).sum())
    print(f"\nrandom controls (same day, same hold, uptrend stocks): mean net {ctrl_means.mean() * 100:+.3f}% "
          f"(range {ctrl_means.min() * 100:+.3f}% .. {ctrl_means.max() * 100:+.3f}%); strategy beat {beat}/99")
    print(f"\nportfolio 10 slots: annual {ann:+.1%}  max drawdown {dd:.1%}  Sharpe {sharpe:.2f}  "
          f"(SPY buy & hold same days: annual {spy_ann:+.1%}, max dd {spy_dd:.1%}, Sharpe {spy_sh:.2f})")

    c1 = allS["mean_net_%"] > 0 and allS["t_days"] >= 2.0
    c2 = beat >= 95
    c3 = post["mean_net_%"] > 0
    c4 = ann > 0
    print(f"\nPASS CHECK: 1) mean>0 and t>=2: {c1}   2) beats >=95/99 random: {c2}   "
          f"3) 2024+ mean>0: {c3}   4) portfolio return>0: {c4}")
    print("RESULT:", "PASS" if c1 and c2 and c3 and c4 else "FAIL")


if __name__ == "__main__":
    main()
