"""V1.6.3 aureon/review.py on the Assembler: the Sunday weekly review (aureon.weekly_review,
read by GET /api/v1/review and /review/latest).

Per live tradeable signal of the week: the hypothetical outcome under the live rule
(indicators.simulate, copied verbatim) into aureon.hypo_outcomes, then the operator's
decisions against the bot, as in V1.6.3. The week's bars are first re-pulled by the collector
(`verify_week` job); as everywhere on the Assembler that wait is resumable: the job id is kept
in aureon.control, and each run advances once the collector has answered (or after 300 s, when
the review is built on unverified bars and says so). The optional Gemini summary of V1.6.3 is
not run here (summary left empty). Read-only toward MT5.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.aureon import alerter, config, db, indicators
from app.aureon.detector import load_bars

RV = config.RULES_VERSION
VERIFY_WAIT_SECONDS = 300  # V1.6.3: 60 x 5 s


def last_week_start(today: dt.date | None = None) -> str:
    d = today or dt.datetime.now(dt.UTC).date()
    return (d - dt.timedelta(days=d.weekday() + 7)).isoformat()


async def fill_outcomes(s: AsyncSession, week_start: str) -> int:
    ws = pd.Timestamp(week_start)
    we = ws + pd.Timedelta(days=7)
    df = await load_bars(s, end=we + pd.Timedelta(days=2))
    if df.empty:
        return 0
    d = indicators.compute(df)
    idx = {t: i for i, t in enumerate(d["time_utc"])}
    sigs = await db.q(
        s,
        """select * from aureon.signals where source='live' and rules_version=:rv
           and (features->>'tradeable')='true' and time_utc>=:a and time_utc<:b""",
        {"rv": RV, "a": db.utc(ws.to_pydatetime()), "b": db.utc(we.to_pydatetime())},
    )
    for sg in sigs:
        i = idx.get(pd.Timestamp(sg["time_utc"]).tz_convert("UTC").tz_localize(None))
        if i is None:
            continue
        o = indicators.simulate(d, i, sg["dir"], sg["suggested_sl"])
        if not o:
            continue
        await db.q(
            s,
            """insert into aureon.hypo_outcomes(signal_id,rules_version,mfe,mae,bars_to_20,
               dip_before_20,secured,pl_rule,exit_how) values(:i,:rv,:mfe,:mae,:b20,:dip,:sec,:pl,:how)
               on conflict(signal_id,rules_version) do update set mfe=excluded.mfe,mae=excluded.mae,
               bars_to_20=excluded.bars_to_20,dip_before_20=excluded.dip_before_20,
               secured=excluded.secured,pl_rule=excluded.pl_rule,exit_how=excluded.exit_how,
               computed_at=now()""",
            {"i": sg["id"], "rv": RV, "mfe": o["mfe"], "mae": o["mae"], "b20": o["bars_to_20"],
             "dip": o["dip_before_20"], "sec": o["secured"], "pl": o["pl_rule"], "how": o["exit_how"]},
        )  # fmt: skip
    return len(sigs)


async def compare(s: AsyncSession, week_start: str) -> tuple[dict[str, Any], list[str]]:
    ws = pd.Timestamp(week_start)
    we = ws + pd.Timedelta(days=7)
    rows = await db.q(
        s,
        """select s.id, s.time_utc, s.dir, h.pl_rule, h.exit_how, h.secured,
           coalesce((select tag from aureon.decisions d where d.signal_id=s.id order by tagged_at desc limit 1),'auto_skip') tag,
           (select note from aureon.decisions d where d.signal_id=s.id order by tagged_at desc limit 1) note,
           (select pnl_usd from aureon.trades t where t.signal_id=s.id limit 1) pnl_you,
           (select status from aureon.trades t where t.signal_id=s.id limit 1) trade_status, s.features
           from aureon.signals s join aureon.hypo_outcomes h on h.signal_id=s.id and h.rules_version=:rv
           where s.source='live' and s.time_utc>=:a and s.time_utc<:b order by s.time_utc""",
        {"rv": RV, "a": db.utc(ws.to_pydatetime()), "b": db.utc(we.to_pydatetime())},
    )
    lots = config.LOTS_DEFAULT
    k = config.USD_PER_DOLLAR_PER_LOT
    rep: dict[str, Any] = dict(
        alerts=len(rows), taken=0, skipped=0, unreconciled=0, both_right=0, both_wrong=0,
        bot_right_you_wrong=0, you_right_bot_wrong=0, pl_bot_usd=0.0, pl_you_usd=0.0,
    )  # fmt: skip
    lines = []
    for r in rows:
        if r["pl_rule"] is None:  # AMBIGUOUS outcome: unresolved, never counted as a result
            lines.append(f"#{r['id']} outcome ambiguous ({r['exit_how']}) — not counted")
            continue
        bot = r["pl_rule"] * lots * k
        rep["pl_bot_usd"] += bot
        took = r["tag"] == "take"
        if took and (r["pnl_you"] is None or r.get("trade_status") == "unreconciled"):
            rep["unreconciled"] += 1  # taken but no reconciled P&L -> report, never count as 0
            lines.append(f"#{r['id']} taken but no trade P&L on record — check guard/MT5")
            continue
        you = (r["pnl_you"] or 0.0) if took else 0.0
        rep["pl_you_usd"] += you
        rep["taken" if took else "skipped"] += 1
        if took:
            if bot > 0 and you > 0:
                rep["both_right"] += 1
            elif bot <= 0 and you <= 0:
                rep["both_wrong"] += 1
            elif bot > 0:
                rep["bot_right_you_wrong"] += 1
            else:
                rep["you_right_bot_wrong"] += 1
        elif bot > 0:
            rep["bot_right_you_wrong"] += 1  # skipped a winner
        else:
            rep["you_right_bot_wrong"] += 1  # skipped a loser
        t = r["time_utc"]
        lines.append(
            f"#{r['id']} {t:%a %H:%M} {'BUY' if r['dir'] > 0 else 'SELL'} bot {bot:+.0f}$ ({r['exit_how']}, safe {r['secured']:.0f}) "
            f"you: {r['tag']}{' ' + r['note'] if r['note'] else ''} {('%+.0f$' % you) if took else ''}"
        )  # fmt: skip
    return rep, lines


async def run(
    maker: async_sessionmaker[AsyncSession], week_start: str | None = None, verify: bool = True
) -> dict[str, Any]:
    """One advance of the Sunday review for `week_start` (default: last week). Returns
    {state: waiting|done, ...}."""
    week = week_start or last_week_start()
    key = f"weekly_review_verify:{week}"
    async with maker() as s, s.begin():
        if verify:
            pending = await db.get_control(s, key, "")
            if not pending:
                jid = await db.request_job(s, "verify_week", {"week_start": week})
                await db.set_control(
                    s, key, f"{jid}:{dt.datetime.now(dt.UTC).isoformat()}", "review"
                )
                return {"state": "waiting", "job_id": jid}
            if pending != "used":
                jid_s, _, since = pending.partition(":")
                j = await db.job_status(s, int(jid_s))
                waited = (
                    dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(since)
                ).total_seconds()
                if not (j and j["done_at"]) and waited < VERIFY_WAIT_SECONDS:
                    return {"state": "waiting", "job_id": int(jid_s)}
                if j and j["done_at"] and j["ok"]:
                    ws = pd.Timestamp(week)
                    await db.q(
                        s,
                        """update aureon.bars set verified_week=true where symbol=:sym
                           and time_utc>=:a and time_utc<:b""",
                        {"sym": config.SYMBOL, "a": db.utc(ws.to_pydatetime()),
                         "b": db.utc((ws + pd.Timedelta(days=7)).to_pydatetime())},
                    )  # fmt: skip
                else:
                    await alerter.post(
                        s,
                        f"weekly verify for {week} did not complete — review built on unverified bars",
                        key=f"verify-incomplete:{week}",
                    )
                await db.set_control(s, key, "used", "review")
        n = await fill_outcomes(s, week)
        rep, lines = await compare(s, week)
        await db.q(
            s,
            """insert into aureon.weekly_review(week_start,alerts,taken,skipped,both_right,both_wrong,
               bot_right_you_wrong,you_right_bot_wrong,pl_bot_usd,pl_you_usd,summary)
               values(:w,:alerts,:taken,:skipped,:both_right,:both_wrong,:bot_right_you_wrong,
               :you_right_bot_wrong,:pl_bot_usd,:pl_you_usd,'')
               on conflict(week_start) do update set alerts=excluded.alerts,taken=excluded.taken,
               skipped=excluded.skipped,both_right=excluded.both_right,both_wrong=excluded.both_wrong,
               bot_right_you_wrong=excluded.bot_right_you_wrong,
               you_right_bot_wrong=excluded.you_right_bot_wrong,pl_bot_usd=excluded.pl_bot_usd,
               pl_you_usd=excluded.pl_you_usd,summary=excluded.summary,created_at=now()""",
            {
                "w": dt.date.fromisoformat(week),
                **{k: v for k, v in rep.items() if k != "unreconciled"},
            },
        )
        msg = (
            f"**Weekly review {week}** · alerts {rep['alerts']} · taken {rep['taken']} · skipped {rep['skipped']}\n"
            f"both right {rep['both_right']} · both wrong {rep['both_wrong']} · bot right/you wrong {rep['bot_right_you_wrong']} · you right/bot wrong {rep['you_right_bot_wrong']}\n"
            f"$ if you followed the bot blindly: {rep['pl_bot_usd']:+.0f} · $ you made: {rep['pl_you_usd']:+.0f}"
            + (f" · ⚠️ {rep['unreconciled']} taken trade(s) without P&L" if rep["unreconciled"] else "")
            + "\n" + "\n".join(lines[:25])
        )  # fmt: skip
        await alerter.enqueue(s, f"weekly-review:{week}", "report", msg[:1900])
    return {"state": "done", "signals": n, **rep}
