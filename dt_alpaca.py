"""5-year research history from Alpaca (market data only - no orders, no funding needed).

    python dt_alpaca.py --check                 # test the keys with one small request
    python dt_alpaca.py --years 5               # universe -> daily bars -> gap days -> 1-minute bars
    python dt_alpaca.py --years 5 --no-minutes  # only find the gap days

Everything goes to data_alpaca/ (its own gappers.csv, bars and holdout), never into the
Longbridge data/. Backtest it with:  python dt_backtest.py --data alpaca ...

Keys: add these lines to DT_Iris\\.env (the same keys as ..\\Claude ALPACA\\.env work):
    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ALPACA_BASE_URL=https://paper-api.alpaca.markets      (or https://api.alpaca.markets)

Why Alpaca for research:
- free history back to 2016 for every US stock, consolidated (SIP) volume - the volume the
  course numbers were written for (Longbridge shows Nasdaq only, about 57%);
- delisted stocks are included (status=inactive), so less survivorship bias than the
  Longbridge universe, which only knows today's listings;
- no 400-symbol history quota. The free plan allows 200 requests a minute; each request
  returns up to 10,000 bars, so 5 years of gap days takes roughly an hour.

Prices: daily bars are fetched split-adjusted (gaps and ATR are not fooled by splits) and raw
(the price filters use the price as it traded that day). Minute bars are raw, like Longbridge's.
"""
from __future__ import annotations

import os

os.environ.setdefault("DT_DATA", "alpaca")       # must be set before dt_config is imported

import argparse
import re
import time
from datetime import date, timedelta

import pandas as pd
import requests

import dt_bars as bars
import dt_config as cfg
import dt_data as data
import dt_gappers as gap

DATA_URL = "https://data.alpaca.markets/v2"
PAUSE = 60 / 190                     # free plan: 200 requests a minute
SYMBOLS_PER_CALL = 100
MAIN_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
LB_UNIVERSE = cfg.ROOT / "data" / "universe_all.csv"    # Longbridge's ETF/ADR/fund screen, reused where it knows the symbol

_session: requests.Session | None = None
_last = 0.0


class AlpacaError(RuntimeError):
    pass


def _keys() -> tuple[str, str, str]:
    from dt_broker import load_env
    load_env()
    key, secret = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    base = re.sub(r"/v2$", "", base)
    if not key or not secret:
        raise AlpacaError(f"ALPACA_API_KEY / ALPACA_SECRET_KEY missing from {cfg.ENV_FILE} (see the top of dt_alpaca.py)")
    return key, secret, base


def _get(url: str, params: dict | None = None) -> dict:
    global _session, _last
    if _session is None:
        key, secret, _ = _keys()
        _session = requests.Session()
        _session.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(6):
        wait = PAUSE - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
        try:
            r = _session.get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            time.sleep(5 * (attempt + 1))
            last_err = str(exc)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(20)
            last_err = "rate limited"
            continue
        if r.status_code in (401, 403):
            raise AlpacaError(f"Alpaca refused the request ({r.status_code}): {r.text[:200]}")
        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        time.sleep(5 * (attempt + 1))
    raise AlpacaError(last_err)


def bars_multi(symbols: list[str], timeframe: str, start: pd.Timestamp, end: pd.Timestamp,
               adjustment: str) -> dict[str, list[dict]]:
    """All bars for several symbols, following next_page_token. start/end are tz-aware."""
    out: dict[str, list[dict]] = {}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start.isoformat(), "end": end.isoformat(),
              "adjustment": adjustment, "feed": "sip", "limit": 10000}
    while True:
        js = _get(f"{DATA_URL}/stocks/bars", params)
        for sym, rows in (js.get("bars") or {}).items():
            out.setdefault(sym, []).extend(rows)
        tok = js.get("next_page_token")
        if not tok:
            return out
        params["page_token"] = tok


def check() -> None:
    end = pd.Timestamp(date.today() - timedelta(days=3), tz=cfg.TZ)
    got = bars_multi(["AAPL"], "1Day", end - pd.Timedelta(days=10), end, "split")
    first = bars_multi(["AAPL"], "1Day", pd.Timestamp("2021-01-04", tz=cfg.TZ), pd.Timestamp("2021-01-08", tz=cfg.TZ), "split")
    print(f"keys OK: AAPL {len(got.get('AAPL', []))} recent daily bars, {len(first.get('AAPL', []))} bars from Jan 2021 (SIP feed)")


# ------------------------------------------------------------------ universe
def build_universe(refresh: bool = False) -> pd.DataFrame:
    if gap.UNIVERSE_FILE.exists() and not refresh:
        return pd.read_csv(gap.UNIVERSE_FILE, dtype={"symbol": str, "name": str}, keep_default_na=False)
    _, _, base = _keys()
    rows = []
    for status in ("active", "inactive"):
        for a in _get(f"{base}/v2/assets", {"status": status, "asset_class": "us_equity"}):
            rows.append({"symbol": a["symbol"], "name": a.get("name") or "", "exchange": a.get("exchange", ""),
                         "status": status})
    df = pd.DataFrame(rows).drop_duplicates("symbol", keep="first")        # active listed first
    name = " " + df["name"].str.upper() + " "
    bad = pd.Series(False, index=df.index)
    for w in gap.EXCLUDE_WORDS:
        bad |= name.str.contains(w, regex=False)
    keep = df["exchange"].isin(MAIN_EXCHANGES) & df["symbol"].str.fullmatch(r"[A-Z]{1,5}") & ~bad
    if LB_UNIVERSE.exists():                 # Longbridge knows ETFs/funds/ADRs better for symbols it lists
        lb = (pd.read_csv(LB_UNIVERSE, dtype={"symbol": str}, keep_default_na=False)
                .groupby("symbol")["keep"].max())                   # a few symbols are listed twice
        known = df["symbol"].isin(lb.index)
        keep &= ~known | df["symbol"].map(lb).fillna(True).astype(bool)
    df["keep"] = keep
    gap.UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(gap.UNIVERSE_FILE, index=False)
    return df


# ------------------------------------------------------------------ daily bars
def _to_frame(rows: list[dict], daily: bool) -> pd.DataFrame:
    df = pd.DataFrame(rows).rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if daily:
        df["date"] = df["time"].dt.tz_convert(cfg.TZ).dt.date
        return df.drop_duplicates("date").set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def fetch_daily(symbols: list[str], start: date, end: date) -> int:
    """Split-adjusted daily bars plus the raw open/close, for symbols not already on disk."""
    todo = []
    for s in symbols:
        d = gap.load_daily(s)
        if d is None or not len(d) or max(d.index) < end - timedelta(days=4) or min(d.index) > start + timedelta(days=10):
            todo.append(s)
    print(f"daily bars: {len(symbols) - len(todo)} on disk, {len(todo)} to fetch", flush=True)
    t0, saved = time.time(), 0
    s0, s1 = pd.Timestamp(start, tz=cfg.TZ), pd.Timestamp(end, tz=cfg.TZ) + pd.Timedelta(days=1)
    for k in range(0, len(todo), SYMBOLS_PER_CALL):
        chunk = todo[k:k + SYMBOLS_PER_CALL]
        adj = bars_multi(chunk, "1Day", s0, s1, "split")
        raw = bars_multi(chunk, "1Day", s0, s1, "raw")
        for sym, rows in adj.items():
            if not rows:
                continue
            df = _to_frame(rows, daily=True)
            if raw.get(sym):
                r = _to_frame(raw[sym], daily=True)
                df["raw_open"], df["raw_close"] = r["open"], r["close"]
            gap.DAILY_DIR.mkdir(parents=True, exist_ok=True)
            df.to_parquet(gap._daily_path(sym))
            saved += 1
        done = k + len(chunk)
        rate = done / max(time.time() - t0, 1e-9)
        print(f"  daily: {done}/{len(todo)} symbols ({saved} with data), ~{(len(todo) - done) / rate / 60:.0f} min left",
              flush=True)
    return saved


# ------------------------------------------------------------------ minutes
def fetch_minutes(g: pd.DataFrame) -> None:
    """1-minute bars 04:00-16:00 ET for every gap symbol-day not already on disk."""
    todo = g[[not data._path(r.symbol, r.date).exists() for r in g.itertuples()]]
    days = sorted(todo["date"].unique())
    print(f"1-minute bars: {len(g) - len(todo)} symbol-days on disk, {len(todo)} to fetch on {len(days)} sessions",
          flush=True)
    t0, saved, empty = time.time(), 0, 0
    for n, day in enumerate(days, 1):
        syms = sorted(todo[todo["date"] == day]["symbol"])
        s0 = pd.Timestamp(f"{day} {cfg.PREMARKET_START}", tz=cfg.TZ)
        s1 = pd.Timestamp(f"{day} {cfg.SESSION_CLOSE}", tz=cfg.TZ)
        for k in range(0, len(syms), SYMBOLS_PER_CALL):
            got = bars_multi(syms[k:k + SYMBOLS_PER_CALL], "1Min", s0, s1, "raw")
            for sym in syms[k:k + SYMBOLS_PER_CALL]:
                rows = got.get(sym)
                if not rows:
                    empty += 1
                    continue
                df = _to_frame(rows, daily=False)
                df = df[df["time"] < s1.tz_convert("UTC")]
                data.save_day(sym, day, data.fix_bar_start(bars.normalize(df)))
                saved += 1
        if n % 25 == 0 or n == len(days):
            rate = n / max(time.time() - t0, 1e-9)
            print(f"  minutes: {n}/{len(days)} sessions, {saved} saved, {empty} empty, "
                  f"~{(len(days) - n) / rate / 60:.0f} min left", flush=True)


def main(years: float, minutes: bool, refresh_universe: bool) -> None:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=int(years * 365.25))
    print(f"DT_Iris Alpaca history: {start} .. {end} -> {cfg.DATA_DIR}")
    uni = build_universe(refresh_universe)
    keep = sorted(uni[uni["keep"]]["symbol"])
    print(f"universe: {len(uni)} US equities ({(uni.status == 'inactive').sum()} delisted), {len(keep)} kept")
    fetch_daily(keep, start - timedelta(days=45), end)
    g = gap.find_gappers(keep, start, end)
    gap.GAPPERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    g.to_csv(gap.GAPPERS_FILE, index=False)
    if g.empty:
        print("no gap days found")
        return
    print(f"gap days: {len(g)} symbol-days on {g['date'].nunique()} sessions "
          f"({(g.side == 'long').sum()} long, {(g.side == 'short').sum()} short) -> {gap.GAPPERS_FILE}")
    if minutes:
        fetch_minutes(g)
    print("done. Backtest with: python dt_backtest.py --data alpaca --strategies ...")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="test the keys with one small request")
    ap.add_argument("--years", type=float, default=5)
    ap.add_argument("--no-minutes", action="store_true")
    ap.add_argument("--refresh-universe", action="store_true")
    a = ap.parse_args()
    try:
        check() if a.check else main(a.years, not a.no_minutes, a.refresh_universe)
    except AlpacaError as exc:
        raise SystemExit(f"Alpaca: {exc}")
