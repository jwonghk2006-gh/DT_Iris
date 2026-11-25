"""The four Iris intraday setups as code.

Every detector looks at ONE bar, `i`, of a session frame that already has
indicators (dt_bars.add_indicators), and only reads rows 0..i. The indicators
are causal, so the backtester (which walks i forward over a finished day) and
the live runner (which calls with i = the newest closed bar) get the same answer.

Shorts are handled by mirroring the bars (price -> -price) and running the long
logic, so a buy rule and its sell rule can never drift apart.

A detector returns a Setup: a stop order to enter at `trigger`, a protective
stop at `stop`, a limit `target`, valid until `valid_until`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg

PRICE_COLS = ["open", "high", "low", "close", "ema", "vwap"]


@dataclass
class Setup:
    symbol: str
    strategy: str
    side: str            # "long" | "short"
    bar_time: pd.Timestamp   # start time of the bar that completed the setup (UTC)
    ready_time: pd.Timestamp  # when the setup is known = end of that bar
    trigger: float
    stop: float
    target: float
    valid_until: pd.Timestamp
    reason: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return abs(self.trigger - self.stop)

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.strategy}|{self.side}|{self.bar_time.isoformat()}"

    def as_row(self) -> dict:
        d = asdict(self)
        d["bar_time"], d["ready_time"], d["valid_until"] = str(self.bar_time), str(self.ready_time), str(self.valid_until)
        return d


def mirror(df: pd.DataFrame) -> pd.DataFrame:
    """Price-flipped copy: a short setup in `df` is a long setup in mirror(df)."""
    m = df.copy()
    m["high"], m["low"] = -df["low"], -df["high"]
    for c in ("open", "close", "ema", "vwap"):
        if c in m:
            m[c] = -df[c]
    return m


def _r(x: float) -> float:
    return float(round(x / cfg.TICK) * cfg.TICK) if np.isfinite(x) else x


def _make(symbol, strategy, side, df, i, tf, trig, stop, target_r, params, reason, target_cap=None, extra=None):
    """Build a Setup from long-space numbers (un-mirrors for shorts) and apply the global filters."""
    sgn = 1.0 if side == "long" else -1.0
    risk = trig - stop
    if not np.isfinite(risk) or risk <= cfg.TICK:
        return None
    target = trig + target_r * risk
    if target_cap is not None:
        target = min(target, target_cap)
    if target - trig < 1.0 * risk - 1e-9:       # less than 1R of room: skip
        return None
    trig, stop, target = sgn * trig, sgn * stop, sgn * target
    if side == "long" and trig < cfg.LONG_MIN_PRICE:
        return None
    if side == "short" and trig < cfg.SHORT_MIN_PRICE:
        return None
    t0 = df.index[i]
    ready = t0 + pd.Timedelta(minutes=tf)
    t_ready, cut_t, _, _ = bars.times_at(ready)                  # early-close days cut off earlier
    if t_ready >= cut_t:
        return None
    valid = ready + pd.Timedelta(minutes=tf * params["entry_valid_bars"])
    cutoff = ready.tz_convert(cfg.TZ).normalize() + pd.Timedelta(hours=cut_t.hour, minutes=cut_t.minute)
    valid = min(valid, cutoff.tz_convert("UTC"))
    return Setup(symbol, strategy, side, t0, ready, _r(trig), _r(stop), _r(target), valid, reason, extra or {})


# ------------------------------------------------------------------ Power Ranger (opening range breakout)
def power_ranger(symbol, df, i, ctx, memo, params=None):
    p = params or cfg.STRATEGIES["power_ranger"]
    k = int(p["range_minutes"] // p["timeframe_min"])
    if i != k - 1 or len(df) < k:
        return []
    rng = df.iloc[:k]
    gap = ctx.basis_gap_pct if ctx is not None and ctx.basis_gap_pct is not None else None
    if gap is None and ctx is not None and ctx.prev_close:
        gap = (rng.open.iloc[0] / ctx.prev_close - 1) * 100
    if gap is None:
        return []
    out = []
    for side in ("long", "short"):
        if side == "long" and gap < p["min_gap_pct"]:
            continue
        if side == "short" and gap > -p["min_gap_pct"]:
            continue
        d = rng if side == "long" else mirror(rng)
        first_low = d.low.iloc[0]
        counter = d[d.close < d.open]                       # early bar against the gap
        broke = bool((d.low.iloc[1:] < first_low).any()) if k > 1 else False
        if broke and p.get("skip_if_first_bar_broken", True):
            continue                                        # review: quality drops when the opening low breaks
        if p["require_failed_counter_bar"] and counter.empty:
            continue
        hi, lo = d.high.max(), d.low.min()
        datr = ctx.daily_atr if ctx is not None else None
        if datr:
            width = (hi - lo) / datr
            if width > p["max_range_atr"] or width < p["min_range_atr"]:
                continue
        trig = hi + cfg.TICK
        stop = lo - cfg.TICK
        if p["stop_mode"] == "vwap":
            vw = d.vwap.iloc[-1]
            if vw < trig:
                stop = vw - cfg.TICK
        s = _make(symbol, "power_ranger", side, df, i, p["timeframe_min"], trig, stop, p["target_r"], p,
                  f"gap {gap:+.1f}%, range {hi - lo:.2f}" + (", failed counter bar" if len(counter) else ""))
        if s:
            out.append(s)
    return out


# ------------------------------------------------------------------ Cupid (EMA / VWAP pullback)
def _cupid_long(d, i, p):
    if i < 6:
        return None
    row = d.iloc[i]
    a = row.atr
    if not (a > 0):
        return None
    if not (row.close > row.open and row.close > row.ema and row.close > row.vwap):
        return None
    if row.body < p["min_trigger_body_atr"] * a:
        return None
    if not (d.ema.iloc[i] > d.ema.iloc[max(0, i - 3)]):
        return None
    lo_w = max(0, i - p["max_pullback_bars"] - 1)
    win = d.iloc[lo_w:i]
    h = lo_w + int(np.argmax(win.high.values))           # swing high before the pullback
    n_pb = i - h - 1
    if n_pb < p["min_pullback_bars"] or n_pb > p["max_pullback_bars"]:
        return None
    pb = d.iloc[h + 1:i]
    highs = pb.high.values
    if np.any(np.diff(highs) > 0.10 * a):                 # sequential: no higher highs inside the pullback
        return None
    seq = d.iloc[h:i]
    for j in range(1, len(seq)):
        a_, b_ = seq.iloc[j - 1], seq.iloc[j]
        ov = min(a_.high, b_.high) - max(a_.low, b_.low)
        if b_.range > 0 and ov / b_.range >= p["max_overlap"]:
            return None                                   # choppy, not smooth
    H = d.high.iloc[h]
    L = d.low.iloc[:h + 1].min()
    leg = H - L
    if leg < p["min_leg_atr"] * a:
        return None
    P = min(pb.low.min(), row.low)
    retr = (H - P) / leg
    if retr < p["pullback_min"] or retr > p["pullback_max"]:
        return None
    tol = p["ref_tolerance_atr"] * a
    near_ema = bool(((pb.low - pb.ema).abs() <= tol).any() or (pb.low <= pb.ema).any())
    near_vwap = bool(((pb.low - pb.vwap).abs() <= tol).any())
    if not (near_ema or near_vwap):
        return None
    va = row.vol_avg
    vmult = row.volume / va if va and va > 0 else np.nan
    if np.isfinite(vmult) and vmult < p["trigger_volume_mult"]:
        return None
    tr = p["big_volume_target_r"] if np.isfinite(vmult) and vmult >= p["big_volume_mult"] else p["target_r"]
    ref = "9EMA" if near_ema else "VWAP"
    return row.high + cfg.TICK, P - cfg.TICK, tr, f"{retr:.0%} pullback to {ref}, vol x{vmult:.1f}"


def cupid(symbol, df, i, ctx, memo, params=None):
    p = params or cfg.STRATEGIES["cupid"]
    out = []
    for side in ("long", "short"):
        d = df if side == "long" else mirror(df)
        r = _cupid_long(d, i, p)
        if r:
            s = _make(symbol, "cupid", side, df, i, p["timeframe_min"], r[0], r[1], r[2], p, r[3])
            if s:
                out.append(s)
    return out


# ------------------------------------------------------------------ Tug-Of-War (chop on the 9 EMA, then the break)
def _tow_long(d, i, p):
    if i < p["min_bars"] + 1:
        return None
    row = d.iloc[i]
    best = None
    for m in range(p["min_bars"], min(p["max_bars"], i) + 1):
        box = d.iloc[i - m:i]
        a = box.atr.iloc[-1]
        if not (a > 0):
            continue
        hi, lo = box.high.max(), box.low.min()
        if hi - lo > p["max_range_atr"] * a:
            break                                           # boxes only get taller going back
        above = (box.close > box.ema).astype(int).values
        crosses = int(np.abs(np.diff(above)).sum())
        slope = abs(box.ema.iloc[-1] - box.ema.iloc[0]) / m
        if crosses >= p["min_ema_crosses"] and slope <= p["max_ema_slope_atr"] * a:
            best = (m, hi, lo, crosses)
    if best is None:
        return None
    m, hi, lo, crosses = best
    if not (row.close > hi and row.close > row.ema and row.close > row.open):
        return None
    return row.high + cfg.TICK, lo - cfg.TICK, f"{m}-bar chop, {crosses} EMA crosses, break above {hi:.2f}"


def tug_of_war(symbol, df, i, ctx, memo, params=None):
    p = params or cfg.STRATEGIES["tug_of_war"]
    out = []
    for side in ("long", "short"):
        d = df if side == "long" else mirror(df)
        r = _tow_long(d, i, p)
        if r:
            s = _make(symbol, "tug_of_war", side, df, i, p["timeframe_min"], r[0], r[1], p["target_r"], p, r[2])
            if s:
                out.append(s)
    return out


# ------------------------------------------------------------------ Roller Coaster (waterfall reversal)
def _rc_long(d, i, p):
    lb = p["lookback_bars"]
    if i < lb + 2:
        return None
    row = d.iloc[i]
    win = d.iloc[i - lb:i]
    a_ref = d.atr.iloc[i - lb - 1]
    if not (a_ref > 0):
        return None
    wide = win[(win.close < win.open) & (win.range >= p["wide_bar_mult"] * a_ref)]
    if len(wide) < p["min_wide_bars"]:
        return None
    top = win.high.max()
    bottom = min(win.low.min(), row.low)
    if top - bottom < p["min_move_atr"] * a_ref:
        return None
    if row.vwap - row.close < p["min_vwap_dist_atr"] * a_ref:
        return None
    if row.low > win.low.min() + 0.25 * a_ref:
        return None                                          # the reversal bar must be at the bottom
    rng = row.range
    if rng <= 0:
        return None
    body = abs(row.close - row.open) / rng
    tail = (min(row.open, row.close) - row.low) / rng
    if not ((row.close > row.open and body <= p["max_reversal_body"]) or tail >= p["min_tail"]):
        return None
    cap = bottom + p["max_retrace_target"] * (top - bottom)
    return row.high + cfg.TICK, row.low - cfg.TICK, cap, f"waterfall {top - bottom:.2f} ({len(wide)} wide bars), tail {tail:.0%}"


def roller_coaster(symbol, df, i, ctx, memo, params=None):
    p = params or cfg.STRATEGIES["roller_coaster"]
    out = []
    for side in ("long", "short"):
        used = memo.setdefault(f"rc_{side}", 0)
        if used >= p["max_entries"]:
            continue
        d = df if side == "long" else mirror(df)
        r = _rc_long(d, i, p)
        if r:
            s = _make(symbol, "roller_coaster", side, df, i, p["timeframe_min"], r[0], r[1], p["target_r"], p, r[3],
                      target_cap=r[2])
            if s:
                memo[f"rc_{side}"] = used + 1
                out.append(s)
    return out


# ------------------------------------------------------------------ ORB Desk v2 retest (A = retest close, B = confirmed swing)
# Port of ORB_Desk_v2 (compare_orb.first_opportunity / orb_desk_strategy.pine) into the DT_Iris engine.
# One locked opportunity per symbol, side and day: the FIRST economically eligible retest. A enters on
# that retest close; B waits two more 1-minute bars for a strict 2-left/2-right swing on the same wick.
# The hidden-RSI tag (C) is recorded in the reason text ("rsi_tag=yes|no|na") and never filters.
# Differences from ORB Desk, by design of the DT_Iris engine: entry is a stop-limit at the cap (it needs
# price to trade through the cap next minute instead of resting a buy limit), the watchlist is the
# gapper list (gap direction) instead of 2x opening RVOL, RSI starts each session (no carry-over),
# and management (break-even, 15:00 flatten, daily limits) is the shared dt_sim policy.
def _wilder_rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    gain = loss = 0.0
    for j in range(1, len(close)):
        ch = close[j] - close[j - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if j <= n:
            gain += up / n
            loss += dn / n
        else:
            gain = ((n - 1) * gain + up) / n
            loss = ((n - 1) * loss + dn) / n
        if j >= n and gain + loss > 0:
            out[j] = 100.0 * gain / (gain + loss)
    return out


def _ticks(x: float) -> int:
    return int(np.floor(x / cfg.TICK + 0.5))


def _orb_plan(c: float, vw: float, wick: float, hi: float, width: float, p: dict):
    """Long-space entry cap and stop for a decision bar closing at `c`, or None if a check fails."""
    if not (c > vw):
        return None
    cap_t = _ticks(c) + p["entry_cap_ticks"]
    stop_t = _ticks(wick) - p["stop_buffer_ticks"]
    risk_t = cap_t - stop_t
    if risk_t < 1:
        return None
    cap, stop, risk = cap_t * cfg.TICK, stop_t * cfg.TICK, risk_t * cfg.TICK
    ext = cap - hi
    if round(ext / cfg.TICK, 8) < 0 or round((ext - p["max_chase_range"] * width) / cfg.TICK, 8) > 0:
        return None                                           # entry too extended
    if round((p["est_spread"] - p["max_spread_risk"] * risk) / cfg.TICK, 8) > 0:
        return None                                           # estimated spread too big for the risk
    return cap, stop


def _orb_first_opportunity(m, h, l, c, vw, rsi, k: int, p: dict):
    """Scan rows 0..len-1 (long space). Returns (a_idx, a_plan, b_plan, tag) or None.

    b_plan is None when B failed OR is not yet decidable (row a_idx + 2 not seen yet)."""
    hi, lo = max(h[:k]), min(l[:k])
    width = hi - lo
    tol = p["retest_ticks"] * cfg.TICK + 1e-8
    gate = bars.hm(p["last_decision"])
    gate_m = gate.hour * 60 + gate.minute                     # decision bar must START by last_decision - 1
    win = p["retest_window_min"]
    open_end = m[0] + k

    def strict_low(i):
        if i < 2 or i + 2 >= len(m) or m[i + 2] - m[i - 2] != 4:
            return False
        return all(l[i] < l[j] for j in (i - 2, i - 1, i + 1, i + 2))

    latest_pivot = pivot_before = armed_at = None
    for i in range(len(m)):
        if m[i] + 1 > gate_m:
            break
        consecutive = i > 0 and m[i] == m[i - 1] + 1
        if armed_at is not None and (not consecutive or m[i] - armed_at > win or c[i] < hi - tol):
            armed_at = None
        if armed_at is not None and m[i] > armed_at and abs(l[i] - hi) <= tol and c[i] > hi:
            aplan = _orb_plan(c[i], vw[i], l[i], hi, width, p)
            if aplan:
                bplan = tag = None
                j = i + 2
                if (j < len(m) and strict_low(i) and m[j] - armed_at <= win and m[j] + 1 <= gate_m
                        and all(c[x] > hi for x in range(i + 1, j + 1))):
                    bplan = _orb_plan(c[j], vw[j], l[i], hi, width, p)
                    if bplan and pivot_before is not None and np.isfinite(rsi[pivot_before]) and np.isfinite(rsi[i]):
                        tag = bool(l[i] > l[pivot_before] and rsi[i] < rsi[pivot_before])
                return i, aplan, bplan, tag
        if m[i] >= open_end and consecutive and armed_at is None and c[i - 1] <= hi and c[i] > hi:
            armed_at = m[i]
            pivot_before = latest_pivot
        if strict_low(i - 2):
            latest_pivot = i - 2
    return None


def _orb_retest(symbol, df, i, ctx, memo, name):
    p = cfg.STRATEGIES[name]
    k = int(p["range_minutes"])
    if i < k:
        return []
    t = df.index[: i + 1].tz_convert(cfg.TZ)
    m = np.asarray(t.hour * 60 + t.minute)
    gate = bars.hm(p["last_decision"])
    if m[i] + 1 > gate.hour * 60 + gate.minute:
        return []
    if not (m[0] == 570 and m[k - 1] == 570 + k - 1 and (df.volume.values[:k] > 0).all()):
        return []                                             # complete 09:30 opening interval required
    if df.open.iloc[0] < p["min_open_price"]:
        return []
    adv = ctx.avg_daily_volume if ctx is not None else None
    if p["min_avg_daily_volume"] and (adv is None or not adv == adv or adv < p["min_avg_daily_volume"]):
        return []
    out = []
    for side in ("long", "short"):
        if side == "long":
            h, l, c = (df[x].values[: i + 1] for x in ("high", "low", "close"))
            vw = df.vwap.values[: i + 1]
        else:                                                 # mirrored prices: short = long rules upside down
            h, l, c = -df.low.values[: i + 1], -df.high.values[: i + 1], -df.close.values[: i + 1]
            vw = -df.vwap.values[: i + 1]
        opp = _orb_first_opportunity(m, h, l, c, vw, _wilder_rsi(c), k, p)
        if opp is None:
            continue
        a_idx, aplan, bplan, tag = opp
        if p["entry"] == "A" and a_idx == i:
            plan_ = aplan
        elif p["entry"] == "B" and a_idx + 2 == i and bplan:
            plan_ = bplan
        else:
            continue
        tag_txt = "na" if tag is None else ("yes" if tag else "no")
        s = _make(symbol, name, side, df, i, 1, plan_[0], plan_[1], p["target_r"], p,
                  f"ORB {p['entry']} retest {t[a_idx].strftime('%H:%M')} ET, rsi_tag={tag_txt}",
                  extra={"rsi_tag": tag_txt})
        if s:
            out.append(s)
    return out


def orb_retest_a(symbol, df, i, ctx, memo, params=None):
    return _orb_retest(symbol, df, i, ctx, memo, "orb_retest_a")


def orb_retest_b(symbol, df, i, ctx, memo, params=None):
    return _orb_retest(symbol, df, i, ctx, memo, "orb_retest_b")


DETECTORS = {
    "power_ranger": power_ranger,
    "cupid": cupid,
    "tug_of_war": tug_of_war,
    "roller_coaster": roller_coaster,
    "orb_retest_a": orb_retest_a,
    "orb_retest_b": orb_retest_b,
}


def timeframe(strategy: str) -> int:
    return int(cfg.STRATEGIES[strategy]["timeframe_min"])


def session_setups(symbol: str, reg1: pd.DataFrame, ctx, strategies: list[str], sides=("long", "short")) -> list[Setup]:
    """Every setup a finished regular session would have produced, in time order (backtest path)."""
    found: list[Setup] = []
    frames: dict[int, pd.DataFrame] = {}
    for st in strategies:
        tf = timeframe(st)
        if tf not in frames:
            frames[tf] = bars.add_indicators(bars.resample(reg1, tf))
        df = frames[tf]
        memo: dict = {}
        for i in range(len(df)):
            for s in DETECTORS[st](symbol, df, i, ctx, memo):
                if s.side in sides:
                    found.append(s)
    found.sort(key=lambda s: (s.ready_time, s.strategy))
    return found
