"""Fill rules, shared by the backtester and the paper runner.

Feed 1-minute bars in time order with `on_bar`. Add setups with `add_setup`
once they are known (at their `ready_time`). Because the backtest and paper
trading call the same code, a paper day and a backtest of the same day give the
same trades.

Fill rules (all deliberately pessimistic):
- Entry is a stop-limit. Long fills when the bar trades at or above the trigger,
  at max(trigger, bar open) + slippage. If the bar OPENS beyond the limit
  (trigger + ENTRY_LIMIT_OFFSET_R x R) there is no fill and the order is dropped.
- On the entry bar the stop can be hit, the target cannot (we do not know the
  order of prices inside the bar).
- If stop and target are both inside a later bar: SAME_BAR_RULE (stop first).
- A bar that opens through the stop fills at the open (gap risk), plus slippage.
- Break-even: once a bar reaches +BREAKEVEN_AT_R, the stop moves to entry from the next bar.
- New entries stop at ENTRY_CUTOFF; anything open at FLATTEN_TIME is closed at that bar's open.
- Portfolio limits: MAX_OPEN_POSITIONS, MAX_TRADES_PER_DAY, MAX_DAILY_LOSS_R, one position per symbol.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

import dt_bars as bars
import dt_config as cfg
from dt_strategies import Setup


@dataclass
class Position:
    setup: Setup
    entry_time: pd.Timestamp
    entry: float
    shares: int
    stop: float
    target: float
    risk_ps: float               # planned risk per share (trigger - stop)
    be_armed: bool = False
    entry_cost: float = 0.0

    @property
    def sgn(self) -> float:
        return 1.0 if self.setup.side == "long" else -1.0


@dataclass
class Sim:
    account: float = cfg.ACCOUNT_SIZE
    risk_per_trade: float = cfg.RISK_PER_TRADE
    max_open: int = cfg.MAX_OPEN_POSITIONS
    max_trades: int = cfg.MAX_TRADES_PER_DAY
    max_daily_loss_r: float = cfg.MAX_DAILY_LOSS_R
    slip: float = cfg.SLIPPAGE_TICKS * cfg.TICK
    pending: dict = field(default_factory=dict)      # key -> Setup
    positions: dict = field(default_factory=dict)    # symbol -> Position
    trades: list = field(default_factory=list)
    events: list = field(default_factory=list)
    day: object = None
    day_r: float = 0.0
    trades_today: int = 0

    # ------------------------------------------------------------ helpers
    def _new_day(self, ts: pd.Timestamp) -> None:
        d = ts.tz_convert(cfg.TZ).date()
        if d != self.day:
            self.day, self.day_r, self.trades_today = d, 0.0, 0
            self.pending = {k: s for k, s in self.pending.items() if s.ready_time.tz_convert(cfg.TZ).date() == d}

    def add_setup(self, s: Setup) -> None:
        self.pending.setdefault(s.key, s)

    def _can_open(self, symbol: str) -> str | None:
        if symbol in self.positions:
            return "already in a position"
        if len(self.positions) >= self.max_open:
            return "max open positions"
        if self.trades_today >= self.max_trades:
            return "max trades today"
        if self.day_r <= -self.max_daily_loss_r:
            return "daily loss limit"
        return None

    def size(self, entry: float, risk_ps: float) -> int:
        if risk_ps <= 0:
            return 0
        sh = math.floor(self.account * self.risk_per_trade / risk_ps)
        cap = math.floor(self.account * cfg.MAX_POSITION_VALUE / max(entry, 1e-9))
        return max(0, min(sh, cap))

    def _close(self, sym: str, pos: Position, ts, price: float, reason: str) -> dict:
        exit_cost = cfg.commission(pos.shares)
        gross = pos.sgn * (price - pos.entry) * pos.shares
        costs = pos.entry_cost + exit_cost
        pnl = gross - costs
        risk_usd = pos.risk_ps * pos.shares
        r = pnl / risk_usd if risk_usd > 0 else 0.0
        t = {"date": str(ts.tz_convert(cfg.TZ).date()), "symbol": sym, "strategy": pos.setup.strategy,
             "side": pos.setup.side, "setup_time": str(pos.setup.ready_time), "entry_time": str(pos.entry_time),
             "entry": round(pos.entry, 4), "stop": pos.setup.stop, "target": pos.target, "shares": pos.shares,
             "exit_time": str(ts), "exit": round(price, 4), "exit_reason": reason, "r_multiple": round(r, 3),
             "pnl": round(pnl, 2), "costs": round(costs, 2), "reason": pos.setup.reason}
        self.trades.append(t)
        self.day_r += r
        del self.positions[sym]
        self.events.append(("exit", t))
        return t

    # ------------------------------------------------------------ main step
    def on_bar(self, sym: str, ts: pd.Timestamp, o: float, h: float, l: float, c: float) -> list[tuple]:
        """Process one CONFIRMED 1-minute bar (ts = bar start, UTC). Returns new events."""
        self._new_day(ts)
        n0 = len(self.events)
        t_ny = ts.tz_convert(cfg.TZ).time()

        # 1) manage an open position
        pos = self.positions.get(sym)
        if pos is not None:
            if t_ny >= bars.FLATTEN_T:
                self._close(sym, pos, ts, o - pos.sgn * self.slip, "flatten 15:00")
            else:
                self._manage(sym, pos, ts, o, h, l, c, entry_bar=False)

        # 2) pending entries for this symbol
        for key, s in list(self.pending.items()):
            if s.symbol != sym:
                continue
            if ts >= s.valid_until:
                del self.pending[key]
                self.events.append(("expired", s))
                continue
            if ts < s.ready_time:
                continue
            sg = 1.0 if s.side == "long" else -1.0
            touched = (h >= s.trigger) if sg > 0 else (l <= s.trigger)
            if not touched:
                continue
            del self.pending[key]
            limit = s.trigger + sg * cfg.ENTRY_LIMIT_OFFSET_R * s.risk
            if sg * (o - limit) > 0:
                self.events.append(("missed", s, "opened beyond the limit"))
                continue
            why = self._can_open(sym)
            if why:
                self.events.append(("skipped", s, why))
                continue
            fill = (max(s.trigger, o) if sg > 0 else min(s.trigger, o)) + sg * self.slip
            if sg * (fill - limit) > 0:
                fill = limit
            risk_ps = abs(fill - s.stop)
            shares = self.size(fill, risk_ps)
            if shares <= 0:
                self.events.append(("skipped", s, "size 0"))
                continue
            p = Position(s, ts, fill, shares, s.stop, s.target, risk_ps, entry_cost=cfg.commission(shares))
            self.positions[sym] = p
            self.trades_today += 1
            self.events.append(("entry", p))
            self._manage(sym, p, ts, o, h, l, c, entry_bar=True)
            break                                   # one entry per symbol per bar
        return self.events[n0:]

    def _manage(self, sym, pos: Position, ts, o, h, l, c, entry_bar: bool) -> None:
        sg = pos.sgn
        stop, target = pos.stop, pos.target
        if not entry_bar and sg * (stop - o) >= 0:                  # opened through the stop
            self._close(sym, pos, ts, o - sg * self.slip, "stop (gap)")
            return
        hit_stop = (l <= stop) if sg > 0 else (h >= stop)
        hit_target = (not entry_bar) and ((h >= target) if sg > 0 else (l <= target))
        if not entry_bar and sg * (o - target) >= 0:                # opened through the target: take the open
            self._close(sym, pos, ts, o, "target (gap)")
            return
        if hit_stop and (not hit_target or cfg.SAME_BAR_RULE == "stop_first"):
            reason = "breakeven" if pos.be_armed else "stop"
            self._close(sym, pos, ts, stop - sg * self.slip, reason)
            return
        if hit_target:
            self._close(sym, pos, ts, target, "target")
            return
        if cfg.BREAKEVEN_AT_R and not pos.be_armed:
            be_level = pos.entry + sg * cfg.BREAKEVEN_AT_R * pos.risk_ps
            if (h >= be_level) if sg > 0 else (l <= be_level):
                pos.stop, pos.be_armed = pos.entry, True

    def close_all(self, ts: pd.Timestamp, prices: dict, reason: str = "end of data") -> None:
        for sym, pos in list(self.positions.items()):
            px = prices.get(sym, pos.entry)
            self._close(sym, pos, ts, px - pos.sgn * self.slip, reason)
