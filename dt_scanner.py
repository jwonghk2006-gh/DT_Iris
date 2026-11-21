"""Pre-market gap scanner -> today's long and short watchlist. No pasting needed.

    python dt_scanner.py                      # AUTO: scan the whole Longbridge universe now
    python dt_scanner.py --scheduled          # wait until SCAN_TIME (09:00 ET), scan, lock the list (Windows task)
    python dt_scanner.py --symbols PL,LULU    # only these symbols
    python dt_scanner.py --universe file.txt  # a symbol list file (e.g. a TradingView screener export)

Universe: data/universe_all.csv (every Longbridge US security, kept if main exchange, not an
ETF/ETN/fund/ADR/warrant; built by dt_gappers.build_universe). Quotes are pulled in batches of
400 (a few seconds for the whole market). Average daily volume comes from data/daily, refreshed
with the quota-free latest-bars call - the scan never touches the 400-symbol history quota.

Filters (dt_config, from the course notes):
    long watch  : premarket change >= +MIN_GAP_PCT and price >= LONG_MIN_PRICE
    short watch : premarket change <= -MIN_GAP_PCT and price >= SHORT_MIN_PRICE
    premarket volume >= 5,000 (price >= $5) or >= 1,000,000 (price < $5)
    average daily volume >= MIN_AVG_DAILY_VOLUME
Ranked by |gap %| x premarket volume; top MAX_WATCHLIST per side.
Writes data/watchlists/<NY date>.csv, which the runner reads at the open. A scheduled scan
writes it once and marks it locked; it does not overwrite a list that is already locked.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import dt_config as cfg

COLS = ["date", "symbol", "side", "price", "prev_close", "gap_pct", "premarket_volume", "avg_daily_volume", "score", "note"]
BATCH = 400


def premarket_min_volume(price: float) -> float:
    return cfg.PREMARKET_VOL_MIN_PRICE_GE_5 if price >= 5 else cfg.PREMARKET_VOL_MIN_PRICE_LT_5


def classify(row: dict) -> tuple[str | None, str]:
    """Return (side, note) for one candidate, side None if it fails a filter."""
    sym, px, gap, pmv, adv = row["symbol"], row["price"], row["gap_pct"], row["premarket_volume"], row.get("avg_daily_volume")
    if sym in cfg.EXCLUDE_SYMBOLS:
        return None, "excluded"
    if gap is None or px is None:
        return None, "no premarket quote"
    if pmv < premarket_min_volume(px):
        return None, f"premarket volume {pmv:,.0f} too low"
    if adv is not None and adv == adv and adv < cfg.MIN_AVG_DAILY_VOLUME:
        return None, f"avg daily volume {adv:,.0f} too low"
    ok = "ok" + (f" ({row['split']}, gap vs split-adjusted close)" if row.get("split") else "")
    if gap >= cfg.MIN_GAP_PCT:
        return ("long", ok) if px >= cfg.LONG_MIN_PRICE else (None, f"long below ${cfg.LONG_MIN_PRICE:g}")
    if gap <= -cfg.MIN_GAP_PCT:
        return ("short", ok) if px >= cfg.SHORT_MIN_PRICE else (None, f"short below ${cfg.SHORT_MIN_PRICE:g}")
    return None, f"gap {gap:+.1f}% too small" + (f" ({row['split']})" if row.get("split") else "")


def rank(rows: list[dict], day: date) -> pd.DataFrame:
    out = []
    for r in rows:
        side, note = classify(r)
        r = dict(r, date=str(day), side=side or "", note=note)
        r["score"] = abs(r["gap_pct"] or 0) * (r["premarket_volume"] or 0) if side else 0
        out.append(r)
    df = pd.DataFrame(out, columns=COLS)
    keep = []
    for side in ("long", "short"):
        keep.append(df[df.side == side].sort_values("score", ascending=False).head(cfg.MAX_WATCHLIST))
    rejected = df[df.side == ""]
    return pd.concat(keep + [rejected], ignore_index=True)


def universe_symbols() -> list[str]:
    """Every tradable common stock in the Longbridge universe file (built once by dt_gappers)."""
    import dt_gappers as gap
    uni = gap.build_universe()
    return sorted(uni[uni["keep"]]["symbol"].astype(str))


def avg_daily_volume(sym: str, day: date) -> float | None:
    """20-session average volume before `day`, from data/daily (fetched quota-free when missing or stale)."""
    import dt_gappers as gap
    d = gap.load_daily(sym)
    if d is None or not len(d) or max(d.index) < day - timedelta(days=5):
        try:
            gap.fetch_daily(sym, day - timedelta(days=60), day - timedelta(days=1))
        except Exception:
            return None
        d = gap.load_daily(sym)
    if d is None:
        return None
    vol = d[d.index < day]["volume"].tail(20)
    return float(vol.mean()) if len(vol) >= 10 else None


def scan_longbridge(symbols: list[str], day: date | None = None, with_adv: bool = True,
                    log=print) -> tuple[list[dict], int]:
    """Quote every symbol; returns (rows, number with a premarket quote from `day`)."""
    import dt_broker as broker
    day = day or ny_today()
    ctx = broker.quote_context()
    rows, live = [], 0
    for k in range(0, len(symbols), BATCH):
        chunk = [broker.lb_symbol(s) for s in symbols[k:k + BATCH]]
        try:
            quotes = ctx.quote(chunk)
        except Exception as exc:
            log(f"quote batch {k // BATCH + 1} failed: {exc}")
            time.sleep(1.0)
            continue
        for q in quotes:
            pm = q.pre_market_quote
            fresh = bool(pm and pm.last_done and pm.timestamp
                         and broker.to_utc(pm.timestamp).tz_convert(cfg.TZ).date() == day)
            live += fresh
            # Before the open, q.prev_close is the close of the session BEFORE yesterday and q.last_done is
            # yesterday's close. The pre-market quote's own prev_close is yesterday's close, already adjusted
            # for a split that takes effect today - so that is the reference for the gap.
            pm_prev = float(pm.prev_close) if fresh and getattr(pm, "prev_close", None) else None
            prev = pm_prev or (float(q.last_done) if q.last_done else None)
            px = float(pm.last_done) if fresh else None
            pmv = float(pm.volume) if fresh else 0.0
            gap = (px / prev - 1) * 100 if px and prev else None
            split = None
            if pm_prev and q.last_done and float(q.last_done) > 0:
                ratio = float(q.last_done) / pm_prev                  # yesterday's raw close / adjusted close
                if abs(ratio - 1) > 0.2:                              # a split or reverse split takes effect today
                    split = f"{ratio:.0f}:1 split today" if ratio > 1 else f"1:{1 / ratio:.0f} reverse split today"
            rows.append({"symbol": broker.plain(q.symbol), "price": px, "prev_close": prev, "gap_pct": gap,
                         "premarket_volume": pmv, "avg_daily_volume": None, "split": split})
        time.sleep(0.12)                                  # Longbridge allows about 10 quote calls a second
    if with_adv:
        cands = [r for r in rows if r["gap_pct"] is not None and abs(r["gap_pct"]) >= cfg.MIN_GAP_PCT
                 and r["premarket_volume"] >= premarket_min_volume(r["price"])]
        for r in cands:
            r["avg_daily_volume"] = avg_daily_volume(r["symbol"], day)
    return rows, live


def ny_today() -> date:
    return pd.Timestamp.now(tz=cfg.TZ).date()


def _meta_path(day: date) -> Path:
    return cfg.WATCHLIST_DIR / f"{day}.json"


def scan_info(day: date) -> dict:
    p = _meta_path(day)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save(df: pd.DataFrame, day: date, meta: dict | None = None) -> Path:
    cfg.WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
    p = cfg.WATCHLIST_DIR / f"{day}.csv"
    df.to_csv(p, index=False)
    if meta is not None:
        _meta_path(day).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return p


def load_watchlist(day: date | None = None) -> pd.DataFrame:
    p = cfg.WATCHLIST_DIR / f"{day or ny_today()}.csv"
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(p)
    return df[df["side"].isin(["long", "short"])]


def read_universe(path: Path) -> list[str]:
    syms = []
    for line in path.read_text(encoding="utf-8", errors="ignore").replace(",", "\n").splitlines():
        s = line.strip().split(":")[-1].upper()            # accepts "NASDAQ:ARM" from TradingView exports
        if s and s.replace(".", "").isalnum() and s not in ("SYMBOL", "TICKER"):
            syms.append(s)
    return sorted(set(syms))


def run_scan(symbols: list[str] | None = None, source: str = "auto", lock: bool = False, log=print) -> dict:
    """Scan, rank and save today's watchlist. Returns a summary dict (also stored next to the CSV)."""
    day = ny_today()
    t0 = time.time()
    syms = symbols or universe_symbols()
    rows, live = scan_longbridge(syms, day, log=log)
    if live == 0:
        msg = f"no premarket quotes dated {day}: market closed or data not available - nothing saved"
        log(msg)
        return {"date": str(day), "saved": False, "reason": msg, "scanned": len(syms)}
    df = rank(rows, day)
    nl, ns = int((df.side == "long").sum()), int((df.side == "short").sum())
    meta = {"date": str(day), "saved": True, "source": source, "locked": lock, "scanned": len(syms),
            "with_premarket_quote": live, "long": nl, "short": ns,
            "time_ny": pd.Timestamp.now(tz=cfg.TZ).strftime("%H:%M"), "seconds": round(time.time() - t0, 1)}
    path = save(df, day, meta)
    log(df[df.side != ""][["symbol", "side", "price", "gap_pct", "premarket_volume", "avg_daily_volume"]].to_string(index=False))
    log(f"{nl} long, {ns} short from {len(syms)} symbols ({live} with a premarket quote) in {meta['seconds']}s -> {path}")
    return meta


def notify_watchlist(meta: dict) -> None:
    import dt_telegram as tg
    if not tg.configured():
        return
    if not meta.get("saved"):
        tg.send(f"DT_Iris scanner: {meta.get('reason')}")
        return
    wl = load_watchlist(date.fromisoformat(meta["date"]))
    lines = [f"DT_Iris watchlist {meta['date']} (locked {meta['time_ny']} ET): {meta['long']} long, {meta['short']} short"]
    for side in ("long", "short"):
        part = wl[wl.side == side]
        if len(part):
            lines.append(side.upper() + ": " + ", ".join(f"{r.symbol} {r.gap_pct:+.1f}%" for r in part.itertuples()))
    tg.send("\n".join(lines))


def scheduled() -> int:
    """Windows-task entry point: wait for SCAN_TIME, scan once, lock. Weekends/holidays exit quietly."""
    now = pd.Timestamp.now(tz=cfg.TZ)
    if now.weekday() >= 5:
        print(f"{now:%a %Y-%m-%d}: weekend, no scan")
        return 0
    day = now.date()
    if scan_info(day).get("locked"):
        print(f"{day}: watchlist already locked, not overwritten")
        return 0
    h, m = map(int, cfg.SCAN_TIME.split(":"))
    lh, lm = map(int, cfg.SCAN_LATEST.split(":"))
    start = now.normalize() + pd.Timedelta(hours=h, minutes=m)
    latest = now.normalize() + pd.Timedelta(hours=lh, minutes=lm)
    if now > latest:
        print(f"{now:%H:%M} ET is after {cfg.SCAN_LATEST}: too late for a pre-market scan, nothing done")
        return 0
    wait = (start - now).total_seconds()
    if wait > 0:
        print(f"waiting {wait / 60:.0f} min until {cfg.SCAN_TIME} ET ...", flush=True)
        time.sleep(wait)
    meta = {}
    for attempt in range(3):                         # a network blip should not cost the day's list
        try:
            meta = run_scan(source="scheduled", lock=True)
            break
        except Exception as exc:
            print(f"scan attempt {attempt + 1} failed: {exc}", flush=True)
            meta = {"date": str(day), "saved": False, "reason": f"scan failed: {exc}"}
            time.sleep(60)
    try:
        notify_watchlist(meta)
    except Exception as exc:
        print(f"telegram: {exc}")
    return 0 if meta.get("saved") or "market closed" in str(meta.get("reason", "")) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduled", action="store_true", help="wait for SCAN_TIME, scan once and lock (Windows task)")
    ap.add_argument("--universe", type=Path, help="scan only the symbols in this file")
    ap.add_argument("--symbols")
    a = ap.parse_args()
    if a.scheduled:
        sys.exit(scheduled())
    if a.symbols:
        run_scan([s.strip().upper() for s in a.symbols.split(",") if s.strip()], source="manual list")
    elif a.universe:
        run_scan(read_universe(a.universe), source=f"file {a.universe.name}")
    else:
        run_scan(source="auto")
