"""Build aureon.mt5.push/v1 documents from the v1 fixture export (shape of bot PR #38)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIX = Path(__file__).parent / "fixtures" / "mt5" / "status.json"
TOKEN = "k" * 8 + "Q7f2-Xa9_pL3mN8rT5vW1yZ4bC6dE0gH"  # 41 chars, test only


def push_doc(**changes: Any) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(FIX.read_text())
    now = datetime.now(UTC)
    d["schema"] = "aureon.mt5.push/v1"
    d["push_id"] = str(uuid.uuid4())
    d["seq"] = 1
    d["generated_at"] = now.isoformat()
    d["generated_ts"] = int(now.timestamp())
    for i, ev in enumerate(d["events"]):
        ev["id"] = f"ev-{i}-{ev['event']}"
    d["health"] = {
        "status": "ok",
        "mt5": {"connected": True, "terminal": "CONNECTED", "last_error": None},
        "discord": {"ok": True, "last_error": None, "last_error_at": None},
        "market": {
            "XAUUSD": {"open": True, "tick_age_s": 1.4, "session": "london"},
            "XAGUSD": {"open": True, "tick_age_s": 3.0, "session": "london"},
        },
    }
    d["guardian"] = {
        "XAUUSD": [
            {
                "ticket": 101,
                "phase": "protect",
                "leg_phase": "B",
                "profile": "default",
                "sl": 2640.0,
                "secured_level": 2648.0,
                "peak": 2656.1,
                "secured_pts": 2.5,
                "peak_pts": 5.6,
                "bars": 7,
                "frozen": False,
                "direction": "long",
                "entry": 2650.55,
                "updated_at": now.isoformat(),
            }
        ]
    }
    d["signals_latest"] = {
        "XAUUSD": {"cross": {"side": "LONG", "bar": 1793620500}, "partial": {"x": 1}}
    }
    d["tracebacks"] = []
    d["strategy"] = {
        "mode": "ema5080",
        "symbols": ["XAUUSD", "XAGUSD"],
        "exec_tf": "M5",
        "timeframes": ["M5"],
        "ema": {"fast": 50, "slow": 80},
        "params": {"poll_seconds": 15, "htf_align": False, "guardian_profile": "default"},
    }
    base = 1793620500
    d["candles"] = {
        "XAUUSD": {
            "M5": [
                {
                    "t": base - 300 * (2 - i),
                    "o": 2650 + i,
                    "h": 2652 + i,
                    "l": 2649 + i,
                    "c": 2651.5 + i,
                    "v": 100,
                    "ema": {"50": 2645.1 + i, "80": 2640.2 + i},
                }
                for i in range(3)
            ]
        }
    }
    d.update(changes)
    return d
