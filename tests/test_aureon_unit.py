"""Aureon V1.6.3 on the Assembler, without a database (docs/34-aureon-v163.md).

The V1.6.3 package's own unit tests for the server-side modules, ported to app.aureon
(scheduler gaps, indicators warm-up/determinism, AMBIGUOUS outcomes, deal-history positions,
matching classes, weekend week selection, chronological replay without lookahead, reports),
plus the Assembler's own: role tokens, prod checks, Cloudflare machine paths, Discord send
classification, and the execution boundary (no module can place an order or reach MT5).
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib

import httpx
import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr

from app.aureon import alerter, auth, config, indicators, matcher, weekend_review
from app.aureon.positions import build_positions
from app.aureon.scheduler import expected_bars, expected_gap
from app.core.cf_access import is_machine_path
from app.core.config import Settings
from app.core.prod_checks import aureon_token_problems

ROOT = pathlib.Path(__file__).resolve().parents[1]
MON = dt.datetime(2026, 11, 2)  # week 2026-W45: Mon 2 Nov .. Fri 6 Nov
T = dt.datetime.fromisoformat


# ---------------- scheduler gaps (V1.6.3 tests/test_scheduler_gaps.py) ----------------
def test_roll_and_weekend_are_not_gaps() -> None:
    assert expected_gap(T("2026-11-03 22:30"), T("2026-11-04 01:00"), 30)  # daily roll
    assert expected_gap(T("2026-11-03 23:30"), T("2026-11-04 01:00"), 30)  # DST extras optional
    assert expected_gap(T("2026-11-06 22:30"), T("2026-11-09 01:00"), 30)  # weekend
    assert expected_gap(T("2026-11-03 10:00"), T("2026-11-03 10:30"), 30)


def test_real_gaps_detected() -> None:
    assert not expected_gap(T("2026-11-03 10:00"), T("2026-11-03 11:00"), 30)  # one bar missing
    assert not expected_gap(T("2026-11-03 10:00"), T("2026-11-03 14:00"), 30)  # 4 h hole
    assert not expected_gap(T("2026-11-03 10:00"), T("2026-11-04 10:00"), 30)  # a day missing


def test_expected_bars_count() -> None:
    bars = expected_bars(T("2026-11-03 00:00"), T("2026-11-03 23:59"), "M30")
    assert bars[0] == T("2026-11-03 01:00") and bars[-1] == T("2026-11-03 22:30")
    assert len(bars) == 44
    assert expected_bars(T("2026-11-07 00:00"), T("2026-11-08 23:59"), "M30") == []


# ---------------- indicators (V1.6.3 tests/test_infra.py, test_weekend_review.py) -----------
def synth(n: int = 600, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2026-09-01 01:00", periods=n, freq="30min")
    c = 4000 + np.cumsum(rng.normal(0, 2, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 2, n)
    lo = np.minimum(o, c) - rng.uniform(0, 2, n)
    return pd.DataFrame(
        dict(time_utc=t, open=o, high=h, low=lo, close=c, tick_volume=rng.integers(500, 5000, n))
    )


def synth_week(
    start: dt.datetime = MON - dt.timedelta(days=14), days: int = 21, seed: int = 3
) -> pd.DataFrame:
    """M30 bars Mon-Fri 01:00..22:30 UTC with a trend so VWAP crosses occur (V1.6.3 fixture)."""
    rng = np.random.default_rng(seed)
    rows = []
    px = 4000.0
    for d in range(days):
        day = start + dt.timedelta(days=d)
        if day.weekday() >= 5:
            continue
        for k in range(44):
            t = day.replace(hour=1) + dt.timedelta(minutes=30 * k)
            drift = 1.5 * np.sin(k / 6) + rng.normal(0, 2)
            o = px
            c = px + drift
            h = max(o, c) + rng.uniform(0, 2)
            lo = min(o, c) - rng.uniform(0, 2)
            rows.append(
                dict(
                    time_utc=t,
                    open=o,
                    high=h,
                    low=lo,
                    close=c,
                    tick_volume=int(rng.integers(500, 5000)),
                )
            )
            px = c
    return pd.DataFrame(rows)


def test_warmup_and_invalid_bars_never_tradeable() -> None:
    df = synth()
    df.loc[450, "high"] = df.loc[450, "low"] - 1  # corrupt bar
    d = indicators.compute(df)
    assert not d["warm"].iloc[: config.EMA_WARMUP_BARS].any() and not d["valid"].iloc[450]
    for i in range(2, config.EMA_WARMUP_BARS):
        assert indicators.signal_at(d, i) is None
    assert indicators.signal_at(d, 450) is None and indicators.signal_at(d, 451) is None


def test_ema_vwap_deterministic_across_restart() -> None:
    d1 = indicators.compute(synth(600))
    d2 = indicators.compute(synth(600).iloc[:500])
    for i in (420, 480, 499):
        assert indicators.signal_at(d1, i) == indicators.signal_at(d2, i)
        assert abs(d1["vwap"].iloc[i] - d2["vwap"].iloc[i]) < 1e-9


def test_ambiguous_outcome_never_resolved_as_tp() -> None:
    d = pd.DataFrame(
        dict(
            time_utc=pd.date_range("2026-11-02 01:00", periods=5, freq="30min"),
            open=[4000] * 5,
            high=[4001, 4060, 4061, 4062, 4063],
            low=[3999, 3950, 3951, 3952, 3953],
            close=[4000, 4005, 4006, 4007, 4008],
            tick_volume=[1] * 5,
        )
    )
    d["vwap_side"] = 1
    d["bar_of_day"] = 5
    r = indicators.simulate(d, 0, 1, sl=20.0, tp=40.0)
    assert r["exit_how"] == "AMBIGUOUS" and r["pl_rule"] is None


def test_strategy_rules_frozen() -> None:
    """EMA 8/32/200, VWAP, M30, 1.5 ATR stop and the lock ladder are the V1.6.3 R3.0 rule."""
    r = config.RULES
    assert config.RULES_VERSION == "R3.0" and r["tf"] == "M30"
    assert (r["ema_fast"], r["ema_slow"], r["ema_trend"], r["atr_len"]) == (8, 32, 200, 14)
    assert r["sl_atr"] == 1.5 and r["lock_ladder"] == [[15, 5], [20, 15], [30, 25]]
    assert r["exit"] == "opposite_vwap_cross" and r["flat_bar_of_day"] == 44


# ---------------- deal history -> positions ----------------
def deal(ticket: int, pid: int, typ: int, entry: int, t: dt.datetime, vol: float = 0.1, price: float = 4000.0,
         profit: float = 0.0, comment: str = "") -> dict[str, object]:  # fmt: skip
    return dict(
        account_id="777", deal_ticket=ticket, order_ticket=ticket, position_id=pid, symbol="XAUUSD",
        type=typ, entry=entry, time_utc=t, volume=vol, price=price, profit=profit, commission=-0.1,
        swap=0.0, fee=0.0, comment=comment, magic=0,
    )  # fmt: skip


def test_positions_hedging_partial_and_open() -> None:
    t = MON + dt.timedelta(hours=10)
    deals = [
        deal(1, 100, 0, 0, t, 0.2, 4000.0, comment="aureon:abc123"),
        deal(2, 100, 1, 1, t + dt.timedelta(hours=1), 0.1, 4010.0, profit=10.0),
        deal(3, 100, 1, 1, t + dt.timedelta(hours=2), 0.1, 4020.0, profit=20.0),
        deal(4, 200, 1, 0, t + dt.timedelta(hours=3), 0.1, 4015.0),
        deal(5, 300, 0, 0, t + dt.timedelta(hours=4), 0.1, 4000.0, comment="manual"),
    ]
    ps = {p["position_id"]: p for p in build_positions(deals, "XAUUSD")}
    assert ps[100]["status"] == "closed" and ps[100]["partial_close"] and ps[100]["fills_out"] == 2
    assert abs(ps[100]["exit_price"] - 4015.0) < 1e-9 and ps[100]["origin"] == "aureon"
    assert ps[100]["request_ids"] == ["abc123"] and abs(ps[100]["net_pnl"] - (30.0 - 0.3)) < 1e-9
    assert ps[200]["status"] == "open" and ps[300]["origin"] == "discretionary"


def test_positions_netting_reversal() -> None:
    t = MON + dt.timedelta(hours=10)
    deals = [
        deal(1, 100, 0, 0, t, 0.1, 4000.0),
        deal(2, 100, 1, 2, t + dt.timedelta(hours=1), 0.2, 3990.0, profit=-10.0),
    ]
    p = build_positions(deals, "XAUUSD")[0]
    assert p["status"] == "closed" and p["dir"] == 1 and p["gross_pnl"] == -10.0


# ---------------- matching ----------------
def _pos(
    pid: int,
    d: int,
    t: dt.datetime,
    px: float,
    status: str = "closed",
    partial: bool = False,
    fills: int = 1,
) -> dict[str, object]:
    return dict(position_id=pid, symbol="XAUUSD", dir=d, entry_time=t, entry_price=px, status=status,
                request_ids=[], partial_close=partial, fills_in=fills, net_pnl=1.0)  # fmt: skip


def test_matching_one_to_one_and_classes() -> None:
    t = MON + dt.timedelta(hours=10)
    sigs = [
        dict(id=1, time_utc=t, dir=1, price=4000.0),
        dict(id=2, time_utc=t + dt.timedelta(minutes=30), dir=1, price=4002.0),
        dict(id=3, time_utc=t + dt.timedelta(hours=5), dir=-1, price=4050.0, decision="skip"),
    ]
    pos = [
        _pos(100, 1, t + dt.timedelta(minutes=20), 4001.0),
        _pos(200, 1, t + dt.timedelta(hours=8), 4100.0),
    ]
    m, us, up = matcher.match(sigs, pos)
    assert len(m) == 1 and m[0]["signal"]["id"] == 1 and m[0]["position"]["position_id"] == 100
    assert m[0]["classification"] == "DETECTED_AND_TRADED"
    assert {s["id"] for s in us} == {2, 3} and [p["position_id"] for p in up] == [200]
    assert matcher.classify_unmatched_signal(sigs[2])[0] == "DETECTED_NOT_TRADED"
    assert matcher.classify_unmatched_signal(dict(id=9, risk_blocked=True))[0] == "REJECTED_SIGNAL"
    assert (
        matcher.classify_unmatched_signal(dict(id=9, decision="take"))[0]
        == "UNMATCHED_OR_UNCERTAIN"
    )


def test_matching_late_early_direction_partial_and_id_preferred() -> None:
    t = MON + dt.timedelta(hours=10)
    s1 = [dict(id=1, time_utc=t, dir=1, price=4000.0)]
    late = matcher.match(s1, [_pos(1, 1, t + dt.timedelta(minutes=70), 4003.0)])[0][0]
    assert late["classification"] == "LATE_ENTRY" and late["delay_min"] == 70
    early = matcher.match(s1, [_pos(1, 1, t - dt.timedelta(minutes=3), 4000.0)])[0][0]
    assert early["classification"] == "EARLY_ENTRY"
    dm = matcher.match(s1, [_pos(1, -1, t + dt.timedelta(minutes=10), 4000.0)])[0][0]
    assert dm["classification"] == "DIRECTION_MISMATCH"
    pe = matcher.match(
        s1, [_pos(1, 1, t + dt.timedelta(minutes=10), 4000.0, partial=True, fills=2)]
    )[0][0]
    assert pe["classification"] == "PARTIAL_EXECUTION"
    byid = matcher.match(
        [dict(id=1, time_utc=t, dir=1, price=4000.0, position_id=55)],
        [_pos(55, 1, t + dt.timedelta(hours=9), 4300.0)],
    )[0][0]
    assert byid["how"] == "id"
    assert matcher.match(s1, [_pos(1, 1, t, 4000.0, status="open")])[0] == []  # open never matched


# ---------------- weekend review (pure parts) ----------------
def test_weekend_detection_and_week_selection() -> None:
    w = weekend_review
    assert w.should_auto_run(dt.datetime(2026, 11, 7, 10)) and w.should_auto_run(
        dt.datetime(2026, 11, 8, 10)
    )
    assert not w.should_auto_run(dt.datetime(2026, 11, 8, 23, 30))  # Sunday reopened
    assert not w.should_auto_run(dt.datetime(2026, 11, 4, 10))  # weekday
    assert w.last_completed_week(dt.datetime(2026, 11, 7, 10)) == dt.date(2026, 11, 2)
    assert w.last_completed_week(dt.datetime(2026, 11, 10, 10)) == dt.date(2026, 11, 2)


def test_replay_uses_live_rule_chronologically_no_lookahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    bars = synth_week()
    dets = weekend_review.replay_frame(bars, MON, MON + dt.timedelta(days=7))
    assert dets and all(MON <= d["time_utc"] < MON + dt.timedelta(days=7) for d in dets)
    assert {d["category"] for d in dets} <= {"qualified", "rejected"}
    q = [d for d in dets if d["category"] == "qualified"]
    assert q and all(d["hypo"] is not None for d in q)
    assert all(d["reason"] for d in dets if d["category"] == "rejected")
    # knowing only the bars up to the signal bar gives the same signal (no lookahead)
    d_short = indicators.compute(bars[bars.time_utc < q[0]["time_utc"] + dt.timedelta(minutes=30)])
    s = indicators.signal_at(d_short, len(d_short) - 1)
    assert s and s["dir"] == q[0]["dir"] and abs(s["price"] - q[0]["price"]) < 1e-9


def test_reports_keep_actual_and_hypothetical_separate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    dets = weekend_review.replay_frame(synth_week(), MON, MON + dt.timedelta(days=7))
    t = MON + dt.timedelta(days=3, hours=15)
    positions = build_positions(
        [
            deal(3, 200, 0, 0, t, 0.1, 4100.0),
            deal(4, 200, 1, 1, t + dt.timedelta(hours=1), 0.1, 4090.0, profit=-10.0),
        ],
        "XAUUSD",
    )
    am, rm = weekend_review.actual_metrics(positions), weekend_review.replay_metrics(dets)
    cov = {
        tf: dict(expected=220, present=220, missing=0, warmup_bars=500, warmup_ok=True)
        for tf in ("M30", "H1")
    } | {"complete": True}
    meta = dict(account_id="777", symbol="XAUUSD", week_start="2026-11-02", week_end="2026-11-08", week_label="2026-W45",
                strategy_version="R3.0", generated_at="now", history_fetch_ok=True, live_signals=0)  # fmt: skip
    bodies, summary = weekend_review.build_reports(
        meta, dets, positions, [], [], positions, am, rm, cov, lambda n: f"/x/{n}"
    )
    assert set(bodies) == {
        "detections.csv",
        "actual_trades.csv",
        "comparison.csv",
        "summary.json",
        "summary.html",
    }
    assert (
        "No orders were placed" in bodies["summary.html"] and "AMBIGUOUS" in bodies["summary.html"]
    )
    s = json.loads(bodies["summary.json"])
    assert s["actual"]["net_pnl"] == round(-10.0 - 0.2, 2) and "hypo_pnl_per_oz" in s["replay"]
    assert "net_pnl" not in s["replay"] and "hypo_pnl_per_oz" not in s["actual"]  # never combined
    msg = weekend_review.discord_summary(meta, am, rm, summary, cov, "COMPLETED")
    assert "Weekly review 2026-W45" in msg and "Hypothetical" in msg and "Actual net P&L" in msg


def test_html_report_escapes_text() -> None:
    cov = {
        tf: dict(expected=1, present=1, missing=0, warmup_bars=1, warmup_ok=True)
        for tf in ("M30", "H1")
    } | {"complete": True}
    meta = dict(account_id="<script>x</script>", symbol="XAUUSD", week_start="a", week_end="b", week_label="L",
                strategy_version="R3.0", generated_at="g", history_fetch_ok=True, live_signals=0)  # fmt: skip
    bodies, _ = weekend_review.build_reports(
        meta,
        [],
        [],
        [],
        [],
        [],
        weekend_review.actual_metrics([]),
        weekend_review.replay_metrics([]),
        cov,
        lambda n: n,
    )
    assert (
        "<script>x</script>" not in bodies["summary.html"]
        and "&lt;script&gt;" in bodies["summary.html"]
    )


# ---------------- execution boundary ----------------
def test_no_aureon_module_can_place_an_order_or_reach_mt5() -> None:
    """Code, not prose: no import of MetaTrader5 or V1.6.3's `orders`, and no call/attribute
    that sends an order, reads broker positions or opens an MT5 terminal."""
    files = [
        *(ROOT / "app" / "aureon").glob("*.py"),
        ROOT / "app" / "api" / "v1" / "aureon_v163.py",
    ]
    assert len(files) >= 12
    banned_modules = {"MetaTrader5", "orders", "aureon.orders"}
    banned_names = {
        "order_send",
        "order_check",
        "positions_get",
        "initialize",
        "TRADE_ACTION_DEAL",
        "TRADE_ACTION_SLTP",
    }
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & banned_modules, (f.name, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = {a.name for a in node.names}
                assert mod not in banned_modules and not (names & banned_modules), (
                    f.name,
                    node.lineno,
                )
            elif isinstance(node, ast.Attribute):
                assert node.attr not in banned_names, (f.name, node.lineno, node.attr)
            elif isinstance(node, ast.Name):
                assert node.id not in banned_names, (f.name, node.lineno, node.id)


# ---------------- roles / tokens ----------------
def _settings(**tok: str) -> Settings:
    base = Settings(
        ENV="test", DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none", SECRET_KEY="t" * 40
    )
    return base.model_copy(update={k: SecretStr(v) for k, v in tok.items()})


def test_role_of_and_legacy_svc_token() -> None:
    s = _settings(AUREON_API_READ_TOKEN="r" * 40, AUREON_API_SVC_TOKEN="v" * 40)
    assert (
        auth.role_of(s, "r" * 40) == "reader"
        and auth.role_of(s, "nope") is None
        and auth.role_of(s, "") is None
    )
    assert auth.role_of(s, "v" * 40) in ("collector", "guard")  # legacy: collector + guard
    s2 = _settings(AUREON_API_COLLECTOR_TOKEN="c" * 40, AUREON_API_SVC_TOKEN="v" * 40)
    assert auth.role_of(s2, "v" * 40) is None  # ignored once a role token is set
    assert (
        auth.bearer("Bearer abc") == "abc"
        and auth.bearer("Basic abc") == ""
        and auth.bearer(None) == ""
    )


def test_prod_checks_on_aureon_tokens() -> None:
    assert aureon_token_problems(_settings(AUREON_API_GUARD_TOKEN="short")) == [
        "AUREON_API_GUARD_TOKEN is shorter than 32 characters"
    ]
    same = "Lq3Zp8Tw1Ns6Vb4Hk9Rd2Mf7Gc5Jx0Ya"
    probs = aureon_token_problems(
        _settings(AUREON_API_GUARD_TOKEN=same, AUREON_API_COLLECTOR_TOKEN=same)
    )
    assert probs == [
        "AUREON_API_COLLECTOR_TOKEN must not reuse the value of AUREON_API_GUARD_TOKEN"
    ]
    probs = aureon_token_problems(_settings(AUREON_API_ADMIN_TOKEN=same, MT5_INGEST_TOKEN=same))
    assert probs == ["AUREON_API_ADMIN_TOKEN must not reuse the value of MT5_INGEST_TOKEN"]
    assert aureon_token_problems(_settings()) == []


def test_cloudflare_machine_paths_are_exact() -> None:
    for p in ("/api/v1/ingest/bars", "/api/v1/ingest/trade/12", "/api/v1/ingest/jobs/claim", "/api/v1/ingest/jobs/3/finish",
              "/api/v1/status", "/api/v1/signals/9", "/api/v1/review/latest", "/api/v1/review/reports/4/files/summary.html",
              "/api/v1/admin/risk/clear", "/api/v1/aureon/healthz", "/api/trading/mt5/ingest"):  # fmt: skip
        assert is_machine_path(p), p
    for p in ("/api/v1/settings", "/api/v1/reviews", "/api/v1/ingest/../settings", "/api/v1/ingest/jobs/claim/x",
              "/api/v1/admin/users", "/api/v1/statusx", "/api/v1/review/reports/4/files/../../x"):  # fmt: skip
        assert not is_machine_path(p), p


# ---------------- Discord send classification (V1.6.3 tests/test_infra.py) ----------------
async def test_send_classification() -> None:
    def client(code: int, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(code, headers=headers or {}, json={})
            )
        )

    url = "https://discord.test/hook"
    assert (await alerter.send(client(204), url, "t"))[0] is True
    ok, ra, _ = await alerter.send(client(429, {"Retry-After": "7"}), url, "t")
    assert not ok and ra == 7.0
    ok, ra, _ = await alerter.send(client(400), url, "t")
    assert not ok and ra is None  # permanent

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    ok, _, err = await alerter.send(
        httpx.AsyncClient(transport=httpx.MockTransport(boom)), url, "t"
    )
    assert not ok and err == "timeout"
    ok, ra, err = await alerter.send(client(204), None, "t")
    assert not ok and err == "DISCORD_WEBHOOK_URL not set"
