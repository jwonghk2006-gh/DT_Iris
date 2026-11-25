"""Pre-market gap scanner -> today's long and short watchlist. No pasting needed.

    python dt_scanner.py                      # AUTO: scan the whole Longbridge universe now
    python dt_scanner.py --scheduled          # wait until SCAN_TIME (09:00 ET), scan, lock the list (Windows task)
    python dt_scanner.py --symbols PL,LULU    # only these symbols
    python dt_scanner.py --universe file.txt  # a symbol list file (e.g. a TradingView screener export)

Universe: data/universe_all.csv (every Longbridge US security, kept if main exchange, not an
ETF/ETN/fund/ADR/warrant; built by dt_gappers.build_universe). Quotes are pulled in batches of
400 (a few seconds for the whole market). Average daily volume comes from data/daily, refreshed
with the quota-free latest-bars call - the scan never touches the 400-symbol history quota.

Two lists, one row per symbol (column `lists`), see dt_config:
    gap      (Power Ranger, Roller Coaster): |relative gap| >= MIN_GAP_PCT, where relative gap =
             stock gap - beta x SPY gap (GAP_BASIS "relative"; "raw" = the plain gap). The sign gives
             the side: relative strength -> long (price >= LONG_MIN_PRICE), relative weakness -> short
             (price >= SHORT_MIN_PRICE). Premarket volume >= 5,000 (price >= $5) or >= 1,000,000 (< $5),
             average daily volume >= MIN_AVG_DAILY_VOLUME. Ranked by |gap| x premarket volume, top
             MAX_WATCHLIST per side.
    in_play  (Cupid, Tug-Of-War): no gap needed. Price >= IN_PLAY_MIN_PRICE, average daily volume >=
             IN_PLAY_MIN_ADV, premarket volume >= IN_PLAY_MIN_PM_VOLUME and >= IN_PLAY_MIN_PM_RVOL of a
             normal day. Side = sign of the relative gap. Ranked by pm_rvol, top IN_PLAY_MAX.
Every candidate also gets its best-matching sector ETF and the gap relative to it (information).
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

COLS = ["date", "symbol", "side", "lists", "price", "prev_close", "gap_pct", "spy_gap", "beta", "rel_gap", "sector",
        "sector_gap", "premarket_volume", "avg_daily_volume", "pm_rvol", "score", "note"]
BATCH = 400


def premarket_min_volume(price: float) -> float:
    return cfg.PREMARKET_VOL_MIN_PRICE_GE_5 if price >= 5 else cfg.PREMARKET_VOL_MIN_PRICE_LT_5


def basis_gap(row: dict) -> float | None:
    """The gap the gap list is judged on: relative to SPY (default) or raw."""
    if cfg.GAP_BASIS == "relative" and row.get("rel_gap") is not None:
        return row["rel_gap"]
    return row.get("gap_pct")


def classify(row: dict) -> tuple[str | None, str]:
    """Gap list: return (side, note) for one candidate, side None if it fails a filter."""
    sym, px, pmv, adv = row["symbol"], row["price"], row["premarket_volume"], row.get("avg_daily_volume")
    gap = basis_gap(row)
    what = "rel gap" if cfg.GAP_BASIS == "relative" and row.get("rel_gap") is not None else "gap"
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
    return None, f"{what} {gap:+.1f}% too small" + (f" ({row['split']})" if row.get("split") else "")


def in_play_candidate(row: dict) -> bool:
    px = row.get("price")
    return bool(px and px >= cfg.IN_PLAY_MIN_PRICE and (row.get("premarket_volume") or 0) >= cfg.IN_PLAY_MIN_PM_VOLUME)


def in_play(row: dict) -> tuple[str | None, str]:
    """Stocks-in-play list: return (side, note); side None if it fails."""
    sym, px, adv, rv = row["symbol"], row.get("price"), row.get("avg_daily_volume"), row.get("pm_rvol")
    if sym in cfg.EXCLUDE_SYMBOLS:
        return None, "excluded"
    if not in_play_candidate(row):
        return None, "in play: price or premarket volume too low"
    if adv is None or adv != adv or adv < cfg.IN_PLAY_MIN_ADV:
        return None, "in play: average daily volume too low"
    if rv is None or rv < cfg.IN_PLAY_MIN_PM_RVOL:
        return None, f"in play: premarket volume {0 if rv is None else rv:.1%} of a day, too low"
    g = basis_gap(row)
    side = "long" if (g or 0) >= 0 else "short"
    if side == "short" and px < cfg.SHORT_MIN_PRICE:
        return None, f"in play: short below ${cfg.SHORT_MIN_PRICE:g}"
    return side, f"in play: premarket volume {rv:.0%} of a day"


def rank(rows: list[dict], day: date) -> pd.DataFrame:
    out = []
    for r in rows:
        gside, gnote = classify(r)
        iside, inote = in_play(r)
        out.append(dict(r, date=str(day), _gside=gside or "", _gnote=gnote, _iside=iside or "", _inote=inote,
                        _gscore=abs(basis_gap(r) or 0) * (r["premarket_volume"] or 0) if gside else 0.0,
                        _iscore=(r.get("pm_rvol") or 0.0) if iside else 0.0))
    df = pd.DataFrame(out)
    if df.empty:
        return pd.DataFrame(columns=COLS)
    gap_keep = set()
    for side in ("long", "short"):
        gap_keep |= set(df[df._gside == side].sort_values("_gscore", ascending=False).head(cfg.MAX_WATCHLIST).index)
    play_keep = set(df[df._iside != ""].sort_values("_iscore", ascending=False).head(cfg.IN_PLAY_MAX).index)
    sides, lists, notes, scores = [], [], [], []
    for i, r in df.iterrows():
        on = [n for n, keep in (("gap", i in gap_keep), ("in_play", i in play_keep)) if keep]
        lists.append(",".join(on))
        sides.append(r._gside if i in gap_keep else (r._iside if i in play_keep else ""))
        scores.append(r._gscore if i in gap_keep else (r._iscore if i in play_keep else 0.0))
        if on:
            notes.append("; ".join(([r._gnote] if i in gap_keep else []) + ([r._inote] if i in play_keep else [])))
        else:
            cut = []
            if r._gside:
                cut.append(f"gap ok but below the top {cfg.MAX_WATCHLIST}")
            if r._iside:
                cut.append(f"in play ok but below the top {cfg.IN_PLAY_MAX}")
            notes.append("; ".join(cut) or r._gnote + ("" if r._inote.startswith("in play: price") else f"; {r._inote}"))
    df["side"], df["lists"], df["note"], df["score"] = sides, lists, notes, scores
    for c in COLS:
        if c not in df:
            df[c] = None
    df = df[COLS]
    kept = df[df.side != ""].copy()
    kept["_o"] = kept["lists"].map(lambda x: 0 if "gap" in x.split(",") else 1)
    kept = kept.sort_values(["_o", "side", "score"], ascending=[True, True, False]).drop(columns="_o")
    return pd.concat([kept, df[df.side == ""]], ignore_index=True)


def universe_symbols() -> list[str]:
    """Every tradable common stock in the Longbridge universe file (built once by dt_gappers),
    without share classes and EXCLUDE_SYMBOLS."""
    import dt_gappers as gap
    return gap.tradable_symbols()


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


QUOTE_RETRIES, QUOTE_RETRY_WAIT_S = 2, 0.5


def _quote_batch(ctx, chunk: list[str], retries: int = QUOTE_RETRIES, wait: float = QUOTE_RETRY_WAIT_S,
                 log=print) -> tuple[list, list[str]]:
    """ctx.quote(chunk), then ask again for any symbol the reply left out (or the whole batch if the call
    failed). 2026-11-23: Longbridge silently dropped 267 of 5,842 symbols - AMD, TSM, DELL, AMZN ... -
    with no error, so the biggest gappers of the day never reached the list. Returns (quotes, still missing)."""
    got, todo = {}, list(dict.fromkeys(chunk))
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(wait)
        try:
            for q in ctx.quote(todo):
                got.setdefault(q.symbol, q)
        except Exception as exc:
            log(f"quote call for {len(todo)} symbols failed (try {attempt + 1}): {exc}")
        todo = [s for s in todo if s not in got]
        if not todo:
            break
    return list(got.values()), todo


def _quote(ctx, symbols: list[str], day: date, log=print) -> tuple[list[dict], int]:
    """Premarket quote rows for `symbols`; returns (rows, number with a premarket quote from `day`)."""
    import dt_broker as broker
    rows, live, missing = [], 0, []
    for k in range(0, len(symbols), BATCH):
        chunk = [broker.lb_symbol(s) for s in symbols[k:k + BATCH]]
        quotes, left = _quote_batch(ctx, chunk, log=log)
        missing += left
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
    if missing:
        log(f"no quote after {QUOTE_RETRIES} retries for {len(missing)} of {len(symbols)} symbols: "
            f"{', '.join(missing[:12])}{' ...' if len(missing) > 12 else ''}")
    return rows, live


def enrich(rows: list[dict], day: date, bench_gaps: dict, log=print) -> None:
    """Adds spy_gap, beta, rel_gap, sector, sector_gap, avg_daily_volume and pm_rvol to every candidate
    of either list (in place). Uses daily bars before `day` only (quota-free fetch when missing)."""
    import dt_rs as rs
    spy = bench_gaps.get(cfg.RS_BENCHMARK)
    if spy is None:
        log(f"no premarket quote for {cfg.RS_BENCHMARK}: relative gaps fall back to the raw gap")
    bench_c = rs.daily_closes(cfg.RS_BENCHMARK, day)
    etf_c = {}
    for e in cfg.SECTOR_ETFS:
        c = rs.daily_closes(e, day)
        if c is not None:
            etf_c[e] = c
    for r in rows:
        r["spy_gap"] = None if spy is None else round(spy, 2)
        g = rs.rel_gap(r["gap_pct"], spy, None)          # beta 1 until (and unless) the real beta is computed
        r["rel_gap"] = None if g is None else round(g, 2)
    thr = cfg.MIN_GAP_PCT
    cands = [r for r in rows if r["gap_pct"] is not None and (
        (r["premarket_volume"] >= premarket_min_volume(r["price"])
         and (rs.could_pass(r["gap_pct"], spy, thr) if cfg.GAP_BASIS == "relative" else abs(r["gap_pct"]) >= thr))
        or in_play_candidate(r))]
    for r in cands:
        closes = rs.daily_closes(r["symbol"], day)        # first: fetches enough days for beta too
        r["avg_daily_volume"] = avg_daily_volume(r["symbol"], day)
        if r["avg_daily_volume"]:
            r["pm_rvol"] = round(r["premarket_volume"] / r["avg_daily_volume"], 4)
        if closes is None:
            continue
        b = rs.beta(closes, bench_c) if bench_c is not None else None
        r["beta"] = None if b is None else round(b, 2)
        g = rs.rel_gap(r["gap_pct"], spy, b)
        r["rel_gap"] = None if g is None else round(g, 2)
        etf, corr, sb = rs.best_sector(closes, etf_c)
        if etf:
            r["sector"] = f"{etf} ({corr:.2f})"
            sg = rs.rel_gap(r["gap_pct"], bench_gaps.get(etf), sb)
            r["sector_gap"] = None if sg is None else round(sg, 2)


def scan_longbridge(symbols: list[str], day: date | None = None, with_adv: bool = True,
                    log=print) -> tuple[list[dict], int]:
    """Quote every symbol (+ SPY and the sector ETFs); returns (rows, number with a premarket quote from `day`)."""
    import dt_broker as broker
    day = day or ny_today()
    ctx = broker.quote_context()
    rows, live = _quote(ctx, symbols, day, log)
    if with_adv:
        bench_rows, _ = _quote(ctx, [cfg.RS_BENCHMARK] + list(cfg.SECTOR_ETFS), day, log)
        enrich(rows, day, {r["symbol"]: r["gap_pct"] for r in bench_rows}, log)
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
    shown = df[df.side != ""][["symbol", "lists", "side", "price", "gap_pct", "rel_gap", "beta", "sector", "sector_gap", "pm_rvol"]]
    log(shown.to_string(index=False))
    meta["spy_gap"] = None if df.empty else df["spy_gap"].iloc[0]
    meta["gap_list"] = int(df["lists"].fillna("").str.contains("gap").sum())
    meta["in_play"] = int(df["lists"].fillna("").str.contains("in_play").sum())
    save(df, day, meta)
    log(f"{nl} long, {ns} short from {len(syms)} symbols ({live} with a premarket quote) in {meta['seconds']}s -> {path}")
    return meta


def _fmt(v, spec: str) -> str:
    return "n/a" if v is None or v != v else format(v, spec)


def notify_watchlist(meta: dict) -> None:
    import dt_telegram as tg
    if not tg.configured():
        return
    if not meta.get("saved"):
        tg.send(f"DT_Iris scanner: {meta.get('reason')}")
        return
    wl = load_watchlist(date.fromisoformat(meta["date"]))
    lists = wl["lists"].fillna("") if "lists" in wl else pd.Series("gap", index=wl.index)
    lines = [f"DT_Iris watchlist {meta['date']} (locked {meta['time_ny']} ET), "
             f"SPY {_fmt(meta.get('spy_gap'), '+.2f')}% pre-market"]
    gap = wl[lists.str.contains("gap")]
    lines.append("GAP list (Power Ranger, Roller Coaster): gap / relative to SPY")
    for side in ("long", "short"):
        part = gap[gap.side == side]
        if len(part):
            lines.append(f"  {side.upper()}: " + ", ".join(
                f"{r.symbol} {r.gap_pct:+.1f}/{_fmt(getattr(r, 'rel_gap', None), '+.1f')}" for r in part.itertuples()))
    play = wl[lists.str.contains("in_play")]
    if len(play):
        lines.append("IN PLAY (Cupid, Tug-Of-War): pre-market volume as % of a normal day")
        for side in ("long", "short"):
            part = play[play.side == side]
            if len(part):
                lines.append(f"  {side.upper()}: " + ", ".join(
                    f"{r.symbol} {_fmt(r.pm_rvol * 100 if r.pm_rvol == r.pm_rvol else None, '.0f')}%"
                    for r in part.itertuples()))
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
