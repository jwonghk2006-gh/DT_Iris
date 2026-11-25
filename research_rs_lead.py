"""RS continuation: trade in the direction the stock is already beating (or lagging) SPY.

PRE-DECLARED on 2026-11-24, before this rule was backtested. Everything below is fixed now.

Why: in research_rs.py the Tug-Of-War random-entry controls (gap direction, on symbol-days where
RS agreed with the side) made about +0.04R a trade (+49.8R over 1,157 trades). Those controls could
enter BEFORE the RS was known (look-ahead), so this test asks the clean question: if you only
know RS at a fixed time and enter then, is there an edge after costs?

Data: Alpaca 5 years, the same lists, minute bars, SPY minutes and pre-market volume as
research_rs.py (no new download). Holdout (from 2026-09-25) is not read unless step 6 is reached.
  TRAIN    = 2021-11-19 .. 2024-06-30   (the variant is chosen here only)
  VALIDATE = 2024-07-01 .. 2026-09-24   (judged once)

Rule (one possible trade per symbol-day):
  Lists (from research_rs.build_lists): raw-4 (|gap| >= 4%), rel-4 (|gap - beta x SPY gap| >= 4%),
    inplay-0.05 (pre-market volume >= 5% of a normal day). Side = the list's side.
  At check time C, RS = stock % change from the 09:30 open to the close of the bar ending at C
    - beta x SPY's % change over the same minutes. Take the trade only if RS agrees with the side
    by at least K: RS >= K for longs, RS <= -K for shorts.
  Entry: buy (sell short) stop 1 tick beyond the close of the bar ending at C, valid 3 minutes.
  Stop: the day's low since the open minus 1 tick (long) / high plus 1 tick (short); the distance is
    kept between 0.2 and 1.0 x the 14-day ATR (moved in or out to fit).
  Target 2R. Everything else as the live system: breakeven at +1R, flat at 15:00, 1 cent slippage
    per fill + $0.005/share (min $1), max 3 trades a day, max 2 open, -3R daily stop. When several
    names qualify at the same time, the biggest |RS| is filled first.
  Variants: list x C in {10:00, 10:30, 11:00} x K in {0, 1, 2} %  = 27 variants.
  Reference (not selectable): the same list and C with no RS condition (every list member, its side).

Selection (TRAIN only): the variant with the highest average R per trade among those with >= 100
train trades.

PASS on VALIDATE, ALL of:
  1. >= 50 trades and total R > 0
  2. beats >= 95 of 99 random-entry controls (same symbol-days, side, stop distance, target R;
     random entry times between C and 12:00, so the controls know no more than the rule does)
  3. average R per trade higher than its no-RS reference (same list and C) on VALIDATE
Other variants' VALIDATE numbers are printed for information only.

Step 6, only if it PASSES: the holdout, once, chosen variant only. Holdout pass = total R > 0 and
beats >= 90 of 99 controls. If it fails anywhere: nothing goes live; the result is logged.

    python research_rs_lead.py --run [--smoke] [--workers N]
"""
from __future__ import annotations

import os

os.environ["DT_DATA"] = "alpaca"

import argparse
import json
import sys
import time
from datetime import date

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg
import research_rs as rr
from dt_strategies import Setup

OUT_DIR = cfg.ROOT / "results_alpaca" / "rs_lead"
LISTS = ["raw-4", "rel-4", "inplay-0.05"]
CHECKS = ["10:00", "10:30", "11:00"]
K_GRID = [0.0, 1.0, 2.0]
TARGET_R = 2.0
RISK_ATR_MIN, RISK_ATR_MAX = 0.2, 1.0
MIN_TRAIN_TRADES = 100
CONTROLS = 99


def candidate(reg: pd.DataFrame, spy: pd.DataFrame, ctx, beta: float, sym: str, side: str, check: str):
    """(rs, Setup) for the bar ending at `check`, or None."""
    t = pd.Timestamp(f"{reg.index[0].tz_convert(cfg.TZ).date()} {check}", tz=cfg.TZ).tz_convert(reg.index.tz)
    rs = rr.rs_at(reg, spy, beta, t)
    a = reg[reg.index < t]
    if rs is None or a.empty or a.index[-1] != t - pd.Timedelta(minutes=1):
        return None
    sg = 1 if side == "long" else -1
    px = float(a.close.iloc[-1])
    trig = round(px + sg * cfg.TICK, 2)
    ext = float(a.low.min()) - cfg.TICK if side == "long" else float(a.high.max()) + cfg.TICK
    risk = abs(trig - ext) if (ext - trig) * sg < 0 else 0.0
    atr = getattr(ctx, "daily_atr", None) or getattr(ctx, "atr14", None)
    if atr and atr > 0:
        risk = min(max(risk, RISK_ATR_MIN * atr), RISK_ATR_MAX * atr)
    if risk < 2 * cfg.TICK:
        return None
    stop = round(trig - sg * risk, 2)
    s = Setup(sym, "rs_cont", side, a.index[-1], t, trig, stop, round(trig + sg * TARGET_R * abs(trig - stop), 2),
              t + pd.Timedelta(minutes=3), f"RS {rs:+.2f}% at {check}")
    return rs, s


def random_controls_after(day_inputs: dict, trades: pd.DataFrame, n: int, start: str) -> np.ndarray:
    """dt_backtest.random_controls, but entries are drawn between `start` and the entry cutoff."""
    import dt_backtest as bt
    rng = np.random.default_rng(cfg.CONTROL_SEED)
    t0 = bars.hm(start)
    runs = []
    for _ in range(n):
        by_day = {}
        for _, t in trades.iterrows():
            d = pd.Timestamp(t["date"]).date()
            reg = day_inputs[d][t["symbol"]][0]
            tn = reg.index.tz_convert(cfg.TZ).time
            ok = np.where((tn >= t0) & (tn < bars.CUTOFF_T))[0]
            if len(ok) == 0:
                continue
            j = int(rng.choice(ok))
            px = float(reg.close.iloc[j])
            rf = abs(t["entry"] - t["stop"]) / t["entry"]
            trr = abs(t["target"] - t["entry"]) / max(abs(t["entry"] - t["stop"]), 1e-9)
            sg = 1 if t["side"] == "long" else -1
            trig = px + sg * cfg.TICK
            stop = trig - sg * rf * trig
            ts = reg.index[j]
            by_day.setdefault(d, []).append(Setup(t["symbol"], "random", t["side"], ts, ts + pd.Timedelta(minutes=1),
                                                  round(trig, 2), round(stop, 2),
                                                  round(trig + sg * trr * abs(trig - stop), 2),
                                                  ts + pd.Timedelta(minutes=3), "control"))
        runs.append(by_day)
    traded = {(pd.Timestamp(x).date(), s) for x, s in zip(trades["date"], trades["symbol"])}
    di = {d: {s: v for s, v in per.items() if (d, s) in traded} for d, per in day_inputs.items()}
    bt._ctrl_init({d: p for d, p in di.items() if p}, cfg.BREAKEVEN_AT_R)
    return np.array([bt._ctrl_job(r) for r in runs])


def simulate(cands: list, first: date, last: date, k: float | None):
    """cands: [(date, sym, rs, setup, reg, ctx)] for one list and check time."""
    import dt_backtest as bt
    by_day, di = {}, {}
    for d, sym, rs, s, reg, ctx in sorted(cands, key=lambda x: -abs(x[2])):      # biggest |RS| first
        if not (first <= d <= last):
            continue
        if k is not None and (rs < k if s.side == "long" else rs > -k):
            continue
        by_day.setdefault(d, []).append(s)
        di.setdefault(d, {})[sym] = (reg, ctx, (s.side,))
    sim = bt.simulate(di, by_day)
    return pd.DataFrame(sim.trades), di


def run(smoke: bool = False) -> dict:
    t0 = time.time()
    P = rr.daily_panel()
    lists = rr.build_lists(P, rr.load_pm())
    lists = lists[lists.list.isin(LISTS)]
    if smoke:
        lists = lists[((lists.date >= date(2022, 3, 1)) & (lists.date <= date(2022, 4, 15)))
                      | ((lists.date >= date(2025, 3, 1)) & (lists.date <= date(2025, 4, 15)))]
    spy = bars.regular(pd.read_parquet(rr.RS_DIR / "spy_1m.parquet"))
    inputs = rr.load_inputs(lists.drop_duplicates(["date", "symbol"]))
    print(f"lists {lists.groupby('list').size().to_dict()}; minute inputs {len(inputs)} ({time.time() - t0:.0f}s)", flush=True)

    cands = {}                                   # (list, check) -> [(date, sym, rs, setup, reg, ctx)]
    for r in lists.itertuples():
        inp = inputs.get((r.date, r.symbol))
        if inp is None:
            continue
        reg, ctx = inp
        for c in CHECKS:
            got = candidate(reg, spy, ctx, r.beta, r.symbol, r.side, c)
            if got:
                cands.setdefault((r.list, c), []).append((r.date, r.symbol, got[0], got[1], reg, ctx))
    print(f"candidates built ({time.time() - t0:.0f}s)", flush=True)

    rows = []
    for (lst, c), cs in sorted(cands.items()):
        ref_v, _ = simulate(cs, rr.VAL_START, rr.RESEARCH_END, None)
        ref_t, _ = simulate(cs, rr.START, rr.TRAIN_END, None)
        for k in K_GRID:
            tr_t, _ = simulate(cs, rr.START, rr.TRAIN_END, k)
            tr_v, di_v = simulate(cs, rr.VAL_START, rr.RESEARCH_END, k)
            row = {"list": lst, "check": c, "k": k, "train": rr.summarize(tr_t), "validate": rr.summarize(tr_v),
                   "ref_train": rr.summarize(ref_t), "ref_validate": rr.summarize(ref_v), "_tv": tr_v, "_di": di_v}
            rows.append(row)
            print(f"  {lst:12} {c} K {k:g}  train {row['train']}  validate {row['validate']}  "
                  f"(no-RS ref validate {row['ref_validate']})", flush=True)

    elig = [r for r in rows if r["train"]["trades"] >= (5 if smoke else MIN_TRAIN_TRADES)]
    report = {"protocol": __doc__.split("    python research_rs_lead.py")[0],
              "variants": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]}
    if not elig:
        report.update(chosen=None, pass_=False)
        print("no variant has enough train trades -> FAIL")
    else:
        ch = max(elig, key=lambda r: r["train"]["avg_r"])
        v, ref = ch["validate"], ch["ref_validate"]
        ctrl = random_controls_after(ch["_di"], ch["_tv"], 5 if smoke else CONTROLS, ch["check"]) if len(ch["_tv"]) else np.zeros(1)
        real = float(ch["_tv"]["r_multiple"].sum()) if len(ch["_tv"]) else 0.0
        cc = {"beat": int((real > ctrl).sum()), "mean_r": round(float(ctrl.mean()), 1),
              "p": round(float(((ctrl >= real).sum() + 1) / (len(ctrl) + 1)), 3)}
        checks = {"trades>=50_and_total>0": v["trades"] >= 50 and v["total_r"] > 0,
                  "beats>=95/99": cc["beat"] >= 95,
                  "avg_r_above_no_rs_ref": (v["avg_r"] if v["avg_r"] is not None else -9)
                  > (ref["avg_r"] if ref["avg_r"] is not None else -9)}
        report.update(chosen={"list": ch["list"], "check": ch["check"], "k": ch["k"]}, controls=cc, checks=checks,
                      pass_=all(checks.values()))
        print(f"== chosen {ch['list']} {ch['check']} K {ch['k']:g}: validate {v}; controls {cc}; {checks} -> "
              f"{'PASS' if report['pass_'] else 'FAIL'}", flush=True)
        if not smoke and len(ch["_tv"]):
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            ch["_tv"].to_csv(OUT_DIR / "chosen_validate_trades.csv", index=False)
    report["seconds"] = round(time.time() - t0)
    if not smoke:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"saved {OUT_DIR / 'report.json'}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run(a.smoke)
