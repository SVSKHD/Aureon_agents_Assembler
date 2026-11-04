"""Every route's auth dependency, enumerated (docs/17-security-review.md §1).

- Every route except an explicit public allowlist requires a signed-in user.
- Every non-GET route requires CSRF (or is public by design).
- The set of admin-only routes is pinned: adding or dropping one fails this test, so the
  change is reviewed on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.main import create_app
from tests.routes import api_routes, other_paths

PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/health/ready"),
    ("POST", "/api/auth/login"),  # rate-limited per (email, IP) and per IP
    # Read-only public data for the frontend, rate-limited per IP (docs/02, D-backend).
    ("GET", "/api/public/shares/{doc_id}"),
    ("GET", "/api/public/forex"),
    # GitHub webhook receiver: authenticated by its HMAC signature instead (docs/20).
    ("POST", "/api/v1/github/webhooks"),
    # MT5 bot push: machine-to-machine, bearer MT5_INGEST_TOKEN instead (docs/12 §3).
    ("POST", "/api/trading/mt5/ingest"),
}
# Public routes that change state, each with its own authentication.
PUBLIC_UNSAFE = {"/api/auth/login", "/api/v1/github/webhooks", "/api/trading/mt5/ingest"}
# Public routes that must carry the per-IP limiter dependency.
PUBLIC_RATE_LIMITED = {p for m, p in PUBLIC if p.startswith("/api/public/")}

ADMIN = {
    ("GET", "/api/v1/settings"),
    ("PUT", "/api/v1/settings/{key}"),
    ("DELETE", "/api/v1/settings/{key}"),
    ("POST", "/api/v1/settings/{key}/test"),
    ("PUT", "/api/finance/portfolio-profiles/{profile_id}"),
    ("POST", "/api/finance/claim"),
    ("GET", "/api/agents/{agent_id}/logs"),
    ("POST", "/api/agents/{agent_id}/pause"),
    ("POST", "/api/agents/{agent_id}/resume"),
    ("GET", "/api/scheduler/jobs"),
    ("POST", "/api/scheduler/jobs/{job_id}/pause"),
    ("POST", "/api/scheduler/jobs/{job_id}/resume"),
    ("POST", "/api/scheduler/jobs/{job_id}/run"),
    ("GET", "/api/system/status"),
    ("GET", "/api/v1/system/integrations"),
    ("POST", "/api/trading/accounts"),
    ("POST", "/api/trading/accounts/{account_id}/mode/live-token"),
    ("POST", "/api/trading/accounts/{account_id}/mode"),
    ("PUT", "/api/trading/accounts/{account_id}/risk-policy"),
    ("GET", "/api/trading/agents/{agent_id}/logs"),
    ("PUT", "/api/trading/mt5/instruments/{symbol}"),
    ("POST", "/api/trading/strategies"),
    ("POST", "/api/trading/order-intents/{intent_id}/approve"),
    ("POST", "/api/trading/accounts/{account_id}/paper/reconcile"),
    ("GET", "/api/repos"),
    ("GET", "/api/repos/{name}/branches"),
    ("GET", "/api/repos/{name}/tree"),
    ("GET", "/api/repos/{name}/file"),
    ("GET", "/api/repos/{name}/commits"),
    ("POST", "/api/code/analyze"),
    ("POST", "/api/code/tasks"),
    ("GET", "/api/code/tasks"),
    ("GET", "/api/code/tasks/{task_id}"),
    ("GET", "/api/code/tasks/{task_id}/diff"),
    ("POST", "/api/code/tasks/{task_id}/approve"),
    ("POST", "/api/code/tasks/{task_id}/reject"),
    ("GET", "/api/v1/github/actions/monitor"),
    ("GET", "/api/code/git/repos"),
    ("POST", "/api/code/git/clone"),
    ("POST", "/api/code/git/pull"),
    ("POST", "/api/code/git/push"),
    ("GET", "/api/code/git/ops"),
    ("GET", "/api/code/git/ops/{op_id}"),
    # Phase 15 GitHub foundation: admin only, GETs included (docs/18-github-foundation.md).
    ("GET", "/api/v1/github/connection"),
    ("POST", "/api/v1/github/connection/check"),
    ("GET", "/api/v1/github/repositories"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/branches"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/commits"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/tree"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/contents/{path:path}"),
    # Phase 16 issues + PRs + approval gate: admin only (docs/19-github-issues-prs.md).
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/issues"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}"),
    ("PATCH", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/close"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/reopen"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/comments"),
    ("PATCH", "/api/v1/github/repositories/{owner}/{repo}/issues/comments/{comment_id}"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/issues/comments/{comment_id}"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/labels"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/labels/{name}"),
    ("PUT", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/lock"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/lock"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/links"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/issues/{n}/links"),
    ("GET", "/api/v1/github/links"),
    ("GET", "/api/v1/github/operations"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}"),
    ("PATCH", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/files"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/diff"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/commits"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/merge-status"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/comments"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/comments"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/issue-comments"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/issue-comments"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/reviews"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/reviews"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/merge"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/force-push"),
    ("PUT", "/api/v1/github/repositories/{owner}/{repo}/branch-protection/{branch:path}"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/branch-protection/{branch:path}"),
    ("DELETE", "/api/v1/github/repositories/{owner}/{repo}/branches/{branch:path}"),
    ("PATCH", "/api/v1/github/repositories/{owner}/{repo}/settings"),
    ("PUT", "/api/v1/github/repositories/{owner}/{repo}/actions-permissions"),
    ("GET", "/api/v1/approvals"),
    ("GET", "/api/v1/approvals/{approval_id}"),
    ("POST", "/api/v1/approvals/{approval_id}/approve"),
    ("POST", "/api/v1/approvals/{approval_id}/deny"),
    # Phase 20 approval engine, audit log and voice navigation (docs/23-approval-engine.md).
    ("GET", "/api/v1/approvals/inbox"),
    ("GET", "/api/v1/approvals/stream"),
    ("GET", "/api/v1/approvals/policies"),
    ("PUT", "/api/v1/approvals/policies"),
    ("POST", "/api/v1/approvals/reauth"),
    ("DELETE", "/api/v1/approvals/reauth"),
    ("GET", "/api/v1/approvals/{approval_id}/events"),
    ("GET", "/api/v1/audit/events"),
    ("POST", "/api/v1/voice/intent"),
    # Phase 18 incidents + self-repair: admin only (docs/21-self-repair.md).
    ("GET", "/api/v1/repairs/status"),
    ("GET", "/api/v1/repairs/incidents"),
    ("GET", "/api/v1/repairs/incidents/{incident_id}"),
    ("POST", "/api/v1/repairs/incidents/{incident_id}/resolve"),
    ("POST", "/api/v1/repairs/incidents/{incident_id}/ignore"),
    ("GET", "/api/v1/repairs"),
    ("GET", "/api/v1/repairs/{repair_id}"),
    ("POST", "/api/v1/repairs"),
    ("POST", "/api/v1/repairs/{repair_id}/cancel"),
    ("POST", "/api/v1/repairs/{repair_id}/retry"),
    # Phase 17 Actions: admin only (docs/20-github-actions-webhooks.md).
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/workflows"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/runs"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}/jobs"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}/logs"),
    ("GET", "/api/v1/github/repositories/{owner}/{repo}/actions/jobs/{job_id}/logs"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/actions/workflows/{workflow}/dispatch"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}/rerun"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}/rerun-failed"),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/actions/runs/{run_id}/cancel"),
    # Phase 19 Claude review: admin only (docs/22-claude-review.md).
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/claude-review"),
    ("GET", "/api/v1/reviews"),
    ("GET", "/api/v1/reviews/{review_id}"),
    ("POST", "/api/v1/reviews/{review_id}/publish"),
}

SAFE = {"GET", "HEAD", "OPTIONS"}


def _pairs(settings: Settings) -> list[tuple[str, str, frozenset[str]]]:
    app = create_app(settings)
    routes = api_routes(app)
    assert len(routes) > 50, "route walk found too few routes"
    return [(m, r.path, r.deps) for r in routes for m in r.methods if m != "HEAD"]


def test_every_route_requires_auth_except_the_public_allowlist(settings: Settings) -> None:
    pairs = _pairs(settings)
    found = {(m, p) for m, p, _ in pairs}
    assert found >= PUBLIC, f"public routes missing: {PUBLIC - found}"
    unauthenticated = sorted(
        (m, p) for m, p, deps in pairs if (m, p) not in PUBLIC and "get_current" not in deps
    )
    assert unauthenticated == [], unauthenticated
    # The allowlist itself stays unauthenticated (login must work signed out).
    for m, p, deps in pairs:
        if (m, p) in PUBLIC:
            assert "get_current" not in deps, (m, p)
            assert m in SAFE or p in PUBLIC_UNSAFE, (m, p)
        if p in PUBLIC_RATE_LIMITED:
            assert "rate_limited" in deps, (m, p)


def test_every_unsafe_route_requires_csrf(settings: Settings) -> None:
    missing = sorted(
        (m, p)
        for m, p, deps in _pairs(settings)
        if m not in SAFE and (m, p) not in PUBLIC and "require_csrf" not in deps
    )
    assert missing == [], f"state-changing routes without CSRF: {missing}"


def test_admin_routes_are_pinned(settings: Settings) -> None:
    admin = {(m, p) for m, p, deps in _pairs(settings) if "require_admin" in deps}
    assert admin == ADMIN, {"unexpected": admin - ADMIN, "missing": ADMIN - admin}


def test_no_websocket_or_mounted_apps(settings: Settings) -> None:
    app = create_app(settings)
    extra = [
        p for p in other_paths(app) if not p.startswith(("/api/docs", "/api/openapi", "/docs"))
    ]
    assert extra == []


def test_prod_app_has_no_docs_or_openapi(data_root: Path) -> None:
    prod = Settings(
        ENV="prod",
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        SECRET_KEY="Zq8-vR2_kLm4Np7Xs1Tb9Wc3Yd6Fg0Hj5Ae",
        DATA_ROOT=data_root,
    )
    app = create_app(prod)
    assert other_paths(app) == []
    assert app.openapi_url is None and app.docs_url is None


@pytest.mark.parametrize("path", ["/api/openapi.json", "/api/docs"])
async def test_docs_answer_outside_prod(client: object, path: str) -> None:
    r = await client.get(path)  # type: ignore[attr-defined]
    assert r.status_code == 200
