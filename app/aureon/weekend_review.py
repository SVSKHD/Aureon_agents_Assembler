"""Weekend Auto-Replay & Trade Comparison Engine: V1.6.3 aureon/weekend_review.py on the Assembler.

READ-ONLY toward MT5: this module imports nothing that can send orders (asserted by a test).
Reuses the live rule (indicators.compute/signal_at/simulate, copied verbatim), the matcher and
build_positions (copied verbatim), collector jobs (`backfill_range`, `mt5_history`) that the
Windows box executes, and the durable alert queue.

The week, coverage, replay, metrics, reports and Discord summary follow V1.6.3 line for line.
What changed is the waiting: V1.6.3 slept up to 2 x 600 s for the collector inside one call.
Here a review is a row in aureon.weekly_reports that moves through phases, one `step()` at a
time (the scheduler calls it every few minutes, an admin can call it through the API):

    coverage -> [wait_backfill] -> wait_history -> finalize -> COMPLETED | INCOMPLETE | FAILED

so a restart, a deploy or a slow collector resumes the same review instead of losing it.
Waiting is bounded by REVIEW_JOB_WAIT_SECONDS (600) per job, as in V1.6.3; a job that did not
finish in time leaves the data gap visible and the report INCOMPLETE. Never fabricates bars.

Dedup on (account, symbol, week, strategy): a COMPLETED week is skipped unless forced; the
automatic trigger only starts a week that has no report yet; a row being advanced by another
worker is skipped (row lock). Report bodies (JSON, HTML, CSV) are stored in PostgreSQL
(aureon.weekly_report_files) and, when AUREON_REVIEW_REPORTS_DIR is set, written there too.
Hypothetical (replay, $/oz) and actual (broker) P&L are kept in separate fields, never summed.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import html
import io
import json
import os
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.aureon import config, db, indicators, matcher
from app.aureon.positions import build_positions
from app.aureon.scheduler import expected_bars as _expected_bars
from app.aureon.scheduler import naive

SV = config.RULES_VERSION
REPORTS_DIR = os.getenv("AUREON_REVIEW_REPORTS_DIR", "")
CONTENT_TYPES = {
    "detections.csv": "text/csv; charset=utf-8",
    "actual_trades.csv": "text/csv; charset=utf-8",
    "comparison.csv": "text/csv; charset=utf-8",
    "summary.json": "application/json",
    "summary.html": "text/html; charset=utf-8",
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------- week & session ----------------
def last_completed_week(t: Any = None) -> dt.date:
    """Monday 00:00 UTC of the most recently COMPLETED trading week (Mon..Fri before the
    current weekend)."""
    t = naive(t or now())
    monday: dt.date = (t - dt.timedelta(days=t.weekday())).date()
    if t.weekday() >= 5 or (t.weekday() == 0 and t.hour < 1):
        return monday if t.weekday() >= 5 else monday - dt.timedelta(days=7)
    return monday - dt.timedelta(days=7)


def closed_by_clock(t: dt.datetime) -> bool | None:
    """Sat all day, Sun before 23:00, Fri after 22:00 -> True; otherwise None (ask holidays)."""
    wd, hm = t.weekday(), t.strftime("%H:%M")
    if wd == 5:
        return True
    if wd == 6:
        return hm < "23:00"
    if wd == 4 and hm >= "22:00":
        return True
    return None


async def market_closed_now(s: AsyncSession, t: Any = None) -> bool:
    """Broker-aware: the clock rule above, and holidays from aureon.market_calendar."""
    t = naive(t or now())
    c = closed_by_clock(t)
    if c is not None:
        return c
    hol = await db.q(
        s,
        """select 1 from aureon.market_calendar where kind='holiday'
           and start_utc<=:t and end_utc>:t""",
        {"t": db.utc(t)},
        one=True,
    )
    return bool(hol)


def should_auto_run(t: Any = None) -> bool:
    t = naive(t or now())
    return bool(closed_by_clock(t)) and t.weekday() in (5, 6)


# ---------------- data coverage ----------------
async def expected_bars(
    s: AsyncSession, ws: dt.datetime, we: dt.datetime, tf: str
) -> tuple[list[dt.datetime], set[dt.date]]:
    hol = {
        naive(r["start_utc"]).date()
        for r in await db.q(
            s,
            """select start_utc from aureon.market_calendar where kind='holiday'
               and start_utc>=:a and start_utc<:b""",
            {"a": db.utc(ws), "b": db.utc(we)},
        )
    }
    return _expected_bars(ws, we, tf, hol), hol


async def coverage(s: AsyncSession, ws: dt.datetime, we: dt.datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tf in ("M30", "H1"):
        exp, hol = await expected_bars(s, ws, we, tf)
        have = {
            naive(r["time_utc"])
            for r in await db.q(
                s,
                """select time_utc from aureon.bars where symbol=:sym and tf=:tf
                   and time_utc>=:a and time_utc<:b""",
                {"sym": config.SYMBOL, "tf": tf, "a": db.utc(ws), "b": db.utc(we)},
            )
        }
        missing = [t for t in exp if t not in have]
        warm = (
            await db.q(
                s,
                "select count(*) n from aureon.bars where symbol=:sym and tf=:tf and time_utc<:a",
                {"sym": config.SYMBOL, "tf": tf, "a": db.utc(ws)},
                one=True,
            )
        )["n"]
        out[tf] = dict(
            expected=len(exp), present=len(exp) - len(missing), missing=len(missing),
            missing_times=[str(m) for m in missing[:50]], warmup_bars=warm,
            warmup_ok=warm >= config.EMA_WARMUP_BARS, holidays=sorted(str(h) for h in hol),
        )  # fmt: skip
    out["complete"] = all(out[tf]["missing"] == 0 and out[tf]["warmup_ok"] for tf in ("M30", "H1"))
    return out


async def deals_for(
    s: AsyncSession, ws: dt.datetime, we: dt.datetime, acct: str | None = None
) -> list[dict[str, Any]]:
    rows = await db.q(
        s,
        """select * from aureon.mt5_deals where symbol=:sym and time_utc>=:a and time_utc<:b
           and (cast(:acct as text) is null or account_id=cast(:acct as text))
           order by time_utc, deal_ticket""",
        {"sym": config.SYMBOL, "a": db.utc(ws - dt.timedelta(days=7)),
         "b": db.utc(we + dt.timedelta(days=1)), "acct": acct},
    )  # fmt: skip
    for r in rows:
        r["time_utc"] = naive(r["time_utc"])
        for k in ("volume", "price", "profit", "commission", "swap", "fee"):
            r[k] = float(r[k] or 0.0)
        r["comment"] = r["comment"] or ""
        r["magic"] = r["magic"] or 0
    out: list[dict[str, Any]] = rows
    return out


# ---------------- replay ----------------
async def replay_week(s: AsyncSession, ws: dt.datetime, we: dt.datetime) -> list[dict[str, Any]]:
    """Chronological replay with the LIVE rule. Returns raw detections (every VWAP cross),
    each labelled rejected/qualified + hypothetical result."""
    from app.aureon.detector import load_bars

    df = await load_bars(s, end=we + dt.timedelta(days=2))
    return replay_frame(df, ws, we)


def replay_frame(df: pd.DataFrame, ws: dt.datetime, we: dt.datetime) -> list[dict[str, Any]]:
    if df.empty:
        return []
    d = indicators.compute(df)
    out = []
    for i in range(2, len(d)):
        t = d.iloc[i]["time_utc"]
        if t < pd.Timestamp(ws) or t >= pd.Timestamp(we):
            continue
        sig = indicators.signal_at(d, i)
        if not sig:  # not a cross, or warm-up/invalid
            continue
        if sig["features"].get("tradeable"):
            hypo = indicators.simulate(d, i, sig["dir"], sig["suggested_sl"])
            cat, reason = "qualified", ""
            eligible = d.iloc[i + 1]["time_utc"] if i + 1 < len(d) else t + pd.Timedelta(minutes=30)
        else:
            hypo = None
            cat, reason = "rejected", sig["features"].get("reject_reason", "filtered")
            eligible = None
        out.append(dict(
            time_utc=t.to_pydatetime(),
            eligible_time=(eligible.to_pydatetime() if eligible is not None else None),
            tf=config.RULES["tf"], dir=sig["dir"], price=sig["price"], category=cat,
            reason=reason, features=sig["features"], hypo=hypo,
        ))  # fmt: skip
    return out


async def live_signals(s: AsyncSession, ws: dt.datetime, we: dt.datetime) -> list[dict[str, Any]]:
    rows = await db.q(
        s,
        """select s.id,s.time_utc,s.dir,s.price,s.suggested_sl,s.features,
           (select tag from aureon.decisions dd where dd.signal_id=s.id order by tagged_at desc limit 1) decision,
           (select mt5_ticket from aureon.trades t where t.signal_id=s.id limit 1) position_id
           from aureon.signals s where s.source='live' and s.symbol=:sym and s.time_utc>=:a
           and s.time_utc<:b and (s.features->>'tradeable')='true' order by s.time_utc""",
        {"sym": config.SYMBOL, "a": db.utc(ws), "b": db.utc(we)},
    )
    for r in rows:
        r["time_utc"] = naive(r["time_utc"])
    out: list[dict[str, Any]] = rows
    return out


# ---------------- metrics (V1.6.3, verbatim) ----------------
def _pf(pnls: list[float]) -> float:
    g = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    return (g / loss) if loss else (float("inf") if g else 0.0)


def _maxdd(pnls: list[float]) -> float:
    peak = cum = 0.0
    dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def session(t: dt.datetime) -> str:
    h = t.hour
    return "Asia" if h < 7 else "London" if h < 13 else "NY" if h < 21 else "Roll"


def actual_metrics(positions: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [p for p in positions if p["status"] == "closed"]
    pn = [p["net_pnl"] for p in closed]
    gr = [p["gross_pnl"] for p in closed]
    w = [x for x in pn if x > 0]
    lo = [x for x in pn if x <= 0]

    def by(key: Any) -> dict[str, float]:
        return {
            k: round(sum(p["net_pnl"] for p in closed if key(p) == k), 2)
            for k in sorted({key(p) for p in closed})
        }

    return dict(
        trades=len(closed), open_positions=len([p for p in positions if p["status"] == "open"]),
        win_rate=(len(w) / len(closed)) if closed else None, denominator=len(closed),
        gross_pnl=round(sum(gr), 2), net_pnl=round(sum(pn), 2),
        profit_factor=round(_pf(pn), 3) if closed else None,
        avg_winner=round(sum(w) / len(w), 2) if w else None,
        avg_loser=round(sum(lo) / len(lo), 2) if lo else None,
        max_drawdown=round(_maxdd(pn), 2),
        avg_holding_min=round(sum(p["holding_min"] for p in closed) / len(closed), 1) if closed else None,
        by_weekday=by(lambda p: p["entry_time"].strftime("%a")),
        by_session=by(lambda p: session(p["entry_time"])),
        by_origin={k: len([p for p in closed if p["origin"] == k]) for k in ("aureon", "discretionary")},
    )  # fmt: skip


def replay_metrics(dets: list[dict[str, Any]]) -> dict[str, Any]:
    q = [x for x in dets if x["category"] == "qualified" and x["hypo"]]
    resolved = [x for x in q if x["hypo"]["exit_how"] != "AMBIGUOUS"]
    pn = [x["hypo"]["pl_rule"] for x in resolved]
    w = [p for p in pn if p > 0]
    return dict(
        raw_detections=len(dets), rejected=len([x for x in dets if x["category"] == "rejected"]),
        qualified=len(q), resolved=len(resolved), ambiguous=len(q) - len(resolved),
        hypo_win_rate=(len(w) / len(resolved)) if resolved else None, denominator=len(resolved),
        hypo_pnl_per_oz=round(sum(pn), 2) if resolved else None,
        hypo_profit_factor=round(_pf(pn), 3) if resolved else None,
        avg_mfe=round(sum(x["hypo"]["mfe"] for x in q) / len(q), 2) if q else None,
        avg_mae=round(sum(x["hypo"]["mae"] for x in q) / len(q), 2) if q else None,
        by_exit={k: len([x for x in q if x["hypo"]["exit_how"] == k]) for k in sorted({x["hypo"]["exit_how"] for x in q})},
        note="Hypothetical: $/oz per unit, bar-by-bar on M30 OHLC, stop counted first, no spread/slippage. NOT account P&L.",
    )  # fmt: skip


# ---------------- report files ----------------
def _csv(rows: list[dict[str, Any]], fields: list[str]) -> str:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    wr.writeheader()
    for r in rows:
        wr.writerow({k: (json.dumps(v) if isinstance(v, dict | list) else v) for k, v in r.items()})
    return buf.getvalue()


def build_reports(
    meta: dict[str, Any], dets: list[dict[str, Any]], positions: list[dict[str, Any]],
    matches: list[dict[str, Any]], unm_sig: list[dict[str, Any]], unm_pos: list[dict[str, Any]],
    am: dict[str, Any], rm: dict[str, Any], cov: dict[str, Any], file_ref: Any,
) -> tuple[dict[str, str], dict[str, Any]]:  # fmt: skip
    """The five V1.6.3 report bodies {name: text} and the summary. `file_ref(name)` names
    where each file can be fetched (API path or disk path)."""
    bodies: dict[str, str] = {}
    bodies["detections.csv"] = _csv(
        [{**d, "hypo_exit": (d["hypo"] or {}).get("exit_how"), "hypo_pl_per_oz": (d["hypo"] or {}).get("pl_rule"),
          "mfe": (d["hypo"] or {}).get("mfe"), "mae": (d["hypo"] or {}).get("mae")} for d in dets],
        ["time_utc", "eligible_time", "tf", "dir", "price", "category", "reason", "hypo_exit", "hypo_pl_per_oz", "mfe", "mae", "features"],
    )  # fmt: skip
    bodies["actual_trades.csv"] = _csv(
        positions,
        ["position_id", "account_id", "symbol", "dir", "entry_time", "exit_time", "entry_price", "exit_price", "volume_in", "volume_out",
         "fills_in", "fills_out", "partial_close", "gross_pnl", "costs", "net_pnl", "holding_min", "status", "origin", "order_tickets"],
    )  # fmt: skip
    comp: list[dict[str, Any]] = []
    for m in matches:
        sg, p = m["signal"], m["position"]
        comp.append(dict(
            classification=m["classification"], signal_time=sg["time_utc"], signal_dir=sg["dir"], signal_price=sg["price"],
            live_signal_id=sg.get("live_signal_id"), decision=sg.get("decision"), position_id=p["position_id"],
            actual_entry=p["entry_price"], actual_exit=p["exit_price"], entry_delay_min=m["delay_min"], slippage=m["slippage"],
            hypo_exit=(sg.get("hypo") or {}).get("exit_how"), hypo_pl_per_oz=(sg.get("hypo") or {}).get("pl_rule"),
            actual_net_pnl=p["net_pnl"], actual_exit_reason=p["status"], holding_min=p["holding_min"],
            mfe=(sg.get("hypo") or {}).get("mfe"), mae=(sg.get("hypo") or {}).get("mae"),
        ))  # fmt: skip
    for sg in unm_sig:
        cls, why = matcher.classify_unmatched_signal(sg)
        comp.append(dict(
            classification=cls, signal_time=sg["time_utc"], signal_dir=sg["dir"], signal_price=sg["price"],
            live_signal_id=sg.get("live_signal_id"), decision=sg.get("decision"), reason=why,
            hypo_exit=(sg.get("hypo") or {}).get("exit_how"), hypo_pl_per_oz=(sg.get("hypo") or {}).get("pl_rule"),
        ))  # fmt: skip
    for p in unm_pos:
        comp.append(dict(
            classification="TRADED_WITHOUT_MATCHING_DETECTION", position_id=p["position_id"], actual_entry=p["entry_price"],
            actual_exit=p["exit_price"], actual_net_pnl=p["net_pnl"], signal_dir=p["dir"], reason=p["origin"],
        ))  # fmt: skip
    bodies["comparison.csv"] = _csv(
        comp,
        ["classification", "signal_time", "signal_dir", "signal_price", "live_signal_id", "decision", "position_id", "actual_entry",
         "actual_exit", "entry_delay_min", "slippage", "hypo_exit", "hypo_pl_per_oz", "actual_net_pnl", "actual_exit_reason",
         "holding_min", "mfe", "mae", "reason"],
    )  # fmt: skip
    files = {name: file_ref(name) for name in CONTENT_TYPES}
    summary = dict(
        meta=meta, coverage=cov, actual=am, replay=rm,
        classification_counts={k: len([c for c in comp if c["classification"] == k]) for k in sorted({c["classification"] for c in comp})},
        matches=len(matches), unmatched_signals=len(unm_sig), unmatched_positions=len(unm_pos), files=files,
    )  # fmt: skip
    bodies["summary.json"] = json.dumps(summary, indent=2, default=str)
    bodies["summary.html"] = render_html(summary, comp)
    return bodies, summary


def _write_files(folder: str, bodies: dict[str, str]) -> None:
    os.makedirs(folder, exist_ok=True)
    for name, body in bodies.items():
        with open(os.path.join(folder, name), "w", newline="") as fh:
            fh.write(body)


def render_html(s: dict[str, Any], comp: list[dict[str, Any]]) -> str:
    def e(x: Any) -> str:
        return html.escape(str(x))

    meta, am, rm, cov = s["meta"], s["actual"], s["replay"], s["coverage"]
    rows = "".join(
        f"<tr><td>{e(c['classification'])}</td><td>{e(c.get('signal_time',''))}</td><td>{e(c.get('signal_dir',''))}</td><td>{e(c.get('signal_price',''))}</td><td>{e(c.get('decision',''))}</td><td>{e(c.get('position_id',''))}</td><td>{e(c.get('actual_entry',''))}</td><td>{e(c.get('entry_delay_min',''))}</td><td>{e(c.get('hypo_exit',''))}</td><td>{e(c.get('hypo_pl_per_oz',''))}</td><td>{e(c.get('actual_net_pnl',''))}</td><td>{e(c.get('reason',''))}</td></tr>"
        for c in comp
    )  # fmt: skip

    def kv(d: dict[str, Any]) -> str:
        return "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in d.items())

    status = (
        "COMPLETE"
        if cov["complete"]
        else f"INCOMPLETE — missing M30 {cov['M30']['missing']}, H1 {cov['H1']['missing']}"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Aureon weekly review {e(meta['week_label'])}</title>
<style>body{{font:14px system-ui;margin:24px;color:#1a1d24}}table{{border-collapse:collapse;margin:8px 0 20px}}th,td{{border:1px solid #d8dbe2;padding:4px 8px;text-align:left;font-size:13px}}th{{background:#f3f4f6}}h2{{margin-top:28px}}.warn{{color:#b45309}}.sep{{color:#6b7280}}</style></head><body>
<h1>Aureon weekly review — {e(meta['week_label'])} ({e(meta['week_start'])} → {e(meta['week_end'])})</h1>
<p>Account {e(meta['account_id'])} · {e(meta['symbol'])} · strategy {e(meta['strategy_version'])} · generated {e(meta['generated_at'])} · data: <b class="{'' if cov['complete'] else 'warn'}">{e(status)}</b></p>
<h2>Actual account results (broker records)</h2><table>{kv({k: v for k, v in am.items() if not isinstance(v, dict)})}</table>
<table><tr><th>by weekday</th><td>{e(am['by_weekday'])}</td></tr><tr><th>by session</th><td>{e(am['by_session'])}</td></tr><tr><th>by origin</th><td>{e(am['by_origin'])}</td></tr></table>
<h2>Hypothetical replay (strategy rule, $/oz — <span class="sep">kept separate from account P&amp;L</span>)</h2><table>{kv({k: v for k, v in rm.items() if not isinstance(v, dict)})}</table><table><tr><th>by exit</th><td>{e(rm['by_exit'])}</td></tr></table>
<h2>Signal ↔ trade comparison</h2><table>{kv(s['classification_counts'])}</table>
<table><tr><th>class</th><th>signal time</th><th>dir</th><th>signal px</th><th>decision</th><th>position</th><th>actual entry</th><th>delay min</th><th>hypo exit</th><th>hypo $/oz</th><th>actual net $</th><th>reason</th></tr>{rows}</table>
<h2>Data coverage</h2><table>{kv({tf: f"{cov[tf]['present']}/{cov[tf]['expected']} bars, missing {cov[tf]['missing']}, warm-up {cov[tf]['warmup_bars']} ({'ok' if cov[tf]['warmup_ok'] else 'INSUFFICIENT'})" for tf in ('M30', 'H1')})}</table>
<p class="sep">Read-only report. No orders were placed or modified. Hypothetical results assume bar-by-bar M30 OHLC, stop first, no spread/slippage; AMBIGUOUS = TP and stop in the same bar, unresolved.</p></body></html>"""  # fmt: skip


def discord_summary(
    meta: dict[str, Any],
    am: dict[str, Any],
    rm: dict[str, Any],
    s: dict[str, Any],
    cov: dict[str, Any],
    state: str,
) -> str:
    cc = s["classification_counts"]
    return (
        f"**📊 Weekly review {meta['week_label']}** ({meta['week_start']} → {meta['week_end']}) · {state}{'' if cov['complete'] else ' — data incomplete, treat as provisional'}\n"
        f"Qualified detections {rm['qualified']} (raw {rm['raw_detections']}, rejected {rm['rejected']}) · trades placed {am['trades']} · matched {s['matches']} · "
        f"missed {cc.get('DETECTED_NOT_TRADED', 0)} · without detection {cc.get('TRADED_WITHOUT_MATCHING_DETECTION', 0)} · risk-rejected {cc.get('REJECTED_SIGNAL', 0)}\n"
        f"**Actual net P&L ${am['net_pnl']:+.2f}** (gross {am['gross_pnl']:+.2f}, win rate {('%.0f%%' % (100*am['win_rate'])) if am['win_rate'] is not None else 'n/a'} of {am['denominator']})\n"
        f"Hypothetical (replay, $/oz, separate): {rm['hypo_pnl_per_oz']} over {rm['resolved']} resolved, {rm['ambiguous']} ambiguous, win rate {('%.0f%%' % (100*rm['hypo_win_rate'])) if rm['hypo_win_rate'] is not None else 'n/a'}\n"
        f"Report: {s['files'].get('summary.html')}"
    )  # fmt: skip


# ---------------- orchestration (resumable) ----------------
def week_of(week_start: Any) -> tuple[dt.datetime, dt.datetime, str]:
    if week_start:
        ws = naive(pd.Timestamp(week_start).to_pydatetime())
    else:
        ws = dt.datetime.combine(last_completed_week(), dt.time(0, 0))
    ws = ws.replace(hour=0, minute=0, second=0, microsecond=0)
    we = ws + dt.timedelta(days=7)
    iso = ws.isocalendar()
    return ws, we, f"{iso[0]}-W{iso[1]:02d}"


async def account_for(s: AsyncSession, account_id: str | None) -> str:
    if account_id:
        return str(account_id)
    r = await db.q(
        s, "select account_id from aureon.risk_state order by updated_at desc limit 1", one=True
    )
    return str((r or {}).get("account_id") or "unknown")


async def _set_phase(s: AsyncSession, rid: int, phase: str, **cols: Any) -> None:
    sets = ", ".join(["phase=:phase", "phase_at=now()"] + [f"{k}=:{k}" for k in cols])
    await db.q(
        s,
        f"update aureon.weekly_reports set {sets} where id=:rid",  # noqa: S608 - fixed column names
        {"phase": phase, "rid": rid, **cols},
    )


async def _job_settled(
    s: AsyncSession, jid: int | None, since: dt.datetime | None
) -> tuple[bool, dict[str, Any] | None]:
    """(settled, job): settled when the job finished (done, or failed for good) or the wait
    (REVIEW_JOB_WAIT_SECONDS since the phase began) ran out."""
    j = await db.job_status(s, jid) if jid else None
    if j and j["done_at"]:
        return True, j
    waited = (now() - since).total_seconds() if since else 0
    return waited >= config.REVIEW_JOB_WAIT_SECONDS, j


async def step(
    maker: async_sessionmaker[AsyncSession],
    week_start: Any = None,
    force: bool = False,
    account_id: str | None = None,
    auto: bool = False,
) -> dict[str, Any]:
    """Advance one review by one or more phases. Returns {report_id, state, phase, ...}, or
    {skipped: reason}. Exceptions mark the report FAILED and queue an alert, then re-raise."""
    ws, we, label = week_of(week_start)
    async with maker() as s, s.begin():
        acct = await account_for(s, account_id)
        key = {"a": acct, "sym": config.SYMBOL, "w": ws.date(), "sv": SV}
        existing = await db.q(
            s,
            """select * from aureon.weekly_reports where account_id=:a and symbol=:sym
               and week_start=:w and strategy_version=:sv""",
            key,
            one=True,
        )
        if existing and existing["state"] == "COMPLETED" and not force:
            return dict(
                skipped="already completed", report_id=existing["id"], files=existing["files"]
            )
        if existing and auto and existing["state"] in ("INCOMPLETE", "FAILED"):
            return dict(
                skipped=f"already {existing['state'].lower()} (an admin can re-run it)",
                report_id=existing["id"],
            )
        restart = not existing or existing["state"] != "RUNNING" or force
        if restart:
            row = await db.q(
                s,
                """insert into aureon.weekly_reports(account_id,symbol,week_start,strategy_version,
                   state,started_at,attempts,phase,phase_at)
                   values(:a,:sym,:w,:sv,'RUNNING',now(),1,'coverage',now())
                   on conflict(account_id,symbol,week_start,strategy_version) do update
                   set state='RUNNING', started_at=now(), attempts=aureon.weekly_reports.attempts+1,
                   error=null, phase='coverage', phase_at=now(), backfill_job_id=null,
                   history_job_id=null, history_ok=null returning id""",
                key,
                one=True,
            )
            rid = int(row["id"])
    if not restart:
        rid = int(existing["id"])
    try:
        return await _advance(maker, rid, ws, we, label, acct)
    except Exception as e:
        async with maker() as s, s.begin():
            await db.q(
                s,
                """update aureon.weekly_reports set state='FAILED', finished_at=now(), error=:e
                   where id=:i""",
                {"e": f"{type(e).__name__}: {e}"[:1000], "i": rid},
            )
            await db.alert_enqueue(
                s,
                f"weekly-report-failed:{acct}:{label}:{now():%Y%m%d%H}",
                "report",
                f"❌ weekly review {label} FAILED: {type(e).__name__}: {e}"[:1900],
            )
        raise


async def _advance(
    maker: async_sessionmaker[AsyncSession],
    rid: int,
    ws: dt.datetime,
    we: dt.datetime,
    label: str,
    acct: str,
) -> dict[str, Any]:
    async with maker() as s, s.begin():
        r = await db.q(
            s,
            "select * from aureon.weekly_reports where id=:i for update skip locked",
            {"i": rid},
            one=True,
        )
        if r is None:
            return dict(skipped="running elsewhere", report_id=rid)
        if r["state"] != "RUNNING":
            return dict(report_id=rid, state=r["state"], phase=r["phase"])
        phase = r["phase"] or "coverage"
        if phase == "coverage":
            cov = await coverage(s, ws, we)
            if not cov["complete"]:  # ask the MT5 box for missing bars; never fabricate them
                jid = await db.request_job(
                    s,
                    "backfill_range",
                    {"start": ws.isoformat(), "end": we.isoformat(), "tfs": ["M30", "H1"]},
                )
                await _set_phase(
                    s,
                    rid,
                    "wait_backfill",
                    backfill_job_id=jid,
                    coverage=json.dumps(cov, default=str),
                )
                return dict(report_id=rid, state="RUNNING", phase="wait_backfill", job_id=jid)
            phase = "request_history"
        if phase == "wait_backfill":
            settled, _ = await _job_settled(s, r["backfill_job_id"], r["phase_at"])
            if not settled:
                return dict(
                    report_id=rid,
                    state="RUNNING",
                    phase="wait_backfill",
                    job_id=r["backfill_job_id"],
                )
            phase = "request_history"
        if phase == "request_history":  # the week's deals arrive via POST /api/v1/ingest/mt5_deals
            jid = await db.request_job(
                s, "mt5_history", {"start": ws.isoformat(), "end": we.isoformat()}
            )
            await _set_phase(s, rid, "wait_history", history_job_id=jid)
            return dict(report_id=rid, state="RUNNING", phase="wait_history", job_id=jid)
        if phase == "wait_history":
            settled, j = await _job_settled(s, r["history_job_id"], r["phase_at"])
            if not settled:
                return dict(
                    report_id=rid, state="RUNNING", phase="wait_history", job_id=r["history_job_id"]
                )
            hist_ok = bool(j and j["done_at"] and j["state"] == "done")
            return await finalize(s, rid, ws, we, label, acct, hist_ok)
        if phase == "finalize":
            return await finalize(s, rid, ws, we, label, acct, bool(r["history_ok"]))
    return dict(report_id=rid, state="RUNNING", phase=phase)


async def finalize(
    s: AsyncSession,
    rid: int,
    ws: dt.datetime,
    we: dt.datetime,
    label: str,
    acct: str,
    hist_ok: bool,
) -> dict[str, Any]:
    cov = await coverage(s, ws, we)
    deals = await deals_for(s, ws, we, acct if acct != "unknown" else None)
    positions = [
        p for p in build_positions(deals, config.SYMBOL)
        if (p["entry_time"] or p["exit_time"]) and ws <= (p["entry_time"] or p["exit_time"]) < we
    ]  # fmt: skip
    dets = await replay_week(s, ws, we)
    live = await live_signals(s, ws, we)
    risk_locked_days = {
        r["broker_day"]
        for r in await db.q(
            s,
            """select broker_day from aureon.risk_state where account_id=:a and broker_day>=:f
               and broker_day<:t and state in ('LOCKED','UNKNOWN')""",
            {"a": acct, "f": ws.date(), "t": we.date()},
        )
    }
    # qualified detections become match candidates; attach the live signal id when one exists
    qualified = []
    for n, dd in enumerate(x for x in dets if x["category"] == "qualified"):
        ls = next(
            (lv for lv in live if abs((lv["time_utc"] - dd["time_utc"]).total_seconds()) < 60), None
        )
        qualified.append(dict(
            id=n, time_utc=dd["eligible_time"] or dd["time_utc"], dir=dd["dir"], price=dd["price"], hypo=dd["hypo"],
            live_signal_id=ls["id"] if ls else None, decision=(ls or {}).get("decision"),
            position_id=(ls or {}).get("position_id"), risk_blocked=(dd["time_utc"].date() in risk_locked_days),
        ))  # fmt: skip
    matches, unm_sig, unm_pos = matcher.match(qualified, positions)
    am, rm = actual_metrics(positions), replay_metrics(dets)
    meta = dict(
        account_id=acct, symbol=config.SYMBOL, week_start=str(ws.date()), week_end=str((we - dt.timedelta(days=1)).date()),
        week_label=label, strategy_version=SV, generated_at=str(now()), history_fetch_ok=hist_ok, live_signals=len(live),
    )  # fmt: skip
    folder = os.path.join(REPORTS_DIR, label) if REPORTS_DIR else ""
    bodies, summary = build_reports(
        meta, dets, positions, matches, unm_sig, unm_pos, am, rm, cov,
        lambda name: os.path.join(folder, name) if folder else f"/api/v1/review/reports/{rid}/files/{name}",
    )  # fmt: skip
    if folder:
        await asyncio.to_thread(_write_files, folder, bodies)
    await db.q(s, "delete from aureon.replay_detections where report_id=:i", {"i": rid})
    for d in dets:
        await db.q(
            s,
            """insert into aureon.replay_detections(report_id,time_utc,tf,dir,price,category,reason,
               features,live_signal_id,hypo) values(:r,:t,:tf,:dir,:p,:c,:re,cast(:f as jsonb),null,cast(:h as jsonb))
               on conflict do nothing""",
            {"r": rid, "t": db.utc(d["time_utc"]), "tf": d["tf"], "dir": d["dir"], "p": d["price"],
             "c": d["category"], "re": d["reason"], "f": json.dumps(d["features"], default=str),
             "h": json.dumps(d["hypo"], default=str)},
        )  # fmt: skip
    for name, body in bodies.items():
        await db.q(
            s,
            """insert into aureon.weekly_report_files(report_id,name,content_type,body)
               values(:r,:n,:ct,:b) on conflict(report_id,name) do update
               set content_type=excluded.content_type, body=excluded.body, created_at=now()""",
            {"r": rid, "n": name, "ct": CONTENT_TYPES[name], "b": body},
        )
    state = "COMPLETED" if (cov["complete"] and hist_ok) else "INCOMPLETE"
    await db.q(
        s,
        """update aureon.weekly_reports set state=:st, finished_at=now(), phase='done', phase_at=now(),
           history_ok=:h, coverage=cast(:cov as jsonb), actual_metrics=cast(:am as jsonb),
           replay_metrics=cast(:rm as jsonb), files=cast(:f as jsonb), summary=cast(:su as jsonb)
           where id=:i""",
        {"st": state, "h": hist_ok, "cov": json.dumps(cov, default=str), "am": json.dumps(am, default=str),
         "rm": json.dumps(rm, default=str), "f": json.dumps(summary["files"]),
         "su": json.dumps(summary, default=str), "i": rid},
    )  # fmt: skip
    await db.alert_enqueue(
        s,
        f"weekly-report:{acct}:{label}:{SV}:{state}",
        "report",
        discord_summary(meta, am, rm, summary, cov, state)[:1900],
    )
    return dict(report_id=rid, state=state, phase="done", files=summary["files"], summary=summary)


async def auto(maker: async_sessionmaker[AsyncSession], t: Any = None) -> list[dict[str, Any]]:
    """Scheduler entry: advance every RUNNING review, and on a closed weekend start last week's
    review once (a week that already has a report is not restarted automatically)."""
    out = []
    async with maker() as s:
        running = await db.q(
            s,
            """select id, account_id, week_start from aureon.weekly_reports
               where state='RUNNING' and strategy_version=:sv order by id""",
            {"sv": SV},
        )
    for r in running:
        out.append(await step(maker, week_start=str(r["week_start"]), account_id=r["account_id"]))
    if should_auto_run(t):
        out.append(await step(maker, week_start=str(last_completed_week(t)), auto=True))
    return out
