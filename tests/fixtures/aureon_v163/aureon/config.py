"""All tunables. Trading-rule params are FROZEN per rules_version; change them only by adding a new version."""
import os
from dotenv import load_dotenv
load_dotenv()

STORE_MODE = os.getenv("STORE_MODE", "db")                     # db = Linux services (direct Postgres); api = Windows MT5 box
DATABASE_URL = os.getenv("DATABASE_URL")                       # required only in db mode (checked lazily in db.py)
if STORE_MODE == "db" and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required when STORE_MODE=db")
if STORE_MODE == "api" and not (os.getenv("ASSEMBLER_API_URL") and (os.getenv("ASSEMBLER_API_SVC_TOKEN") or os.getenv("ASSEMBLER_API_GUARD_TOKEN") or os.getenv("ASSEMBLER_API_COLLECTOR_TOKEN"))):
    raise RuntimeError("ASSEMBLER_API_URL and ASSEMBLER_API_SVC_TOKEN are required when STORE_MODE=api")

SYMBOL = os.getenv("SYMBOL", "XAUUSD")
# Timestamp policy — NO default. Decide once per broker with scripts/diagnose_timestamps.py, then set one of:
#   MT5_TIME_POLICY=utc                 MT5 epochs are true UTC
#   MT5_TIME_POLICY=server_offset:+2    MT5 epochs are server time stamped as UTC; subtract 2 h (DST: +3 in summer, +2 in winter)
# The collector refuses to start without it, and verifies it against broker data (weekly-open bars) before writing a single bar.
MT5_TIME_POLICY = os.getenv("MT5_TIME_POLICY", "").strip()
#   MT5_TIME_POLICY=server_dst:eu:+2    server follows EU clocks: base +2 in winter, +3 while EU DST is in effect (EET/EEST, the
#                                       most common MT5 broker setting). The offset is computed per bar from the bar's own date.
def _parse_policy(p):
    if p == "utc": return True, 0.0
    if p.startswith("server_offset:"): return False, float(p.split(":", 1)[1])
    if p.startswith("server_dst:"): return False, float(p.split(":")[2]) if p.count(":") >= 2 else 2.0
    return None, None
MT5_TIMESTAMPS_ARE_UTC, BROKER_UTC_OFFSET_HOURS = _parse_policy(MT5_TIME_POLICY)
MT5_SERVER_DST = MT5_TIME_POLICY.split(":")[1] if MT5_TIME_POLICY.startswith("server_dst:") else ""   # "eu" | "us" | ""
if MT5_TIMESTAMPS_ARE_UTC is None:                                             # legacy env still accepted, but only if explicit
    if os.getenv("MT5_TIMESTAMPS_ARE_UTC", "").lower() == "true": MT5_TIMESTAMPS_ARE_UTC, BROKER_UTC_OFFSET_HOURS = True, 0.0
    elif os.getenv("BROKER_UTC_OFFSET_HOURS") not in (None, ""): MT5_TIMESTAMPS_ARE_UTC, BROKER_UTC_OFFSET_HOURS = False, float(os.environ["BROKER_UTC_OFFSET_HOURS"])
    else: MT5_TIMESTAMPS_ARE_UTC, BROKER_UTC_OFFSET_HOURS = False, 0.0; MT5_TIME_POLICY = ""   # unset: collector will refuse to run
    MT5_SERVER_DST = ""
MT5_PAPER = os.getenv("MT5_PAPER", "true").lower() == "true"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or 0)
DISCORD_AUTHORIZED_USER_IDS = {int(x) for x in os.getenv("DISCORD_AUTHORIZED_USER_IDS", "").split(",") if x.strip().isdigit()}
DISCORD_ADMIN_USER_IDS = {int(x) for x in os.getenv("DISCORD_ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()}   # may clear a lock / set baseline (audited)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
LOTS_DEFAULT = float(os.getenv("LOTS_DEFAULT", "0.10"))
MAX_LOSS_PER_DAY_USD = float(os.getenv("MAX_LOSS_PER_DAY_USD", "60"))
WARN_LOSS_PER_DAY_USD = float(os.getenv("WARN_LOSS_PER_DAY_USD", str(0.7 * float(os.getenv("MAX_LOSS_PER_DAY_USD", "60")))))
COUNT_FLOATING_IN_DAILY_LOSS = os.getenv("COUNT_FLOATING_IN_DAILY_LOSS", "true").lower() == "true"   # most funded-account rules count floating; verify with YOUR provider
FLATTEN_ON_LOCK = os.getenv("FLATTEN_ON_LOCK", "true").lower() == "true"            # conservative default; set false if your provider forbids auto-close
NAKED_POSITION_POLICY = os.getenv("NAKED_POSITION_POLICY", "flatten_after")      # alert_only | flatten_after
NAKED_POSITION_GRACE_MIN = int(os.getenv("NAKED_POSITION_GRACE_MIN", "5"))       # minutes a position may stay without a verifiable stop before fail-safe flatten
EMA_WARMUP_BARS = 400                                                            # indicators are not tradeable before this many bars of history
RESTART_CMD = os.getenv("RESTART_CMD", "")   # default alert-only; services self-exit on repeated failure and the supervisor (docker/systemd/NSSM) restarts them

RULES_VERSION = "R3.0"
RULES = {
    "tf": "M30",
    "ema_fast": 8, "ema_slow": 32, "ema_trend": 200, "atr_len": 14,
    "skip_first_bars_of_day": 3,
    "chop_flips": 3, "chop_window": 8,
    "sl_atr": 1.5,
    "lock_ladder": [[15, 5], [20, 15], [30, 25]],
    "exit": "opposite_vwap_cross",
    "flat_bar_of_day": 44,
    "horizon_bars": 48,
    "max_open_trades": 1,
    "no_entry_utc_hours": [21, 22, 23, 0],
    "friday_no_entry_after_utc": "21:30",
    "friday_flat_utc": "21:55",
}
WEEKLY_OPEN_RULE = os.getenv("WEEKLY_OPEN_RULE", "us_dst")            # us_dst | fixed:22 | fixed:23 ... (per broker)
WEEKLY_OPEN_MIN_FRACTION = float(os.getenv("WEEKLY_OPEN_MIN_FRACTION", "0.75"))   # tolerate one odd week (holiday) in four
SESSION_FIRST_BAR_UTC = os.getenv("SESSION_FIRST_BAR_UTC", "01:00")   # first M30 bar open of the broker day (from your CSVs)
SESSION_LAST_BAR_UTC  = os.getenv("SESSION_LAST_BAR_UTC",  "22:30")   # last M30 bar open (H1: 22:00)
OFFSET_TOLERANCE_MIN = 30                                             # collector refuses to run if MT5 server clock disagrees with BROKER_UTC_OFFSET_HOURS by more
# ---- weekend review ----
REVIEW_MAX_ENTRY_DELAY_MIN = int(os.getenv("REVIEW_MAX_ENTRY_DELAY_MIN", "90"))
REVIEW_MAX_PRICE_DEV = float(os.getenv("REVIEW_MAX_PRICE_DEV", "15"))          # $/oz between signal price and actual fill to still match
REVIEW_REPORTS_DIR = os.getenv("REVIEW_REPORTS_DIR", "reports/weekly")
AUREON_MAGIC = int(os.getenv("AUREON_MAGIC", "0") or 0)                          # magic number if you tag manual orders in MT5
HEARTBEAT_SEC = 60
HEARTBEAT_MISS_LIMIT = 3
SELFHEAL_ENABLED = os.getenv("SELFHEAL_ENABLED", "false").lower() == "true"   # off by default; needs docker + aureon-sandbox image
SELFHEAL_MAX_ATTEMPTS = 3
SELFHEAL_PATCHABLE = ("aureon/collector.py", "aureon/detector.py", "aureon/alerter.py", "aureon/review.py", "aureon/indicators.py")
USD_PER_DOLLAR_PER_LOT = 100.0
PRICE_MIN, PRICE_MAX = 500.0, 50000.0        # sanity bounds for XAUUSD bars
