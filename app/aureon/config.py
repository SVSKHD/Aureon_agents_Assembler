"""Aureon V1.6.3 tunables, server side (docs/34-aureon-v163.md).

Same names and defaults as V1.6.3 aureon/config.py, so the copied strategy modules
(indicators, matcher, positions) read exactly what they read there. Values come from
`AUREON_*` environment variables on the Assembler (the bare V1.6.3 names such as SYMBOL are
too generic for this process). Nothing here is a credential.

The trading rule R3.0 (EMA 8/32/200, VWAP, M30, lock ladder, exits) is FROZEN: it is copied
verbatim and changes only by adding a new rules version.
"""

from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.getenv(f"AUREON_{name}", default)


SYMBOL = _env("SYMBOL", "XAUUSD")
SESSION_FIRST_BAR_UTC = _env("SESSION_FIRST_BAR_UTC", "01:00")
SESSION_LAST_BAR_UTC = _env("SESSION_LAST_BAR_UTC", "22:30")
LOTS_DEFAULT = float(_env("LOTS_DEFAULT", "0.10"))
REVIEW_MAX_ENTRY_DELAY_MIN = int(_env("REVIEW_MAX_ENTRY_DELAY_MIN", "90"))
REVIEW_MAX_PRICE_DEV = float(_env("REVIEW_MAX_PRICE_DEV", "15"))
AUREON_MAGIC = int(_env("MAGIC", "0") or 0)
# Seconds to wait for the collector to answer a review's backfill / history job (V1.6.3: 600).
REVIEW_JOB_WAIT_SECONDS = int(_env("REVIEW_JOB_WAIT_SECONDS", "600"))

EMA_WARMUP_BARS = 400
HEARTBEAT_SEC = 60
HEARTBEAT_MISS_LIMIT = 3
USD_PER_DOLLAR_PER_LOT = 100.0
PRICE_MIN, PRICE_MAX = 500.0, 50000.0

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
}  # fmt: skip
