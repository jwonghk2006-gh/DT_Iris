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
import dt_rs
import dt_store as store
import dt_telegram as tg
from dt_sim import Sim
from dt_strategies import DETECTORS, Setup, timeframe

HEARTBEAT_S = 15
DIAG_EVERY_S = 300            # status line in the runner log every 5 minutes
POLL_S = 2
# 2026-11-24: the FIRST pushed 1-minute candle of the session reached back to the previous close
# (19 of 20 Power Ranger opening ranges spanned the whole gap -> 10-20% stops), while Longbridge's
# candle history for the same minute was clean. So for the first minutes of the session every pushed
# candle is replaced by the history candle for that minute (quota-free candlesticks()).
VERIFY_FIRST_MINUTES = 15
VERIFY_TRIES, VERIFY_WAIT_S = 3, 0.5
VERIFY_SETTLE_S = 1.0          # wait ONCE per new minute so the history has the bar (not once per symbol)
VERIFY_CALL_GAP_S = 0.12       # at most ~8 history calls a second (Longbridge allows about 10)
ERROR_ALERT_EVERY_S = 300      # Telegram at most once per 5 minutes per kind of error


class Pacer:
    """Keeps at least `gap` seconds between calls."""

    def __init__(self, gap: float, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.clock, self.sleep, self.last = gap, clock, sleep, None

    def wait(self) -> None:
        if self.last is not None:
            left = self.gap - (self.clock() - self.last)
            if left > 0:
                self.sleep(left)
        self.last = self.clock()


class ErrorGuard:
    """Runs one step of the live loop; an exception is printed, logged and (throttled) sent to Telegram,
    and the loop carries on. 2026-11-25 review: before this, ANY error (a network blip while checking an
    order, a rate-limit reply) ended the runner - and with it the locally watched stops."""

    def __init__(self, mode: str, notify, clock=time.time):
        self.mode, self.notify, self.clock = mode, notify, clock
        self.last_alert: dict[str, float] = {}
        self.count: dict[str, int] = {}

    def run(self, what: str, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            kind = what.split()[0]
            self.count[kind] = self.count.get(kind, 0) + 1
            msg = f"{what}: {type(exc).__name__}: {exc}"
            print(f"ERROR (runner keeps going) {msg}", flush=True)
            store.log_activity(self.mode, "error", msg)
            if self.clock() - self.last_alert.get(kind, -1e18) >= ERROR_ALERT_EVERY_S:
                self.last_alert[kind] = self.clock()
                self.notify(f"DT_Iris {self.mode} ERROR (still running, {self.count[kind]}x so far): {msg[:300]}")
            return None


def daily_stats(sym: str, day: date) -> tuple[float | None, float | None]:
    """(14-day daily ATR, 20-day average volume) from the days before `day` - the values the backtests
    put in DayContext. 2026-11-25 review: live contexts had neither, so Power Ranger's opening-range
    width check (0.05-1.5 x daily ATR) never ran live. Daily bars are fetched quota-free when stale."""
    try:
        import dt_gappers as gap
        from dt_scanner import avg_daily_volume
        adv = avg_daily_volume(sym, day)                  # refreshes data/daily/<sym> when stale
        d = gap.load_daily(sym)
        if d is None or not len(d):
            return None, adv
        before = d[d.index < day]
        return (bars.daily_atr(before) if len(before) >= 2 else None), adv
    except Exception as exc:
        print(f"  {sym}: daily stats failed ({exc})")
        return None, None


def verified_bar(fetch, sym: str, ts: pd.Timestamp, push: tuple, tries: int = VERIFY_TRIES,
                 wait: float = VERIFY_WAIT_S) -> tuple[tuple, str]:
    """Return ((o, h, l, c, v), note) for the minute starting at `ts` (UTC).

    fetch(sym) -> [(ts_utc, o, h, l, c, v), ...] from the candle history. The history row for `ts`
    wins; note says how far the push was off ("" when they agree). If history never has the
    minute, the pushed candle is used and note starts with "UNVERIFIED"."""
    err = ""
    for k in range(tries):
        try:
            for r in fetch(sym):
                if r[0] == ts:
                    good = tuple(float(x) for x in r[1:6])
                    off = max(abs(p - g) / g for p, g in zip(push[:4], good[:4]) if g)
                    note = "" if off < 0.001 else (f"push candle replaced: push o/h/l/c {push[0]:.2f}/{push[1]:.2f}/"
                                                   f"{push[2]:.2f}/{push[3]:.2f} vs history {good[0]:.2f}/"
                                                   f"{good[1]:.2f}/{good[2]:.2f}/{good[3]:.2f}")
                    return good, note
        except Exception as exc:
            err = f" ({exc})"
        if k < tries - 1:
            time.sleep(wait)
    return tuple(push), f"UNVERIFIED {ts:%H:%M} UTC candle, history had no row{err}"


class Engine:
    """Bars in -> setups out -> fills (paper Sim) or orders (Executor). Shared by live and replay."""

    def __init__(self, mode: str, strategies: list[str], watch: dict[str, set], contexts: dict | None = None,
                 executor=None, notify=None, log: bool = True, allowed: dict[str, set] | None = None,
                 betas: dict[str, float] | None = None):
        self.mode = mode
        self.strategies = strategies
        self.watch = watch                       # symbol -> {"long","short"} allowed sides
        self.allowed = allowed or {}             # symbol -> strategies allowed by its watchlist (empty = all)
        self.rs = dt_rs.RSTracker(betas)         # intraday relative strength vs SPY (information)
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
        t, _, _, close_t = bars.times_at(ts)
        if not (bars.OPEN_T <= t < close_t):
            return []
        self.rs.on_bar(sym, o, c)
        if sym == self.rs.bench and sym not in self.watch:
            return []                                    # benchmark: only feeds the RS line
        if ts > pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=2):
            print(f"IGNORED future-stamped bar {sym} {ts.tz_convert(cfg.TZ):%Y-%m-%d %H:%M} ET", flush=True)
            return []                                    # must never become last_ts (it would block every real bar)
        st = self._sym(sym)
        if st["last_ts"] is not None and ts <= st["last_ts"]:
            self.dropped = getattr(self, "dropped", 0) + 1
            if self.dropped <= 5:
                print(f"dropped bar {sym} {ts.tz_convert(cfg.TZ):%H:%M} ET (last bar {st['last_ts'].tz_convert(cfg.TZ):%H:%M})",
                      flush=True)
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
                if s.side in self.watch.get(sym, {"long", "short"}) and name in self.allowed.get(sym, {name}):
                    out.append(s)
        return out

    def status_line(self) -> str:
        """One line for the runner log: is the engine actually getting bars?"""
        n = {s: len(st["frames"].get(1, [])) for s, st in self.state.items()}
        last = max((st["last_ts"] for st in self.state.values() if st["last_ts"] is not None), default=None)
        return (f"bars in engine: {sum(n.values())} over {len(n)} symbols (fewest {min(n.values(), default=0)}), "
                f"latest bar {last.tz_convert(cfg.TZ):%H:%M} ET" if last is not None else "no bars in engine yet") + \
               f"; setups so far {len(self.setups)}; duplicates dropped {getattr(self, 'dropped', 0)}"

    def _publish(self, s: Setup) -> None:
        print(f"SETUP {s.symbol} {s.strategy} {s.side} trigger {s.trigger:.2f} stop {s.stop:.2f} "
              f"target {s.target:.2f} ready {s.ready_time.tz_convert(cfg.TZ):%H:%M} ET ({s.reason})", flush=True)
        if self.sim is not None:
            self.sim.add_setup(s)
        if self.ex is not None:
            self.ex.add_setup(s)
        if self.log:
            store.log_signal({"date": str(s.ready_time.tz_convert(cfg.TZ).date()), "mode": self.mode, "symbol": s.symbol,
                              "strategy": s.strategy, "side": s.side, "trigger": s.trigger, "stop": s.stop,
                              "target": s.target, "status": "setup", "reason": f"{s.reason}; {self.rs.label(s.symbol)}"})
            self.notify(f"DT_Iris SETUP {s.symbol} {s.strategy} {s.side.upper()}  trigger {s.trigger:.2f}  "
                        f"stop {s.stop:.2f}  target {s.target:.2f}  ({s.reason})  {self.rs.label(s.symbol)}")

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
        t, cut_t, flat_t, _ = bars.times_at(now)
        if t >= cut_t or store.kill_switch_on():
            if self.sim is not None:
                self.sim.pending.clear()
            if self.ex is not None and self.ex.pending:
                self.ex.drop_pending(f"{cut_t:%H:%M} cutoff" if t >= cut_t else "kill switch")
        if t >= flat_t:
            if self.sim is not None and self.sim.positions:
                n0 = len(self.sim.events)
                self.sim.close_all(now, prices, f"flatten {flat_t:%H:%M}")
                for ev in self.sim.events[n0:]:
                    self._event(ev)
            if self.ex is not None and (self.ex.positions or self.ex.working):
                self.ex.flatten_all(prices, f"flatten {flat_t:%H:%M}")


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


def lists_from_watchlist(day: date) -> tuple[dict[str, set], dict[str, float]]:
    """(symbol -> strategies its list allows, symbol -> beta) from the day's watchlist. Older watchlists
    without a `lists` column allow every strategy."""
    from dt_scanner import load_watchlist
    wl = load_watchlist(day)
    allowed: dict[str, set] = {}
    betas: dict[str, float] = {}
    for _, r in wl.iterrows():
        sym = str(r["symbol"]).upper()
        lists = str(r.get("lists") or "")
        if lists and lists != "nan":
            allowed[sym] = {st for name in lists.split(",") for st in cfg.LIST_STRATEGIES.get(name, [])}
        b = r.get("beta")
        if b is not None and b == b:
            betas[sym] = float(b)
    return allowed, betas


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
        datr, adv = daily_stats(s, day)                  # same daily ATR / 20-day volume the backtests use
        contexts[s] = bars.DayContext(s, day, prev,
                                      float(pm.volume) if pm else 0.0,
                                      float(pm.last_done) if pm and pm.last_done else None, datr, adv)
        if datr is None:
            print(f"  {s}: no daily ATR (Power Ranger range check off for it)")
    allowed, betas = ({}, {}) if settings.get("symbols") else lists_from_watchlist(day)
    if not settings.get("symbols"):                      # relative gap as locked by the 09:00 scan (Power Ranger reads it)
        from dt_scanner import load_watchlist
        for r in load_watchlist(day).itertuples():
            rg = getattr(r, "rel_gap", None)
            if str(r.symbol).upper() in contexts and rg is not None and rg == rg:
                contexts[str(r.symbol).upper()].rel_gap_pct = float(rg)
    eng = Engine(mode, settings["strategies"], watch, contexts, executor, notify, allowed=allowed, betas=betas)
    if allowed:
        for name, strats in cfg.LIST_STRATEGIES.items():
            on = [s for s in syms if allowed.get(s, set()) & set(strats)]
            print(f"  {name} list -> {', '.join(strats)}: {', '.join(on) or '-'}")

    # warm-up with today's bars so far (indicators need the whole session), no trading on them
    now = pd.Timestamp.now(tz="UTC")
    bench = cfg.RS_BENCHMARK
    for s in syms + ([bench] if bench not in syms else []):
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
    print(f"warm-up done: {eng.status_line()}", flush=True)

    pace = Pacer(VERIFY_CALL_GAP_S)

    def history_rows(sym):
        pace.wait()                                       # Longbridge: about 10 quote calls a second
        cs = qctx.candlesticks(broker.lb_symbol(sym), Period.Min_1, 10, AdjustType.NoAdjust, TradeSessions.Intraday)
        return [(broker.to_utc(x.timestamp), x.open, x.high, x.low, x.close, x.volume) for x in cs]

    def on_candle(symbol, ev):
        if ev.is_confirmed:
            c = ev.candlestick
            q.put(("bar", broker.plain(symbol), c))

    def on_quote(symbol, ev):
        q.put(("quote", broker.plain(symbol), float(ev.last_done)))

    qctx.set_on_candlestick(on_candle)
    qctx.set_on_quote(on_quote)
    for s in syms + ([bench] if bench not in syms else []):
        qctx.subscribe_candlesticks(broker.lb_symbol(s), Period.Min_1, TradeSessions.Intraday)
    if executor is not None:
        qctx.subscribe([broker.lb_symbol(s) for s in syms], [SubType.Quote])

    store.log_activity(mode, "session_start", f"runner started: {len(syms)} symbols, {', '.join(settings['strategies'])}")
    notify(f"DT_Iris runner started ({mode}): {', '.join(syms)}")
    print(f"DT_Iris runner [{mode}] watching {len(syms)}: {', '.join(syms)}  (Ctrl+C to stop)")
    stop_at = time.time() + minutes * 60 if minutes else None
    prices: dict[str, float] = {}
    last_beat = last_poll = 0.0
    last_diag = time.time() - DIAG_EVERY_S + 30     # first status line 30 s after start
    settled_minute = None                                 # opening minutes: wait once per minute for the history
    guard = ErrorGuard(mode, notify)
    _, flat_t, _ = bars.session_times(day)
    end_t = (pd.Timestamp.combine(day, flat_t) + pd.Timedelta(minutes=5)).time()   # 15:05, or 12:50 on a half day

    def handle(kind, sym, payload):
        nonlocal settled_minute
        if kind == "bar":
            c = payload
            ts = broker.to_utc(c.timestamp)
            bar = (float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume))
            t_ny = ts.tz_convert(cfg.TZ)
            since_open = (t_ny - t_ny.normalize()).total_seconds() / 60 - (bars.OPEN_T.hour * 60 + bars.OPEN_T.minute)
            if 0 <= since_open < VERIFY_FIRST_MINUTES:
                if settled_minute is None or ts > settled_minute:
                    time.sleep(VERIFY_SETTLE_S)           # once per minute, not once per symbol
                    settled_minute = ts
                bar, note = verified_bar(history_rows, sym, ts, bar)
                if note:
                    print(f"{sym} {t_ny:%H:%M}: {note}")
                    store.log_activity(mode, "bar_check", f"{sym} {t_ny:%H:%M} {note}")
            prices[sym] = bar[3]
            eng.on_bar(sym, ts, *bar)
        elif kind == "quote" and executor is not None:
            prices[sym] = payload
            executor.on_price(sym, payload, pd.Timestamp.now(tz="UTC"))

    try:
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is not None:
                guard.run(f"{item[0]} {item[1]}", handle, *item)   # one bad event never stops the runner
            now = pd.Timestamp.now(tz="UTC")
            guard.run("clock", eng.clock, now, prices)
            if executor is not None and time.time() - last_poll >= POLL_S:
                guard.run("order check", executor.poll, now)
                guard.run("save state", lambda: store.save_state(executor.to_state()))
                last_poll = time.time()
            if time.time() - last_diag >= DIAG_EVERY_S:
                print(f"[{pd.Timestamp.now(tz=cfg.TZ):%H:%M} ET] {eng.status_line()}; queue {q.qsize()}", flush=True)
                last_diag = time.time()
            if time.time() - last_beat >= HEARTBEAT_S:
                guard.run("heartbeat", lambda: store.write_runner_status(
                    {"pid": os.getpid(), "beat": time.time(), "mode": mode, "symbols": syms, "time": store.now_iso(),
                     "open_positions": list((executor.positions if executor else eng.sim.positions).keys())}))
                last_beat = time.time()
            t_ny = now.tz_convert(cfg.TZ).time()
            if t_ny >= end_t or (stop_at and time.time() > stop_at):
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
        mode = a.mode or store.load_settings()["mode"]
        try:
            run_live(mode, a.minutes)
        except Exception as exc:                          # set-up failure or a crash outside the guarded loop
            import traceback
            traceback.print_exc()
            store.log_activity(mode, "error", f"runner CRASHED: {type(exc).__name__}: {exc}")
            if tg.configured():
                tg.send(f"DT_Iris runner ({mode}) CRASHED: {type(exc).__name__}: {exc}. "
                        f"Check open positions in the Longbridge app.")
            raise
