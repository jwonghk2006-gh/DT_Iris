"""DT_Iris intraday backtester + noise check.

    python dt_backtest.py --strategies cupid,power_ranger          # all symbols on disk
    python dt_backtest.py --symbols RMSG,BGMS --controls 99

Walks every symbol-day: finds setups with dt_strategies (the same detectors the
live runner uses) and fills them with dt_sim (the same fill rules the paper
runner uses). Holdout days are refused unless --holdout is passed.

Noise check: the same number of trades, on the same symbol-days, same side,
same stop distance and same target in R, but entered at RANDOM times between
the opening range and the entry cutoff. If random entries do as well as the
setups, the setups are not adding anything. p = share of random runs whose
total R is at least the real total R.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

if "--data" in sys.argv[:-1]:            # --data alpaca: pick the data set BEFORE dt_config is imported
    os.environ["DT_DATA"] = sys.argv[sys.argv.index("--data") + 1]
from datetime import datetime

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_data as data
from dt_sim import Sim
from dt_strategies import Setup, session_setups


def config_hash() -> str:
    return hashlib.sha1((cfg.ROOT / "dt_config.py").read_bytes()).hexdigest()[:10]


def _day_inputs(symbols, research):
    """{day: {symbol: (regular 1m frame, DayContext, allowed sides)}}"""
    out: dict = {}
    for sym in symbols:
        for d, item in symbol_days(sym, research).items():
            out.setdefault(d, {})[sym] = item
    return out


_GAPPERS: dict | None = None


def symbol_days(sym: str, research: bool = True, only_day=None) -> dict:
    """{day: (regular 1m frame, DayContext, allowed sides)} for one symbol.

    On a day listed in data/gappers.csv the context comes from the daily bars
    (previous close, daily ATR, average volume) and only the gap direction is
    traded - exactly what the live scanner would have handed the runner.
    Other days (e.g. the old Iris June data) allow both sides.
    """
    global _GAPPERS
    if _GAPPERS is None:
        from dt_gappers import gapper_lookup
        _GAPPERS = gapper_lookup()
    df = data.load_symbol(sym, research=research)
    if df.empty:
        return {}
    sessions = bars.split_sessions(df)
    out = {}
    for d, g in sessions.items():
        if only_day is not None and d != only_day:
            continue
        reg = bars.regular(g)
        if len(reg) < 30:
            continue
        row = _GAPPERS.get((sym, d))
        if row is not None:
            from dt_gappers import context_from_gapper
            out[d] = (reg, context_from_gapper(row, sym, d, sessions), (row.side,))
        else:
            out[d] = (reg, bars.day_context(sym, d, sessions), ("long", "short"))
    return out


def simulate(day_inputs: dict, setups_by_day: dict, sim: Sim | None = None) -> Sim:
    sim = sim or Sim()
    for d in sorted(day_inputs):
        setups = sorted(setups_by_day.get(d, []), key=lambda s: s.ready_time)
        if not setups:
            continue
        with_setup = {s.symbol for s in setups}       # other symbols never hold orders or positions: skip their bars
        stream = []
        for sym, (reg, *_rest) in day_inputs[d].items():
            if sym in with_setup:
                stream.append(reg.assign(symbol=sym))
        if not stream:
            continue
        allbars = pd.concat(stream).sort_index(kind="stable")
        k = 0
        last_px = {}
        for r in allbars.itertuples():
            ts = r.Index
            while k < len(setups) and setups[k].ready_time <= ts:
                sim.add_setup(setups[k])
                k += 1
            sim.on_bar(r.symbol, ts, r.open, r.high, r.low, r.close)
            last_px[r.symbol] = r.close
        if sim.positions:
            sim.close_all(allbars.index[-1], last_px, "session end")
    return sim


def stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    r = trades["r_multiple"]
    eq = r.cumsum()
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": int(len(r)), "win_rate": float((r > 0).mean()), "avg_r": float(r.mean()),
        "total_r": float(r.sum()), "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "max_dd_r": float((eq - eq.cummax()).min()), "pnl": float(trades["pnl"].sum()),
        "costs": float(trades["costs"].sum()), "days": int(trades["date"].nunique()),
    }


_CTRL_INPUTS: dict = {}


def _ctrl_init(day_inputs: dict, breakeven) -> None:
    global _CTRL_INPUTS
    _CTRL_INPUTS = day_inputs
    cfg.BREAKEVEN_AT_R = breakeven              # worker processes re-import dt_config: carry --no-breakeven over


def _ctrl_job(by_day: dict) -> float:
    sim = simulate(_CTRL_INPUTS, by_day, Sim(max_trades=99, max_open=99, max_daily_loss_r=1e9))
    return sum(x["r_multiple"] for x in sim.trades)


def random_controls(day_inputs: dict, trades: pd.DataFrame, n: int, seed: int = cfg.CONTROL_SEED,
                    workers: int = 1) -> np.ndarray:
    """Total R of `n` random-entry runs matched to the real trades.

    The random entries are drawn here, in order, from one seeded generator; only the replays run
    in parallel, so the result is the same for any number of workers."""
    rng = np.random.default_rng(seed)
    out = np.zeros(n)
    if trades.empty or n <= 0:
        return out
    traded = {(pd.Timestamp(t).date(), sym) for t, sym in zip(trades["date"], trades["symbol"])}
    day_inputs = {d: {s: v for s, v in per.items() if (d, s) in traded} for d, per in day_inputs.items()}
    day_inputs = {d: per for d, per in day_inputs.items() if per}      # only replay symbol-days that traded
    runs = []
    for k in range(n):
        by_day: dict = {}
        for _, t in trades.iterrows():
            d = pd.Timestamp(t["date"]).date()
            reg = day_inputs[d][t["symbol"]][0]
            t_ny = reg.index.tz_convert(cfg.TZ).time
            ok = np.where((t_ny >= bars.hm("09:35")) & (t_ny < bars.CUTOFF_T))[0]
            if len(ok) == 0:
                continue
            j = int(rng.choice(ok))
            px = float(reg.close.iloc[j])
            risk_frac = abs(t["entry"] - t["stop"]) / t["entry"]
            tr = abs(t["target"] - t["entry"]) / max(abs(t["entry"] - t["stop"]), 1e-9)
            sg = 1 if t["side"] == "long" else -1
            trig = px + sg * cfg.TICK
            stop = trig - sg * risk_frac * trig
            ts = reg.index[j]
            s = Setup(t["symbol"], "random", t["side"], ts, ts + pd.Timedelta(minutes=1), round(trig, 2), round(stop, 2),
                      round(trig + sg * tr * abs(trig - stop), 2), ts + pd.Timedelta(minutes=3), "control")
            by_day.setdefault(d, []).append(s)
        runs.append(by_day)
    if workers > 1 and n > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(min(workers, n), initializer=_ctrl_init,
                                 initargs=(day_inputs, cfg.BREAKEVEN_AT_R)) as pool:
            out[:] = list(pool.map(_ctrl_job, runs))
    else:
        _ctrl_init(day_inputs, cfg.BREAKEVEN_AT_R)
        out[:] = [_ctrl_job(r) for r in runs]
    return out


def _setups_job(job):
    sym, reg, ctx, strategies, sides = job
    return session_setups(sym, reg, ctx, strategies, sides)


def run(symbols: list[str] | None = None, strategies: list[str] | None = None, sides=("long", "short"),
        controls: int = 0, holdout: bool = False, workers: int = 1) -> dict:
    """workers > 1 finds setups in parallel processes (command line only, not from the dashboard)."""
    strategies = strategies or [k for k, v in cfg.STRATEGIES.items() if v["enabled"]]
    av = data.available()
    symbols = symbols or sorted(set(av["symbol"]) - set(cfg.BENCHMARKS))
    day_inputs = _day_inputs(symbols, research=not holdout)
    if holdout:
        day_inputs = {d: v for d, v in day_inputs.items() if data.is_holdout(d)}
    jobs = [(sym, reg, ctx, strategies, tuple(x for x in sides if x in allowed))
            for d, per_sym in day_inputs.items() for sym, (reg, ctx, allowed) in per_sym.items()]
    if workers > 1 and len(jobs) > 50:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(workers) as pool:
            results = list(pool.map(_setups_job, jobs, chunksize=20))
    else:
        results = [_setups_job(j) for j in jobs]
    setups_by_day: dict = {}
    all_setups = []
    for ss in results:
        for s_ in ss:
            setups_by_day.setdefault(s_.ready_time.tz_convert(cfg.TZ).date(), []).append(s_)
        all_setups += ss
    sim = simulate(day_inputs, setups_by_day)
    trades = pd.DataFrame(sim.trades)
    res = {"stats": stats(trades), "trades": trades,
           "setups": pd.DataFrame([s.as_row() for s in all_setups]),
           "by_strategy": {k: stats(g) for k, g in trades.groupby("strategy")} if len(trades) else {},
           "symbols": symbols, "strategies": strategies, "days": sorted(str(d) for d in day_inputs),
           "config_hash": config_hash()}
    if controls and len(trades):
        ctrl = random_controls(day_inputs, trades, controls, workers=workers)
        real = res["stats"]["total_r"]
        res["controls"] = {"n": controls, "mean_r": float(ctrl.mean()), "max_r": float(ctrl.max()),
                           "beat": int((real > ctrl).sum()),
                           "p_value": float(((ctrl >= real).sum() + 1) / (controls + 1))}
    return res


def save(res: dict, name: str | None = None) -> str:
    name = name or datetime.now().strftime("bt_%Y%m%d_%H%M%S")
    out = cfg.RESULTS_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["setups"].to_csv(out / "setups.csv", index=False)
    meta = {k: v for k, v in res.items() if k not in ("trades", "setups")}
    (out / "summary.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return str(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols")
    ap.add_argument("--strategies")
    ap.add_argument("--sides", default="long,short")
    ap.add_argument("--controls", type=int, default=0)
    ap.add_argument("--holdout", action="store_true", help="evaluate on the LOCKED holdout (spends it)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--no-breakeven", action="store_true",
                    help="research: no stop move to break-even (applies to the setups AND the noise check)")
    ap.add_argument("--data", default="", help="'alpaca' = the 5-year Alpaca history in data_alpaca/ (default: Longbridge data/)")
    a = ap.parse_args()
    if a.no_breakeven:
        cfg.BREAKEVEN_AT_R = None
    res = run([s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None,
              a.strategies.split(",") if a.strategies else None, tuple(a.sides.split(",")), a.controls, a.holdout,
              a.workers)
    res["data_source"] = cfg.DATA_SOURCE or "longbridge"
    print(json.dumps({k: res[k] for k in ("stats", "by_strategy", "days", "config_hash")}, indent=2, default=str))
    if "controls" in res:
        print("noise check:", res["controls"])
    print("saved to", save(res))
