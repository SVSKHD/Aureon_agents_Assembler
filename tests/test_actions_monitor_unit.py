"""The combined Actions monitor (docs/20, "Actions monitor"): deploy runs and CI runs mapped
onto one row shape, statuses normalised, newest first, CI filtered by the GitHub allowlist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.models.agents import AgentRun
from app.models.github import GitHubWorkflowRun
from app.services import actions_monitor as mon

T0 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


def deploy(run_id: int, at: datetime, status: str = "succeeded", **details: Any) -> AgentRun:
    return AgentRun(
        id=run_id,
        agent_id="deployer",
        task="deploy:backend",
        status=status,
        started_at=at,
        finished_at=at + timedelta(seconds=42),
        duration_ms=42_000,
        error=None if status != "failed" else "health",
        details={"target": "backend", "sha": "a" * 40, "from_sha": "b" * 40, **details},
    )


def ci(
    run_id: int, at: datetime, repo: str = "SVSKHD/ASTRA-FRONTEND", **kw: Any
) -> GitHubWorkflowRun:
    values: dict[str, Any] = {
        "id": run_id,
        "repo": repo,
        "name": "CI",
        "event": "pull_request",
        "branch": "feat/x",
        "head_sha": "c" * 40,
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
        "created_at": at,
        "updated_at": at + timedelta(minutes=3),
    }
    values.update(kw)
    return GitHubWorkflowRun(**values)


def test_deploy_rows_carry_repo_branch_sha_and_link(settings: Settings) -> None:
    it = mon.deploy_item(settings, deploy(7, T0, rolled_back=True))
    assert it.kind == "deploy" and it.id == "deploy:7"
    assert (it.repo, it.branch) == ("SVSKHD/Aureon_agents_Assembler", "main")
    assert it.sha == "a" * 40 and it.status == "success" and it.duration_ms == 42_000
    assert it.url == f"https://github.com/SVSKHD/Aureon_agents_Assembler/commit/{'a' * 40}"
    assert it.rolled_back is True
    failed = mon.deploy_item(settings, deploy(8, T0, status="failed"))
    assert failed.status == "failure" and failed.error == "health"
    running = mon.deploy_item(settings, deploy(9, T0, status="running"))
    assert running.status == "running"


def test_unknown_deploy_target_has_no_repo(settings: Settings) -> None:
    r = deploy(1, T0)
    r.details = {"target": "elsewhere", "sha": "a" * 40}
    it = mon.deploy_item(settings, r)
    assert it.repo is None and it.url is None


def test_ci_rows_and_status_mapping() -> None:
    it = mon.ci_item(ci(123, T0))
    assert it.kind == "ci" and it.id == "ci:123" and it.status == "success"
    assert it.duration_ms == 180_000 and it.url and it.url.endswith("/123")
    assert mon.ci_item(ci(1, T0, status="in_progress", conclusion=None)).status == "running"
    assert mon.ci_item(ci(1, T0, status="in_progress", conclusion=None)).duration_ms is None
    assert mon.ci_item(ci(1, T0, status="queued", conclusion=None)).status == "queued"
    assert mon.ci_status("completed", "failure") == "failure"
    assert mon.ci_status("completed", "timed_out") == "failure"
    assert mon.ci_status("completed", "cancelled") == "cancelled"
    assert mon.ci_status(None, None) == "unknown"


class FakeDb:
    """Answers the two list queries in order and the login lookup."""

    def __init__(self, deploys: list[AgentRun], runs: list[GitHubWorkflowRun]) -> None:
        self.answers: list[list[Any]] = [deploys, runs]

    async def scalars(self, _stmt: Any) -> Any:
        rows = self.answers.pop(0)

        class R:
            def all(self) -> list[Any]:
                return rows

        return R()

    async def scalar(self, _stmt: Any) -> str:
        return "svskhd"


async def test_one_timeline_newest_first_and_allowlisted(settings: Settings) -> None:
    s = settings.model_copy(update={"GITHUB_ALLOWED_OWNERS": ["svskhd"]})
    db = FakeDb(
        [deploy(1, T0 + timedelta(minutes=5)), deploy(2, T0 - timedelta(hours=1))],
        [ci(10, T0), ci(11, T0 + timedelta(minutes=10)), ci(12, T0, repo="evil/repo")],
    )
    out = await mon.monitor(db, s, limit=10)  # type: ignore[arg-type]
    assert [i.id for i in out.items] == ["ci:11", "deploy:1", "ci:10", "deploy:2"]
    assert out.latest_at == T0 + timedelta(minutes=10)


async def test_limit_and_kind_filter(settings: Settings) -> None:
    s = settings.model_copy(update={"GITHUB_ALLOWED_OWNERS": ["svskhd"]})
    db = FakeDb([], [ci(n, T0 + timedelta(minutes=n)) for n in reversed(range(5))])  # DB order
    db.answers = [db.answers[1]]  # kind=ci: only the runs query is made
    out = await mon.monitor(db, s, limit=2, kind="ci")  # type: ignore[arg-type]
    assert [i.id for i in out.items] == ["ci:4", "ci:3"]
