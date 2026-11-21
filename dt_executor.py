"""Order manager for the broker modes (lbpaper / live).

The detectors hand it setups; it watches live prices and works the orders:

    setup waiting --price touches trigger--> entry limit sent (trigger + offset)
    entry filled  --> position open, target limit resting at the broker
    price hits stop (watched here, STOP_MODE "local") --> cancel target, send exit
    target filled --> trade closed
    12:00 ET --> waiting setups dropped;  15:00 ET --> everything flattened
    kill switch --> no new entries, waiting setups dropped (open trades keep their stop/target)

Every order goes through dt_broker.Broker, so the gates in dt_broker apply.
State is plain dicts so the runner can save it after every change and pick up
after a crash (`from_state` + `reconcile`).
"""
from __future__ import annotations

import math
import time

import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_store as store
from dt_broker import Broker, BrokerError, OrderIntent
from dt_strategies import Setup

ENTRY_TIMEOUT_S = 20
EXIT_RETRY_S = 10
EXIT_AWAY = (0.005, 0.015)      # marketable exit limit: 0.5% through the price, then 1.5% on retry


def _ny(ts: pd.Timestamp):
    return ts.tz_convert(cfg.TZ).time()


class Executor:
    def __init__(self, broker: Broker, notify=None, clock=time.time):
        self.b = broker
        self.mode = broker.mode
        self.notify = notify or (lambda text: None)
        self.clock = clock
        self.pending: dict[str, Setup] = {}
        self.working: dict[str, dict] = {}     # symbol -> entry order in flight
        self.positions: dict[str, dict] = {}
        self.day = None
        self.day_pnl = 0.0
        self.day_r = 0.0
        self.trades_today = 0
        self.account = cfg.ACCOUNT_SIZE

    # ------------------------------------------------------------ sizing / limits
    def refresh_account(self) -> None:
        na = self.b.net_assets_usd()
        if na and na > 0:
            self.account = min(cfg.ACCOUNT_SIZE, na)

    def risk_budget(self) -> float:
        r = self.account * cfg.RISK_PER_TRADE
        return min(r, cfg.LIVE_MAX_RISK_PER_TRADE) if self.mode == "live" else r

    def size(self, price: float, risk_ps: float) -> int:
        if risk_ps <= 0:
            return 0
        cap_value = self.account * cfg.MAX_POSITION_VALUE
        if self.mode == "live":
            cap_value = min(cap_value, cfg.LIVE_MAX_POSITION_VALUE)
        return max(0, min(math.floor(self.risk_budget() / risk_ps), math.floor(cap_value / price)))

    def _blocked(self, sym: str) -> str | None:
        if store.kill_switch_on():
            return "kill switch"
        if sym in self.positions or sym in self.working:
            return "already in a trade"
        if len(self.positions) + len(self.working) >= cfg.MAX_OPEN_POSITIONS:
            return "max open positions"
        if self.trades_today >= cfg.MAX_TRADES_PER_DAY:
            return "max trades today"
        if self.day_r <= -cfg.MAX_DAILY_LOSS_R:
            return "daily loss limit (R)"
        return None

    # ------------------------------------------------------------ events
    def new_day(self, now: pd.Timestamp) -> None:
        d = now.tz_convert(cfg.TZ).date()
        if d != self.day:
            self.day, self.day_pnl, self.day_r, self.trades_today = d, 0.0, 0.0, 0
            self.pending = {k: s for k, s in self.pending.items() if s.ready_time.tz_convert(cfg.TZ).date() == d}

    def add_setup(self, s: Setup) -> None:
        self.pending.setdefault(s.key, s)

    def on_price(self, sym: str, last: float, now: pd.Timestamp) -> None:
        """Called for every live price update (quote push)."""
        self.new_day(now)
        pos = self.positions.get(sym)
        if pos is not None and not pos.get("exit_oid"):
            sg = 1 if pos["side"] == "long" else -1
            if sg * (last - pos["stop"]) <= 0:
                self._exit(sym, last, "breakeven" if pos.get("be") else "stop")
            elif cfg.BREAKEVEN_AT_R and not pos.get("be"):
                if sg * (last - (pos["entry"] + sg * cfg.BREAKEVEN_AT_R * pos["risk_ps"])) >= 0:
                    pos["stop"], pos["be"] = pos["entry"], True
                    store.log_activity(self.mode, "manage", f"{sym}: stop to breakeven {pos['entry']:.2f}")
        if _ny(now) >= bars.CUTOFF_T:
            return
        for key, s in list(self.pending.items()):
            if s.symbol != sym:
                continue
            if now >= s.valid_until:
                del self.pending[key]
                continue
            if now < s.ready_time:
                continue
            sg = 1 if s.side == "long" else -1
            if sg * (last - s.trigger) < 0:
                continue
            del self.pending[key]
            limit = s.trigger + sg * cfg.ENTRY_LIMIT_OFFSET_R * s.risk
            if sg * (last - limit) > 0:
                store.log_signal({"date": str(self.day), "mode": self.mode, "symbol": sym, "strategy": s.strategy,
                                  "side": s.side, "trigger": s.trigger, "stop": s.stop, "target": s.target,
                                  "status": "missed", "reason": f"price {last:.2f} beyond limit {limit:.2f}"})
                continue
            why = self._blocked(sym)
            if why:
                store.log_signal({"date": str(self.day), "mode": self.mode, "symbol": sym, "strategy": s.strategy,
                                  "side": s.side, "trigger": s.trigger, "stop": s.stop, "target": s.target,
                                  "status": "skipped", "reason": why})
                continue
            shares = self.size(limit, abs(limit - s.stop))
            intent = OrderIntent(sym, s.side, "open", shares, limit, risk_usd=shares * abs(limit - s.stop))
            try:
                oid = self.b.submit_limit(intent, self.day_pnl, tag=s.strategy)
            except BrokerError as exc:
                store.log_activity(self.mode, "blocked", f"{sym} {s.strategy}: {exc}")
                self.notify(f"DT_Iris BLOCKED {sym} {s.strategy}: {exc}")
                continue
            self.working[sym] = {"oid": oid, "setup": s, "shares": shares, "limit": limit, "sent": self.clock()}
            self.trades_today += 1
            break

    def poll(self, now: pd.Timestamp) -> None:
        """Check every order in flight. Call every few seconds."""
        self.new_day(now)
        for sym, w in list(self.working.items()):
            st = self.b.order_state(w["oid"])
            age = self.clock() - w["sent"]
            done = st["status"] in ("Filled", "Canceled", "Rejected", "Expired")
            if st["status"] == "Filled" or (age > ENTRY_TIMEOUT_S and st["filled"] > 0) or (done and st["filled"] > 0):
                if st["status"] != "Filled":
                    self.b.cancel(w["oid"])
                self._opened(sym, w, int(st["filled"]), st["avg_price"] or w["limit"], now)
            elif done:
                del self.working[sym]
                self.trades_today -= 1
                store.log_activity(self.mode, "entry", f"{sym}: entry {st['status'].lower()}")
            elif age > ENTRY_TIMEOUT_S:
                self.b.cancel(w["oid"])
        for sym, pos in list(self.positions.items()):
            if pos.get("exit_oid"):
                st = self.b.order_state(pos["exit_oid"])
                if st["status"] == "Filled":
                    self._closed(sym, st["avg_price"] or pos["exit_px"], pos["exit_reason"], now)
                elif self.clock() - pos["exit_sent"] > EXIT_RETRY_S:
                    self.b.cancel(pos["exit_oid"])
                    left = pos["shares"] - int(st["filled"])
                    pos["shares"] = left
                    pos["exit_oid"] = None
                    if left > 0:
                        self._exit(sym, pos["exit_px"], pos["exit_reason"], away=EXIT_AWAY[1])
            elif pos.get("target_oid"):
                st = self.b.order_state(pos["target_oid"])
                if st["status"] == "Filled":
                    self._closed(sym, st["avg_price"] or pos["target"], "target", now)

    def drop_pending(self, why: str) -> None:
        if self.pending:
            store.log_activity(self.mode, "pending", f"{len(self.pending)} waiting setups dropped: {why}")
        self.pending.clear()

    def flatten_all(self, prices: dict, reason: str = "flatten 15:00") -> None:
        self.drop_pending(reason)
        for sym, w in list(self.working.items()):
            self.b.cancel(w["oid"])
        for sym, pos in list(self.positions.items()):
            if not pos.get("exit_oid"):
                self._exit(sym, prices.get(sym, pos["entry"]), reason)

    # ------------------------------------------------------------ internals
    def _opened(self, sym, w, shares, price, now) -> None:
        s: Setup = w["setup"]
        del self.working[sym]
        pos = {"side": s.side, "strategy": s.strategy, "shares": shares, "entry": float(price), "stop": s.stop,
               "target": s.target, "risk_ps": abs(float(price) - s.stop), "be": False, "entry_time": str(now),
               "setup_reason": s.reason, "target_oid": None, "exit_oid": None}
        self.positions[sym] = pos
        try:
            pos["target_oid"] = self.b.submit_limit(OrderIntent(sym, s.side, "target", shares, s.target), tag=s.strategy)
        except BrokerError as exc:
            store.log_activity(self.mode, "warn", f"{sym}: target order not placed: {exc}")
        msg = (f"DT_Iris {self.mode.upper()} ENTRY {sym} {s.strategy} {s.side.upper()} {shares} @ {price:.2f} "
               f"stop {s.stop:.2f} target {s.target:.2f}")
        store.log_activity(self.mode, "entry", msg)
        self.notify(msg)

    def _exit(self, sym, ref_price, reason, away=EXIT_AWAY[0]) -> None:
        pos = self.positions[sym]
        if pos.get("target_oid"):
            self.b.cancel(pos["target_oid"])
            st = self.b.order_state(pos["target_oid"])
            pos["target_oid"] = None
            if st["filled"] >= pos["shares"]:
                self._closed(sym, st["avg_price"] or pos["target"], "target", pd.Timestamp.now(tz="UTC"))
                return
            pos["shares"] -= int(st["filled"])
        sg = 1 if pos["side"] == "long" else -1
        px = ref_price * (1 - sg * away)
        px = min(px, ref_price - 2 * cfg.TICK) if sg > 0 else max(px, ref_price + 2 * cfg.TICK)
        try:
            pos["exit_oid"] = self.b.submit_limit(OrderIntent(sym, pos["side"], "close", pos["shares"], px), tag=reason)
        except BrokerError as exc:
            store.log_activity(self.mode, "ERROR", f"{sym}: EXIT NOT SENT ({exc}) - close it by hand")
            self.notify(f"DT_Iris EXIT FAILED {sym}: {exc}. Close it by hand.")
            return
        pos["exit_sent"], pos["exit_px"], pos["exit_reason"] = self.clock(), ref_price, reason

    def _closed(self, sym, price, reason, now) -> None:
        pos = self.positions.pop(sym)
        sg = 1 if pos["side"] == "long" else -1
        costs = 2 * cfg.commission(pos["shares"])
        pnl = sg * (float(price) - pos["entry"]) * pos["shares"] - costs
        risk = pos["risk_ps"] * pos["shares"]
        r = pnl / risk if risk > 0 else 0.0
        self.day_pnl += pnl
        self.day_r += r
        store.log_trade({"date": str(self.day), "mode": self.mode, "symbol": sym, "strategy": pos["strategy"],
                         "side": pos["side"], "entry_time": pos["entry_time"], "entry": pos["entry"], "stop": pos["stop"],
                         "target": pos["target"], "shares": pos["shares"], "exit_time": str(now), "exit": float(price),
                         "exit_reason": reason, "r_multiple": round(r, 3), "pnl": round(pnl, 2), "costs": round(costs, 2),
                         "notes": pos.get("setup_reason", "")})
        msg = f"DT_Iris {self.mode.upper()} EXIT {sym} {reason} @ {float(price):.2f}  {r:+.2f}R  ${pnl:+,.2f}"
        store.log_activity(self.mode, "exit", msg, pnl)
        self.notify(msg)

    # ------------------------------------------------------------ persistence
    def to_state(self) -> dict:
        return {"date": str(self.day) if self.day else None, "day_pnl": self.day_pnl, "day_r": self.day_r,
                "trades_today": self.trades_today, "positions": self.positions,
                "working": {k: {**v, "setup": v["setup"].as_row()} for k, v in self.working.items()}}

    def from_state(self, st: dict) -> None:
        today = pd.Timestamp.now(tz=cfg.TZ).date()
        if st.get("date") != str(today):
            return
        self.day = today
        self.day_pnl, self.day_r = st.get("day_pnl", 0.0), st.get("day_r", 0.0)
        self.trades_today = st.get("trades_today", 0)
        self.positions = st.get("positions", {})
        for sym, w in st.get("working", {}).items():
            self.b.cancel(w["oid"])            # an entry left over from a crash is not trusted: cancel it

    def reconcile(self) -> list[str]:
        """Compare our book with the broker's positions; report differences (never trades on its own)."""
        notes = []
        try:
            real = self.b.positions()
        except Exception as exc:
            return [f"could not read broker positions: {exc}"]
        for sym, pos in self.positions.items():
            sg = 1 if pos["side"] == "long" else -1
            if abs(real.get(sym, 0.0) - sg * pos["shares"]) > 1e-6:
                notes.append(f"{sym}: DT_Iris thinks {sg * pos['shares']}, broker shows {real.get(sym, 0.0)}")
        for sym, q in real.items():
            if sym not in self.positions:
                notes.append(f"{sym}: broker position {q} not opened by DT_Iris (left alone)")
        return notes
