#!/usr/bin/env python3
"""Aureon / Spasta end-to-end acceptance run (docs/24-production-integration.md, Phase 22).

    AUREON_ACCEPTANCE_EMAIL=owner@spasta.online \\
    python3 scripts/ops/acceptance.py --base-url http://127.0.0.1:8000 --report out.json
    # the password is read from AUREON_ACCEPTANCE_PASSWORD, else from stdin (prompted on a tty)

Runs the 16 acceptance steps against a running API with an admin session and prints one line
per step: PASS, FAIL or SKIP (SKIP = an external piece is not set up here, e.g. no GitHub
PAT, no MT5 export, no image worker; the reason is printed). Writes a JSON report.
Credentials never go on the command line. Exit 0 when nothing FAILed.

Optional environment (all absent = those steps SKIP):
  CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET   service token, when going through Access
  AUREON_ACCEPTANCE_PR=owner/repo#123             an OPEN test PR for the merge-gate step
                                                  (the merge is filed and then DENIED)
  AUREON_ACCEPTANCE_WEBHOOK_SECRET                the GITHUB_WEBHOOK_SECRET, for the signed
                                                  webhook idempotency step
  AUREON_ACCEPTANCE_CODE_REPO                     a CODE_REPOS entry for the coding step
  AUREON_ACCEPTANCE_DRILL_DIR                     BACKUP_DIR/drills, for the restore drill
  AUREON_ACCEPTANCE_INSTRUMENT_ID                 an mt5 instrument with a live price (the
                                                  bot export's last close) for the paper step
  --skip-heavy                                    never start a model/GPU job (chat, image,
                                                  coding): those steps SKIP

What it writes on the server (all small, all tagged "acceptance"): one finance expense
(deleted again), one MT5 broker account `ACCEPTANCE-PAPER` in PAPER mode with a risk policy
and one instrument `ACCEPTANCE` (reused on every run), one paper order intent per run (LIMIT,
far from any price), approval requests (the PR merge one is denied), one webhook `ping`
delivery. LIVE is only probed, never enabled. Uses only the standard library + httpx.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

STEPS: list[tuple[str, str]] = [
    ("health", "health/ready"),
    ("login_csrf", "login + CSRF"),
    ("finance", "finance CRUD + ownership"),
    ("paper_trade", "paper trade via the approval engine"),
    ("live_unreachable", "LIVE unreachable"),
    ("mt5_fresh", "MT5 feed fresh"),
    ("dhan_read", "Dhan read"),
    ("chat", "chat via Ollama"),
    ("image_job", "image job"),
    ("coding_task", "coding agent task"),
    ("github", "GitHub connection + repo browse"),
    ("pr_merge_gate", "PR merge gated by approval with re-auth"),
    ("webhook", "webhook HMAC + idempotency"),
    ("repair_pr", "incident -> repair PR (no merge)"),
    ("claude_review", "Claude review advisory-only"),
    ("backup_drill", "backup + restore drill"),
]


class Skip(Exception):
    pass


class Fail(Exception):
    pass


def check(cond: Any, msg: str) -> None:
    if not cond:
        raise Fail(msg)


@dataclass
class Result:
    n: int
    id: str
    name: str
    status: str
    detail: str
    seconds: float


@dataclass
class Session:
    base: str
    http: httpx.Client
    password: str
    cookies: dict[str, str] = field(default_factory=dict)
    csrf: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    def _headers(self, unsafe: bool, csrf: bool) -> dict[str, str]:
        h = dict(self.extra_headers)
        if self.cookies:
            # Sent by hand: http.cookiejar never returns Secure (__Host-) cookies over plain
            # http, and on the box the API is reached on http://127.0.0.1:8000.
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if unsafe and csrf and self.csrf:
            h["x-csrf-token"] = self.csrf
        return h

    def req(
        self, method: str, path: str, *, csrf: bool = True, auth: bool = True, **kw: Any
    ) -> httpx.Response:
        unsafe = method.upper() not in ("GET", "HEAD", "OPTIONS")
        headers = self._headers(unsafe, csrf) if auth else dict(self.extra_headers)
        headers.update(kw.pop("headers", {}))
        r = self.http.request(method, self.base + path, headers=headers, **kw)
        self.http.cookies.clear()  # only our own jar is ever sent (auth=False means none)
        if auth:
            self.cookies.update(dict(r.cookies.items()))
        return r

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.req("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.req("POST", path, **kw)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {}


class Acceptance:
    def __init__(self, s: Session, email: str, *, heavy: bool, wait_s: float) -> None:
        self.s = s
        self.email = email
        self.heavy = heavy
        self.wait_s = wait_s
        self.card: dict[str, Any] | None = None
        self.agents: dict[str, Any] | None = None
        self.tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    # --- helpers ------------------------------------------------------------------------

    def integrations(self) -> dict[str, Any]:
        if self.card is None:
            r = self.s.get("/api/v1/system/integrations")
            check(r.status_code == 200, f"integrations card answered {r.status_code}")
            self.card = _json(r)["integrations"]
        return self.card

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.agents is None:
            r = self.s.get("/api/agents")
            check(r.status_code == 200, f"/api/agents answered {r.status_code}")
            body = _json(r)
            items = body.get("agents", body.get("items", body)) if isinstance(body, dict) else body
            self.agents = {a["id"]: a for a in items}
        return self.agents.get(agent_id)

    def alive(self, agent_id: str) -> bool:
        a = self.agent(agent_id)
        return bool(a) and a["state"] not in ("stale", "stopped", "error", "paused")

    def poll(self, path: str, done: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        deadline = time.monotonic() + self.wait_s
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            body = _json(self.s.get(path))
            if done(body):
                return body
            time.sleep(3)
        raise Fail(f"timed out after {self.wait_s:.0f}s waiting on {path}: {body.get('status')}")

    # --- the 16 steps ---------------------------------------------------------------------

    def step_health(self) -> str:
        r = self.s.get("/api/health", auth=False)
        check(r.status_code == 200 and _json(r).get("status") == "ok", f"health {r.status_code}")
        r = self.s.get("/api/health/ready", auth=False)
        if r.status_code == 404:
            return "health ok; ready is on-box only here (nginx 404)"
        check(r.status_code == 200, f"ready answered {r.status_code}: {r.text[:200]}")
        checks = _json(r).get("checks", {})
        bad = [k for k, v in checks.items() if not v.get("ok")]
        check(not bad, f"not ready: {bad}")
        return f"health ok; ready ok ({', '.join(sorted(checks))})"

    def step_login_csrf(self) -> str:
        r = self.s.post(
            "/api/auth/login", auth=False, json={"email": self.email, "password": self.s.password}
        )
        check(r.status_code == 200, f"login answered {r.status_code}")
        self.s.csrf = _json(r).get("csrf_token", "")
        self.s.cookies = {k: v for k, v in r.cookies.items()}
        check(self.s.csrf and self.s.cookies, "no session/CSRF cookie in the login answer")
        me = _json(self.s.get("/api/auth/me"))
        role = me.get("role") or (me.get("user") or {}).get("role")
        check((me and role in ("admin", None)) or me.get("is_admin"), f"not an admin: {role}")
        r = self.s.post("/api/auth/sessions/revoke-all", csrf=False)
        check(r.status_code == 403, f"unsafe request without CSRF answered {r.status_code}")
        anon = self.s.get("/api/auth/me", auth=False)
        check(anon.status_code == 401, f"/me without a session answered {anon.status_code}")
        return "login 200, CSRF enforced (403 without the header), 401 without a session"

    def step_finance(self) -> str:
        eid = f"acceptance-{self.tag}"
        body = {"id": eid, "date": "2026-11-02", "amount": "12.34", "category": "acceptance"}
        r = self.s.post("/api/finance/expenses", json=body)
        check(r.status_code == 201, f"create answered {r.status_code}: {r.text[:200]}")
        check(_json(r)["amount"] == "12.34", "amount not exact")
        try:
            r = self.s.req("PATCH", f"/api/finance/expenses/{eid}", json={"note": "acceptance"})
            check(
                r.status_code == 200 and _json(r)["note"] == "acceptance", f"patch {r.status_code}"
            )
            got = self.s.get(f"/api/finance/expenses/{eid}")
            check(got.status_code == 200, f"read answered {got.status_code}")
            # Ownership: another owner's (or a missing) row is a 404, never someone's data;
            # no session at all is a 401.
            other = self.s.get(f"/api/finance/expenses/not-mine-{uuid.uuid4().hex[:8]}")
            check(other.status_code == 404, f"foreign id answered {other.status_code}")
            anon = self.s.get(f"/api/finance/expenses/{eid}", auth=False)
            check(anon.status_code == 401, f"anonymous read answered {anon.status_code}")
        finally:
            d = self.s.req("DELETE", f"/api/finance/expenses/{eid}")
        check(d.status_code == 204, f"delete answered {d.status_code}")
        return "create/read/patch/delete with exact decimals; foreign id 404; anonymous 401"

    def _paper_account(self) -> dict[str, Any]:
        r = self.s.post(
            "/api/trading/accounts",
            json={
                "broker": "mt5",
                "account_id": "ACCEPTANCE-PAPER",
                "base_currency": "USD",
                "label": "acceptance (paper only)",
            },
        )
        if r.status_code == 409:
            items = _json(self.s.get("/api/trading/accounts", params={"limit": 200}))["items"]
            acct = next(a for a in items if a["account_id"] == "ACCEPTANCE-PAPER")
        else:
            check(r.status_code == 201, f"account create answered {r.status_code}: {r.text[:200]}")
            acct = _json(r)
        if acct["mode"] != "PAPER":
            r = self.s.post(f"/api/trading/accounts/{acct['id']}/mode", json={"mode": "PAPER"})
            check(r.status_code == 200, f"PAPER mode answered {r.status_code}: {r.text[:200]}")
            acct = _json(r)
        r = self.s.req(
            "PUT",
            f"/api/trading/accounts/{acct['id']}/risk-policy",
            json={
                "max_daily_loss": "100",
                "max_exposure": "100000",
                "max_position_size": "1",
                "max_drawdown_pct": "10",
                "allowed_sessions": ["Asia", "London", "NY"],
            },
        )
        check(r.status_code == 200, f"risk policy answered {r.status_code}: {r.text[:200]}")
        return acct

    def step_paper_trade(self) -> str:
        acct = self._paper_account()
        inst_env = os.environ.get("AUREON_ACCEPTANCE_INSTRUMENT_ID")
        if inst_env:
            inst = int(inst_env)  # an mt5 instrument with a live price (the bot's last close)
        else:
            r = self.s.req(
                "PUT",
                "/api/trading/mt5/instruments/ACCEPTANCE",
                json={
                    "lot_size": "0.01",
                    "tick_size": "0.01",
                    "currency": "USD",
                    "digits": 2,
                    "contract_size": "1",
                },
            )
            check(r.status_code == 200, f"instrument answered {r.status_code}: {r.text[:200]}")
            inst = _json(r)["id"]
        r = self.s.post(
            "/api/trading/order-intents",
            json={
                "account_id": acct["id"],
                "instrument_id": inst,
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": "0.01",
                "limit_price": "1.00",
                "correlation_id": f"acceptance-{self.tag}",
            },
        )
        check(r.status_code in (200, 201), f"intent answered {r.status_code}: {r.text[:300]}")
        intent = _json(r)
        if intent["status"] == "REJECTED":
            failed = [
                c["name"]
                for d in intent.get("risk_decisions", [])
                for c in d.get("checks", [])
                if not c.get("ok")
            ]
            if failed == ["price_fresh"]:
                raise Skip(
                    "risk check refused it (no fresh price: fail-closed); set "
                    "AUREON_ACCEPTANCE_INSTRUMENT_ID to an mt5 instrument with a live "
                    "price"
                )
            raise Fail(f"intent REJECTED by risk: {failed}")
        check(intent["status"] == "PENDING_RISK", f"intent is {intent['status']}")
        url = f"/api/trading/order-intents/{intent['id']}/approve"
        filed = self.s.post(url)
        check(filed.status_code == 202, f"approve without a password answered {filed.status_code}")
        approval = (
            _json(filed)["approval_id"] if "approval_id" in _json(filed) else _json(filed).get("id")
        )
        wrong = self.s.post(url, json={"password": "wrong-" + uuid.uuid4().hex})
        check(wrong.status_code in (401, 403), f"wrong password answered {wrong.status_code}")
        ok = self.s.post(url, json={"password": self.s.password})
        check(ok.status_code == 200, f"approve with re-auth answered {ok.status_code}: {ok.text}")
        status = _json(ok)["status"]
        check(status in ("FILLED", "SUBMITTED", "REJECTED"), f"intent ended {status}")
        return f"approval {approval} filed (202), wrong password refused, re-auth -> {status}"

    def step_live_unreachable(self) -> str:
        acct = self._paper_account()
        tok = self.s.post(f"/api/trading/accounts/{acct['id']}/mode/live-token")
        check(tok.status_code >= 400, f"a LIVE token was issued ({tok.status_code})")
        live = self.s.post(f"/api/trading/accounts/{acct['id']}/mode", json={"mode": "LIVE"})
        check(live.status_code >= 400, f"switching to LIVE answered {live.status_code}")
        code = (_json(live).get("detail") or {}).get("code")
        after = _json(self.s.get(f"/api/trading/accounts/{acct['id']}"))["mode"]
        check(after != "LIVE", "the account is LIVE")
        return f"live-token {tok.status_code}, mode LIVE {live.status_code} ({code}); still {after}"

    def step_mt5_fresh(self) -> str:
        mt5 = self.integrations()["mt5"]
        if mt5["status"] == "not_configured":
            raise Skip("MT5_STATUS_FILE / MT5_STATUS_URL not set (no bot export here)")
        check(mt5["status"] == "ok" and mt5["fresh"], f"MT5 feed {mt5['agent']}")
        st = self.s.get("/api/trading/mt5/status")
        check(st.status_code == 200 and _json(st).get("ok"), f"mt5/status {st.status_code}")
        return f"mt5-bot heartbeat {mt5['agent']['age_s']}s old (window {mt5['stale_after_s']}s)"

    def step_dhan_read(self) -> str:
        dhan = self.integrations()["dhan"]
        if dhan["status"] == "not_configured":
            raise Skip("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set")
        items = _json(self.s.get("/api/trading/accounts", params={"limit": 200}))["items"]
        accts = [a for a in items if a["broker"] == "dhan"]
        if not accts:
            raise Skip("no Dhan account registered")
        h = _json(self.s.get(f"/api/trading/accounts/{accts[0]['id']}/health"))
        check(h.get("ok"), f"Dhan health: {h}")
        pos = self.s.get(f"/api/trading/accounts/{accts[0]['id']}/positions")
        check(pos.status_code == 200, f"positions answered {pos.status_code}")
        return f"health ok, {_json(pos)['total']} position(s) read"

    def step_chat(self) -> str:
        models = _json(self.s.get("/api/models"))
        names = [
            m["name"] for m in (models if isinstance(models, list) else models.get("models", []))
        ]
        if not names:
            raise Skip("Ollama has no models here (or is unreachable)")
        if not self.heavy:
            raise Skip("--skip-heavy")
        model = os.environ.get("AUREON_ACCEPTANCE_CHAT_MODEL") or names[0]
        r = self.s.post(
            "/api/chat",
            json={
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with the word ok."}],
            },
            timeout=900,
        )
        check(r.status_code == 200, f"chat answered {r.status_code}: {r.text[:200]}")
        events = [ln for ln in r.text.splitlines() if ln.startswith("event: ")]
        check(
            any("token" in e for e in events) and any("done" in e for e in events),
            f"no token/done events: {events[:5]}",
        )
        return f"{model}: {sum('token' in e for e in events)} token event(s), done"

    def step_image_job(self) -> str:
        if not self.alive("imaging"):
            raise Skip("the image worker is not running (profile imaging)")
        if not self.heavy:
            raise Skip("--skip-heavy")
        r = self.s.post(
            "/api/images/generate",
            json={"prompt": "acceptance test, a blue square", "size": "512x512", "steps": 4},
        )
        check(r.status_code == 202, f"generate answered {r.status_code}: {r.text[:200]}")
        job = _json(r)["id"]
        body = self.poll(
            f"/api/images/jobs/{job}", lambda b: b.get("status") in ("done", "failed", "cancelled")
        )
        check(body["status"] == "done", f"job ended {body['status']}: {body.get('error')}")
        return f"job {job} done"

    def step_coding_task(self) -> str:
        repo = os.environ.get("AUREON_ACCEPTANCE_CODE_REPO")
        if not self.alive("coding"):
            raise Skip("the coding worker is not running (profile coding)")
        if not repo:
            raise Skip("AUREON_ACCEPTANCE_CODE_REPO not set")
        if not self.heavy:
            raise Skip("--skip-heavy")
        r = self.s.post(
            "/api/code/tasks",
            json={
                "repo": repo,
                "title": f"acceptance {self.tag}",
                "paths": ["README.md"],
                "instruction": "Append one line 'acceptance check' to the end of README.md.",
            },
        )
        check(r.status_code == 202, f"task answered {r.status_code}: {r.text[:200]}")
        tid = _json(r)["id"]
        body = self.poll(
            f"/api/code/tasks/{tid}", lambda b: b.get("status") not in ("queued", "running")
        )
        self.s.post(f"/api/code/tasks/{tid}/reject", json={"reason": "acceptance run"})
        check(body["status"] in ("ready", "tests_failed"), f"task ended {body['status']}")
        return f"task {tid} -> {body['status']} (then rejected; nothing reached a branch)"

    def step_github(self) -> str:
        if self.integrations()["github"]["status"] == "not_configured":
            raise Skip("GITHUB_TOKEN (PAT) not set")
        conn = _json(self.s.post("/api/v1/github/connection/check"))
        check(conn.get("ok") is True, f"connection check: ok={conn.get('ok')} {conn.get('detail')}")
        repos = _json(self.s.get("/api/v1/github/repositories"))["items"]
        synced = ""
        if not repos:
            # The list is the stored sync (GITHUB_SYNC_CRON, every 20 min; none yet right after
            # the token is added): sync once now, as the UI's refresh does.
            repos = _json(self.s.get("/api/v1/github/repositories", params={"refresh": "true"}))[
                "items"
            ]
            synced = " (synced now)"
        check(repos, "no repositories visible (check GITHUB_ALLOWED_OWNERS / _REPOS)")
        owner, name = repos[0]["full_name"].split("/", 1)
        tree = self.s.get(f"/api/v1/github/repositories/{owner}/{name}/tree")
        check(tree.status_code == 200, f"tree answered {tree.status_code}")
        return f"{len(repos)} repo(s){synced}; browsed {owner}/{name}"

    def step_pr_merge_gate(self) -> str:
        if self.integrations()["github"]["status"] == "not_configured":
            raise Skip("GITHUB_TOKEN (PAT) not set")
        target = os.environ.get("AUREON_ACCEPTANCE_PR")
        if not target or "#" not in target:
            raise Skip("AUREON_ACCEPTANCE_PR=owner/repo#n not set (an open test PR)")
        repo, n = target.split("#", 1)
        r = self.s.post(f"/api/v1/github/repositories/{repo}/pulls/{n}/merge", json={})
        check(r.status_code == 202, f"merge without approval answered {r.status_code}: {r.text}")
        aid = _json(r).get("approval_id") or _json(r).get("id")
        wrong = self.s.post(
            f"/api/v1/approvals/{aid}/approve", json={"password": "wrong-" + uuid.uuid4().hex}
        )
        check(
            wrong.status_code in (401, 403), f"approve with a wrong password: {wrong.status_code}"
        )
        deny = self.s.post(f"/api/v1/approvals/{aid}/deny", json={"reason": "acceptance run"})
        check(deny.status_code == 200, f"deny answered {deny.status_code}")
        pr = _json(self.s.get(f"/api/v1/github/repositories/{repo}/pulls/{n}"))
        check(not pr.get("merged"), "the PR was merged")
        return f"merge filed approval {aid} (202), wrong re-auth refused, denied; PR not merged"

    def step_webhook(self) -> str:
        path = "/api/v1/github/webhooks"
        body = json.dumps({"zen": "acceptance", "hook_id": 1}).encode()
        hdr = {"content-type": "application/json", "x-github-event": "ping"}
        bad = self.s.post(
            path,
            auth=False,
            content=body,
            headers={
                **hdr,
                "x-github-delivery": str(uuid.uuid4()),
                "x-hub-signature-256": "sha256=00",
            },
        )
        if bad.status_code == 503:
            raise Skip("GITHUB_WEBHOOK_SECRET not set on the server")
        check(bad.status_code == 401, f"bad signature answered {bad.status_code}")
        secret = os.environ.get("AUREON_ACCEPTANCE_WEBHOOK_SECRET")
        if not secret:
            raise Skip(
                "bad signature refused (401); AUREON_ACCEPTANCE_WEBHOOK_SECRET not set "
                "for the idempotency half"
            )
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        delivery = str(uuid.uuid4())
        h = {**hdr, "x-github-delivery": delivery, "x-hub-signature-256": sig}
        first = self.s.post(path, auth=False, content=body, headers=h)
        again = self.s.post(path, auth=False, content=body, headers=h)
        check(first.status_code == 202, f"signed delivery answered {first.status_code}")
        check(
            again.status_code == 200 and _json(again).get("status") == "duplicate",
            f"replay answered {again.status_code} {_json(again)}",
        )
        return "bad HMAC 401; signed 202; replay 200 duplicate"

    def step_repair_pr(self) -> str:
        st = _json(self.s.get("/api/v1/repairs/status"))
        if not st.get("enabled"):
            raise Skip(f"self-repair disabled: {st.get('reason')}")
        items = _json(self.s.get("/api/v1/repairs", params={"status": "pr_opened"})).get(
            "items", []
        )
        if not items:
            raise Skip("no repair has opened a PR yet")
        merge = self.s.post(f"/api/v1/repairs/{items[0]['id']}/merge")
        check(merge.status_code in (404, 405), f"a repair merge route exists ({merge.status_code})")
        return f"{len(items)} repair PR(s), e.g. {items[0].get('pr_url')}; no merge route"

    def step_claude_review(self) -> str:
        cl = self.integrations()["claude_cli"]
        if cl["status"] == "disabled":
            raise Skip("CLAUDE_REVIEW_ENABLED=false")
        if cl["status"] != "ok":
            raise Skip(f"Claude CLI unavailable: {cl.get('reason')}")
        reviews = _json(self.s.get("/api/v1/reviews", params={"limit": 20})).get("items", [])
        if not reviews:
            raise Skip("CLI available; no review has run yet")
        check(all(r.get("advisory") is True for r in reviews), "a review is not advisory")
        return f"{len(reviews)} review(s), all advisory (published as COMMENT only)"

    def step_backup_drill(self) -> str:
        b = self.integrations()["backups"]
        if b["status"] == "unknown":
            raise Skip(f"backup status file not readable ({b.get('reason')})")
        check(b["status"] == "ok", f"backups: {b.get('problems')}")
        drills = os.environ.get("AUREON_ACCEPTANCE_DRILL_DIR")
        if not drills:
            return f"last backup {b['last_success_backup']} ({b['age_hours']} h); drill not checked"
        reports = sorted(Path(drills).glob("drill-*.json"))
        check(reports, f"no restore drill report in {drills}")
        last = json.loads(reports[-1].read_text())
        check(last.get("passed"), f"last drill failed: {reports[-1].name}")
        return f"backup {b['last_success_backup']}; drill {reports[-1].name} passed"

    # --- runner -------------------------------------------------------------------------

    def run(self, only: set[str] | None) -> list[Result]:
        out: list[Result] = []
        logged_in = False
        for n, (sid, name) in enumerate(STEPS, 1):
            if only and sid not in only and sid not in ("login_csrf",):
                continue
            if sid not in ("health", "login_csrf") and not logged_in:
                out.append(Result(n, sid, name, "FAIL", "not logged in", 0.0))
                continue
            t0 = time.monotonic()
            try:
                detail = getattr(self, f"step_{sid}")()
                status = "PASS"
                if sid == "login_csrf":
                    logged_in = True
            except Skip as exc:
                status, detail = "SKIP", str(exc)
            except Fail as exc:
                status, detail = "FAIL", str(exc)
            except Exception as exc:
                status = "FAIL"
                detail = f"{type(exc).__name__}: {exc}"[:300]
                if os.environ.get("AUREON_ACCEPTANCE_DEBUG"):
                    traceback.print_exc()
            res = Result(n, sid, name, status, detail, round(time.monotonic() - t0, 2))
            out.append(res)
            print(f"{n:2d}. {status:4s}  {name:42s} {detail}", flush=True)
        return out


def read_password() -> str:
    pw = os.environ.get("AUREON_ACCEPTANCE_PASSWORD")
    if pw:
        return pw
    if sys.stdin.isatty():
        return getpass.getpass("admin password: ")
    return sys.stdin.readline().rstrip("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--report", default="acceptance-report.json")
    ap.add_argument("--only", help="comma list of step ids")
    ap.add_argument("--skip-heavy", action="store_true", help="never start a model/GPU job")
    ap.add_argument("--wait", type=float, default=900.0, help="seconds to wait on a job")
    args = ap.parse_args(argv)
    email = os.environ.get("AUREON_ACCEPTANCE_EMAIL", "")
    if not email:
        print(
            "set AUREON_ACCEPTANCE_EMAIL (the password comes from the env or stdin)",
            file=sys.stderr,
        )
        return 2
    password = read_password()
    extra = {}
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        extra = {
            "CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"],
            "CF-Access-Client-Secret": os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
        }
    with httpx.Client(timeout=60.0, follow_redirects=False) as http:
        s = Session(args.base_url.rstrip("/"), http, password, extra_headers=extra)
        acc = Acceptance(s, email, heavy=not args.skip_heavy, wait_s=args.wait)
        only = set(args.only.split(",")) if args.only else None
        results = acc.run(only)
    counts = {k: sum(r.status == k for r in results) for k in ("PASS", "FAIL", "SKIP")}
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "counts": counts,
        "results": [asdict(r) for r in results],
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"\nPASS {counts['PASS']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}  -> {args.report}"
    )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
