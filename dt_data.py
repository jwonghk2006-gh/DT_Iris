"""DT_Iris data store: 1-minute bars, one parquet per symbol per NY day.

    data/bars/<SYMBOL>/<YYYY-MM-DD>.parquet     index = bar START time (UTC), all sessions
    data/manifest.json                          holdout cut date (fixed on first use)

Sources: Longbridge history (dt_broker.history_1m) or the old Iris IBKR CSVs.
Research code calls `load_days(..., research=True)`, which refuses holdout dates.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import dt_bars as bars
import dt_config as cfg

MANIFEST = cfg.DATA_DIR / "manifest.json"


def _path(symbol: str, day) -> Path:
    return cfg.BARS_DIR / symbol.upper() / f"{pd.Timestamp(day).date()}.parquet"


def fix_bar_start(df: pd.DataFrame) -> pd.DataFrame:
    """Some feeds stamp a bar with its END time. If the regular session starts at 09:31
    and has a 16:00 bar, shift everything back one minute."""
    if df.empty:
        return df
    t = df.index.tz_convert(cfg.TZ)
    hhmm = t.hour * 100 + t.minute
    if (hhmm == 930).sum() == 0 and (hhmm == 1600).sum() > 0 and (hhmm == 931).sum() > 0:
        df = df.copy()
        df.index = df.index - pd.Timedelta(minutes=1)
    return df


def save_day(symbol: str, day, df: pd.DataFrame) -> Path:
    p = _path(symbol, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    return p


def available(symbol: str | None = None) -> pd.DataFrame:
    """Table of (symbol, date) pairs on disk."""
    rows = []
    if not cfg.BARS_DIR.exists():
        return pd.DataFrame(columns=["symbol", "date"])
    dirs = [cfg.BARS_DIR / symbol.upper()] if symbol else sorted(cfg.BARS_DIR.iterdir())
    for d in dirs:
        if d.is_dir():
            rows += [{"symbol": d.name, "date": pd.Timestamp(f.stem).date()} for f in sorted(d.glob("*.parquet"))]
    return pd.DataFrame(rows, columns=["symbol", "date"])


# ------------------------------------------------------------------ holdout
def manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def holdout_cut() -> date | None:
    """First holdout date. Fixed the first time there are enough sessions; never moves."""
    m = manifest()
    if m.get("holdout_start"):
        return pd.Timestamp(m["holdout_start"]).date()
    days = sorted(set(available()["date"]))
    if len(days) < cfg.HOLDOUT_TRADING_DAYS * 3:
        return None                       # too little data to lock anything yet
    cut = days[-cfg.HOLDOUT_TRADING_DAYS]
    m.update({"holdout_start": str(cut), "holdout_days": cfg.HOLDOUT_TRADING_DAYS,
              "locked_on": str(date.today()), "released": False})
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")
    return cut


def is_holdout(day) -> bool:
    m = manifest()
    if m.get("released"):
        return False
    cut = holdout_cut()
    return cut is not None and pd.Timestamp(day).date() >= cut


def release_holdout() -> None:
    m = manifest()
    m["released"] = True
    m["released_on"] = str(date.today())
    MANIFEST.write_text(json.dumps(m, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ loading
def load_symbol(symbol: str, research: bool = True, days: list | None = None) -> pd.DataFrame:
    av = available(symbol)
    if av.empty:
        return pd.DataFrame(columns=bars.COLS)
    want = set(pd.Timestamp(d).date() for d in days) if days else None
    frames = []
    for d in av["date"]:
        if want is not None and d not in want:
            continue
        if research and is_holdout(d):
            continue
        frames.append(pd.read_parquet(_path(symbol, d)))
    if not frames:
        return pd.DataFrame(columns=bars.COLS)
    return bars.normalize(pd.concat(frames))


def load_days(symbols: list[str], research: bool = True) -> dict[str, pd.DataFrame]:
    return {s: load_symbol(s, research) for s in symbols}


# ------------------------------------------------------------------ importers
def import_iris_csv(path: Path) -> list[tuple[str, date, int]]:
    """Old Iris IBKR export (timestamp,symbol,open,high,low,close,volume) -> per-day parquet."""
    raw = pd.read_csv(path)
    out = []
    for sym, g in raw.groupby("symbol"):
        df = fix_bar_start(bars.normalize(g))
        for d, day_df in bars.split_sessions(df).items():
            p = _path(sym, d)
            if p.exists():
                old = pd.read_parquet(p)
                day_df = bars.normalize(pd.concat([old, day_df]))
            save_day(sym, d, day_df)
            out.append((sym, d, len(day_df)))
    return out


def fetch_longbridge(symbols: list[str], start: date, end: date, overwrite: bool = False) -> list[str]:
    """Download 1-minute bars (all sessions) for every trading day in [start, end]."""
    import dt_broker as broker
    from longport.openapi import Market
    days = broker.quote_context().trading_days(Market.US, start, end)
    trade_days = sorted(set(list(days.trading_days) + list(getattr(days, "half_trading_days", []))))
    log = []
    for sym in symbols:
        for d in trade_days:
            if _path(sym, d).exists() and not overwrite:
                continue
            try:
                df = broker.history_1m(sym, d)
            except Exception as exc:
                log.append(f"{sym} {d}: {type(exc).__name__}: {exc}")
                continue
            if df.empty:
                log.append(f"{sym} {d}: no bars")
                continue
            save_day(sym, d, fix_bar_start(bars.normalize(df)))
            log.append(f"{sym} {d}: {len(df)} bars")
    return log


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="DT_Iris stage 1: get 1-minute data")
    ap.add_argument("--import-iris", nargs="*", help="Iris *_1min.csv files to import")
    ap.add_argument("--symbols", help="comma list to fetch from Longbridge")
    ap.add_argument("--days", type=int, default=30, help="calendar days back to fetch")
    ap.add_argument("--release-holdout", action="store_true")
    a = ap.parse_args()
    if a.import_iris is not None:
        files = [Path(f) for f in a.import_iris] or sorted((cfg.ROOT.parent / "Iris" / "data" / "market").glob("*_1min.csv"))
        for f in files:
            res = import_iris_csv(f)
            print(f"{f.name}: {len(res)} symbol-days")
    if a.symbols:
        end = date.today()
        for line in fetch_longbridge([s.strip().upper() for s in a.symbols.split(",") if s.strip()],
                                     end - timedelta(days=a.days), end):
            print(line)
    if a.release_holdout:
        release_holdout()
        print("holdout released")
    print(f"holdout starts: {holdout_cut()}")
