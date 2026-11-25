"""Relative strength (gap vs SPY) and "stocks in play" lists: test + limited optimisation.

PRE-DECLARED on 2026-11-23, before any of these lists was backtested. The rules, the variants,
the train/validate split, the selection rule and the pass bar below are fixed now. Strategy rules
(dt_config.STRATEGIES), fills and costs are NOT changed or tuned. Only the stock SELECTION is.

Data: Alpaca 5 years (data_alpaca/), research period 2021-11-19 .. 2026-09-24. The holdout
(data_alpaca/manifest.json, from 2026-09-25) is not read unless step 6 is reached.
  TRAIN    = 2021-11-19 .. 2024-06-30   (variants are chosen here only)
  VALIDATE = 2024-07-01 .. 2026-09-24   (the chosen variant is judged here, once)

Definitions (daily values use only data before the day; prices split-adjusted, price filters raw):
  gap        = open / previous close - 1  (the same gap the 5-year baseline used)
  beta       = cov/var of 60 daily returns vs SPY ending the day before, at least 40 days,
               clamped to [0.3, 3.0]; missing -> 1.0
  rel gap    = gap - beta x SPY gap (same day)
  pm volume  = consolidated volume 04:00-09:00 ET (Alpaca SIP hourly bars) = what a 09:00 scan sees
  pm_rvol    = pm volume / 20-day average daily volume
  RS at t    = stock % change from the 09:30 open to the last finished bar before t
               - beta x SPY's % change over the same minutes

Lists (each day, top-N as in the live scanner):
  raw-T      : |gap| >= T, side = sign; price >= $5 long / >= $25 short; adv20 >= 500k;
               top 20 per side by |gap| x 20-day dollar volume. raw-4 = the current baseline.
  rel-T      : the same with |rel gap| >= T (side = sign of the rel gap)
  inplay-R   : price >= $5 (>= $25 if short); adv20 >= 1.75M (= 1M on Longbridge's ~57% volume);
               pm volume >= 88k (= 50k Longbridge); pm_rvol >= R; side = sign of the rel gap;
               top 20 by pm_rvol
  T in {3, 4, 5, 6} %; R in {2, 5, 10, 20} %.

Variants per strategy (each strategy simulated alone, current portfolio limits):
  Power Ranger : {raw-T, rel-T} x T x {RS filter off, on}            16 variants
  Cupid        : {raw-4, inplay-R} x R x {RS filter off, on}          10 variants
  Tug-Of-War   : same as Cupid                                       10 variants
  RS filter on = a setup is taken only if RS at its ready time agrees with its side
                 (RS > 0 for longs, < 0 for shorts). For Power Ranger the gap used by the setup
                 itself is the list's gap (raw or relative).

Selection (TRAIN only): per strategy, the variant with the highest average R per trade among
variants with >= 100 train trades. Baseline = raw-4 with the RS filter off (today's rules).

PASS for a strategy, on VALIDATE, ALL of:
  1. the chosen variant has >= 50 trades and total R > 0
  2. it beats >= 95 of 99 random-entry controls (p <= 0.05; same symbol-days, side, stop, target)
  3. its average R per trade is higher than the baseline's on VALIDATE
Every other variant's VALIDATE numbers are printed for information only; they are not used to pick.

Step 6, only for a strategy that PASSES: the holdout, once, chosen variant only. Holdout pass =
total R > 0 and beats >= 90 of 99 controls. Nothing is tuned after looking at it.

If nothing passes: the live lists stay as the user asked (relative gap 4%, in-play 5%), clearly
marked as unproven, and the runner stays paper-only.

    python research_rs.py --fetch      # SPY minutes, pre-market volume, list minutes (Alpaca, ~1 h)
    python research_rs.py --run        # backtests, selection, validation, report
"""
from __future__ import annotations

import os

os.environ["DT_DATA"] = "alpaca"                 # before dt_config is imported (also in worker processes)

import argparse
import glob
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_data as data

RS_DIR = cfg.DATA_DIR / "rs"
PM_DIR = RS_DIR / "pm"
OUT_DIR = cfg.ROOT / "results_alpaca" / "rs_test"
START = date(2021, 11, 19)
TRAIN_END = date(2024, 6, 30)
VAL_START = date(2024, 7, 1)
RESEARCH_END = date(2026, 9, 24)
T_GRID = [3.0, 4.0, 5.0, 6.0]
R_GRID = [0.02, 0.05, 0.10, 0.20]
VOL_SCALE_LB = 0.57                       # Longbridge volume / consolidated volume
INPLAY_ADV = 1_000_000 / VOL_SCALE_LB
INPLAY_PMV = 50_000 / VOL_SCALE_LB
TOP = 20
MIN_TRAIN_TRADES = 100
CONTROLS = 99


def hold_start() -> date:
    m = json.loads((cfg.DATA_DIR / "manifest.json").read_text())
    return date.fromisoformat(m["holdout_start"])


# ------------------------------------------------------------------ 1. daily panel
def daily_panel() -> dict[str, pd.DataFrame]:
    """Wide (date x symbol) frames: open, high, low, close, volume, raw_open + derived features."""
    cols = {k: {} for k in ("open", "high", "low", "close", "volume", "raw_open")}
    for f in glob.glob(str(cfg.DATA_DIR / "daily" / "*.parquet")):
        d = pd.read_parquet(f)
        sym = Path(f).stem
        for k in cols:
            if k in d:
                cols[k][sym] = d[k]
    P = {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}
    idx = P["close"].index
    P = {k: v.reindex(idx) for k, v in P.items()}
    P["raw_open"] = P["raw_open"].reindex(columns=P["close"].columns)
    spy = pd.read_parquet(cfg.DATA_DIR / "spy_daily.parquet").reindex(idx)
    pc = P["close"].shift(1)
    P["gap"] = (P["open"] / pc - 1) * 100
    P["adv20"] = P["volume"].rolling(20, min_periods=10).mean().shift(1)
    P["dv20"] = (P["volume"] * P["close"]).rolling(20, min_periods=10).mean().shift(1)
    hi, lo = P["high"], P["low"]
    tr = np.maximum(hi - lo, np.maximum((hi - pc).abs(), (lo - pc).abs()))
    tr = tr.where(pc.notna(), hi - lo)
    P["atr14"] = tr.ewm(alpha=1 / cfg.DAILY_ATR_LEN, adjust=False, min_periods=1).mean().shift(1)
    P["prev_close"] = pc
    r = P["close"].pct_change(fill_method=None)
    m = spy["close"].pct_change()
    n, mp = cfg.BETA_DAYS, 40
    mx = r.rolling(n, min_periods=mp).mean()
    mm = m.rolling(n, min_periods=mp).mean()
    cov = (r.mul(m, axis=0)).rolling(n, min_periods=mp).mean() - mx.mul(mm, axis=0)
    var = (m * m).rolling(n, min_periods=mp).mean() - mm * mm
    beta = cov.div(var, axis=0).clip(cfg.BETA_MIN, cfg.BETA_MAX).shift(1)
    P["beta"] = beta.fillna(1.0)
    P["spy_gap"] = (spy["open"] / spy["close"].shift(1) - 1) * 100
    P["rel_gap"] = P["gap"].sub(P["beta"].mul(P["spy_gap"], axis=0))
    return P


# ------------------------------------------------------------------ 2. lists
def build_lists(P: dict, pm: pd.DataFrame | None, last_day: date = RESEARCH_END, first_day: date = START) -> pd.DataFrame:
    """One row per (list, date, symbol): list, T/R, side, and the context fields the backtest needs."""
    idx = [d for d in P["close"].index if first_day <= d <= last_day]
    fields = ["gap", "rel_gap", "beta", "adv20", "dv20", "atr14", "prev_close", "open", "raw_open"]
    L = pd.concat({k: P[k].loc[idx].stack() for k in fields}, axis=1).reset_index()
    L.columns = ["date", "symbol"] + fields
    L["spy_gap"] = L["date"].map(P["spy_gap"])
    L["px"] = L["raw_open"].fillna(L["open"])
    L = L[L["gap"].notna() & L["prev_close"].notna()]
    out = []
    for basis in ("raw", "rel"):
        g = L["gap"] if basis == "raw" else L["rel_gap"]
        for T in T_GRID:
            c = L[(g.abs() >= T) & (L.adv20 >= cfg.MIN_AVG_DAILY_VOLUME)].copy()
            c["side"] = np.where(g[c.index] > 0, "long", "short")
            c = c[((c.side == "long") & (c.px >= cfg.LONG_MIN_PRICE)) | ((c.side == "short") & (c.px >= cfg.SHORT_MIN_PRICE))]
            c["score"] = g[c.index].abs() * c.dv20
            c = c.sort_values("score", ascending=False).groupby(["date", "side"], group_keys=False).head(TOP)
            c["list"] = f"{basis}-{T:g}"
            out.append(c)
    if pm is not None:
        Lp = L.merge(pm, on=["date", "symbol"], how="inner")
        Lp["pm_rvol"] = Lp["pm_volume"] / Lp["adv20"]
        Lp["side"] = np.where(Lp["rel_gap"] >= 0, "long", "short")
        base = Lp[(Lp.adv20 >= INPLAY_ADV) & (Lp.pm_volume >= INPLAY_PMV) & (Lp.px >= cfg.IN_PLAY_MIN_PRICE)
                  & ((Lp.side == "long") | (Lp.px >= cfg.SHORT_MIN_PRICE))]
        for R in R_GRID:
            c = base[base.pm_rvol >= R].copy()
            c["score"] = c.pm_rvol
            c = c.sort_values("score", ascending=False).groupby("date", group_keys=False).head(TOP)
            c["list"] = f"inplay-{R:g}"
            out.append(c)
    res = pd.concat(out, ignore_index=True)
    res["date"] = pd.to_datetime(res["date"]).dt.date
    return res


# ------------------------------------------------------------------ 3. fetch
def fetch_spy_minutes(first: date, last: date) -> pd.DataFrame:
    import dt_alpaca as alp
    p = RS_DIR / "spy_1m.parquet"
    old = pd.read_parquet(p) if p.exists() else None
    if old is not None and old.index.max().tz_convert(cfg.TZ).date() >= last:
        return old
    rows = []
    for m0 in pd.date_range(pd.Timestamp(first).replace(day=1), pd.Timestamp(last), freq="MS"):
        s0 = pd.Timestamp(m0, tz=cfg.TZ)
        s1 = min(pd.Timestamp(m0 + pd.offsets.MonthBegin(1), tz=cfg.TZ), pd.Timestamp(last, tz=cfg.TZ) + pd.Timedelta(days=1))
        got = alp.bars_multi(["SPY"], "1Min", s0, s1, "raw").get("SPY", [])
        rows += got
        print(f"  SPY minutes {m0:%Y-%m}: {len(got)}", flush=True)
    df = alp._to_frame(rows, daily=False).set_index("time").sort_index()
    df = df[~df.index.duplicated()][["open", "high", "low", "close", "volume"]]
    RS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    return df


def pm_symbols(P: dict) -> list[str]:
    """Only symbols that could ever pass the in-play liquidity and price filters."""
    ok = (P["adv20"] >= INPLAY_ADV) & (P["raw_open"].fillna(P["open"]) >= cfg.IN_PLAY_MIN_PRICE)
    return sorted(ok.columns[ok.any()])


def _pm_day(args) -> list[tuple]:
    import dt_alpaca as alp
    syms, day = args
    s0 = pd.Timestamp(f"{day} 04:00", tz=cfg.TZ)
    got = alp.bars_multi(syms, "1Hour", s0, pd.Timestamp(f"{day} 08:59", tz=cfg.TZ), "raw")
    out = []
    for sym, rows in got.items():
        v = sum(float(x["v"]) for x in rows if 4 <= pd.Timestamp(x["t"]).tz_convert(cfg.TZ).hour <= 8)
        out.append((day, sym, v))
    return out


def fetch_pm_volume(syms: list[str], first: date, last: date, days: list | None = None) -> None:
    """Pre-market (04:00-09:00 ET) consolidated volume per symbol-day, one parquet per month.
    One request per day for every symbol (only the pre-market hours), three days in parallel
    (Alpaca free plan: 200 requests a minute; dt_alpaca paces the calls)."""
    from concurrent.futures import ThreadPoolExecutor
    PM_DIR.mkdir(parents=True, exist_ok=True)
    sessions = sorted(d for d in (days or []) if first <= d <= last)
    months = sorted({(d.year, d.month) for d in sessions})
    t0 = time.time()
    with ThreadPoolExecutor(3) as pool:
        for n, (y, mo) in enumerate(months, 1):
            p = PM_DIR / f"{y}-{mo:02d}.parquet"
            if p.exists():
                continue
            todo = [(syms, d) for d in sessions if (d.year, d.month) == (y, mo)]
            recs = [x for part in pool.map(_pm_day, todo) for x in part]
            df = pd.DataFrame(recs, columns=["date", "symbol", "pm_volume"])
            df.to_parquet(p)
            print(f"  pre-market volume {y}-{mo:02d}: {len(df)} symbol-days ({n}/{len(months)}, "
                  f"{(time.time() - t0) / 60:.0f} min)", flush=True)


def load_pm() -> pd.DataFrame:
    fs = sorted(PM_DIR.glob("*.parquet"))
    if not fs:
        return pd.DataFrame(columns=["date", "symbol", "pm_volume"])
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def fetch(last: date = RESEARCH_END, first: date = START) -> None:
    import dt_alpaca as alp
    print("1. daily panel", flush=True)
    P = daily_panel()
    print("2. SPY 1-minute bars", flush=True)
    fetch_spy_minutes(first, last)
    syms = pm_symbols(P)
    print(f"3. pre-market volume for {len(syms)} symbols", flush=True)
    fetch_pm_volume(syms, first, last, [d for d in P["close"].index if P["close"].loc[d].notna().sum() > 1000])
    print("4. lists", flush=True)
    lists = build_lists(P, load_pm(), last, first)
    need = lists[["date", "symbol"]].drop_duplicates()
    print(f"5. 1-minute bars for {len(need)} list symbol-days", flush=True)
    alp.fetch_minutes(need)


# ------------------------------------------------------------------ 4. backtest pieces
def _init_worker():
    import dt_config as c
    c.STRATEGIES["power_ranger"]["min_gap_pct"] = -1e9     # both sides; the list decides the side and the gap


def _detect_job(job):
    _init_worker()
    from dt_strategies import session_setups
    sym, day, reg, ctx, strategies = job
    try:
        return session_setups(sym, reg, ctx, strategies, ("long", "short"))
    except Exception:
        return []


def load_inputs(pairs: pd.DataFrame) -> dict:
    """{(date, symbol): (reg, DayContext)} for every listed symbol-day with minute bars."""
    from dt_gappers import context_from_gapper
    out = {}
    for r in pairs.itertuples():
        p = data._path(r.symbol, r.date)
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        sessions = bars.split_sessions(df)
        if r.date not in sessions:
            continue
        reg = bars.regular(sessions[r.date])
        if len(reg) < 30:
            continue
        row = pd.Series({"open_adj": r.open, "prev_close_adj": r.prev_close, "atr14_adj": r.atr14, "adv20": r.adv20})
        out[(r.date, r.symbol)] = (reg, context_from_gapper(row, r.symbol, r.date, sessions))
    return out


def rs_at(reg: pd.DataFrame, spy: pd.DataFrame, beta: float, t: pd.Timestamp) -> float | None:
    a = reg[reg.index < t]
    day0 = reg.index[0]
    s = spy[(spy.index >= day0) & (spy.index < t)]
    if a.empty or s.empty:
        return None
    return (a.close.iloc[-1] / reg.open.iloc[0] - 1) * 100 - beta * (s.close.iloc[-1] / s.open.iloc[0] - 1) * 100


def simulate_variant(inputs: dict, members: pd.DataFrame, setups: list, rs_filter: bool, first: date, last: date):
    import dt_backtest as bt
    mem = members[(members.date >= first) & (members.date <= last)]
    side = {(r.date, r.symbol): r.side for r in mem.itertuples()}
    by_day, day_inputs = {}, {}
    for s in setups:
        d = s.ready_time.tz_convert(cfg.TZ).date()
        k = (d, s.symbol)
        if side.get(k) != s.side:
            continue
        if rs_filter:
            v = s.extra.get("_rs")
            if v is None or (v <= 0 if s.side == "long" else v >= 0):
                continue
        by_day.setdefault(d, []).append(s)
    for (d, sym), sd in side.items():
        if (d, sym) in inputs:
            reg, ctx = inputs[(d, sym)]
            day_inputs.setdefault(d, {})[sym] = (reg, ctx, (sd,))
    sim = bt.simulate(day_inputs, by_day)
    return pd.DataFrame(sim.trades), day_inputs


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0, "total_r": 0.0, "avg_r": None, "win": None}
    r = tr["r_multiple"]
    return {"trades": int(len(r)), "total_r": round(float(r.sum()), 1), "avg_r": round(float(r.mean()), 3),
            "win": round(float((r > 0).mean()), 3)}


def run(workers: int, smoke: bool = False) -> dict:
    import dt_backtest as bt
    t0 = time.time()
    P = daily_panel()
    pm = load_pm()
    lists = build_lists(P, pm)
    if smoke:                                 # quick check of the code path: a few weeks per period, no controls
        lists = lists[((lists.date >= date(2022, 3, 1)) & (lists.date <= date(2022, 4, 15)))
                      | ((lists.date >= date(2025, 3, 1)) & (lists.date <= date(2025, 4, 15)))]
    spy = pd.read_parquet(RS_DIR / "spy_1m.parquet")
    spy = bars.regular(spy) if len(spy) else spy
    print(f"lists: {lists.groupby('list').size().to_dict()}", flush=True)
    pairs = lists.drop_duplicates(["date", "symbol"])
    inputs = load_inputs(pairs)
    print(f"minute inputs: {len(inputs)} of {len(pairs)} symbol-days ({time.time() - t0:.0f}s)", flush=True)
    gap_days = set(map(tuple, lists[lists.list.str.match(r"(raw|rel)-")][["date", "symbol"]].values))
    play_days = set(map(tuple, lists[lists.list.str.startswith("inplay") | (lists.list == "raw-4")][["date", "symbol"]].values))
    jobs = []
    for k, (reg, ctx) in inputs.items():
        st = (["power_ranger"] if k in gap_days else []) + (["cupid", "tug_of_war"] if k in play_days else [])
        if st:
            jobs.append((k[1], k[0], reg, ctx, st))
    print(f"detecting setups on {len(jobs)} symbol-days with {workers} workers ...", flush=True)
    with ProcessPoolExecutor(workers, initializer=_init_worker) as pool:
        res = list(pool.map(_detect_job, jobs, chunksize=25))
    setups = [s for ss in res for s in ss]
    beta = {(r.date, r.symbol): r.beta for r in pairs.itertuples()}
    for s in setups:
        d = s.ready_time.tz_convert(cfg.TZ).date()
        reg = inputs[(d, s.symbol)][0]
        s.extra["_rs"] = rs_at(reg, spy, beta.get((d, s.symbol), 1.0), s.ready_time)
    print(f"{len(setups)} setups ({time.time() - t0:.0f}s)", flush=True)

    def members(name, strategy):
        m = lists[lists.list == name][["date", "symbol", "side", "gap"]].copy()
        if strategy == "power_ranger" and name.startswith("raw-"):
            # raw basis: the setup's own gap check uses the pre-market gap, as in the baseline backtest
            T = float(name.split("-")[1])
            ok = []
            for r in m.itertuples():
                inp = inputs.get((r.date, r.symbol))
                g = inp[1].gap_pct if inp else None
                ok.append(g is not None and (g >= T if r.side == "long" else g <= -T))
            m = m[ok]
        return m

    variants = {"power_ranger": [(f"{b}-{T:g}", rsf) for b in ("raw", "rel") for T in T_GRID for rsf in (False, True)]}
    for st in ("cupid", "tug_of_war"):
        variants[st] = [("raw-4", rsf) for rsf in (False, True)] + [(f"inplay-{R:g}", rsf) for R in R_GRID for rsf in (False, True)]
    report = {"protocol": __doc__.split("    python research_rs.py")[0], "strategies": {}}
    for st, vs in variants.items():
        ss = [s for s in setups if s.strategy == st]
        rows = []
        for name, rsf in vs:
            m = members(name, st)
            tr_t, _ = simulate_variant(inputs, m, ss, rsf, START, TRAIN_END)
            tr_v, di_v = simulate_variant(inputs, m, ss, rsf, VAL_START, RESEARCH_END)
            rows.append({"variant": name, "rs_filter": rsf, "train": summarize(tr_t), "validate": summarize(tr_v),
                         "_tv": tr_v, "_di": di_v})
            print(f"  {st:13} {name:11} RS {'on ' if rsf else 'off'}  train {summarize(tr_t)}  validate {summarize(tr_v)}",
                  flush=True)
        base = next(r for r in rows if r["variant"] == "raw-4" and not r["rs_filter"])
        elig = [r for r in rows if r["train"]["trades"] >= (5 if smoke else MIN_TRAIN_TRADES) and r["train"]["avg_r"] is not None]
        chosen = max(elig, key=lambda r: r["train"]["avg_r"]) if elig else base
        out = {"variants": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
               "baseline": {"variant": "raw-4", "rs_filter": False}, "chosen": {"variant": chosen["variant"],
                                                                                "rs_filter": chosen["rs_filter"]}}
        for tag, r in (("chosen", chosen), ("baseline", base)):
            tv = r["_tv"]
            if len(tv):
                ctrl = bt.random_controls(r["_di"], tv, 5 if smoke else CONTROLS, workers=workers)
                real = float(tv["r_multiple"].sum())
                out[f"{tag}_controls"] = {"beat": int((real > ctrl).sum()), "mean_r": round(float(ctrl.mean()), 1),
                                          "p": round(float(((ctrl >= real).sum() + 1) / (CONTROLS + 1)), 3)}
        v, b = chosen["validate"], base["validate"]
        c = out.get("chosen_controls", {"beat": 0})
        checks = {"trades>=50_and_total>0": v["trades"] >= 50 and v["total_r"] > 0,
                  "beats>=95/99": c["beat"] >= 95,
                  "avg_r_above_baseline": (v["avg_r"] or -9) > (b["avg_r"] if b["avg_r"] is not None else -9)}
        out["checks"] = checks
        out["pass"] = all(checks.values())
        report["strategies"][st] = out
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for r in rows:
            if len(r["_tv"]) and r is chosen:
                r["_tv"].to_csv(OUT_DIR / f"{st}_chosen_validate_trades.csv", index=False)
        print(f"== {st}: chosen {chosen['variant']} RS {'on' if chosen['rs_filter'] else 'off'} -> "
              f"{'PASS' if out['pass'] else 'FAIL'} {checks} {out.get('chosen_controls')}", flush=True)
    report["seconds"] = round(time.time() - t0)
    if smoke:
        return report
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT_DIR / 'report.json'}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="with --run: tiny sample, checks the code path only")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args()
    if a.fetch:
        fetch()
    if a.run:
        run(a.workers, a.smoke)
    if not (a.fetch or a.run):
        ap.print_help()
        sys.exit(1)
