"""Historical gapper finder: builds the research sample DT_Iris actually trades.

    python dt_gappers.py --months 12             # universe -> daily bars -> gap days -> 1-minute bars
    python dt_gappers.py --months 12 --no-minutes  # only find the gap days

Steps (each one is cached, so a re-run only fetches what is missing):
1. Universe: every US security on Longbridge, kept if it is on a main exchange
   (board USMain) and is not an ETF/ETN/fund or a depositary receipt (ADR), as in
   the course screener ("remove Depositary Receipt").
2. Daily bars (forward-adjusted, so splits do not look like gaps) -> data/daily/<SYM>.parquet
3. Gap days: |open / previous close - 1| >= MIN_GAP_PCT, long if up and open >= LONG_MIN_PRICE ($5),
   short if down and open >= $25, 20-day average volume >= MIN_AVG_DAILY_VOLUME.
   Top MAX_WATCHLIST per side per day, ranked by |gap| x average dollar volume,
   the same cap the live scanner uses. -> data/gappers.csv
4. 1-minute bars (all sessions) for every gap day -> data/bars/

Honest limits:
- The universe is TODAY's listed stocks, so names that were delisted are missing
  (survivorship bias: the sample is a little too kind).
- The live scanner ranks by PREMARKET volume; history ranks by average volume,
  because premarket volume is only known after the minutes are downloaded.
- Longbridge volume covers only part of the US exchanges (about 55-60% of IBKR's), so
  the 500k average-volume floor is stricter than the course meant.
"""
from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_data as data

DAILY_DIR = cfg.DATA_DIR / "daily"
UNIVERSE_FILE = cfg.DATA_DIR / "universe_all.csv"
GAPPERS_FILE = cfg.DATA_DIR / "gappers.csv"
HISTORY_SYMBOLS_FILE = cfg.DATA_DIR / "history_symbols.txt"   # symbols inside the Longbridge history quota
WORKERS = 4
PAUSE = 0.15                     # seconds between calls per worker (Longbridge allows about 10 quote calls/s)
EXCLUDE_WORDS = (" ETF", " ETN", "FUND", "PROSHARES", "DIREXION", " 2X", " 3X", "-2X", "-3X", "LEVERAGED",
                 "DEPOSITARY", " ADR", "ADS ", "ISHARES", "SPDR", "INVESCO QQQ", "WARRANT", " UNIT", " RIGHT",
                 " FD ", "- CW", "(S/R)", " WT ", "ACQUISITION", " INCOME ", " TRUST ", "MUNICIPAL", "TREASURY",
                 " ISHRS ", " VG ", " BOND ")     # Longbridge abbreviates iShares as ISHRS, Vanguard as VG

_local = threading.local()


def _ctx():
    import dt_broker as broker
    if not hasattr(_local, "ctx"):
        _local.ctx = broker.quote_context()
    return _local.ctx


# ------------------------------------------------------------------ 1. universe
def build_universe(refresh: bool = False) -> pd.DataFrame:
    if UNIVERSE_FILE.exists() and not refresh:
        return pd.read_csv(UNIVERSE_FILE, dtype={"symbol": str, "name": str}, keep_default_na=False)
    from longport.openapi import Market
    import dt_broker as broker
    syms = [s.symbol for s in _ctx().security_list(Market.US)]
    rows = []
    for k in range(0, len(syms), 400):
        for info in _ctx().static_info(syms[k:k + 400]):
            rows.append({"symbol": broker.plain(info.symbol), "name": info.name_en or "",
                         "board": str(info.board).split(".")[-1], "eps_ttm": float(info.eps_ttm or 0),
                         "bps": float(info.bps or 0), "total_shares": int(info.total_shares or 0)})
        time.sleep(PAUSE)
    df = pd.DataFrame(rows)
    name = (" " + df["name"].str.upper() + " ")
    bad_name = np.zeros(len(df), dtype=bool)
    for w in EXCLUDE_WORDS:
        bad_name |= name.str.contains(w, regex=False).values
    plain_sym = df["symbol"].str.fullmatch(r"[A-Z]{1,5}")
    fund_like = (df["eps_ttm"] == 0) & (df["bps"] == 0)          # ETFs and funds report no earnings and no book value
    df["keep"] = (df["board"] == "USMain") & ~bad_name & plain_sym & ~fund_like
    UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(UNIVERSE_FILE, index=False)
    return df


def tradable_symbols(uni: pd.DataFrame | None = None) -> list[str]:
    """Universe symbols the scanner and recorder use: `keep` rows (each once), minus EXCLUDE_SYMBOLS.
    Share classes were stored without their suffix (BRK.B -> BRK). Most such tickers still quote (the main
    class), but six never do; they are in EXCLUDE_SYMBOLS and skipped (the user's choice, 2026-11-25)."""
    uni = build_universe() if uni is None else uni
    return sorted(set(uni[uni["keep"]]["symbol"].astype(str)) - set(cfg.EXCLUDE_SYMBOLS))


# ------------------------------------------------------------------ 2. daily bars
def _daily_path(sym: str):
    return DAILY_DIR / f"{sym}.parquet"


def fetch_daily(sym: str, start: date, end: date, force: bool = False) -> int:
    """Forward-adjusted daily bars into data/daily. force=True re-downloads even a recent-looking file
    (needed after a split: the old file's prices are on the old share basis)."""
    from longport.openapi import AdjustType, Period
    import dt_broker as broker
    p = _daily_path(sym)
    if p.exists() and not force:
        old = pd.read_parquet(p)
        if len(old) and pd.Timestamp(old.index.max()).date() >= end - timedelta(days=4) \
                and pd.Timestamp(old.index.min()).date() <= start + timedelta(days=7):
            return len(old)
    # candlesticks() (latest N bars) is NOT counted in the history-symbol quota; history_candlesticks_by_date is.
    n = min(1000, int((end - start).days * 0.72) + 30)
    rows = _ctx().candlesticks(broker.lb_symbol(sym), Period.Day, n, AdjustType.ForwardAdjust)
    time.sleep(PAUSE)
    recs = [{"date": broker.to_utc(c.timestamp).tz_convert(cfg.TZ).date(), "open": float(c.open), "high": float(c.high),
             "low": float(c.low), "close": float(c.close), "volume": float(c.volume)} for c in rows]
    if not recs:
        return 0
    df = pd.DataFrame(recs).drop_duplicates("date").set_index("date").sort_index()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)
    return len(df)


def load_daily(sym: str) -> pd.DataFrame | None:
    p = _daily_path(sym)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df.index = pd.to_datetime(df.index).date
    return df


def run_parallel(fn, items, label: str) -> list:
    out, t0, done = [], time.time(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(fn, *it): it for it in items}
        for f in as_completed(futs):
            done += 1
            try:
                out.append((futs[f], f.result(), None))
            except Exception as exc:
                out.append((futs[f], None, f"{type(exc).__name__}: {str(exc)[:120]}"))
            if done % 200 == 0 or done == len(items):
                rate = done / max(time.time() - t0, 1e-9)
                print(f"  {label}: {done}/{len(items)}  ({rate:.1f}/s, ~{(len(items) - done) / max(rate, 1e-9) / 60:.0f} min left)",
                      flush=True)
    return out


# ------------------------------------------------------------------ 3. gap days
def daily_features(d: pd.DataFrame) -> pd.DataFrame:
    f = d.copy()
    f["prev_close"] = f["close"].shift(1)
    f["gap_pct"] = (f["open"] / f["prev_close"] - 1) * 100
    f["adv20"] = f["volume"].rolling(20, min_periods=10).mean().shift(1)
    f["dollar_vol20"] = (f["volume"] * f["close"]).rolling(20, min_periods=10).mean().shift(1)
    f["atr14"] = bars.atr(f[["open", "high", "low", "close"]].assign(volume=0), cfg.DAILY_ATR_LEN).shift(1)
    return f


def find_gappers(symbols: list[str], start: date, end: date) -> pd.DataFrame:
    rows = []
    for sym in symbols:
        d = load_daily(sym)
        if d is None or len(d) < 25:
            continue
        f = daily_features(d)
        f = f[(f.index >= start) & (f.index <= end)]
        g = f[(f["gap_pct"].abs() >= cfg.MIN_GAP_PCT) & (f["adv20"] >= cfg.MIN_AVG_DAILY_VOLUME)]
        for day, r in g.iterrows():
            side = "long" if r.gap_pct > 0 else "short"
            px = r.raw_open if "raw_open" in f.columns and r.raw_open == r.raw_open else r.open   # price as traded that day
            if side == "long" and px < cfg.LONG_MIN_PRICE:
                continue
            if side == "short" and px < cfg.SHORT_MIN_PRICE:
                continue
            rows.append({"date": day, "symbol": sym, "side": side, "gap_pct": round(r.gap_pct, 2), "open_adj": r.open,
                         "prev_close_adj": r.prev_close, "atr14_adj": r.atr14, "adv20": r.adv20,
                         "dollar_vol20": r.dollar_vol20, "score": abs(r.gap_pct) * r.dollar_vol20})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = (df.sort_values("score", ascending=False).groupby(["date", "side"], group_keys=False)
            .head(cfg.MAX_WATCHLIST).sort_values(["date", "side", "score"], ascending=[True, True, False]))
    return df.reset_index(drop=True)


def load_gappers() -> pd.DataFrame:
    if not GAPPERS_FILE.exists():
        return pd.DataFrame()
    df = pd.read_csv(GAPPERS_FILE, dtype={"symbol": str}, keep_default_na=False)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def gapper_lookup() -> dict:
    g = load_gappers()
    return {(r.symbol, r.date): r for r in g.itertuples()} if len(g) else {}


def context_from_gapper(row, sym: str, day, sessions: dict) -> bars.DayContext:
    """DayContext for a gap day in the units of the (unadjusted) minute bars.

    Daily bars are forward-adjusted; the minute bars are not. Scaling by
    (minute open / adjusted open) converts the previous close and ATR into the
    minute bars' units, which also keeps a split between the two days from
    looking like a gap.
    """
    reg = bars.regular(sessions[day])
    pm = bars.premarket(sessions[day])
    k = float(reg.open.iloc[0]) / row.open_adj if row.open_adj else 1.0
    return bars.DayContext(sym, day, row.prev_close_adj * k, float(pm.volume.sum()) if len(pm) else 0.0,
                           float(pm.close.iloc[-1]) if len(pm) else float(reg.open.iloc[0]),
                           row.atr14_adj * k if row.atr14_adj == row.atr14_adj else None, row.adv20)


# ------------------------------------------------------------------ 4. minutes
def history_symbols() -> set[str]:
    """Symbols whose minute history we may request. Longbridge counts DISTINCT symbols per quota
    period (400 on this account); symbols already used can be requested again."""
    if not HISTORY_SYMBOLS_FILE.exists():
        return set()
    return {l.strip().upper() for l in HISTORY_SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}


def fetch_minutes(sym: str, day: date) -> int:
    import dt_broker as broker
    p = data._path(sym, day)
    if p.exists():
        return -1
    df = broker.history_1m(sym, day)
    time.sleep(PAUSE)
    if df.empty:
        return 0
    data.save_day(sym, day, data.fix_bar_start(bars.normalize(df)))
    return len(df)


def main(months: int, minutes: bool, refresh_universe: bool) -> None:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(months * 30.5))
    print(f"DT_Iris gapper finder: {start} .. {end}")
    uni = build_universe(refresh_universe)
    keep = sorted(uni[uni["keep"]]["symbol"])
    print(f"universe: {len(uni)} US securities, {len(keep)} kept (main exchange, no ETF/ETN/fund/ADR)")
    res = run_parallel(fetch_daily, [(s, start - timedelta(days=45), end) for s in keep], "daily bars")
    errs = [e for _, _, e in res if e]
    print(f"daily bars done, {len(errs)} errors" + (f" (first: {errs[0]})" if errs else ""))
    g = find_gappers(keep, start, end)
    GAPPERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    g.to_csv(GAPPERS_FILE, index=False)
    if g.empty:
        print("no gap days found")
        return
    print(f"gap days: {len(g)} symbol-days on {g['date'].nunique()} sessions "
          f"({(g.side == 'long').sum()} long, {(g.side == 'short').sum()} short) -> {GAPPERS_FILE}")
    if not minutes:
        return
    allowed = history_symbols()
    todo = [(r.symbol, r.date) for r in g.itertuples()
            if not data._path(r.symbol, r.date).exists() and r.symbol in allowed]
    blocked = sum(1 for r in g.itertuples() if r.symbol not in allowed)
    print(f"1-minute bars to fetch: {len(todo)}  ({blocked} gap days skipped: symbol outside the Longbridge "
          f"history quota of {len(allowed)} symbols; dt_record.py collects new days for free)")
    res = run_parallel(fetch_minutes, todo, "minute bars")
    errs = [e for _, _, e in res if e]
    empty = sum(1 for _, n, _ in res if n == 0)
    print(f"minute bars done: {len(todo) - len(errs) - empty} saved, {empty} empty, {len(errs)} errors"
          + (f" (first: {errs[0]})" if errs else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=float, default=12)
    ap.add_argument("--no-minutes", action="store_true")
    ap.add_argument("--refresh-universe", action="store_true")
    a = ap.parse_args()
    main(a.months, not a.no_minutes, a.refresh_universe)
