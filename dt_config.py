"""DT_Iris (Day Trading Iris) - every setting in one place.

Edit values here, not inside the other files. The backtester fingerprints this
file, so a change that affects results shows up as a new config hash.

Times are New York (America/New_York). Prices are USD. "R" is the distance from
entry to stop; every target and result is measured in R.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Research data set. Default "" = Longbridge (data/, results/). DT_DATA=alpaca = the 5-year Alpaca
# history (data_alpaca/, results_alpaca/, its own holdout). The two never mix. Live trading ignores this.
DATA_SOURCE = os.environ.get("DT_DATA", "").strip().lower()
_SUFFIX = f"_{DATA_SOURCE}" if DATA_SOURCE else ""
DATA_DIR = ROOT / f"data{_SUFFIX}"
BARS_DIR = DATA_DIR / "bars"                 # one parquet per symbol per day (1-minute, all sessions)
HOLDOUT_DIR = DATA_DIR / "holdout_LOCKED"    # never read by research code
WATCHLIST_DIR = ROOT / "data" / "watchlists"  # daily scanner output (always the live folder)
RESULTS_DIR = ROOT / f"results{_SUFFIX}"
# Alpaca volume is consolidated (all US exchanges); Longbridge's is Nasdaq-only, about 57% of it.
VOLUME_SCALE = 1.0 if DATA_SOURCE == "alpaca" else 0.57
STORE_DIR = ROOT / "store"                   # runner state, trades, activity, kill switch
ENV_FILE = ROOT / ".env"
TZ = "America/New_York"

# ------------------------------------------------------------------ session clock
SESSION_OPEN = "09:30"
SESSION_CLOSE = "16:00"
ENTRY_CUTOFF = "12:00"       # no NEW entries from this time (Iris rule)
FLATTEN_TIME = "15:00"       # every intraday position closed at this time (Iris rule)
PREMARKET_START = "04:00"
# Early-close sessions (NYSE half days, 13:00 close). On these days the runner, the paper simulator and
# the setups use: no new entries EARLY_CLOSE_CUTOFF_MIN before the close, flat EARLY_CLOSE_FLATTEN_MIN
# before it (never later than the normal 12:00 / 15:00). Add next year's dates when NYSE publishes them.
EARLY_CLOSE_DAYS = {"2026-11-27": "13:00", "2026-12-24": "13:00"}
EARLY_CLOSE_CUTOFF_MIN = 75      # 13:00 close -> no new entries from 11:45
EARLY_CLOSE_FLATTEN_MIN = 15     # 13:00 close -> everything flat at 12:45

# ------------------------------------------------------------------ watchlist filters (from the course notes)
LONG_MIN_PRICE = 5.0         # long watch: price at least $5 (raised from the course $1 on 2026-11-20: keeps out $1-2 small caps)
SHORT_MIN_PRICE = 25.0       # short watch: price above $25
MIN_GAP_PCT = 4.0            # |premarket change %| to make the watchlist
MIN_AVG_DAILY_VOLUME = 500_000
PREMARKET_VOL_MIN_PRICE_GE_5 = 5_000
PREMARKET_VOL_MIN_PRICE_LT_5 = 1_000_000
MAX_WATCHLIST = 20           # per side, ranked by premarket change % x premarket volume
SCAN_TIME = "09:00"          # scheduled scan: the watchlist is built and locked at this New York time
SCAN_LATEST = "09:25"        # a scheduled scan that starts later than this does nothing (too close to the open)
EXCLUDE_SYMBOLS: list[str] = ["AKO", "AULT", "BF", "BRK", "LGF", "MOG"]   # never quoted (share classes stored without
                                                                          # their suffix); add names you never want
BENCHMARKS = ["SPY", "QQQ"]

# ------------------------------------------------------------------ relative strength / weakness (added 2026-11-23)
# A gap only means something compared with the market: +3% on a day SPY is -1% is strong, +3% on a
# day SPY is +2% is mostly the market. relative gap = stock gap - beta x SPY gap (beta = how much the
# stock usually moves with SPY). The sector ETF is picked per stock from the data: the ETF its daily
# returns correlated with best over BETA_DAYS (Longbridge has no sector field).
GAP_BASIS = "relative"       # "relative": the gap list uses the relative gap and its sign gives the side;
                             # "raw": the plain gap, as in every backtest before 2026-11-23
RS_BENCHMARK = "SPY"
BETA_DAYS = 60               # daily returns used for beta and the sector match
BETA_MIN, BETA_MAX = 0.3, 3.0   # beta is clamped to this range (a few wild days must not make it 8)
SECTOR_ETFS = ["XLK", "SMH", "XLF", "KRE", "XLE", "XLV", "XBI", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "ITB"]
SECTOR_MIN_CORR = 0.5        # below this correlation the stock gets no sector ETF

# ------------------------------------------------------------------ stocks in play (Cupid, Tug-Of-War)
# Cupid and Tug-Of-War do not need a gap: they watch the stocks trading unusually heavily before the
# open. pm_rvol = premarket volume / 20-day average daily volume (both from Longbridge, so both
# Nasdaq-only and comparable). The side is the sign of the relative gap (RS -> long, RW -> short).
IN_PLAY_MIN_PRICE = 5.0
IN_PLAY_MIN_ADV = 1_000_000          # liquidity: 20-day average daily volume
IN_PLAY_MIN_PM_VOLUME = 50_000       # shares traded pre-market
IN_PLAY_MIN_PM_RVOL = 0.05           # pre-market volume at least 5% of a normal full day
IN_PLAY_MAX = 20                     # both sides together, ranked by pm_rvol
# which list feeds which strategy ("gap" = the gap watchlist, "in_play" = the relative-volume list)
LIST_STRATEGIES = {"gap": ["power_ranger", "roller_coaster", "orb_retest_a", "orb_retest_b"],
                   "in_play": ["cupid", "tug_of_war"]}

# ------------------------------------------------------------------ indicators
EMA_LEN = 9                  # 9 EMA on 1, 2 and 5 minute charts
ATR_LEN = 14                 # intraday ATR on the setup timeframe
DAILY_ATR_LEN = 14           # daily ATR for "room" and stop-size sanity
SETUP_TIMEFRAME_MIN = 2      # Cupid / Tug-Of-War / Roller Coaster run on 2-minute bars
VOLUME_AVG_LEN = 20          # bars, for "volume spike" multiples

# ------------------------------------------------------------------ strategies
# Each block is the machine-readable version of docs/STRATEGIES.md. Every
# number here is a first draft taken from the course notes and the June 2026
# review; treat them as hypotheses to test, not as proven settings.
STRATEGIES: dict[str, dict] = {
    "power_ranger": {            # Opening Range Breakout (Power Ranger)
        "enabled": True,
        "timeframe_min": 1,
        "range_minutes": 5,          # opening range = first 5 minutes
        "min_gap_pct": 4.0,          # needs a quality gap in the trade direction
        "require_failed_counter_bar": False,  # True = the "perfect" version only (early counter bar that fails)
        "stop_mode": "range",        # "range" = other side of the opening range, "vwap" = VWAP
        "max_range_atr": 1.5,        # skip if the opening range is wider than this x daily ATR... (too late / too wide)
        "min_range_atr": 0.05,       # ...or narrower than this (stop inside the noise)
        "target_r": 2.0,
        "entry_valid_bars": 30,      # stop order cancelled if not triggered within 30 minutes
    },
    "cupid": {                   # EMA / VWAP pullback in a trend
        "enabled": True,
        "timeframe_min": 2,
        "min_leg_atr": 2.0,          # the move before the pullback must be at least 2 x intraday ATR
        "pullback_min": 0.20,        # retracement of that move: 20%..
        "pullback_max": 0.50,        # ..to 50% (review: >50% was a false signal)
        "min_pullback_bars": 2,      # sequential pullback, at least 2 bars
        "max_pullback_bars": 8,
        "max_overlap": 0.50,         # smooth: each pullback bar overlaps the previous by < 50%
        "ref_tolerance_atr": 0.30,   # pullback low within 0.3 ATR of the 9 EMA or VWAP
        "min_trigger_body_atr": 0.15,  # review: very small weak bars should not trigger
        "trigger_volume_mult": 1.0,  # trigger bar volume >= 1.0 x average
        "big_volume_mult": 2.0,      # >= 2x average -> use big_volume_target_r
        "target_r": 1.5,
        "big_volume_target_r": 2.0,
        "entry_valid_bars": 2,
    },
    "tug_of_war": {              # flat chop around the 9 EMA, then a break
        "enabled": True,
        "timeframe_min": 2,
        "min_bars": 6,               # at least 12 minutes of chop
        "max_bars": 20,
        "max_range_atr": 1.2,        # the chop box is at most 1.2 x intraday ATR tall
        "min_ema_crosses": 2,        # price crossed the 9 EMA at least twice inside the box
        "max_ema_slope_atr": 0.05,   # EMA nearly flat (per bar, in ATR)
        "target_r": 2.0,
        "entry_valid_bars": 2,
    },
    "roller_coaster": {          # waterfall reversal (counter-trend)
        "enabled": True,
        "timeframe_min": 2,
        "lookback_bars": 8,
        "min_wide_bars": 3,          # multiple massive wide-range bars
        "wide_bar_mult": 1.8,        # "wide" = range >= 1.8 x average range
        "min_move_atr": 4.0,         # extended: the waterfall covers >= 4 x ATR
        "min_vwap_dist_atr": 2.0,    # and is >= 2 ATR away from VWAP
        "max_reversal_body": 0.35,   # narrow body (<= 35% of the bar range)...
        "min_tail": 0.50,            # ...or a long tail (>= 50% of the range)
        "max_retrace_target": 0.50,  # target stays inside the 50% retracement
        "max_entries": 2,            # "usually whippy and requires a 2nd entry"
        "target_r": 2.0,
        "entry_valid_bars": 2,
    },
}

# ORB Desk v2 retest models (ported from ..\Collin\ORB_Desk_v2). Research only: disabled, so the
# runner and default backtests are unchanged. A = enter after the retest close, B = after the
# 2-bar swing confirmation on the same wick. Numbers are the ORB Desk defaults, fixed BEFORE testing.
_ORB_DESK = {
    "enabled": False,
    "timeframe_min": 1,
    "range_minutes": 15,          # 09:30-09:45 opening range
    "retest_ticks": 5,            # retest low within 5 ticks of the range high
    "retest_window_min": 10,      # retest (and B's confirmation) within 10 minutes of the breakout close
    "last_decision": "10:28",     # last decision bar closes 10:28 ET
    "stop_buffer_ticks": 2,       # stop 2 ticks beyond the retest wick
    "entry_cap_ticks": 1,         # entry cap = decision close + 1 tick
    "max_chase_range": 0.25,      # cap no more than 25% of the range width beyond the range
    "est_spread": 0.02,           # assumed spread...
    "max_spread_risk": 0.10,      # ...must be <= 10% of the price risk
    "min_open_price": 10.0,
    "min_avg_daily_volume": round(2_000_000 * VOLUME_SCALE),   # ORB Desk 2M consolidated (x0.57 on Longbridge data)
    "target_r": 2.0,
    "entry_valid_bars": 1,        # the entry order lives for the next minute only
}
STRATEGIES["orb_retest_a"] = {**_ORB_DESK, "entry": "A"}
STRATEGIES["orb_retest_b"] = {**_ORB_DESK, "entry": "B"}

# ------------------------------------------------------------------ risk and management
ACCOUNT_SIZE = 10_000.0      # used for sizing in backtests and paper
RISK_PER_TRADE = 0.01        # R = 1% of account (course notes)
MAX_TRADES_PER_DAY = 3       # "1-3 trades per day"
MAX_OPEN_POSITIONS = 2
MAX_DAILY_LOSS_R = 3.0       # stop opening trades after -3R in a day
MAX_POSITION_VALUE = 0.25    # one position at most 25% of the account
BREAKEVEN_AT_R = 1.0         # move stop to entry at +1R (set None to disable)
TICK = 0.01

# ------------------------------------------------------------------ fills and costs (backtest + paper)
ENTRY_LIMIT_OFFSET_R = 0.10  # stop-limit: limit = trigger + 0.10 R; no fill if the bar opens beyond it
SLIPPAGE_TICKS = 1           # added against you on every stop / market fill
COMMISSION_PER_SHARE = 0.005  # set to your Longbridge US plan
MIN_COMMISSION = 1.0
PLATFORM_FEE_PER_ORDER = 0.0
SAME_BAR_RULE = "stop_first"  # if stop and target are both inside one 1-minute bar, assume the stop
NOISE_CONTROLS = 99          # random-entry runs for the noise check
CONTROL_SEED = 20261117

# ------------------------------------------------------------------ research split
HOLDOUT_TRADING_DAYS = 40    # the most recent 40 sessions are locked away (fixed on first run)

# ------------------------------------------------------------------ broker (Longbridge) and live limits
# Modes: "paper"  = simulated fills inside DT_Iris, no broker account needed
#        "lbpaper"= Longbridge paper-trading account (keys LONGPORT_PAPER_*)
#        "live"   = real money (keys LONGPORT_LIVE_* AND DT_LIVE_ENABLED=yes)
ALLOW_SHORT_LIVE = False     # live shorts stay off until long-only has run cleanly
LIVE_MAX_POSITION_VALUE = 2_000.0   # USD per position
LIVE_MAX_RISK_PER_TRADE = 50.0      # USD at risk per trade, hard cap
LIVE_MAX_DAILY_LOSS = 150.0         # USD; runner stops opening trades after this
LIVE_MIN_PAPER_DAYS = 10            # sessions on paper/lbpaper before live is allowed
LOCAL_STOP_BACKUP = True            # runner also watches price and closes at market if the broker stop did not fire

# ------------------------------------------------------------------ alerts
TELEGRAM_ENABLED = True      # needs DT_TELEGRAM_BOT_TOKEN and DT_TELEGRAM_CHAT_ID in .env


def commission(shares: float) -> float:
    if shares <= 0:
        return 0.0
    return max(MIN_COMMISSION, abs(shares) * COMMISSION_PER_SHARE) + PLATFORM_FEE_PER_ORDER
