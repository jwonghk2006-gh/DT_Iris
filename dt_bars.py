"""Bars and indicators: 1-minute -> n-minute aggregation, 9 EMA, VWAP, ATR.

One rule fixes the bug found in the old Iris live monitor: an n-minute bar is
only built from n CONFIRMED (finished) 1-minute bars. The backtester and the
live runner both use `BarBuilder`, so they see exactly the same bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime

import numpy as np
import pandas as pd

import dt_config as cfg

COLS = ["open", "high", "low", "close", "volume"]


def hm(text: str) -> dtime:
    h, m = text.split(":")
    return dtime(int(h), int(m))


OPEN_T, CLOSE_T = hm(cfg.SESSION_OPEN), hm(cfg.SESSION_CLOSE)
CUTOFF_T, FLATTEN_T = hm(cfg.ENTRY_CUTOFF), hm(cfg.FLATTEN_TIME)
PRE_T = hm(cfg.PREMARKET_START)


# ------------------------------------------------------------------ loading / shaping
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Any 1-minute frame -> index = UTC timestamp of the bar START, columns open..volume."""
    df = df.copy()
    tcol = next((c for c in ("timestamp", "time", "datetime", "date") if c in df.columns), None)
    if tcol is not None:
        df.index = pd.to_datetime(df[tcol], utc=True)
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("no time column")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")
    df.index.name = "time"
    out = df[COLS].astype(float)
    return out[~out.index.duplicated(keep="last")].sort_index()


def ny(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index.tz_convert(cfg.TZ)


def split_sessions(df: pd.DataFrame) -> dict:
    """{ny_date: frame with ALL bars of that NY calendar day} (premarket + regular + after)."""
    t = ny(df)
    return {d: g for d, g in df.groupby(t.date)}


def regular(df_day: pd.DataFrame) -> pd.DataFrame:
    t = ny(df_day).time
    return df_day[(t >= OPEN_T) & (t < CLOSE_T)]


def premarket(df_day: pd.DataFrame) -> pd.DataFrame:
    t = ny(df_day).time
    return df_day[(t >= PRE_T) & (t < OPEN_T)]


# ------------------------------------------------------------------ aggregation
@dataclass
class BarBuilder:
    """Feed confirmed 1-minute bars in time order; get back completed n-minute bars.

    Groups are aligned to the 09:30 open (09:30-09:31 for n=2 is one bar,
    09:32-09:33 the next ...). A group is released only when its LAST minute
    arrives, or `flush_until` says its end time has passed (a missing minute
    with no trades). An unfinished bar is never released.
    """
    n: int
    _key: object = None
    _rows: list = field(default_factory=list)

    def _group(self, ts: pd.Timestamp) -> tuple[pd.Timestamp, int]:
        t = ts.tz_convert(cfg.TZ)
        open_ = t.normalize() + pd.Timedelta(hours=OPEN_T.hour, minutes=OPEN_T.minute)
        mins = int((t - open_).total_seconds() // 60)
        g = mins // self.n
        start = open_ + pd.Timedelta(minutes=g * self.n)
        return start.tz_convert("UTC"), mins % self.n

    def _emit(self) -> dict | None:
        if not self._rows:
            return None
        r = self._rows
        bar = {"time": self._key, "open": r[0]["open"], "high": max(x["high"] for x in r),
               "low": min(x["low"] for x in r), "close": r[-1]["close"], "volume": sum(x["volume"] for x in r)}
        self._rows, self._key = [], None
        return bar

    def add(self, ts: pd.Timestamp, o: float, h: float, l: float, c: float, v: float) -> list[dict]:
        out = []
        start, pos = self._group(ts)
        if self._key is not None and start != self._key:
            b = self._emit()
            if b:
                out.append(b)
        self._key = start
        self._rows.append({"open": o, "high": h, "low": l, "close": c, "volume": v})
        if pos == self.n - 1:
            b = self._emit()
            if b:
                out.append(b)
        return out

    def flush_until(self, now: pd.Timestamp) -> list[dict]:
        if self._key is not None and now >= self._key + pd.Timedelta(minutes=self.n):
            b = self._emit()
            return [b] if b else []
        return []


def resample(df1: pd.DataFrame, n: int) -> pd.DataFrame:
    """Regular-session 1-minute frame -> n-minute frame, using BarBuilder (same path as live)."""
    if n == 1 or df1.empty:
        return df1.copy()
    bb = BarBuilder(n)
    rows = []
    for r in df1.itertuples():
        ts = r.Index
        rows += bb.add(ts, r.open, r.high, r.low, r.close, r.volume)
    rows += bb.flush_until(df1.index[-1] + pd.Timedelta(minutes=n + 1))
    out = pd.DataFrame(rows).set_index("time")
    out.index.name = "time"
    return out[COLS]


# ------------------------------------------------------------------ indicators
def ema(x: pd.Series, n: int = cfg.EMA_LEN) -> pd.Series:
    return x.ewm(span=n, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP from the first bar of `df` (pass one regular session)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan).fillna(0.0)
    cum_v = vol.cumsum()
    v = (tp * vol).cumsum() / cum_v.replace(0, np.nan)
    return v.fillna(tp)


def atr(df: pd.DataFrame, n: int = cfg.ATR_LEN) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=1).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds ema, vwap, atr, vol_avg, range, body to one session's bars."""
    out = df.copy()
    out["ema"] = ema(out["close"])
    out["vwap"] = vwap(out)
    out["atr"] = atr(out)
    out["vol_avg"] = out["volume"].rolling(cfg.VOLUME_AVG_LEN, min_periods=3).mean().shift(1)
    out["range"] = out["high"] - out["low"]
    out["body"] = (out["close"] - out["open"]).abs()
    return out


def daily_from_minutes(df1: pd.DataFrame) -> pd.DataFrame:
    """Daily OHLCV from regular-session minutes (index = NY date)."""
    rows = {}
    for d, g in split_sessions(df1).items():
        r = regular(g)
        if r.empty:
            continue
        rows[d] = {"open": r.open.iloc[0], "high": r.high.max(), "low": r.low.min(), "close": r.close.iloc[-1],
                   "volume": r.volume.sum()}
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def daily_atr(daily: pd.DataFrame, n: int = cfg.DAILY_ATR_LEN) -> float | None:
    if daily is None or len(daily) < 2:
        return None
    return float(atr(daily, n).iloc[-1])


@dataclass
class DayContext:
    """What is known before the open: used by watchlist filters and by Power Ranger."""
    symbol: str
    date: object
    prev_close: float | None
    premarket_volume: float
    premarket_last: float | None
    daily_atr: float | None
    avg_daily_volume: float | None

    @property
    def gap_pct(self) -> float | None:
        if not self.prev_close or self.premarket_last is None:
            return None
        return (self.premarket_last / self.prev_close - 1.0) * 100.0


def day_context(symbol: str, day, sessions: dict, daily: pd.DataFrame | None = None) -> DayContext:
    """Context for `day` built from minute sessions (prev close = last regular close before `day`)."""
    days = sorted(sessions)
    prior = [d for d in days if d < day]
    prev_close = None
    for d in reversed(prior):
        r = regular(sessions[d])
        if not r.empty:
            prev_close = float(r.close.iloc[-1])
            break
    if daily is not None and not daily.empty:
        before = daily[daily.index < day]
        if prev_close is None and len(before):
            prev_close = float(before.close.iloc[-1])
    else:
        before = None
    pm = premarket(sessions[day]) if day in sessions else pd.DataFrame(columns=COLS)
    pm_last = float(pm.close.iloc[-1]) if len(pm) else None
    if pm_last is None and day in sessions:
        r = regular(sessions[day])
        pm_last = float(r.open.iloc[0]) if len(r) else None
    datr = daily_atr(before) if before is not None and len(before) >= 2 else None
    adv = float(before.volume.tail(20).mean()) if before is not None and len(before) else None
    return DayContext(symbol, day, prev_close, float(pm.volume.sum()) if len(pm) else 0.0, pm_last, datr, adv)
