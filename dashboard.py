"""DT_Iris dashboard.

    .venv\\Scripts\\python.exe -m streamlit run dashboard.py      (or double-click run_dashboard.bat)
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import dt_broker as broker
import dt_config as cfg
import dt_data as data
import dt_store as store
import dt_telegram as tg
from dt_scanner import load_watchlist

st.set_page_config(page_title="DT_Iris", page_icon="📈", layout="wide")

# ---- text size: ONE base size; every other size is a fixed multiple of it (rem), so the page stays in proportion
SIZES = {"Small": 17, "Normal": 20, "Large": 22, "Larger": 24}
# sidebar order, top to bottom: title + page list, status badges, then the text-size slider
_nav = st.sidebar.container()
_badge_box = st.sidebar.container()
_size = st.sidebar.select_slider("Text size", list(SIZES), value=st.session_state.get("text_scale", "Normal"),
                                 key="text_scale")
st.markdown(f"""<style>
html {{ font-size: {SIZES[_size]}px !important; }}
/* type scale (x base): body 1, small/captions 0.875, h3 1.15, h2 1.35, h1 1.7, metric 1.8, app title 1.6 */
[data-testid="stMain"] h1 {{ font-size: 1.7rem !important; }}
[data-testid="stMain"] h2 {{ font-size: 1.35rem !important; }}
[data-testid="stMain"] h3 {{ font-size: 1.15rem !important; }}
[data-testid="stMain"] p, [data-testid="stMain"] li, [data-testid="stMain"] label {{ font-size: 1rem !important; }}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {{ font-size: 0.875rem !important; }}
[data-testid="stMain"] [data-testid="stMetricValue"], [data-testid="stMain"] [data-testid="stMetricValue"] * {{
    font-size: 1.8rem !important; line-height: 1.3; }}
[data-testid="stMain"] [data-testid="stMetricLabel"] p {{ font-size: 0.875rem !important; font-weight: 700 !important; }}
section[data-testid="stSidebar"] {{ min-width: {SIZES[_size] * 15}px !important; max-width: {SIZES[_size] * 18}px !important; }}
/* the sidebar uses fixed pixel sizes: make it follow the same base size as the page */
section[data-testid="stSidebar"] p, section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] span, section[data-testid="stSidebar"] div[role="radiogroup"] * {{ font-size: 1rem !important; }}
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] * {{ font-size: 0.875rem !important; }}
section[data-testid="stSidebar"] div[role="radiogroup"] {{ gap: 0.15rem; }}
/* sidebar title: bold, one step above the page headings, close to the top */
section[data-testid="stSidebar"] .dt-title, section[data-testid="stSidebar"] .dt-title * {{
    font-size: 1.6rem !important; font-weight: 800 !important; line-height: 1.2; margin: 0 0 0.5rem 0; }}
[data-testid="stSidebarHeader"] {{ height: auto !important; min-height: 0 !important; padding-top: 0.4rem !important;
    padding-bottom: 0 !important; margin-bottom: 0 !important; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0 !important; }}
section[data-testid="stSidebar"] [data-testid="stSlider"] {{ margin-top: 1rem; }}
/* colours: light blue navigator, light grey page */
section[data-testid="stSidebar"], section[data-testid="stSidebar"] > div {{ background-color: #dcecf8 !important; }}
[data-testid="stApp"], [data-testid="stAppViewContainer"], [data-testid="stMain"],
[data-testid="stHeader"] {{ background-color: #f4f5f7 !important; }}
</style>""", unsafe_allow_html=True)
PY = str(cfg.ROOT / ".venv" / "Scripts" / "python.exe")
STRATS = list(cfg.STRATEGIES)
LIVE_STRATS = [k for k, v in cfg.STRATEGIES.items() if v["enabled"]]     # research-only strategies stay out of the runner
NAMES = {"power_ranger": "Power Ranger (ORB)", "cupid": "Cupid", "tug_of_war": "Tug-Of-War", "roller_coaster": "Roller Coaster",
         "orb_retest_a": "ORB Desk A: retest (research)", "orb_retest_b": "ORB Desk B: confirmed swing (research)"}


def runner_alive() -> tuple[bool, dict]:
    import time
    s = store.runner_status()
    return bool(s.get("pid")) and time.time() - s.get("beat", 0) < 60, s


def ny_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz=cfg.TZ)


def launch(args: list[str]) -> None:
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen([PY, *args], cwd=str(cfg.ROOT), creationflags=flags)


def badges() -> None:
    alive, s = runner_alive()
    mode = store.load_settings()["mode"]
    _badge_box.markdown(f"**Mode:** `{mode}`")
    _badge_box.markdown("🟢 Runner on" if alive else "⚪ Runner off")
    if store.kill_switch_on():
        _badge_box.error("Kill switch ON")
    _badge_box.caption(f"New York {ny_now():%a %H:%M}  ·  entries until {cfg.ENTRY_CUTOFF}, flat by {cfg.FLATTEN_TIME}")


# ------------------------------------------------------------------ pages
def page_home():
    st.header("Today")
    tr, act, sig = store.read_trades(), store.read_activity(), store.read_signals()
    today = str(ny_now().date())
    t_today = tr[tr["date"].astype(str) == today] if len(tr) else tr
    c = st.columns(4)
    pl = t_today["pnl"].sum() if len(t_today) else 0.0
    c[0].metric("Trades", len(t_today))
    c[1].metric("Total R", f"{t_today['r_multiple'].sum():+.2f}" if len(t_today) else "0")
    c[2].metric("P/L", f"{'+' if pl >= 0 else '-'}${abs(pl):,.2f}")
    c[3].metric("Setups",int((sig["date"].astype(str) == today).sum()) if len(sig) else 0)
    st.subheader("Recent activity")
    st.dataframe(act.tail(50).iloc[::-1], use_container_width=True, hide_index=True)
    st.subheader("Getting started")
    steps = [("Copy .env.example to .env and fill in your Longbridge key, secret and tokens", broker.quote_venue() is not None),
             ("Build today's watchlist (Scanner)", not load_watchlist().empty),
             ("Run a backtest", any(cfg.RESULTS_DIR.glob("bt_*"))),
             ("Run one session on paper", store.paper_sessions() > 0),
             (f"{cfg.LIVE_MIN_PAPER_DAYS} paper sessions before live", store.paper_sessions() >= cfg.LIVE_MIN_PAPER_DAYS)]
    for text, done in steps:
        st.markdown(f"{'✅' if done else '⬜'} {text}")


def page_scanner():
    st.header("Pre-market scanner")
    st.caption(f"Long watch: gap ≥ +{cfg.MIN_GAP_PCT}% and price ≥ ${cfg.LONG_MIN_PRICE:g}.  Short watch: gap ≤ -{cfg.MIN_GAP_PCT}% "
               f"and price ≥ ${cfg.SHORT_MIN_PRICE:g}.  Premarket volume ≥ 5,000 (≥ $5) or ≥ 1,000,000 (< $5).")
    import dt_scanner as scanner
    st.markdown(f"**Automatic:** the whole Longbridge stock list (main exchanges, no ETFs/funds/ADRs) is scanned at "
                f"**{cfg.SCAN_TIME} New York** every weekday by the Windows task *DT_Iris pre-market scan*, and the "
                f"watchlist is locked for the day. No symbols to paste.")
    today = ny_now().date()
    info = scanner.scan_info(today)
    if info.get("saved"):
        st.success(f"Today's watchlist: {info['long']} long, {info['short']} short from {info['scanned']:,} stocks "
                   f"({info['source']}, {info['time_ny']} ET{', locked' if info.get('locked') else ''}).")
    else:
        st.info(f"No watchlist for {today} yet. The scheduled scan runs at {cfg.SCAN_TIME} ET.")
    with st.expander("Scan now by hand (testing, or a missed scheduled scan)"):
        if info.get("locked"):
            st.warning("Today's list is already locked by the scheduled scan. Scanning again replaces it.")
        limit = st.text_input("Only these symbols (optional, comma separated; empty = whole market)", "")
        if st.button("Scan now", type="primary"):
            syms = [s.strip().upper() for s in limit.replace("\n", ",").split(",") if s.strip()] or None
            msgs: list[str] = []
            try:
                with st.spinner("Asking Longbridge for premarket quotes of the whole market (about a minute)..."):
                    meta = scanner.run_scan(syms, source="dashboard" if not syms else "dashboard list", log=msgs.append)
                if meta.get("saved"):
                    st.success(f"{meta['long']} long, {meta['short']} short from {meta['scanned']:,} stocks in {meta['seconds']}s")
                else:
                    st.warning(meta.get("reason", "nothing saved"))
            except Exception as exc:
                st.error(f"{type(exc).__name__}: {exc}")
    day = st.date_input("Watchlist date", today)
    p = cfg.WATCHLIST_DIR / f"{day}.csv"
    if p.exists():
        df = pd.read_csv(p)
        st.subheader("Watchlist")
        st.dataframe(df[df.side.isin(["long", "short"])], use_container_width=True, hide_index=True)
        with st.expander("Rejected and why"):
            st.dataframe(df[~df.side.isin(["long", "short"])], use_container_width=True, hide_index=True)
    else:
        st.info("No watchlist for that date yet.")


def page_backtester():
    st.header("Backtester")
    av = data.available()
    if av.empty:
        st.warning("No data yet. Go to Data.")
        return
    syms = sorted(set(av.symbol) - set(cfg.BENCHMARKS))
    c = st.columns(3)
    strategies = c[0].multiselect("Strategies", STRATS, default=LIVE_STRATS, format_func=lambda k: NAMES.get(k, k))
    sides = c[1].multiselect("Sides", ["long", "short"], default=["long", "short"])
    controls = c[2].number_input("Noise check runs", 0, 999, 99, help="random-entry runs; 0 = skip")
    chosen = st.multiselect("Symbols (empty = all)", syms)
    breakeven = st.checkbox(f"Move stop to break-even at +{cfg.BREAKEVEN_AT_R or 1:g}R", value=bool(cfg.BREAKEVEN_AT_R),
                            help="Untick to test without the break-even rule (applies to the noise check too)")
    st.caption(f"{av.date.nunique()} trading days on disk, {len(syms)} symbols. Holdout from: {data.holdout_cut() or 'not locked yet'}")
    if st.button("Run backtest", type="primary"):
        import dt_backtest as bt
        saved_be = cfg.BREAKEVEN_AT_R
        cfg.BREAKEVEN_AT_R = saved_be if breakeven else None
        try:
            with st.spinner("Running..."):
                res = bt.run(chosen or None, strategies, tuple(sides), int(controls))
                res["breakeven"] = breakeven
                path = bt.save(res)
        finally:
            cfg.BREAKEVEN_AT_R = saved_be
        st.session_state["bt"] = res
        st.success(f"Saved to {path}")
    res = st.session_state.get("bt")
    if res:
        show_backtest(res)
    st.divider()
    st.subheader("Saved backtests")
    runs = sorted((p for p in cfg.RESULTS_DIR.glob("bt_*") if (p / "summary.json").exists()), reverse=True)
    if not runs:
        st.caption("None yet.")
        return

    def label(p):
        try:
            meta = json.loads((p / "summary.json").read_text(encoding="utf-8"))
        except Exception:
            return p.name
        names = ", ".join(NAMES.get(k, k) for k in meta.get("strategies", []))
        n = meta.get("stats", {}).get("trades", 0)
        return f"{p.name}  ·  {names}  ·  {n} trades"

    pick = st.selectbox("Open a saved run", runs, format_func=label)
    if pick is not None:
        meta = json.loads((pick / "summary.json").read_text(encoding="utf-8"))
        tf = pick / "trades.csv"
        try:
            meta["trades"] = pd.read_csv(tf) if tf.exists() else pd.DataFrame()
        except pd.errors.EmptyDataError:
            meta["trades"] = pd.DataFrame()
        show_backtest(meta)


def show_backtest(res: dict) -> None:
    s = res["stats"]
    if not s.get("trades"):
        st.info("No trades. The rules found no setups on this data.")
        return
    c = st.columns(6)
    c[0].metric("Trades", s["trades"])
    c[1].metric("Win rate", f"{s['win_rate']:.0%}")
    c[2].metric("Avg R", f"{s['avg_r']:+.2f}")
    c[3].metric("Total R", f"{s['total_r']:+.2f}")
    c[4].metric("Max DD (R)", f"{s['max_dd_r']:.2f}")
    c[5].metric("P/L", f"${s['pnl']:+,.0f}")
    if "controls" in res:
        k = res["controls"]
        msg = (f"**Noise check:** beat {k['beat']} of {k['n']} random-entry runs (p = {k['p_value']:.2f}). "
               + ("Better than random." if k["p_value"] <= 0.05 else
                  "Worse than random entries." if k["p_value"] >= 0.95 else
                  "Not distinguishable from random entries yet."))
        (st.success if k["p_value"] <= 0.05 else st.warning)(msg)
    if s["trades"] < 30:
        st.warning(f"Only {s['trades']} trades: far too few to judge. Collect more days of data.")
    t = res["trades"]
    # static chart: st.line_chart zooms and pans with the mouse wheel/drag, which made the line "follow" the mouse
    import altair as alt
    curve = pd.DataFrame({"trade": range(1, len(t) + 1), "cumulative R": t["r_multiple"].cumsum().values})
    line = alt.Chart(curve).mark_line(point=len(curve) <= 60).encode(
        x=alt.X("trade:Q", title="trade #", scale=alt.Scale(domain=[1, max(2, len(curve))])),
        y=alt.Y("cumulative R:Q", title="cumulative R"),
        tooltip=["trade", alt.Tooltip("cumulative R:Q", format="+.2f")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeDash=[4, 4], color="#888").encode(y="y:Q")
    st.altair_chart(line + zero, use_container_width=True)
    st.subheader("By strategy")
    st.dataframe(pd.DataFrame(res["by_strategy"]).T, use_container_width=True)
    st.subheader("Trades")
    st.dataframe(t, use_container_width=True, hide_index=True)


def page_runner():
    st.header("Runner")
    settings = store.load_settings()
    alive, status = runner_alive()
    c = st.columns(3)
    modes = ["paper", "lbpaper", "live"]
    mode = c[0].radio("Mode", modes, index=modes.index(settings["mode"]),
                      captions=["simulated fills", "Longbridge demo account", "REAL MONEY"])
    strategies = c[1].multiselect("Strategies", LIVE_STRATS, default=[x for x in settings["strategies"] if x in LIVE_STRATS],
                                  format_func=lambda k: NAMES.get(k, k))
    sides = c[2].multiselect("Sides", ["long", "short"], default=settings.get("sides", ["long", "short"]))
    sym_text = st.text_input("Symbols (empty = today's scanner watchlist)", ",".join(settings.get("symbols", [])))
    if mode == "live":
        st.error("Live mode sends real orders. Every gate must be green:")
        for ok, text in live_checks():
            st.markdown(f"{'🟢' if ok else '🔴'} {text}")
        confirm = st.text_input("Type LIVE to allow saving live mode")
    else:
        confirm = ""
    if st.button("Save settings"):
        if mode == "live" and (confirm != "LIVE" or not all(ok for ok, _ in live_checks())):
            st.error("Not saved: type LIVE and turn every gate green first.")
        else:
            store.save_settings(mode=mode, strategies=strategies, sides=sides,
                                symbols=[s.strip().upper() for s in sym_text.split(",") if s.strip()])
            st.success("Saved")
    st.divider()
    c = st.columns(3)
    if alive:
        c[0].success(f"Running (pid {status.get('pid')}, {status.get('mode')}): {', '.join(status.get('symbols', []))}")
        c[0].caption(f"open: {status.get('open_positions')}  ·  last beat {status.get('time')}")
    elif c[0].button("Start runner", type="primary"):
        launch(["dt_runner.py", "--mode", store.load_settings()["mode"]])
        st.info("Runner window opened. It stops by itself at 15:05 ET.")
    if store.kill_switch_on():
        if c[1].button("Turn kill switch OFF"):
            store.set_kill_switch(False)
            st.rerun()
    elif c[1].button("KILL SWITCH", help="No new entries and all waiting setups dropped. Open trades keep stop/target."):
        store.set_kill_switch(True)
        st.rerun()
    st.subheader("Today's setups")
    sig = store.read_signals()
    st.dataframe(sig[sig["date"].astype(str) == str(ny_now().date())].iloc[::-1] if len(sig) else sig,
                 use_container_width=True, hide_index=True)


def live_checks() -> list[tuple[bool, str]]:
    return [(broker.keys_present("live"), "LONGPORT_APP_KEY / APP_SECRET / LIVE_ACCESS_TOKEN in .env"),
            (broker.live_enabled(), "DT_LIVE_ENABLED=yes in .env"),
            (store.paper_sessions() >= cfg.LIVE_MIN_PAPER_DAYS,
             f"{store.paper_sessions()} of {cfg.LIVE_MIN_PAPER_DAYS} paper sessions done"),
            (not store.kill_switch_on(), "kill switch off"),
            (True, f"caps: ${cfg.LIVE_MAX_POSITION_VALUE:,.0f} per position, ${cfg.LIVE_MAX_RISK_PER_TRADE:,.0f} risk per trade, "
                   f"${cfg.LIVE_MAX_DAILY_LOSS:,.0f} daily loss, shorts {'on' if cfg.ALLOW_SHORT_LIVE else 'off'}")]


def page_trades():
    st.header("Trade journal")
    # open positions are not in the journal yet: a trade is written there when it CLOSES
    alive, rs = runner_alive()
    open_now = rs.get("open_positions") or [] if alive else []
    act = store.read_activity()
    today = act[pd.to_datetime(act["time"], utc=True) >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=18)] if len(act) else act
    moves = today[today["kind"].isin(["entry", "exit", "stop", "target", "flatten"])] if len(today) else today
    if open_now:
        st.subheader("Open now")
        st.write(", ".join(open_now) + f"  ({rs.get('mode')} mode) - moves to the journal below when it closes.")
    if len(moves):
        st.caption("Today's entries and exits")
        st.dataframe(moves[["time", "mode", "kind", "text"]].iloc[::-1], use_container_width=True, hide_index=True)
    tr = store.read_trades()
    if tr.empty:
        st.info("No closed trades yet." + (" Open positions are listed above." if open_now else ""))
        return
    mode = st.multiselect("Mode", sorted(tr["mode"].dropna().unique()), default=sorted(tr["mode"].dropna().unique()))
    view = tr[tr["mode"].isin(mode)].copy()
    c = st.columns(4)
    c[0].metric("Trades", len(view))
    c[1].metric("Win rate", f"{(view.r_multiple > 0).mean():.0%}" if len(view) else "-")
    c[2].metric("Total R", f"{view.r_multiple.sum():+.2f}")
    c[3].metric("P/L", f"${view.pnl.sum():+,.2f}")
    st.caption("Use the review column to mark each trade valid / false signal / broke plan - the same review Iris used.")
    view["review"] = view["review"].astype("string").fillna("")
    edited = st.data_editor(view, use_container_width=True, hide_index=True,
                            column_config={"review": st.column_config.SelectboxColumn(
                                options=["", "valid", "false signal", "broke plan", "maybe"])},
                            disabled=[c for c in view.columns if c not in ("review", "notes")])
    if st.button("Save reviews"):
        tr.loc[edited.index, ["review", "notes"]] = edited[["review", "notes"]]
        store.save_trades(tr)
        st.success("Saved")


def page_data():
    st.header("Data")
    av = data.available()
    st.caption(f"{len(av)} symbol-days, {av.date.nunique() if len(av) else 0} trading days, holdout from "
               f"{data.holdout_cut() or 'not locked yet (needs ' + str(cfg.HOLDOUT_TRADING_DAYS * 3) + ' days)'}")
    if len(av):
        st.dataframe(av.groupby("date").symbol.count().rename("symbols").iloc[::-1], use_container_width=True)
    st.subheader("Historical gappers (the research sample)")
    from dt_gappers import load_gappers
    g = load_gappers()
    if len(g):
        st.caption(f"{len(g)} gap days on {g['date'].nunique()} sessions, {g['date'].min()} to {g['date'].max()} "
                   f"({(g.side == 'long').sum()} long, {(g.side == 'short').sum()} short)")
        # static chart (st.bar_chart zooms with the mouse wheel, same fix as the backtest chart)
        import altair as alt
        per_day = g.groupby(["date", "side"]).size().rename("gappers").reset_index()
        per_day["date"] = pd.to_datetime(per_day["date"])
        st.altair_chart(alt.Chart(per_day).mark_bar().encode(
            x=alt.X("date:T", title=None), y=alt.Y("gappers:Q", title="gappers per day", stack=True),
            color=alt.Color("side:N", scale=alt.Scale(domain=["long", "short"], range=["#2e9e5b", "#d6453d"])),
            tooltip=[alt.Tooltip("date:T", format="%Y-%m-%d"), "side", "gappers"]), use_container_width=True)
    months = st.number_input("Months back", 1, 24, 12)
    if st.button("Find gappers and download their minutes (opens its own window)"):
        launch(["dt_gappers.py", "--months", str(int(months))])
        st.info("Running in a separate window. Cached steps are skipped, so a re-run only fetches what is new.")
    st.subheader("Download 1-minute bars from Longbridge")
    c = st.columns([3, 1])
    syms = c[0].text_input("Symbols", "SPY,QQQ")
    days = c[1].number_input("Calendar days back", 1, 365, 30)
    if st.button("Download"):
        try:
            with st.spinner("Downloading..."):
                log = data.fetch_longbridge([s.strip().upper() for s in syms.split(",") if s.strip()],
                                            date.today() - timedelta(days=int(days)), date.today())
            st.code("\n".join(log[-200:]) or "nothing new")
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    if st.button("Import old Iris IBKR CSVs"):
        files = sorted((cfg.ROOT.parent / "Iris" / "data" / "market").glob("*_1min.csv"))
        st.write([f"{f.name}: {len(data.import_iris_csv(f))} symbol-days" for f in files])


def page_settings():
    st.header("Settings")
    st.markdown("Every rule and limit lives in `dt_config.py`. Credentials live in `.env` (see `.env.example`).")
    st.subheader("Longbridge connection")
    st.caption("One App Key + App Secret for both accounts; the demo and live accounts each have their own Access Token.")
    c = st.columns(2)
    for col, mode in zip(c, ["lbpaper", "live"]):
        col.markdown(f"**{mode}** credentials: {'present' if broker.keys_present(mode) else 'missing'}")
        if col.button(f"Test {mode} connection", key=f"t_{mode}"):
            col.json(broker.connection_report(mode))
    st.subheader("Broker-side stop check (demo account only)")
    st.caption("Buys 1 SPY share on the demo account, places a LIT sell 3% below and checks it waits. "
               "Run during US market hours. Until this passes, the runner watches stops itself.")
    if st.button("Run stop-order probe on lbpaper"):
        try:
            st.json(broker.probe_stop_order())
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")
    st.subheader("Telegram")
    st.markdown(f"Configured: **{tg.configured()}**")
    if st.button("Send test message"):
        st.write("sent" if tg.send("DT_Iris test message") else "failed - check DT_TELEGRAM_* in .env")
    st.subheader("Current rules")
    st.json({"filters": {"long_min_price": cfg.LONG_MIN_PRICE, "short_min_price": cfg.SHORT_MIN_PRICE,
                         "min_gap_pct": cfg.MIN_GAP_PCT, "min_avg_daily_volume": cfg.MIN_AVG_DAILY_VOLUME},
             "time": {"entry_cutoff": cfg.ENTRY_CUTOFF, "flatten": cfg.FLATTEN_TIME},
             "risk": {"account": cfg.ACCOUNT_SIZE, "risk_per_trade": cfg.RISK_PER_TRADE,
                      "max_trades_per_day": cfg.MAX_TRADES_PER_DAY, "max_daily_loss_r": cfg.MAX_DAILY_LOSS_R},
             "strategies": cfg.STRATEGIES})


PAGES = {"Home": page_home, "Scanner": page_scanner, "Backtester": page_backtester, "Runner": page_runner,
         "Trades": page_trades, "Data": page_data, "Settings": page_settings}
_nav.markdown('<div class="dt-title">DT_Iris</div>', unsafe_allow_html=True)
choice = _nav.radio("DT_Iris", list(PAGES), label_visibility="collapsed")
badges()
PAGES[choice]()
