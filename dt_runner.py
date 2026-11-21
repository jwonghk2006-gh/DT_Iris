"""DT_Iris runner: watches today's watchlist live and trades the setups.

    python dt_runner.py                         # mode from store/runner_settings.json (default paper)
    python dt_runner.py --mode paper            # simulated fills, needs only market data
    python dt_runner.py --mode lbpaper          # Longbridge paper account
    python dt_runner.py --mode live             # real money, every gate in dt_broker must pass
    python dt_runner.py --replay 2026-06-03     # replay stored bars through the same engine (no keys)

Market data: Longbridge pushes 1-minute candles; only CONFIRMED candles are
used, and 2-minute bars are built from them by dt_bars.BarBuilder - the same
code the backtester uses. Leave the window open from before 09:30 ET until
after 15:00 ET (21:30 / 22:30 to 03:00 / 04:00 Singapore time, depending on US daylight saving).
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import time
from datetime import date

import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_store as store
import dt_telegram as tg
from dt_sim import Sim
from dt_strategies import DETECTORS, Setup, timeframe

HEARTBEAT_S = 15
POLL_S = 2


class Engine:
    """Bars in -> setups out -> fills (paper Sim) or orders (Executor). Shared by live and replay."""

    def __init__(self, mode: str, strategies: list[str], watch: dict[str, set], contexts: dict | None = None,
                 executor=None, notify=None, log: bool = True):
        self.mode = mode
        self.strategies = strategies
        self.watch = watch                       # symbol -> {"long","short"} allowed sides
        self.contexts = contexts or {}
        self.sim = Sim() if mode == "paper" else None
        self.ex = executor
        self.notify = notify or (lambda t: None)
        self.log = log
        self.tfs = sorted({timeframe(s) for s in strategies})
        self.state: dict[str, dict] = {}
        self.setups: list[Setup] = []
        self.live_from: pd.Timestamp | None = None    # setups known before this are ignored (warm-up)

    def _sym(self, sym: str) -> dict:
        if sym not in self.state:
            self.state[sym] = {"builders": {tf: bars.BarBuilder(tf) for tf in self.tfs if tf > 1},
                               "frames": {tf: [] for tf in self.tfs}, "memo": {s: {} for s in self.strategies},
                               "last_ts": None}
        return self.state[sym]

    def on_bar(self, sym: str, ts: pd.Timestamp, o, h, l, c, v, trade: bool = True) -> list[Setup]:
        """One CONFIRMED 1-minute bar (ts = bar start, UTC)."""
        t = ts.tz_convert(cfg.TZ).time()
        if not (bars.OPEN_T <= t < bars.CLOSE_T):
            return []
        st = self._sym(sym)
        if st["last_ts"] is not None and ts <= st["last_ts"]:
            return []                                    # duplicate push
        st["last_ts"] = ts
        if trade and self.sim is not None:
            for ev in self.sim.on_bar(sym, ts, o, h, l, c):
                self._event(ev)
        done: dict[int, list[dict]] = {1: [{"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}]}
        for tf, bb in st["builders"].items():
            done[tf] = bb.add(ts, o, h, l, c, v)
        new = []
        for tf in self.tfs:
            for b in done.get(tf, []):
                st["frames"][tf].append(b)
                new += self._detect(sym, tf)
        for s in new:
            if self.live_from is not None and s.ready_time < self.live_from:
                continue
            self.setups.append(s)
            if trade:
                self._publish(s)
        return new

    def _detect(self, sym: str, tf: int) -> list[Setup]:
        rows = self._sym(sym)["frames"][tf]
        df = bars.add_indicators(pd.DataFrame(rows).set_index("time"))
        out = []
        for name in self.strategies:
            if timeframe(name) != tf:
                continue
            for s in DETECTORS[name](sym, df, len(df) - 1, self.contexts.get(sym), self._sym(sym)["memo"][name]):
                if s.side in self.watch.get(sym, {"long", "short"}):
                    out.append(s)
        return out

    def _publish(self, s: Setup) -> None:
        if self.sim is not None:
            self.sim.add_setup(s)
        if self.ex is not None:
            self.ex.add_setup(s)
        if self.log:
            store.log_signal({"date": str(s.ready_time.tz_convert(cfg.TZ).date()), "mode": self.mode, "symbol": s.symbol,
                              "strategy": s.strategy, "side": s.side, "trigger": s.trigger, "stop": s.stop,
                              "target": s.target, "status": "setup", "reason": s.reason})
            self.notify(f"DT_Iris SETUP {s.symbol} {s.strategy} {s.side.upper()}  trigger {s.trigger:.2f}  "
                        f"stop {s.stop:.2f}  target {s.target:.2f}  ({s.reason})")

    def _event(self, ev: tuple) -> None:
        if not self.log:
            return
        kind = ev[0]
        if kind == "entry":
            p = ev[1]
            msg = (f"DT_Iris PAPER ENTRY {p.setup.symbol} {p.setup.strategy} {p.setup.side.upper()} {p.shares} @ "
                   f"{p.entry:.2f} stop {p.stop:.2f} target {p.target:.2f}")
            store.log_activity(self.mode, "entry", msg)
            self.notify(msg)
        elif kind == "exit":
            t = ev[1]
            store.log_trade({**t, "mode": self.mode, "notes": t.get("reason", "")})
            msg = f"DT_Iris PAPER EXIT {t['symbol']} {t['exit_reason']} @ {t['exit']:.2f}  {t['r_multiple']:+.2f}R  ${t['pnl']:+,.2f}"
            store.log_activity(self.mode, "exit", msg, t["pnl"])
            self.notify(msg)
        elif kind in ("skipped", "missed"):
            s = ev[1]
            store.log_signal({"date": str(s.ready_time.tz_convert(cfg.TZ).date()), "mode": self.mode, "symbol": s.symbol,
                              "strategy": s.strategy, "side": s.side, "trigger": s.trigger, "stop": s.stop,
                              "target": s.target, "status": kind, "reason": ev[2]})

    def clock(self, now: pd.Timestamp, prices: dict) -> None:
        """Time rules that must fire even when no bar arrives."""
        t = now.tz_convert(cfg.TZ).time()
        if t >= bars.CUTOFF_T or store.kill_switch_on():
            if self.sim is not None:
                self.sim.pending.clear()
            if self.ex is not None and self.ex.pending:
                self.ex.drop_pending("12:00 cutoff" if t >= bars.CUTOFF_T else "kill switch")
        if t >= bars.FLATTEN_T:
            if self.sim is not None and self.sim.positions:
                n0 = len(self.sim.events)
                self.sim.close_all(now, prices, "flatten 15:00")
                for ev in self.sim.events[n0:]:
                    self._event(ev)
            if self.ex is not None and (self.ex.positions or self.ex.working):
                self.ex.flatten_all(prices)


# ------------------------------------------------------------------ helpers
def watch_from_settings(settings: dict, day: date) -> dict[str, set]:
    from dt_scanner import load_watchlist
    sides = set(settings.get("sides") or ["long", "short"])
    if settings.get("symbols"):
        return {s.upper(): set(sides) for s in settings["symbols"]}
    wl = load_watchlist(day)
    out: dict[str, set] = {}
    for _, r in wl.iterrows():
        if r["side"] in sides:
            out.setdefault(str(r["symbol"]).upper(), set()).add(r["side"])
    return out


def other_runner_alive() -> int | None:
    st = store.runner_status()
    pid = st.get("pid")
    if not pid or pid == os.getpid():
        return None
    age = time.time() - st.get("beat", 0)
    if age > 4 * HEARTBEAT_S:
        return None
    try:
        if os.name == "nt":
            import ctypes
            hnd = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not hnd:
                return None
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(hnd, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(hnd)
            return pid if code.value == 259 else None
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


# ------------------------------------------------------------------ replay
def replay(day: date, symbols: list[str] | None, strategies: list[str], log: bool = False) -> Engine:
    """Feed stored bars of `day` through the live Engine in paper mode, in time order."""
    import dt_backtest as bt
    import dt_data as data
    av = data.available()
    symbols = symbols or sorted(set(av[av.date == day]["symbol"]) - set(cfg.BENCHMARKS))
    stream, contexts, watch = [], {}, {}
    for sym in symbols:
        item = bt.symbol_days(sym, research=False, only_day=day).get(day)
        if item is None:
            continue
        reg, contexts[sym], allowed = item
        watch[sym] = set(allowed)
        stream.append(reg.assign(symbol=sym))
    eng = Engine("paper", strategies, watch, contexts, log=log)
    if not stream:
        return eng
    allb = pd.concat(stream).sort_index(kind="stable")
    last = {}
    for r in allb.itertuples():
        eng.on_bar(r.symbol, r.Index, r.open, r.high, r.low, r.close, r.volume)
        last[r.symbol] = r.close
    if eng.sim.positions:
        eng.sim.close_all(allb.index[-1], last, "session end")
    return eng


# ------------------------------------------------------------------ live
def run_live(mode: str, minutes: int | None = None) -> None:
    import dt_broker as broker
    from longport.openapi import AdjustType, Period, SubType, TradeSessions

    pid = other_runner_alive()
    if pid:
        sys.exit(f"Another runner (pid {pid}) is already running. Two runners would double every trade.")
    settings = store.save_settings(mode=mode)
    if mode in ("paper", "lbpaper") and not settings.get("first_paper_day"):
        store.save_settings(first_paper_day=str(date.today()))
    day = pd.Timestamp.now(tz=cfg.TZ).date()
    watch = watch_from_settings(settings, day)
    if not watch:
        sys.exit("Watchlist is empty: run dt_scanner.py first, or set symbols in the dashboard.")
    notify = tg.send if tg.configured() else (lambda t: None)

    executor = None
    if mode in ("lbpaper", "live"):
        from dt_executor import Executor
        if mode == "live" and not broker.live_enabled():
            sys.exit("Live mode needs DT_LIVE_ENABLED=yes in .env.")
        executor = Executor(broker.Broker(mode), notify)
        executor.refresh_account()
        executor.from_state(store.load_state())
        for note in executor.reconcile():
            print("RECONCILE:", note)
            store.log_activity(mode, "reconcile", note)

    q: queue.Queue = queue.Queue()
    qctx = broker.quote_context()
    syms = sorted(watch)
    contexts = {}
    for qt in qctx.quote([broker.lb_symbol(s) for s in syms]):
        s = broker.plain(qt.symbol)
        pm = qt.pre_market_quote
        # pre-market quote's prev_close = yesterday's close (split-adjusted); before the open qt.prev_close is a day older
        prev = float(pm.prev_close) if pm and getattr(pm, "prev_close", None) else (float(qt.prev_close) if qt.prev_close else None)
        contexts[s] = bars.DayContext(s, day, prev,
                                      float(pm.volume) if pm else 0.0,
                                      float(pm.last_done) if pm and pm.last_done else None, None, None)
    eng = Engine(mode, settings["strategies"], watch, contexts, executor, notify)

    # warm-up with today's bars so far (indicators need the whole session), no trading on them
    now = pd.Timestamp.now(tz="UTC")
    for s in syms:
        try:
            cs = qctx.candlesticks(broker.lb_symbol(s), Period.Min_1, 500, AdjustType.NoAdjust, TradeSessions.All)
        except Exception as exc:
            print(f"warm-up {s}: {exc}")
            continue
        df = pd.DataFrame([{"time": broker.to_utc(c.timestamp), "open": float(c.open), "high": float(c.high), "low": float(c.low),
                            "close": float(c.close), "volume": float(c.volume)} for c in cs])
        if df.empty:
            continue
        df = bars.normalize(df)
        df = df[(df.index.tz_convert(cfg.TZ).date == day) & (df.index + pd.Timedelta(minutes=1) <= now)]
        for r in df.itertuples():
            eng.on_bar(s, r.Index, r.open, r.high, r.low, r.close, r.volume, trade=False)
    eng.live_from = now

    def on_candle(symbol, ev):
        if ev.is_confirmed:
            c = ev.candlestick
            q.put(("bar", broker.plain(symbol), c))

    def on_quote(symbol, ev):
        q.put(("quote", broker.plain(symbol), float(ev.last_done)))

    qctx.set_on_candlestick(on_candle)
    qctx.set_on_quote(on_quote)
    for s in syms:
        qctx.subscribe_candlesticks(broker.lb_symbol(s), Period.Min_1, TradeSessions.Intraday)
    if executor is not None:
        qctx.subscribe([broker.lb_symbol(s) for s in syms], [SubType.Quote])

    store.log_activity(mode, "session_start", f"runner started: {len(syms)} symbols, {', '.join(settings['strategies'])}")
    notify(f"DT_Iris runner started ({mode}): {', '.join(syms)}")
    print(f"DT_Iris runner [{mode}] watching {len(syms)}: {', '.join(syms)}  (Ctrl+C to stop)")
    stop_at = time.time() + minutes * 60 if minutes else None
    prices: dict[str, float] = {}
    last_beat = last_poll = 0.0
    try:
        while True:
            try:
                kind, sym, payload = q.get(timeout=1.0)
                if kind == "bar":
                    c = payload
                    ts = broker.to_utc(c.timestamp)
                    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                    prices[sym] = float(c.close)
                    eng.on_bar(sym, ts, float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume))
                elif kind == "quote" and executor is not None:
                    prices[sym] = payload
                    executor.on_price(sym, payload, pd.Timestamp.now(tz="UTC"))
            except queue.Empty:
                pass
            now = pd.Timestamp.now(tz="UTC")
            eng.clock(now, prices)
            if executor is not None and time.time() - last_poll >= POLL_S:
                executor.poll(now)
                store.save_state(executor.to_state())
                last_poll = time.time()
            if time.time() - last_beat >= HEARTBEAT_S:
                store.write_runner_status({"pid": os.getpid(), "beat": time.time(), "mode": mode, "symbols": syms,
                                           "time": store.now_iso(),
                                           "open_positions": list((executor.positions if executor else eng.sim.positions).keys())})
                last_beat = time.time()
            t_ny = now.tz_convert(cfg.TZ).time()
            if t_ny >= bars.hm("15:05") or (stop_at and time.time() > stop_at):
                break
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        book = executor.positions if executor else (eng.sim.positions if eng.sim else {})
        if book:
            print("WARNING: positions still open:", list(book))
            notify(f"DT_Iris runner stopped with OPEN positions: {list(book)}")
        tr = store.read_trades()
        today = tr[(tr["date"].astype(str) == str(day)) & (tr["mode"] == mode)] if len(tr) else tr
        summary = f"DT_Iris {mode} day done: {len(today)} trades, {today['r_multiple'].sum() if len(today) else 0:+.2f}R"
        store.log_activity(mode, "session_end", summary)
        notify(summary)
        store.write_runner_status({"pid": None, "beat": 0, "mode": mode, "time": store.now_iso()})
        print(summary)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "lbpaper", "live"])
    ap.add_argument("--minutes", type=int, help="stop after N minutes (testing)")
    ap.add_argument("--replay", help="YYYY-MM-DD: replay stored bars instead of live data")
    ap.add_argument("--symbols")
    ap.add_argument("--strategies")
    a = ap.parse_args()
    strategies = a.strategies.split(",") if a.strategies else store.load_settings()["strategies"]
    if a.replay:
        e = replay(pd.Timestamp(a.replay).date(), [s.upper() for s in a.symbols.split(",")] if a.symbols else None,
                   strategies, log=False)
        print(f"{len(e.setups)} setups, {len(e.sim.trades)} trades")
        for t in e.sim.trades:
            print(f"  {t['symbol']:6} {t['strategy']:14} {t['side']:5} {t['entry']:>8.2f} -> {t['exit']:>8.2f} "
                  f"{t['exit_reason']:14} {t['r_multiple']:+.2f}R")
    else:
        run_live(a.mode or store.load_settings()["mode"], a.minutes)
