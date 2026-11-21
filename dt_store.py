"""DT_Iris saved state: runner settings, positions, trades, activity, kill switch.

Everything lives in store/ as small JSON and CSV files so it survives restarts
and can be opened in Excel.
"""
from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import dt_config as cfg

STORE = cfg.STORE_DIR
SETTINGS_FILE = STORE / "runner_settings.json"
STATE_FILE = STORE / "runner_state.json"
STATUS_FILE = STORE / "runner_status.json"
KILL_FILE = STORE / "KILL_SWITCH"
TRADES_FILE = STORE / "trades.csv"
ACTIVITY_FILE = STORE / "activity.csv"
SIGNALS_FILE = STORE / "signals.csv"

TRADE_COLS = ["trade_id", "date", "mode", "symbol", "strategy", "side", "entry_time", "entry", "stop", "target",
              "shares", "exit_time", "exit", "exit_reason", "r_multiple", "pnl", "costs", "review", "notes"]
ACTIVITY_COLS = ["time", "mode", "kind", "text", "pnl"]
SIGNAL_COLS = ["time", "date", "mode", "symbol", "strategy", "side", "trigger", "stop", "target", "status", "reason"]

DEFAULT_SETTINGS = {
    "mode": "paper",                       # paper | lbpaper | live
    "strategies": [k for k, v in cfg.STRATEGIES.items() if v["enabled"]],
    "sides": ["long", "short"],
    "symbols": [],                         # empty = today's scanner watchlist
    "created": None,
    "first_paper_day": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)            # atomic: a crash never leaves half a file


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(_read(SETTINGS_FILE, {}))
    if not s.get("created"):
        s["created"] = now_iso()
        _write(SETTINGS_FILE, s)
    return s


def save_settings(**changes) -> dict:
    s = load_settings()
    s.update(changes)
    _write(SETTINGS_FILE, s)
    return s


def load_state() -> dict:
    return _read(STATE_FILE, {"date": None, "positions": {}, "pending": {}, "day_r": 0.0, "day_pnl": 0.0,
                              "trades_today": 0, "entries_by_setup": {}})


def save_state(state: dict) -> None:
    _write(STATE_FILE, state)


def _append(path: Path, cols: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in cols})


def log_activity(mode: str, kind: str, text: str, pnl: float | None = None) -> None:
    _append(ACTIVITY_FILE, ACTIVITY_COLS, {"time": now_iso(), "mode": mode, "kind": kind, "text": text,
                                           "pnl": "" if pnl is None else round(pnl, 2)})


def log_trade(row: dict) -> None:
    row = dict(row)
    row.setdefault("trade_id", new_id())
    _append(TRADES_FILE, TRADE_COLS, row)


def log_signal(row: dict) -> None:
    row = dict(row)
    row.setdefault("time", now_iso())
    _append(SIGNALS_FILE, SIGNAL_COLS, row)


def _csv(path: Path, cols: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols)
    try:
        return pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        return pd.DataFrame(columns=cols)


def read_trades() -> pd.DataFrame:
    return _csv(TRADES_FILE, TRADE_COLS)


def read_activity() -> pd.DataFrame:
    return _csv(ACTIVITY_FILE, ACTIVITY_COLS)


def read_signals() -> pd.DataFrame:
    return _csv(SIGNALS_FILE, SIGNAL_COLS)


def save_trades(df: pd.DataFrame) -> None:
    """Rewrite the trade journal (used by the dashboard's review column)."""
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(TRADES_FILE, index=False)


def runner_status() -> dict:
    return _read(STATUS_FILE, {})


def write_runner_status(data: dict) -> None:
    _write(STATUS_FILE, data)


def kill_switch_on() -> bool:
    return KILL_FILE.exists()


def set_kill_switch(on: bool) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    if on:
        KILL_FILE.write_text(now_iso(), encoding="utf-8")
    elif KILL_FILE.exists():
        KILL_FILE.unlink()


def paper_sessions() -> int:
    """Distinct trading dates on which the runner ran in paper or lbpaper mode."""
    act = read_activity()
    if act.empty:
        return 0
    act = act[(act["mode"].isin(["paper", "lbpaper"])) & (act["kind"] == "session_start")]
    return int(pd.to_datetime(act["time"], utc=True, errors="coerce").dt.tz_convert(cfg.TZ).dt.date.nunique())
