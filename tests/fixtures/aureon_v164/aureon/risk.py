"""Daily risk engine. Durable per (broker_day, account_id) in risk_state. States: UNLOCKED | WARNING | LOCKED | UNKNOWN.
Fail-closed: UNKNOWN and LOCKED both deny new entries. Existing positions keep protective management regardless.
Baseline rule: the day's reference equity is the previous broker day's CLOSING equity (snapshotted by the guard at its last loop of
that day) or, failing that, an equity reading taken within BASELINE_WINDOW_MIN of the day boundary. Current equity is never
silently used as the baseline later in the day -> UNKNOWN instead."""
import json, datetime as dt
from . import config
from .store import store
from .timeutil import now

BASELINE_WINDOW_MIN = 15          # a reading this close to the boundary counts as the boundary equity
PREV_CLOSE_MAX_AGE_MIN = 20       # a previous-day snapshot is usable only if taken this close BEFORE the boundary

def broker_day(t=None):
    t = (t or now()).replace(tzinfo=None); h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":"))
    return (t - dt.timedelta(hours=h, minutes=m)).date()

def minutes_into_day(t=None):
    t = (t or now()).replace(tzinfo=None); h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":"))
    start = dt.datetime.combine(broker_day(t), dt.time(h, m)); return (t - start).total_seconds() / 60

class RiskEngine:
    def __init__(self, account_id): self.acct = str(account_id)

    def load(self, day=None): return store.risk_get(day or broker_day(), self.acct)

    TOL = 0.01   # account currency

    def day_boundary(self, day=None):
        h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":")); return dt.datetime.combine(day or broker_day(), dt.time(h, m))

    def verified_baseline(self, snap):
        """Broker-VERIFIED starting EQUITY for this broker day, or (None, bal0, reason). Order of evidence:
        1. a boundary reading: equity read within ±BASELINE_WINDOW_MIN of the boundary (this process reads every 15 s, so a running
           guard always has one) — exact, includes floating P&L of overnight positions;
        2. no position open across the boundary and complete history: equity == reconstructed balance (exact by arithmetic);
        3. previous-day snapshot taken within PREV_CLOSE_MAX_AGE_MIN BEFORE the boundary AND whose balance reconciles with the
           reconstructed day-start balance;
        4. otherwise UNKNOWN. The reconstructed balance alone is never used as equity when positions were open."""
        day = broker_day(); b = self.day_boundary(day)
        bal0 = snap["balance"] - snap["realized_today"] - snap.get("deposits_today", 0.0)
        if snap.get("history_suspect"): return None, bal0, "broker deal history incomplete for today — cannot reconstruct day start"
        tnow = now().replace(tzinfo=None)
        if abs((tnow - b).total_seconds()) <= BASELINE_WINDOW_MIN * 60:
            return snap["equity"], bal0, "boundary_reading"
        if not snap.get("positions_open_at_day_start"):
            return bal0, bal0, "reconstructed_no_overnight"
        for back in (1, 2, 3, 4):
            prev = store.risk_get(day - dt.timedelta(days=back), self.acct)
            if not (prev and prev.get("equity") is not None and prev.get("balance") is not None): continue
            at = prev.get("equity_at")
            if at is None: return None, bal0, "previous-close snapshot has no timestamp — not usable"
            at = at.replace(tzinfo=None) if isinstance(at, dt.datetime) else dt.datetime.fromisoformat(str(at)).replace(tzinfo=None)
            age_min = (b - at).total_seconds() / 60
            if not (0 <= age_min <= PREV_CLOSE_MAX_AGE_MIN): return None, bal0, f"previous-close snapshot is {age_min:.0f} min before the boundary (max {PREV_CLOSE_MAX_AGE_MIN})"
            if abs(prev["balance"] - bal0) > self.TOL: return None, bal0, f"snapshot balance {prev['balance']:.2f} != reconstructed day-start balance {bal0:.2f}"
            return prev["equity"], bal0, "prev_close_verified"
        return None, bal0, "overnight positions and no boundary reading or verifiable previous-close snapshot"

    def establish_baseline(self, snap):
        day = broker_day(); row = self.load(day)
        if row: return row
        ref_eq, ref_bal, src = self.verified_baseline(snap)
        state = "UNKNOWN" if ref_eq is None else "UNLOCKED"
        store.risk_upsert(dict(broker_day=day, account_id=self.acct, ref_equity=ref_eq, ref_balance=ref_bal, ref_source=src if ref_eq is not None else "none",
                               equity=snap["equity"], balance=snap["balance"], state=state))
        store.risk_event(day, self.acct, "baseline", dict(source=src, ref_equity=ref_eq, ref_balance=ref_bal), "guard")
        if state == "UNKNOWN": self._alert(f"⚠️ RISK UNKNOWN for {day}: {src}. New entries blocked until an admin sets the baseline: `risk baseline <account> <equity>`.")
        return self.load(day)

    def update(self, snap):
        """snap: dict(equity, balance, floating, realized_today, deposits_today, has_other_symbol_positions). Returns state."""
        row = self.establish_baseline(snap); day = row["broker_day"]
        upd = dict(equity=snap["equity"], balance=snap["balance"], floating=snap["floating"], realized=snap["realized_today"], deposits=snap.get("deposits_today", 0.0),
                   equity_at=now(), history_suspect=bool(snap.get("history_suspect")))
        if row["ref_equity"] is None and not snap.get("history_suspect"):                  # late-established baseline (e.g. history became complete)
            ref_eq, ref_bal, src = self.verified_baseline(snap)
            if ref_eq is not None:
                row = {**row, "ref_equity": ref_eq, "ref_balance": ref_bal, "ref_source": src, "state": "UNLOCKED"}
                store.risk_event(day, self.acct, "baseline", dict(source=src, ref_equity=ref_eq, late=True), "guard")
        if row["ref_equity"] is None:
            store.risk_upsert({**row, **upd}); return "UNKNOWN"
        dep = snap.get("deposits_today", 0.0) or 0.0                                 # deposits/withdrawals move the reference, not the P&L
        dd = (snap["equity"] - (row["ref_equity"] + dep)) if config.COUNT_FLOATING_IN_DAILY_LOSS else (snap["balance"] - ((row["ref_balance"] if row["ref_balance"] is not None else row["ref_equity"]) + dep))
        upd["drawdown"] = dd
        state = row["state"]
        if state == "LOCKED": pass                                                  # sticky until admin clears
        elif dd <= -config.MAX_LOSS_PER_DAY_USD:
            state = "LOCKED"; upd.update(locked_at=now(), lock_reason=f"drawdown {dd:.2f} <= -{config.MAX_LOSS_PER_DAY_USD}")
            store.risk_event(day, self.acct, "lock", dict(drawdown=dd), "guard")
            self._alert(f"🛑 DAILY LOSS LIMIT ({self.acct}): drawdown {dd:+.2f} vs limit -{config.MAX_LOSS_PER_DAY_USD}. LOCKED for broker day {day}. New entries blocked; open positions stay managed.")
        elif dd <= -config.WARN_LOSS_PER_DAY_USD and state != "WARNING":
            state = "WARNING"; store.risk_event(day, self.acct, "warning", dict(drawdown=dd), "guard"); self._alert(f"⚠️ risk warning: day drawdown {dd:+.2f}")
        elif state == "WARNING" and dd > -config.WARN_LOSS_PER_DAY_USD: state = "UNLOCKED"
        store.risk_upsert({**row, **upd, "state": state}); return state

    def mark_unknown(self, why):
        day = broker_day(); row = self.load(day)
        if row and row["state"] != "LOCKED":
            store.risk_upsert({**row, "state": "UNKNOWN"}); store.risk_event(day, self.acct, "unknown", dict(why=why), "guard")

    def state(self):
        row = self.load(); return row["state"] if row else "UNKNOWN"

    def allows_new_entries(self): return self.state() in ("UNLOCKED", "WARNING")

    def close_day_snapshot(self, snap):
        """Each loop stamps equity/balance/equity_at so the last write of the day is a TIMESTAMPED closing snapshot; the next day
        trusts it only if it was taken within PREV_CLOSE_MAX_AGE_MIN of the boundary and its balance reconciles."""
        row = self.load()
        if row: store.risk_upsert({**row, "equity": snap["equity"], "balance": snap["balance"], "equity_at": now()})

    @staticmethod
    def _alert(text):
        from .alerter import enqueue; enqueue(f"risk:{now().isoformat(timespec='minutes')}", "risk", text)

def admin_clear(account_id, actor, reason):
    """Auditable, admin-only. Only valid where the funding provider allows re-entry after a daily lock — Aureon cannot know that."""
    day = broker_day(); row = store.risk_get(day, str(account_id))
    if not row or row["state"] != "LOCKED": return False
    store.risk_upsert({**row, "state": "UNLOCKED", "cleared_at": now(), "cleared_by": actor, "clear_reason": reason})
    store.risk_event(day, str(account_id), "admin_clear", dict(reason=reason), actor); return True

def admin_baseline(account_id, actor, equity):
    day = broker_day(); row = store.risk_get(day, str(account_id))
    if not row: return False
    store.risk_upsert({**row, "ref_equity": float(equity), "ref_balance": float(equity), "ref_source": "manual", "state": "UNLOCKED" if row["state"] == "UNKNOWN" else row["state"]})
    store.risk_event(day, str(account_id), "admin_baseline", dict(equity=equity), actor); return True
