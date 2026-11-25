"""DT_Iris guard tests. Run before trusting any change:

    .venv\\Scripts\\python.exe test_dt.py

Checks bar building, fill rules, detector symmetry, backtest == live-engine
replay, the broker gates and the order manager - without contacting Longbridge.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

import dt_bars as bars
import dt_config as cfg
import dt_store as store

# keep tests away from the real store/
_tmp = Path(tempfile.mkdtemp(prefix="dt_iris_test_"))
for name, fname in [("SETTINGS_FILE", "runner_settings.json"), ("STATE_FILE", "runner_state.json"),
                    ("STATUS_FILE", "runner_status.json"), ("KILL_FILE", "KILL_SWITCH"), ("TRADES_FILE", "trades.csv"),
                    ("ACTIVITY_FILE", "activity.csv"), ("SIGNALS_FILE", "signals.csv")]:
    setattr(store, name, _tmp / fname)
store.STORE = _tmp

from dt_sim import Sim                       # noqa: E402
from dt_strategies import Setup, mirror, power_ranger, session_setups  # noqa: E402
import dt_broker as broker                   # noqa: E402

DAY = pd.Timestamp("2026-06-03 09:30", tz=cfg.TZ).tz_convert("UTC")


def minute(k: int) -> pd.Timestamp:
    return DAY + pd.Timedelta(minutes=k)


def frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df.index = [minute(k) for k in range(len(df))]
    df.index.name = "time"
    return df


def setup(side="long", trig=10.0, stop=9.5, target=11.0, ready=1, valid=30) -> Setup:
    return Setup("TST", "test", side, minute(ready - 1), minute(ready), trig, stop, target, minute(valid), "")


# ------------------------------------------------------------------ bars
def test_barbuilder_only_completed_bars():
    bb = bars.BarBuilder(2)
    assert bb.add(minute(0), 1, 2, 0.5, 1.5, 100) == []          # 09:30 alone: unfinished, not released
    out = bb.add(minute(1), 1.5, 3, 1, 2, 50)                     # 09:31 completes 09:30-09:31
    assert len(out) == 1 and out[0]["time"] == minute(0) and out[0]["high"] == 3 and out[0]["volume"] == 150
    assert bb.add(minute(2), 2, 2, 2, 2, 1) == []
    assert bb.flush_until(minute(3)) == []                        # 09:33 not over yet
    out = bb.flush_until(minute(4))                               # missing 09:33 minute: released once its time passed
    assert len(out) == 1 and out[0]["time"] == minute(2)


def test_resample_matches_builder_alignment():
    rng = np.random.default_rng(1)
    px = 10 + np.cumsum(rng.normal(0, 0.05, 30))
    df = frame([[p, p + 0.05, p - 0.05, p, 100] for p in px])
    r2 = bars.resample(df, 2)
    assert len(r2) == 15 and r2.index[0] == minute(0) and r2.index[1] == minute(2)
    assert r2.high.iloc[0] == df.high.iloc[:2].max()


# ------------------------------------------------------------------ fills
def run_sim(df, setups, **kw):
    sim = Sim(**kw)
    for ts, r in df.iterrows():
        for s in setups:
            if s.ready_time <= ts:
                sim.add_setup(s)
        sim.on_bar("TST", ts, r.open, r.high, r.low, r.close)
    return sim


def test_entry_bar_can_stop_but_not_target():
    df = frame([[9.9, 9.95, 9.85, 9.9, 1], [9.9, 11.5, 9.4, 10.0, 1]])   # touches trigger, target AND stop
    sim = run_sim(df, [setup()])
    assert len(sim.trades) == 1 and sim.trades[0]["exit_reason"] == "stop"


def test_same_bar_stop_first():
    df = frame([[9.9, 9.95, 9.85, 9.9, 1], [9.95, 10.1, 9.9, 10.05, 1], [10.0, 11.2, 9.4, 10.5, 1]])
    sim = run_sim(df, [setup()])
    assert sim.trades[0]["exit_reason"] == "stop", sim.trades


def test_target_and_costs():
    df = frame([[9.9, 9.95, 9.85, 9.9, 1], [9.95, 10.1, 9.9, 10.05, 1], [10.1, 11.2, 10.0, 11.1, 1]])
    sim = run_sim(df, [setup()])
    t = sim.trades[0]
    assert t["exit_reason"] == "target" and t["exit"] == 11.0
    assert t["entry"] == 10.01                                     # trigger + 1 tick slippage
    assert t["costs"] > 0 and t["r_multiple"] < 2.0


def test_open_beyond_limit_is_missed():
    df = frame([[9.9, 9.95, 9.85, 9.9, 1], [10.2, 10.3, 10.1, 10.2, 1]])   # opens > trigger + 0.1R (10.05)
    sim = run_sim(df, [setup()])
    assert not sim.trades and not sim.positions
    assert any(e[0] == "missed" for e in sim.events)


def test_breakeven_then_scratch():
    df = frame([[9.9, 9.95, 9.85, 9.9, 1], [9.95, 10.1, 9.9, 10.05, 1], [10.1, 10.6, 10.05, 10.5, 1],
                [10.4, 10.45, 9.9, 9.95, 1]])
    sim = run_sim(df, [setup()])
    assert sim.trades[0]["exit_reason"] == "breakeven" and abs(sim.trades[0]["exit"] - 10.0) < 0.02


def test_flatten_at_1500():
    rows = [[9.9, 9.95, 9.85, 9.9, 1], [9.95, 10.1, 9.9, 10.05, 1]] + [[10.2, 10.3, 10.1, 10.2, 1]] * 400
    df = frame(rows)
    sim = run_sim(df, [setup(target=20.0)])
    t = sim.trades[0]
    assert t["exit_reason"] == "flatten 15:00"
    assert pd.Timestamp(t["exit_time"]).tz_convert(cfg.TZ).strftime("%H:%M") == "15:00"


def test_short_mirror_fill():
    df = frame([[30.1, 30.15, 30.05, 30.1, 1], [30.05, 30.1, 29.9, 29.95, 1], [29.9, 30.0, 28.9, 29.0, 1]])
    sim = run_sim(df, [setup("short", 30.0, 30.5, 29.0)])
    t = sim.trades[0]
    assert t["exit_reason"] == "target" and t["entry"] == 29.99 and t["pnl"] > 0


def test_daily_loss_limit():
    sim = Sim(max_daily_loss_r=1.0)
    sim._new_day(minute(0))
    sim.day_r = -1.0
    assert sim._can_open("X") == "daily loss limit"


# ------------------------------------------------------------------ detectors
def test_power_ranger_long_and_mirror():
    ctx = bars.DayContext("TST", DAY.date(), 9.0, 50_000, 10.0, None, None)   # +11% gap
    rows = [[10.0, 10.2, 9.95, 10.1, 9e3], [10.1, 10.15, 10.0, 10.05, 5e3], [10.05, 10.3, 10.0, 10.25, 6e3],
            [10.25, 10.3, 10.1, 10.2, 4e3], [10.2, 10.28, 10.12, 10.26, 4e3]]
    df = bars.add_indicators(frame(rows))
    s = power_ranger("TST", df, 4, ctx, {})
    assert len(s) == 1 and s[0].side == "long" and s[0].trigger == 10.31 and s[0].stop == 9.94
    # the same bars mirrored, with a gap down, must give the mirrored short (price filter off for the check)
    old = cfg.SHORT_MIN_PRICE
    cfg.SHORT_MIN_PRICE = -1e9
    try:
        m = mirror(df)
        m[["open", "high", "low", "close", "ema", "vwap"]] += 40.0         # back to positive prices around 30
        ctx2 = bars.DayContext("TST", DAY.date(), 33.0, 50_000, 30.0, None, None)
        s2 = power_ranger("TST", m, 4, ctx2, {})
        assert len(s2) == 1 and s2[0].side == "short"
        assert abs(s2[0].trigger - (40 - 10.31)) < 0.011 and abs(s2[0].stop - (40 - 9.94)) < 0.011
    finally:
        cfg.SHORT_MIN_PRICE = old


def _orb_day(retest_low=49.97, after_lows=(50.10, 50.12), anchor=True):
    """OR 09:30-09:44 high 50.00 / low 48.80, breakout close 09:48, retest 09:50, two bars after."""
    rows = [[49.0, 49.3, 48.8, 49.2, 9e3]] + [[49.5, 49.8, 49.3, 49.6, 5e3] for _ in range(14)]
    rows[0][1] = 50.00
    if anchor:                                       # pivot low at 09:41 (confirmed 09:43) before the breakout
        for k, lo in ((9, 49.40), (10, 49.35), (11, 49.20), (12, 49.35), (13, 49.40)):
            rows[k][2] = lo
    rows += [[49.6, 49.9, 49.5, 49.8, 5e3], [49.8, 49.95, 49.7, 49.9, 5e3], [49.9, 49.98, 49.8, 49.95, 5e3]]  # 09:45-47
    rows += [[49.99, 50.3, 49.99, 50.20, 8e3], [50.2, 50.35, 50.15, 50.30, 6e3]]                             # 09:48 break
    rows += [[50.3, 50.32, retest_low, 50.20, 6e3]]                                                          # 09:50 retest
    rows += [[50.2, 50.30, after_lows[0], 50.22, 5e3], [50.22, 50.31, after_lows[1], 50.25, 5e3]]           # 09:51-52
    rows += [[50.25, 50.4, 50.2, 50.35, 5e3] for _ in range(20)]
    return bars.add_indicators(frame(rows))


ORB_CTX = bars.DayContext("TST", DAY.date(), 45.0, 50_000, 49.0, None, 5_000_000)


def test_orb_retest_a_and_b_share_the_wick():
    from dt_strategies import orb_retest_a, orb_retest_b
    df = _orb_day()
    a = [s for i in range(len(df)) for s in orb_retest_a("TST", df, i, ORB_CTX, {})]
    b = [s for i in range(len(df)) for s in orb_retest_b("TST", df, i, ORB_CTX, {})]
    assert len(a) == 1 and a[0].bar_time == minute(20) and abs(a[0].trigger - 50.21) < 1e-6 and abs(a[0].stop - 49.95) < 1e-6
    assert len(b) == 1 and b[0].bar_time == minute(22) and abs(b[0].trigger - 50.26) < 1e-6 and abs(b[0].stop - 49.95) < 1e-6
    assert abs(b[0].target - (50.26 + 2 * 0.31)) < 1e-6
    assert b[0].valid_until == b[0].ready_time + pd.Timedelta(minutes=1)
    assert b[0].extra["rsi_tag"] in ("yes", "no", "na")


def test_orb_retest_b_needs_a_strict_swing_and_no_second_chance():
    from dt_strategies import orb_retest_a, orb_retest_b
    df = _orb_day(after_lows=(49.96, 50.12))                     # next bar undercuts the retest wick
    assert [s for i in range(len(df)) for s in orb_retest_a("TST", df, i, ORB_CTX, {})]
    assert not [s for i in range(len(df)) for s in orb_retest_b("TST", df, i, ORB_CTX, {})]


def test_orb_retest_filters_and_mirror():
    from dt_strategies import orb_retest_b
    df = _orb_day()
    thin = bars.DayContext("TST", DAY.date(), 45.0, 50_000, 49.0, None, 500_000)
    assert not [s for i in range(len(df)) for s in orb_retest_b("TST", df, i, thin, {})]       # avg volume filter
    old = cfg.SHORT_MIN_PRICE
    cfg.SHORT_MIN_PRICE = -1e9
    try:
        m = mirror(df)
        m[["open", "high", "low", "close", "ema", "vwap"]] += 100.0
        s = [s for i in range(len(m)) for s in orb_retest_b("TST", m, i, ORB_CTX, {})]
        assert len(s) == 1 and s[0].side == "short"
        assert abs(s[0].trigger - (100 - 50.26)) < 0.011 and abs(s[0].stop - (100 - 49.95)) < 0.011
    finally:
        cfg.SHORT_MIN_PRICE = old


def test_no_setups_after_cutoff():
    ctx = bars.DayContext("TST", DAY.date(), 9.0, 50_000, 10.0, None, None)
    rng = np.random.default_rng(3)
    px = 10 + np.cumsum(rng.normal(0, 0.03, 390))
    df = frame([[p, p + 0.04, p - 0.04, p + 0.01, 1000 + 500 * rng.random()] for p in px])
    for s in session_setups("TST", df, ctx, ["power_ranger", "cupid", "tug_of_war", "roller_coaster"]):
        assert s.ready_time.tz_convert(cfg.TZ).time() < bars.CUTOFF_T
        assert s.valid_until.tz_convert(cfg.TZ).time() <= bars.CUTOFF_T


# ------------------------------------------------------------------ parity: backtest == live engine
def test_backtest_equals_replay():
    import dt_backtest as bt
    import dt_data as data
    import dt_runner as runner
    av = data.available()
    if av.empty:
        print("   (skipped: no data imported)")
        return
    day = sorted(av["date"].unique())[1] if av["date"].nunique() > 1 else av["date"].iloc[0]
    syms = sorted(set(av[av.date == day]["symbol"]) - set(cfg.BENCHMARKS))
    strategies = list(cfg.STRATEGIES)
    eng = runner.replay(day, syms, strategies)
    di = {d: v for d, v in bt._day_inputs(syms, research=False).items() if d == day}
    sb = {}
    for d, per in di.items():
        for sym, (reg, ctx, allowed) in per.items():
            sb.setdefault(d, []).extend(session_setups(sym, reg, ctx, strategies, allowed))
    sim = bt.simulate(di, sb)
    key = lambda t: (t["symbol"], t["strategy"], t["entry_time"], t["exit"], t["r_multiple"])
    a, b = sorted(map(key, eng.sim.trades)), sorted(map(key, sim.trades))
    assert a == b, f"replay {a} != backtest {b}"
    assert sorted(s.key for s in eng.setups) == sorted(s.key for v in sb.values() for s in v)


# ------------------------------------------------------------------ Longbridge timestamps
def test_naive_timestamps_are_local_time():
    from datetime import datetime
    naive = datetime(2026, 6, 3, 21, 30)                         # what Longbridge hands back (PC local time)
    expect = pd.Timestamp(naive.astimezone()).tz_convert("UTC")
    assert broker.to_utc(naive) == expect
    assert broker.to_utc(pd.Timestamp("2026-06-03 13:30", tz="UTC")) == pd.Timestamp("2026-06-03 13:30", tz="UTC")


# ------------------------------------------------------------------ broker gates
def _env(**kv):
    for k in ["LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_PAPER_ACCESS_TOKEN", "LONGPORT_LIVE_ACCESS_TOKEN",
              "LONGPORT_PAPER_APP_KEY", "LONGPORT_LIVE_APP_KEY", "DT_LIVE_ENABLED"]:
        os.environ.pop(k, None)
    os.environ.update(kv)


def test_shared_app_key_two_tokens():
    old = cfg.ENV_FILE
    cfg.ENV_FILE = _tmp / "no.env"
    try:
        _env(LONGPORT_APP_KEY="k", LONGPORT_APP_SECRET="s", LONGPORT_PAPER_ACCESS_TOKEN="tp")
        assert broker.keys_present("lbpaper") and not broker.keys_present("live")
        os.environ["LONGPORT_LIVE_ACCESS_TOKEN"] = "tl"
        assert broker.keys_present("live")
        assert broker._cred("live", "ACCESS_TOKEN") == "tl" and broker._cred("lbpaper", "ACCESS_TOKEN") == "tp"
    finally:
        cfg.ENV_FILE = old


def test_live_gates():
    old = cfg.ENV_FILE
    cfg.ENV_FILE = _tmp / "no.env"
    try:
        _env(LONGPORT_APP_KEY="k", LONGPORT_APP_SECRET="s", LONGPORT_LIVE_ACCESS_TOKEN="t")
        i = broker.OrderIntent("ABC", "long", "open", 10, 10.0, risk_usd=10)
        bad = broker.gate_failures("live", i)
        assert any("DT_LIVE_ENABLED" in b for b in bad) and any("paper sessions" in b for b in bad)
        os.environ["DT_LIVE_ENABLED"] = "yes"
        assert any("ALLOW_SHORT_LIVE" in b for b in broker.gate_failures("live", broker.OrderIntent("ABC", "short", "open", 1, 30.0)))
        big = broker.OrderIntent("ABC", "long", "open", 1000, 10.0, risk_usd=500)
        bad = broker.gate_failures("live", big)
        assert any("LIVE_MAX_POSITION_VALUE" in b for b in bad) and any("LIVE_MAX_RISK" in b for b in bad)
        store.set_kill_switch(True)
        assert any("kill switch" in b for b in broker.gate_failures("live", i))
        assert broker.gate_failures("live", broker.OrderIntent("ABC", "long", "close", 10, 9.0)) == []  # exits always allowed
        store.set_kill_switch(False)
        assert broker.gate_failures("paper", i)                                        # paper never sends
    finally:
        cfg.ENV_FILE = old
        _env()


# ------------------------------------------------------------------ order manager with a fake broker
class FakeCtx:
    pass


class FakeBroker(broker.Broker):
    def __init__(self):
        self.mode = "lbpaper"
        self.orders: dict[str, dict] = {}
        self.n = 0

    def submit_limit(self, intent, day_pnl=0.0, tag=""):
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"intent": intent, "status": "New", "filled": 0, "avg_price": None}
        return oid

    def cancel(self, oid):
        if self.orders[oid]["status"] != "Filled":
            self.orders[oid]["status"] = "Canceled"

    def order_state(self, oid):
        o = self.orders[oid]
        return {"status": o["status"], "filled": o["filled"], "avg_price": o["avg_price"]}

    def fill(self, oid, price):
        o = self.orders[oid]
        o.update(status="Filled", filled=o["intent"].shares, avg_price=price)

    def net_assets_usd(self):
        return 10_000.0

    def positions(self):
        return {}


def test_executor_flow():
    from dt_executor import Executor
    fb = FakeBroker()
    clock = [1000.0]
    ex = Executor(fb, clock=lambda: clock[0])
    now = minute(5)
    ex.add_setup(setup(ready=1, valid=60))
    ex.on_price("TST", 9.95, now)
    assert not fb.orders                                        # below trigger: nothing sent
    ex.on_price("TST", 10.01, now)
    entry = fb.orders["o1"]
    assert entry["intent"].action == "open" and abs(entry["intent"].price - 10.05) < 1e-9   # limit = trigger + 0.1R
    fb.fill("o1", 10.02)
    ex.poll(now)
    assert "TST" in ex.positions and fb.orders["o2"]["intent"].action == "target"
    ex.on_price("TST", 9.49, now)                               # through the stop
    assert fb.orders["o2"]["status"] == "Canceled" and fb.orders["o3"]["intent"].action == "close"
    fb.fill("o3", 9.47)
    ex.poll(now)
    assert not ex.positions
    tr = store.read_trades()
    assert len(tr) == 1 and tr["exit_reason"].iloc[0] == "stop" and tr["r_multiple"].iloc[0] < -1.0


def test_executor_cutoff_and_kill_switch():
    from dt_executor import Executor
    fb = FakeBroker()
    ex = Executor(fb)
    late = pd.Timestamp("2026-06-03 12:01", tz=cfg.TZ).tz_convert("UTC")
    ex.add_setup(setup(valid=600))
    ex.on_price("TST", 10.2, late)
    assert not fb.orders                                        # no entries after 12:00
    store.set_kill_switch(True)
    try:
        ex2 = Executor(FakeBroker())
        ex2.add_setup(setup(valid=60))
        ex2.on_price("TST", 10.01, minute(5))
        assert not ex2.b.orders
    finally:
        store.set_kill_switch(False)


# ------------------------------------------------------------------ relative strength and the two watchlists
def test_beta_and_relative_gap():
    import dt_rs as rs
    rng = np.random.default_rng(1)
    m = rng.normal(0, 0.01, 80)
    days = pd.date_range("2026-01-02", periods=81, freq="B").date
    spy = pd.Series(100 * np.cumprod(np.r_[1, 1 + m]), index=days)
    chip = pd.Series(50 * np.cumprod(np.r_[1, 1 + 2 * m]), index=days)            # moves exactly 2x SPY
    assert abs(rs.beta(chip, spy) - 2.0) < 1e-6
    wild = pd.Series(50 * np.cumprod(np.r_[1, 1 + 9 * m]), index=days)
    assert rs.beta(wild, spy) == cfg.BETA_MAX                                     # clamped
    assert rs.beta(chip.tail(20), spy) is None                                    # too few days
    assert rs.rel_gap(3.0, -1.0, 1.0) == 4.0                                      # SPY -1%, stock +3% -> +4% strong
    assert rs.rel_gap(6.0, 0.5, 1.0) == 5.5
    assert rs.rel_gap(3.0, 1.0, 2.0) == 1.0                                       # a 2x-beta stock: mostly market
    assert rs.rel_gap(3.0, None, 1.0) is None
    assert rs.could_pass(3.0, -1.0, 4.0) and not rs.could_pass(1.0, 0.2, 4.0)
    etf, corr, sb = rs.best_sector(chip, {"SMH": chip * 1.5, "XLU": pd.Series(rng.normal(50, 1, 81), index=days)})
    assert etf == "SMH" and corr > 0.99 and abs(sb - 1.0) < 1e-6
    t = rs.RSTracker({"AAA": 1.5})
    t.on_bar("SPY", 100, 100); t.on_bar("AAA", 10, 10)
    t.on_bar("SPY", 100, 99); t.on_bar("AAA", 10, 10.1)                         # SPY -1%, AAA +1%, beta 1.5
    assert abs(t.rs("AAA") - 2.5) < 1e-9


def test_scanner_lists():
    import dt_scanner as sc
    base = dict(prev_close=10.0, premarket_volume=200_000, avg_daily_volume=2_000_000, split=None, spy_gap=-1.0, beta=1.0)
    rows = [
        dict(base, symbol="RSUP", price=30.0, gap_pct=3.0, rel_gap=4.0, pm_rvol=0.01),    # raw 3% but strong vs SPY
        dict(base, symbol="MKT", price=30.0, gap_pct=4.5, rel_gap=2.5, pm_rvol=0.01),     # big raw gap, mostly market
        dict(base, symbol="HOT", price=40.0, gap_pct=0.5, rel_gap=1.5, pm_rvol=0.20),     # no gap, heavy volume
        dict(base, symbol="WEAK", price=60.0, gap_pct=-1.0, rel_gap=-0.4, pm_rvol=0.08),  # in play, relative weakness
        dict(base, symbol="THIN", price=40.0, gap_pct=0.5, rel_gap=1.5, pm_rvol=0.20, avg_daily_volume=600_000),
    ]
    old = cfg.GAP_BASIS
    try:
        cfg.GAP_BASIS = "relative"
        df = sc.rank([dict(r) for r in rows], pd.Timestamp("2026-11-24").date()).set_index("symbol")
        assert df.loc["RSUP", "lists"] == "gap" and df.loc["RSUP", "side"] == "long"
        assert df.loc["MKT", "side"] == ""                                            # rejected: rel gap too small
        assert df.loc["HOT", "lists"] == "in_play" and df.loc["HOT", "side"] == "long"
        assert df.loc["WEAK", "lists"] == "in_play" and df.loc["WEAK", "side"] == "short"
        assert df.loc["THIN", "side"] == ""                                           # not liquid enough
        cfg.GAP_BASIS = "raw"
        df = sc.rank([dict(r) for r in rows], pd.Timestamp("2026-11-24").date()).set_index("symbol")
        assert df.loc["MKT", "lists"] == "gap" and df.loc["RSUP", "side"] == ""
    finally:
        cfg.GAP_BASIS = old


def test_engine_respects_lists():
    import dt_runner
    from dt_strategies import Setup
    eng = dt_runner.Engine("paper", ["power_ranger", "cupid"], {"AAA": {"long"}}, log=False,
                           allowed={"AAA": set(cfg.LIST_STRATEGIES["in_play"])})
    ts = pd.Timestamp("2026-11-24 15:00", tz="UTC")
    mk = lambda name: Setup("AAA", name, "long", ts, ts, 10.0, 9.0, 12.0, ts, "test")
    saved = dt_runner.DETECTORS.copy()
    try:
        for name in ("power_ranger", "cupid"):
            dt_runner.DETECTORS[name] = (lambda n: (lambda *a: [mk(n)]))(name)
        eng._sym("AAA")["frames"] = {tf: [{"time": ts, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1}]
                                     for tf in eng.tfs}
        got = {s.strategy for tf in eng.tfs for s in eng._detect("AAA", tf)}
        assert got == {"cupid"}, got                                                  # in_play list: no Power Ranger
    finally:
        dt_runner.DETECTORS.clear(); dt_runner.DETECTORS.update(saved)
    assert eng.on_bar("SPY", ts, 100, 100, 100, 100, 1) == [] and "SPY" not in eng.state   # benchmark only feeds RS


def test_opening_candle_replaced_by_history():
    """2026-11-24 bug: the first pushed candle reached back to the previous close (ADI high 378.86
    while the real 09:30 high was 337.57). The history candle must win; no history -> UNVERIFIED."""
    import dt_runner
    ts = pd.Timestamp("2026-11-24 14:30", tz="UTC")
    bad = (378.75, 378.86, 328.00, 335.165, 45136.0)
    good = (ts, 329.84, 337.57, 328.00, 335.165, 45136)
    other = (ts - pd.Timedelta(minutes=1), 328.8, 330.51, 327.67, 328.5, 3414)
    bar, note = dt_runner.verified_bar(lambda s: [other, good], "ADI", ts, bad, wait=0)
    assert bar[:4] == (329.84, 337.57, 328.00, 335.165) and "replaced" in note, (bar, note)
    bar, note = dt_runner.verified_bar(lambda s: [good], "ADI", ts, good[1:], wait=0)
    assert note == "", note                                                       # agreeing push: silent
    calls = []
    bar, note = dt_runner.verified_bar(lambda s: calls.append(1) or [other], "ADI", ts, bad, tries=3, wait=0)
    assert bar == bad and note.startswith("UNVERIFIED") and len(calls) == 3, (bar, note, calls)
    def boom(s):
        raise RuntimeError("quote down")
    bar, note = dt_runner.verified_bar(boom, "ADI", ts, bad, tries=2, wait=0)
    assert bar == bad and "quote down" in note


def test_scanner_requotes_dropped_symbols():
    """2026-11-23: Longbridge's quote reply silently left out 267 symbols (AMD, TSM, DELL ...).
    The scanner must ask again for the missing ones, and survive a failed call."""
    import dt_scanner
    from types import SimpleNamespace as NS

    class Ctx:
        def __init__(self, drop_first, fail_first=False):
            self.calls, self.drop, self.fail = [], set(drop_first), fail_first
        def quote(self, syms):
            self.calls.append(list(syms))
            if self.fail and len(self.calls) == 1:
                raise RuntimeError("timeout")
            keep = [s for s in syms if not (len(self.calls) == 1 and s in self.drop)]
            return [NS(symbol=s) for s in keep]

    chunk = ["AAPL.US", "AMD.US", "TSM.US", "NVDA.US"]
    ctx = Ctx({"AMD.US", "TSM.US"})
    got, left = dt_scanner._quote_batch(ctx, chunk, wait=0, log=lambda m: None)
    assert sorted(q.symbol for q in got) == sorted(chunk) and left == [], (got, left)
    assert ctx.calls[1] == ["AMD.US", "TSM.US"]                      # only the dropped ones asked again
    ctx = Ctx(set(), fail_first=True)
    got, left = dt_scanner._quote_batch(ctx, chunk, wait=0, log=lambda m: None)
    assert len(got) == 4 and left == []
    class Never(Ctx):
        def quote(self, syms):
            self.calls.append(list(syms))
            return [NS(symbol=s) for s in syms if s != "BRK.B"]
    ctx = Never(set())
    got, left = dt_scanner._quote_batch(ctx, chunk + ["BRK.B"], retries=2, wait=0, log=lambda m: None)
    assert left == ["BRK.B"] and len(ctx.calls) == 3 and len(got) == 4


def test_runner_error_guard_keeps_going():
    """2026-11-25 review: any exception in the live loop used to end the runner (and the local stops)."""
    import dt_runner
    sent, t = [], [0.0]
    g = dt_runner.ErrorGuard("lbpaper", sent.append, clock=lambda: t[0])
    def boom():
        raise ConnectionError("order_detail timed out")
    assert g.run("order check", boom) is None and len(sent) == 1 and "still running" in sent[0]
    g.run("order check", boom)                                        # same kind within 5 minutes: no new alert
    assert len(sent) == 1 and g.count["order"] == 2
    t[0] = dt_runner.ERROR_ALERT_EVERY_S + 1
    g.run("order check", boom)
    assert len(sent) == 2 and "3x" in sent[1]
    assert g.run("clock", lambda a: a + 1, 1) == 2                     # normal calls pass through
    act = store.read_activity()
    assert (act["kind"] == "error").sum() >= 3


def test_pacer_spaces_calls():
    import dt_runner
    now, slept = [0.0], []
    p = dt_runner.Pacer(0.12, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    p.wait(); p.wait()
    now[0] += 0.5
    p.wait()
    assert len(slept) == 1 and abs(slept[0] - 0.12) < 1e-9


def test_early_close_days():
    """Half days (13:00 close): no new entries from 11:45, flat at 12:45 - in the setups, the paper
    simulator and the order manager. Normal days unchanged."""
    from datetime import date, time as t_
    assert bars.session_times(date(2026, 11, 25)) == (t_(12, 0), t_(15, 0), t_(16, 0))
    assert bars.session_times(date(2026, 11, 27)) == (t_(11, 45), t_(12, 45), t_(13, 0))
    half = lambda hhmm: pd.Timestamp(f"2026-11-27 {hhmm}", tz=cfg.TZ).tz_convert("UTC")
    from dt_strategies import _make
    df = pd.DataFrame({"open": [10.0] * 2}, index=[half("11:44"), half("11:45")])
    p = {"entry_valid_bars": 30}
    s = _make("TST", "test", "long", df, 0, 1, 10.0, 9.5, 2.0, p, "")         # ready 11:45 -> too late
    assert s is None
    df2 = pd.DataFrame({"open": [10.0]}, index=[half("11:30")])
    s = _make("TST", "test", "long", df2, 0, 1, 10.0, 9.5, 2.0, p, "")
    assert s is not None and s.valid_until <= half("11:45")
    sim = Sim()
    sim.add_setup(Setup("TST", "test", "long", half("10:00"), half("10:01"), 10.0, 9.5, 11.0, half("10:30"), ""))
    sim.on_bar("TST", half("10:01"), 10.0, 10.05, 9.99, 10.02)
    assert "TST" in sim.positions
    sim.on_bar("TST", half("12:45"), 10.3, 10.3, 10.3, 10.3)
    assert not sim.positions and sim.trades[-1]["exit_reason"].startswith("flatten")
    from dt_executor import Executor
    fb = FakeBroker()
    ex = Executor(fb)
    ex.add_setup(Setup("TST", "test", "long", half("11:40"), half("11:41"), 10.0, 9.5, 11.0, half("12:30"), ""))
    ex.on_price("TST", 10.01, half("11:50"))
    assert not fb.orders                                              # after the half-day cutoff


class SlowCancelBroker(FakeBroker):
    """cancel() is only a request: the order reports PendingCancel first, then its final state."""
    def cancel(self, oid):
        o = self.orders[oid]
        if o["status"] not in ("Filled", "Canceled", "PartialWithdrawal"):
            o["status"], o["_after"] = "PendingCancel", o.get("_after", "Canceled")

    def order_state(self, oid):
        o = self.orders[oid]
        st = {"status": o["status"], "filled": o["filled"], "avg_price": o["avg_price"]}
        if o["status"] == "PendingCancel":                            # next look: final
            if o["_after"] == "Filled":
                o.update(status="Filled", filled=o["intent"].shares, avg_price=o["intent"].price)
            else:
                o["status"] = o["_after"]
        return st


def test_executor_target_fills_during_cancel():
    """Stop hit while the target fills at the same moment: must NOT send a second closing order."""
    from dt_executor import Executor
    fb = SlowCancelBroker()
    ex = Executor(fb, sleep=lambda s: None)
    ex.add_setup(setup(ready=1, valid=60))
    ex.on_price("TST", 10.01, minute(5))
    fb.fill("o1", 10.02)
    ex.poll(minute(5))
    fb.orders["o2"]["_after"] = "Filled"                              # the target fills while we cancel it
    ex.on_price("TST", 9.49, minute(6))
    assert not ex.positions and len(fb.orders) == 2, fb.orders       # closed as target, no exit order
    assert store.read_trades()["exit_reason"].iloc[-1] == "target"


def test_executor_partial_exit_fills_are_counted():
    """An exit that fills 40 of 100 shares before the retry: the 40 keep their P&L in the trade."""
    from dt_executor import Executor
    fb = FakeBroker()
    clock = [1000.0]
    ex = Executor(fb, clock=lambda: clock[0], sleep=lambda s: None)
    ex.add_setup(setup(ready=1, valid=60))
    ex.on_price("TST", 10.01, minute(5))
    fb.fill("o1", 10.05)
    ex.poll(minute(5))
    n = ex.positions["TST"]["shares"]
    ex.on_price("TST", 9.49, minute(6))                               # stop -> exit order o3
    o3 = fb.orders["o3"]
    o3.update(filled=40, avg_price=9.45)                              # partly filled, still working
    clock[0] += 11                                                    # retry time
    ex.poll(minute(6))
    assert ex.positions["TST"]["shares"] == n - 40 and ex.positions["TST"]["closed_shares"] == 40
    fb.fill("o4", 9.40)                                               # the rest at 9.40
    ex.poll(minute(6))
    t = store.read_trades().iloc[-1]
    exp_gross = (9.45 - 10.05) * 40 + (9.40 - 10.05) * (n - 40)
    exp_costs = cfg.commission(n) + cfg.commission(40) + cfg.commission(n - 40)
    assert int(t["shares"]) == n and abs(t["pnl"] - round(exp_gross - exp_costs, 2)) < 0.011, (t["pnl"], exp_gross - exp_costs)


def test_power_ranger_skips_missing_opening_minute():
    """A thin stock with no trade in one of the first 5 minutes: no Power Ranger setup (the range would
    otherwise reach past 09:34). Same bars with every minute present still find the setup."""
    from dt_strategies import power_ranger
    ctx = bars.DayContext("TST", DAY.date(), 9.0, 50_000, 10.0, 1.0, None)          # +11% gap, ATR 1.0
    rows = [(10.0, 10.2, 9.9, 10.1, 1000), (10.1, 10.25, 10.0, 10.2, 1000), (10.2, 10.3, 10.1, 10.25, 1000),
            (10.25, 10.3, 10.15, 10.28, 1000), (10.28, 10.32, 10.2, 10.3, 1000)]
    full = bars.add_indicators(frame(rows))
    assert power_ranger("TST", full, 4, ctx, {}), "control: complete opening range should give a setup"
    gappy = frame(rows + [(10.3, 10.35, 10.25, 10.33, 1000)]).drop(index=minute(2))   # 09:32 missing
    gappy = bars.add_indicators(gappy)
    assert power_ranger("TST", gappy, 4, ctx, {}) == []


def test_recorder_refreshes_stale_daily_bars():
    """2026-11-25: JAGX's saved daily bars predated a 1:15 reverse split (recorded gap +1,657%, real +11%).
    When the saved close disagrees with Longbridge's previous close, the bars must be re-downloaded."""
    import dt_record as rec
    from datetime import date
    days = pd.date_range("2026-10-01", "2026-11-23", freq="B").date
    stale = pd.DataFrame({"open": 0.40, "high": 0.42, "low": 0.38, "close": 0.39, "volume": 1e6},
                         index=[d for d in days if d <= date(2026, 11, 16)])
    fresh = pd.DataFrame({"open": 6.0, "high": 6.3, "low": 5.7, "close": 6.118, "volume": 1e6}, index=list(days))
    files, calls = {"JAGX": stale}, []
    def fake_fetch(sym, start, end, force=False):
        calls.append(force)
        if force:
            files[sym] = fresh
        return len(files[sym])
    saved = (rec.gap.load_daily, rec.gap.fetch_daily)
    rec.gap.load_daily, rec.gap.fetch_daily = (lambda s: files.get(s)), fake_fetch
    try:
        row = rec.gapper_row("JAGX", date(2026, 11, 24), 6.79, 6.118, "long")
        assert calls == [True] and row is not None and abs(row["gap_pct"] - 10.98) < 0.01, (calls, row)
        files["JAGX"] = stale
        calls.clear()
        rec.gap.fetch_daily = lambda s, a, b, force=False: calls.append(force) or 0      # download keeps failing
        assert rec.gapper_row("JAGX", date(2026, 11, 24), 6.79, 6.118, "long") is None   # skipped, not a fake gap
    finally:
        rec.gap.load_daily, rec.gap.fetch_daily = saved


if __name__ == "__main__":
    tests =[(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS {n}")
        except Exception:
            failed += 1
            print(f"FAIL {n}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
