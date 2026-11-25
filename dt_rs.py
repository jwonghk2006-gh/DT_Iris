"""Relative strength / weakness: is a stock's move its own, or just the market's (or its sector's)?

    relative gap   = stock gap % - beta x SPY gap %
    sector gap     = stock gap % - sector beta x sector-ETF gap %
    intraday RS    = stock % since the open - beta x SPY % since the open   (RSTracker, minute by minute)

beta = cov(stock, SPY) / var(SPY) over the last BETA_DAYS daily returns, clamped to
[BETA_MIN, BETA_MAX]. The sector ETF is the one in SECTOR_ETFS whose daily returns correlate
best with the stock's (Longbridge has no sector field); none if the best correlation is below
SECTOR_MIN_CORR. Only data from BEFORE the scan day is used.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

import dt_config as cfg


# ------------------------------------------------------------------ daily data
def daily_closes(sym: str, day: date, fetch: bool = True) -> pd.Series | None:
    """Daily closes before `day` (split-adjusted), fetched quota-free when missing or too short/stale."""
    import dt_gappers as gap
    need = cfg.BETA_DAYS + 1
    d = gap.load_daily(sym)
    stale = d is None or not len(d) or max(d.index) < day - timedelta(days=5) or (d.index < day).sum() < need
    if stale and fetch:
        try:
            gap.fetch_daily(sym, day - timedelta(days=int(need * 1.6) + 10), day - timedelta(days=1))
        except Exception:
            pass
        d = gap.load_daily(sym)
    if d is None or not len(d):
        return None
    s = d[d.index < day]["close"].astype(float)
    return s if len(s) else None


def _returns(closes: pd.Series) -> pd.Series:
    return closes.pct_change().dropna().tail(cfg.BETA_DAYS)


def beta(stock: pd.Series, bench: pd.Series, min_days: int = 40) -> float | None:
    """Clamped beta of daily returns on the dates both have; None with fewer than min_days."""
    j = pd.concat([_returns(stock), _returns(bench)], axis=1, join="inner").dropna()
    if len(j) < min_days:
        return None
    var = float(j.iloc[:, 1].var())
    if var <= 0:
        return None
    b = float(j.iloc[:, 0].cov(j.iloc[:, 1]) / var)
    return float(np.clip(b, cfg.BETA_MIN, cfg.BETA_MAX))


def best_sector(stock: pd.Series, etfs: dict[str, pd.Series], min_days: int = 40) -> tuple[str | None, float | None, float | None]:
    """(etf, correlation, beta to it) for the best-correlated sector ETF, or (None, best corr, None)."""
    r = _returns(stock)
    best, best_c = None, -1.0
    for name, closes in etfs.items():
        j = pd.concat([r, _returns(closes)], axis=1, join="inner").dropna()
        if len(j) < min_days:
            continue
        c = float(j.iloc[:, 0].corr(j.iloc[:, 1]))
        if c == c and c > best_c:
            best, best_c = name, c
    if best is None:
        return None, None, None
    if best_c < cfg.SECTOR_MIN_CORR:
        return None, round(best_c, 2), None
    return best, round(best_c, 2), beta(stock, etfs[best], min_days)


def rel_gap(gap_pct: float | None, bench_gap_pct: float | None, b: float | None) -> float | None:
    """Stock gap minus the part the benchmark explains. Missing beta -> 1.0; missing benchmark -> None."""
    if gap_pct is None or bench_gap_pct is None:
        return None
    return gap_pct - (1.0 if b is None else b) * bench_gap_pct


def could_pass(gap_pct: float | None, bench_gap_pct: float | None, threshold: float) -> bool:
    """Cheap pre-filter before beta is known: can |relative gap| reach `threshold` for ANY beta in
    [BETA_MIN, BETA_MAX]? (rel gap is linear in beta, so checking both ends is exact.)"""
    if gap_pct is None:
        return False
    if bench_gap_pct is None:
        return abs(gap_pct) >= threshold
    return any(abs(gap_pct - b * bench_gap_pct) >= threshold for b in (cfg.BETA_MIN, cfg.BETA_MAX, 1.0))


# ------------------------------------------------------------------ intraday
class RSTracker:
    """Minute-by-minute relative strength since the open: stock % - beta x benchmark %.
    Feed it every regular-session 1-minute bar of the stocks AND of the benchmark."""

    def __init__(self, betas: dict[str, float] | None = None, bench: str | None = None):
        self.bench = bench or cfg.RS_BENCHMARK
        self.betas = betas or {}
        self.open: dict[str, float] = {}
        self.last: dict[str, float] = {}

    def on_bar(self, sym: str, o: float, c: float) -> None:
        self.open.setdefault(sym, float(o))
        self.last[sym] = float(c)

    def change(self, sym: str) -> float | None:
        if sym not in self.open or not self.open[sym]:
            return None
        return (self.last[sym] / self.open[sym] - 1) * 100

    def rs(self, sym: str) -> float | None:
        s, m = self.change(sym), self.change(self.bench)
        if s is None or m is None:
            return None
        return s - self.betas.get(sym, 1.0) * m

    def label(self, sym: str) -> str:
        v = self.rs(sym)
        return "RS n/a" if v is None else f"RS {v:+.1f}% vs {self.bench}"
