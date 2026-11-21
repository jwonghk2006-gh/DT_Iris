"""DT_Iris broker client (Longbridge OpenAPI): the ONLY file that can send an order.

Two broker venues, same code path:
    lbpaper   Longbridge paper (demo) account    LONGPORT_APP_KEY + LONGPORT_APP_SECRET + LONGPORT_PAPER_ACCESS_TOKEN
    live      Longbridge real account            LONGPORT_APP_KEY + LONGPORT_APP_SECRET + LONGPORT_LIVE_ACCESS_TOKEN
              and only if DT_LIVE_ENABLED=yes is also set
("paper" mode never comes here: its fills are simulated by dt_sim.)

Keys are read from DT_Iris/.env (or the environment) at run time. They are
never written to disk, logged, or shown in the dashboard.

Scope of this version: LIMIT orders only (marketable limits for entries and
exits, a resting limit for the target). The protective stop is watched by the
runner (STOP_MODE "local") until `probe_stop_order` has confirmed on the paper
account how Longbridge's LIT order behaves. No options, no withdrawals, no transfers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import dt_config as cfg
import dt_store as store

# Longbridge issues ONE App Key / App Secret per developer app; the paper (demo)
# and live accounts differ only by their Access Token. So:
#   LONGPORT_APP_KEY, LONGPORT_APP_SECRET            shared
#   LONGPORT_PAPER_ACCESS_TOKEN                      demo account
#   LONGPORT_LIVE_ACCESS_TOKEN                       real account
# A venue-specific LONGPORT_PAPER_APP_KEY etc. still overrides, if you ever need it.
VENUES = {
    "lbpaper": "LONGPORT_PAPER_",
    "live": "LONGPORT_LIVE_",
}
KEY_PARTS = ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN")


def _cred(venue: str, part: str) -> str | None:
    v = os.environ.get(VENUES[venue] + part)
    if v:
        return v
    return os.environ.get("LONGPORT_" + part) if part != "ACCESS_TOKEN" else None
REMARK = "DT_Iris"
STOP_MODE = "local"     # "local" = runner watches price; "broker" only after probe_stop_order passes


class BrokerError(RuntimeError):
    pass


def load_env() -> None:
    """Read KEY=VALUE lines from .env into the process environment (existing values win)."""
    if not cfg.ENV_FILE.exists():
        return
    for line in cfg.ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def keys_present(venue: str) -> bool:
    load_env()
    return all(_cred(venue, part) for part in KEY_PARTS)


def live_enabled() -> bool:
    load_env()
    return os.environ.get("DT_LIVE_ENABLED", "").strip().lower() == "yes"


def quote_venue() -> str | None:
    """Market data uses the demo token first; set DT_QUOTE_VENUE=live to read quotes with the live token."""
    load_env()
    pref = os.environ.get("DT_QUOTE_VENUE", "").strip().lower()
    order = ("live", "lbpaper") if pref == "live" else ("lbpaper", "live")
    for v in order:
        if keys_present(v):
            return v
    return None


def lb_symbol(sym: str) -> str:
    sym = sym.upper().strip()
    return sym if "." in sym else f"{sym}.US"


def plain(sym: str) -> str:
    return sym.split(".")[0].upper()


def to_utc(ts):
    """Longbridge returns NAIVE datetimes in the PC's local time zone (Singapore here).
    Python's astimezone() treats a naive datetime as local time, which is exactly that."""
    import pandas as pd
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.astimezone()                  # attach the local zone
    return pd.Timestamp(ts).tz_convert("UTC")


def price_dec(x: float) -> Decimal:
    tick = Decimal("0.01") if x >= 1 else Decimal("0.0001")
    return Decimal(str(x)).quantize(tick, rounding=ROUND_HALF_UP)


def _config(venue: str):
    from longport.openapi import Config
    load_env()
    if not keys_present(venue):
        raise BrokerError(f"Longbridge credentials for {venue} missing in .env")
    return Config.from_apikey(_cred(venue, "APP_KEY"), _cred(venue, "APP_SECRET"), _cred(venue, "ACCESS_TOKEN"),
                              enable_print_quote_packages=False)


def quote_context(venue: str | None = None):
    from longport.openapi import QuoteContext
    venue = venue or quote_venue()
    if venue is None:
        raise BrokerError("No Longbridge keys in .env (LONGPORT_PAPER_* or LONGPORT_LIVE_*).")
    return QuoteContext(_config(venue))


# ------------------------------------------------------------------ gates
@dataclass
class OrderIntent:
    symbol: str
    side: str            # "long" | "short"
    action: str          # "open" | "close" | "target"
    shares: int
    price: float
    risk_usd: float = 0.0


def gate_failures(mode: str, intent: OrderIntent, day_pnl: float = 0.0) -> list[str]:
    """Every reason this order must NOT be sent. Empty list = allowed.

    Closing orders are only blocked by missing keys: getting OUT must always work.
    """
    bad = []
    if mode not in VENUES:
        return [f"mode {mode!r} does not send broker orders"]
    if not keys_present(mode):
        bad.append(f"Longbridge credentials for {mode} missing in .env")
    if intent.action != "open":
        return bad
    if store.kill_switch_on():
        bad.append("kill switch is ON")
    if intent.shares <= 0:
        bad.append("quantity is zero")
    if mode == "live":
        if not live_enabled():
            bad.append("DT_LIVE_ENABLED=yes is not set")
        if intent.side == "short" and not cfg.ALLOW_SHORT_LIVE:
            bad.append("live shorts are switched off (ALLOW_SHORT_LIVE)")
        if intent.shares * intent.price > cfg.LIVE_MAX_POSITION_VALUE + 1e-6:
            bad.append(f"position value {intent.shares * intent.price:,.0f} > LIVE_MAX_POSITION_VALUE {cfg.LIVE_MAX_POSITION_VALUE:,.0f}")
        if intent.risk_usd > cfg.LIVE_MAX_RISK_PER_TRADE + 1e-6:
            bad.append(f"risk {intent.risk_usd:,.0f} > LIVE_MAX_RISK_PER_TRADE {cfg.LIVE_MAX_RISK_PER_TRADE:,.0f}")
        if day_pnl <= -cfg.LIVE_MAX_DAILY_LOSS:
            bad.append("daily loss limit reached")
        if store.paper_sessions() < cfg.LIVE_MIN_PAPER_DAYS:
            bad.append(f"only {store.paper_sessions()} paper sessions, need {cfg.LIVE_MIN_PAPER_DAYS}")
    return bad


# ------------------------------------------------------------------ the client
class Broker:
    """Thin wrapper over Longbridge TradeContext. `trade_ctx` can be a fake in tests."""

    def __init__(self, mode: str, trade_ctx=None):
        if mode not in VENUES:
            raise BrokerError(f"mode {mode!r} is not a broker venue")
        self.mode = mode
        self._ctx = trade_ctx

    @property
    def ctx(self):
        if self._ctx is None:
            from longport.openapi import TradeContext
            if not keys_present(self.mode):
                raise BrokerError(f"Longbridge credentials for {self.mode} missing in .env")
            self._ctx = TradeContext(_config(self.mode))
        return self._ctx

    def submit_limit(self, intent: OrderIntent, day_pnl: float = 0.0, tag: str = "") -> str:
        """Send one Day limit order after the gates pass. Returns the broker order id."""
        bad = gate_failures(self.mode, intent, day_pnl)
        if bad:
            raise BrokerError("; ".join(bad))
        from longport.openapi import OrderSide, OrderType, OutsideRTH, TimeInForceType
        opening_buy = intent.side == "long"
        buy = opening_buy if intent.action == "open" else not opening_buy
        resp = self.ctx.submit_order(
            lb_symbol(intent.symbol), OrderType.LO, OrderSide.Buy if buy else OrderSide.Sell,
            Decimal(int(intent.shares)), TimeInForceType.Day, submitted_price=price_dec(intent.price),
            outside_rth=OutsideRTH.RTHOnly, remark=f"{REMARK}|{intent.action}|{tag}"[:64])
        store.log_activity(self.mode, "order", f"{intent.action} {'BUY' if buy else 'SELL'} {intent.shares} "
                                               f"{intent.symbol} @ {intent.price:.2f} -> {resp.order_id}")
        return str(resp.order_id)

    def cancel(self, order_id: str) -> None:
        try:
            self.ctx.cancel_order(order_id)
        except Exception as exc:                       # already filled / cancelled
            store.log_activity(self.mode, "warn", f"cancel {order_id}: {exc}")

    def order_state(self, order_id: str) -> dict:
        d = self.ctx.order_detail(order_id)
        status = str(d.status).split(".")[-1]
        return {"status": status, "filled": float(d.executed_quantity or 0),
                "avg_price": float(d.executed_price) if d.executed_price is not None else None}

    def positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for ch in self.ctx.stock_positions().channels:
            for p in ch.positions:
                if float(p.quantity):
                    out[plain(p.symbol)] = out.get(plain(p.symbol), 0.0) + float(p.quantity)
        return out

    def open_order_ids(self) -> list[str]:
        live = {"New", "PartialFilled", "WaitToNew", "NotReported", "PendingReplace", "WaitToReplace", "Replaced"}
        return [o.order_id for o in self.ctx.today_orders()
                if str(o.status).split(".")[-1] in live and (o.remark or "").startswith(REMARK)]

    def net_assets_usd(self) -> float | None:
        try:
            for b in self.ctx.account_balance("USD"):
                return float(b.net_assets)
        except Exception:
            return None
        return None


def connection_report(mode: str) -> dict:
    """Read-only check used by Settings -> Test connection. Sends no order."""
    rep = {"mode": mode, "keys": keys_present(mode) if mode in VENUES else False, "ok": False}
    if mode not in VENUES or not rep["keys"]:
        rep["message"] = "keys missing" if mode in VENUES else "paper mode needs no keys"
        rep["ok"] = mode == "paper"
        return rep
    try:
        b = Broker(mode)
        rep["net_assets_usd"] = b.net_assets_usd()
        rep["positions"] = b.positions()
        q = quote_context(mode).quote(["SPY.US"])
        rep["spy_last"] = float(q[0].last_done) if q else None
        rep["ok"] = True
        rep["message"] = "connected"
    except Exception as exc:
        rep["message"] = f"{type(exc).__name__}: {exc}"
    if mode == "live":
        rep["live_switch"] = live_enabled()
    return rep


def probe_stop_order(symbol: str = "SPY") -> dict:
    """Paper account only: learn how a SELL LIT order behaves before trusting broker-side stops.

    Buys 1 share, places a SELL LIT with the trigger 3% BELOW the market, waits, and
    reports whether it stayed untriggered (correct stop behaviour) or fired at once.
    Then cancels everything and sells the share. Never runs on the live account.
    """
    import time
    from longport.openapi import OrderSide, OrderType, OutsideRTH, TimeInForceType
    b = Broker("lbpaper")
    q = quote_context("lbpaper").quote([lb_symbol(symbol)])[0]
    last = float(q.last_done)
    buy = b.submit_limit(OrderIntent(symbol, "long", "open", 1, last * 1.005), tag="probe")
    time.sleep(5)
    if b.order_state(buy)["filled"] < 1:
        b.cancel(buy)
        return {"ok": False, "message": "probe buy did not fill (market closed?)"}
    trig = last * 0.97
    resp = b.ctx.submit_order(lb_symbol(symbol), OrderType.LIT, OrderSide.Sell, Decimal(1), TimeInForceType.Day,
                              submitted_price=price_dec(trig * 0.995), trigger_price=price_dec(trig),
                              outside_rth=OutsideRTH.RTHOnly, remark=f"{REMARK}|probe-stop")
    time.sleep(8)
    st = b.order_state(str(resp.order_id))
    b.cancel(str(resp.order_id))
    fired = st["filled"] > 0
    if not fired:
        b.submit_limit(OrderIntent(symbol, "long", "close", 1, last * 0.995), tag="probe")
    result = {"ok": not fired, "lit_status": st["status"],
              "message": "LIT sell below market stayed waiting: usable as a broker-side stop" if not fired
              else "LIT sell below market FIRED immediately: keep STOP_MODE = 'local'"}
    store.log_activity("lbpaper", "probe", result["message"])
    return result


# ------------------------------------------------------------------ history (used by dt_data)
def history_1m(symbol: str, day: date):
    """1-minute bars for one NY trading day incl. pre/post market, as a DataFrame (UTC index)."""
    import pandas as pd
    from longport.openapi import AdjustType, Period, TradeSessions
    ctx = quote_context()
    rows = ctx.history_candlesticks_by_date(lb_symbol(symbol), Period.Min_1, AdjustType.NoAdjust, day, day,
                                           TradeSessions.All)
    recs = [{"time": to_utc(c.timestamp), "open": float(c.open), "high": float(c.high), "low": float(c.low),
             "close": float(c.close), "volume": float(c.volume)} for c in rows]
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    return df.set_index("time")


def history_daily(symbol: str, start: date, end: date):
    import pandas as pd
    from longport.openapi import AdjustType, Period
    rows = quote_context().history_candlesticks_by_date(lb_symbol(symbol), Period.Day, AdjustType.ForwardAdjust, start, end)
    recs = [{"date": c.timestamp.date() if isinstance(c.timestamp, datetime) else c.timestamp, "open": float(c.open),
             "high": float(c.high), "low": float(c.low), "close": float(c.close), "volume": float(c.volume)} for c in rows]
    return pd.DataFrame(recs).set_index("date") if recs else pd.DataFrame()
