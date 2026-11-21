"""Tick check of the "Stocks in Play" 5-minute ORB (follow-up to research_sip_orb.py).

PRE-DECLARED on 2026-11-20, after research_sip_orb.py FAILED and showed that the result depends
on what happens inside the entry minute (1-minute bars cannot show whether the stop came after
the entry). The strategy rules, costs and pass bar are UNCHANGED; only the entry minute is
replayed trade by trade.

What changes:
  - "Ambiguous" trades = the entry minute's range also reaches the stop (version B's stop).
    For those, Alpaca SIP trades (ticks) of that one minute are fetched and replayed in time order:
      entry = first tick at/through the trigger (+ slippage); stop = entry -/+ 10% ATR;
      stopped if a LATER tick in that minute trades at/through the stop, filled at that tick
      (- slippage). Otherwise the trade continues with the 1-minute bars exactly as before.
  - Ticks used: regular trades only. Excluded conditions: I (odd lot), W (average price),
    Z (out of sequence), 4 (derivatively priced), 7 / V (contingent), U / T (extended hours),
    P (prior reference), C (cash), N (next day), R (seller), H (price variation), 9 (corrected close).
  - If a minute has no usable ticks, the pre-declared strict rule (stop assumed hit) is kept.
  - Unambiguous trades are simulated exactly as in research_sip_orb.py.

PASS only if ALL hold for version B (realistic costs) on the research period (before the holdout):
  1. mean R per trade > 0 and t-stat of daily mean R >= 2.0
  2. mean R per trade > 0 from 2024-01-01 on
  3. the 20-slot portfolio's annualised return > 0

    python research_sip_orb_ticks.py
"""
from __future__ import annotations

import os

os.environ.setdefault("DT_DATA", "alpaca")

import json
import time

import numpy as np
import pandas as pd

import dt_alpaca as al
import dt_config as cfg
import research_sip_orb as s

TICK_DIR = cfg.DATA_DIR / "ticks"
BAD = set("IWZ47VUTPCNRH9")


def entry_minute(r, df: pd.DataFrame):
    """(bars after 09:35 as arrays, index of the entry bar) or None if never triggered."""
    t = df.index.tz_convert(cfg.TZ)
    hhmm = t.hour * 100 + t.minute
    df = df[(hhmm >= 935) & (hhmm <= 1559)]
    sg = 1 if r.side == "long" else -1
    trig = r.high if sg > 0 else r.low
    hit = (df["high"] >= trig) if sg > 0 else (df["low"] <= trig)
    if not hit.any():
        return None
    return df, int(np.argmax(hit.to_numpy()))


def tick_path(sym: str, ts: pd.Timestamp):
    return TICK_DIR / sym / f"{ts.tz_convert(cfg.TZ):%Y-%m-%d_%H%M}.json"


def fetch_ticks(sym: str, ts: pd.Timestamp) -> list[tuple[str, float]]:
    p = tick_path(sym, ts)
    if p.exists():
        return [tuple(x) for x in json.loads(p.read_text())]
    params = {"symbols": sym, "start": ts.tz_convert("UTC").isoformat(),
              "end": (ts + pd.Timedelta(seconds=59.999)).tz_convert("UTC").isoformat(), "feed": "sip", "limit": 10000}
    out = []
    while True:
        js = al._get("https://data.alpaca.markets/v2/stocks/trades", params)
        for x in (js.get("trades") or {}).get(sym, []):
            if not BAD.intersection(x.get("c") or []):
                out.append((x["t"], float(x["p"])))
        tok = js.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    out.sort()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out))
    return out


def simulate(r, df: pd.DataFrame, comm: float, slip: float, ticks: list | None) -> dict | None:
    em = entry_minute(r, df)
    if em is None:
        return None
    d, i0 = em
    sg = 1 if r.side == "long" else -1
    trig = r.high if sg > 0 else r.low
    dist = s.STOP_ATR * r.atr14
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    entry = None
    exit_px = None
    how = "bars"
    if ticks:
        k = next((j for j, (_, p) in enumerate(ticks) if (p >= trig if sg > 0 else p <= trig)), None)
        if k is not None:
            how = "ticks"
            entry = ticks[k][1] + sg * slip
            stop = entry - sg * dist
            for _, p in ticks[k + 1:]:
                if (p <= stop) if sg > 0 else (p >= stop):
                    exit_px = p - sg * slip
                    break
    if entry is None:                                          # bar rule from research_sip_orb.py
        entry = (max(trig, o[i0]) if sg > 0 else min(trig, o[i0])) + sg * slip
        stop = entry - sg * dist
        if (l[i0] <= stop) if sg > 0 else (h[i0] >= stop):
            exit_px = stop - sg * slip
    if exit_px is None:
        for i in range(i0 + 1, len(d)):
            if (l[i] <= stop) if sg > 0 else (h[i] >= stop):
                exit_px = (min(stop, o[i]) if sg > 0 else max(stop, o[i])) - sg * slip
                break
    why = "stop" if exit_px is not None else "close"
    if exit_px is None:
        exit_px = c[-1] - sg * slip
    r_mult = (sg * (exit_px - entry) - 2 * comm) / dist
    return {"date": r.date, "symbol": r.symbol, "side": r.side, "entry": entry, "exit": exit_px, "why": why,
            "how": how, "r": r_mult, "ret_alloc": r_mult * min(s.RISK, s.MAX_LEV * dist / entry),
            "stop_dist": dist, "cost_r": (2 * comm + 2 * slip) / dist}


def main() -> None:
    cut = pd.Timestamp(json.loads((cfg.DATA_DIR / "manifest.json").read_text())["holdout_start"]).date()
    sel = pd.read_csv(s.SEL_FILE)
    sel["date"] = pd.to_datetime(sel["date"]).dt.date
    sel = sel[sel["date"] < cut]
    comm_b, slip_b = s.COSTS["B_realistic"]
    todo = []
    for r in sel.itertuples():
        df = s.load_full(r.symbol, r.date)
        if df is None:
            continue
        em = entry_minute(r, df)
        if em is None:
            continue
        d, i0 = em
        sg = 1 if r.side == "long" else -1
        trig = r.high if sg > 0 else r.low
        e = (max(trig, d["open"].iloc[i0]) if sg > 0 else min(trig, d["open"].iloc[i0])) + sg * slip_b
        stop = e - sg * s.STOP_ATR * r.atr14
        if (d["low"].iloc[i0] <= stop) if sg > 0 else (d["high"].iloc[i0] >= stop):
            todo.append((r.symbol, d.index[i0]))
    have = sum(tick_path(sym, ts).exists() for sym, ts in todo)
    print(f"ambiguous entry minutes: {len(todo)} ({have} cached) -> ~{(len(todo) - have) / 190:.0f} min", flush=True)
    t0 = time.time()
    for n, (sym, ts) in enumerate(todo, 1):
        fetch_ticks(sym, ts)
        if n % 500 == 0:
            rate = n / (time.time() - t0)
            print(f"  ticks: {n}/{len(todo)}, ~{(len(todo) - n) / rate / 60:.0f} min left", flush=True)
    amb = {(sym, ts.tz_convert(cfg.TZ).date()): ts for sym, ts in todo}

    all_days = sorted(set(sel["date"]))
    results = {}
    for name, (comm, slip) in s.COSTS.items():
        rows = []
        for r in sel.itertuples():
            df = s.load_full(r.symbol, r.date)
            if df is None:
                continue
            ts = amb.get((r.symbol, r.date))
            x = simulate(r, df, comm, slip, fetch_ticks(r.symbol, ts) if ts is not None else None)
            if x:
                rows.append(x)
        results[name] = pd.DataFrame(rows)
        results[name].to_csv(s.RES / f"sip_orb_ticks_trades_{name}.csv", index=False)

    print(f"\nresearch period {min(all_days)} .. {max(all_days)} (holdout from {cut} untouched)")
    print("\n=== all years (entry minute replayed with ticks where ambiguous) ===")
    summ = {n: s.report(tr, n, all_days) for n, tr in results.items()}
    B = results["B_realistic"]
    by = pd.to_datetime(B["date"]).dt.year
    print("\n=== version B by year ===")
    for y in sorted(by.unique()):
        s.report(B[by == y], f"B {y}", [d for d in all_days if d.year == y])
    for sd in ("long", "short"):
        s.report(B[B.side == sd], f"B {sd}", all_days)
    post = B[pd.to_datetime(B["date"]) >= pd.Timestamp("2024-01-01")]
    post_s = s.report(post, "B 2024 onward", [d for d in all_days if d >= pd.Timestamp("2024-01-01").date()])
    t = B[B["how"] == "ticks"]
    print(f"\nreplayed with ticks: {len(t)} trades; of those stopped inside the entry minute: "
          f"{((t.why == 'stop') & (t.r <= -0.9)).mean():.0%} (strict bar rule assumed 100%)")
    sb = summ["B_realistic"]
    c1 = sb["mean_R"] > 0 and sb["t_days"] >= 2.0
    c2 = post_s["mean_R"] > 0
    c3 = sb["ann_return_%"] > 0
    print(f"\nPASS CHECK (version B): 1) mean R>0 and t>=2: {c1}   2) 2024+ mean R>0: {c2}   3) portfolio return>0: {c3}")
    print("RESULT:", "PASS" if c1 and c2 and c3 else "FAIL")


if __name__ == "__main__":
    main()
