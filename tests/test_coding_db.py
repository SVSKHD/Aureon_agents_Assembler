"""Coding agent end to end: a real git repo on disk, a fake model, the API and the worker.

Covers worktree isolation (the main checkout never changes), path escapes in a patch,
writing through a linked directory, non-allowlisted test commands, approval rules
(protected branches, failing tests) and the audit trail."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.agents import CodeTask
from app.models.auth import AuditEvent, User
from app.services import coding
from app.workers.coding import recover

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "demo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "hello.txt").write_text("hello\n")
    (r / ".env").write_text("SECRET=1\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "dep.js").write_text("module.exports = 1\n")
    (r / ".gitignore").write_text("node_modules\n")
    git(r, "init", "-q", "-b", "dev01")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    # A hook that would leave a mark if the agent ever ran repo hooks.
    hook = r / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/HOOK_RAN\n")
    hook.chmod(0o755)
    (tmp_path / "outside.txt").write_text("untouched\n")
    return r


PATCH_OK = """I changed the greeting.

```diff
diff --git a/src/hello.txt b/src/hello.txt
--- a/src/hello.txt
+++ b/src/hello.txt
@@ -1 +1 @@
-hello
+hello, world
```

The greeting now names the world."""

PATCH_ESCAPE = """```diff
--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-untouched
+pwned
```"""

PATCH_LINKED = """```diff
--- a/node_modules/dep.js
+++ b/node_modules/dep.js
@@ -1 +1 @@
-module.exports = 1
+module.exports = 2
```"""


class FakeOllama:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[list[dict[str, str]]] = []

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.prompts.append(messages)
        half = len(self.answer) // 2
        yield {"message": {"content": self.answer[:half]}, "done": False}
        yield {"message": {"content": self.answer[half:]}, "done": True}


class Env:
    def __init__(self, settings: Settings, maker: Any, repo: Path, tmp: Path) -> None:
        self.settings, self.maker, self.repo, self.tmp = settings, maker, repo, tmp

    @property
    def cfg(self) -> coding.CodingConfig:
        return coding.CodingConfig.from_settings(self.settings)

    async def run_next(self, answer: str) -> CodeTask:
        tid = await coding.claim(self.maker)
        assert tid is not None
        await coding.run_task(self.maker, self.cfg, FakeOllama(answer), tid)
        async with self.maker() as db:
            t = await db.get(CodeTask, tid)
            assert t
            return t


def make_settings(
    base: Settings, db_url: str, repo: Path, tmp: Path, tests: list[list[str]]
) -> Settings:
    return Settings.model_validate(
        {
            **base.model_dump(),
            "DATABASE_URL": db_url,
            "CODE_MODEL": "qwen2.5-coder:7b",
            "CODE_WORKTREE_ROOT": str(tmp / "worktrees"),
            "CODE_ALLOWED_COMMANDS": [["true"], ["false"], ["ls"]],
            "CODE_REPOS": {
                "demo": {
                    "path": str(repo),
                    "base": "dev01",
                    "tests": tests,
                    "link": ["node_modules"],
                }
            },
        }
    )


@pytest.fixture
async def env(db_clean: str, settings: Settings, repo: Path, tmp_path: Path) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM agents.code_tasks")
    admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    async with maker() as db, db.begin():
        db.add(User(email="user@spasta.online", password_hash=hash_password(PASSWORD), role_id=2))
    assert admin
    yield Env(make_settings(settings, db_clean, repo, tmp_path, [["true"]]), maker, repo, tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM agents.code_tasks")
    await engine.dispose()


async def client(env: Env, email: str = "admin@spasta.online") -> tuple[Any, AsyncClient, str]:
    mgr = LifespanManager(create_app(env.settings))
    await mgr.__aenter__()
    c = AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test")
    r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return mgr, c, r.json()["csrf_token"]


def main_checkout_untouched(repo: Path) -> None:
    assert (repo / "src" / "hello.txt").read_text() == "hello\n"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "dev01"


async def test_change_task_isolated_tested_and_approved(env: Env) -> None:
    mgr, c, csrf = await client(env)
    h = {"x-csrf-token": csrf}
    try:
        r = await c.post(
            "/api/code/tasks",
            headers=h,
            json={
                "repo": "demo",
                "title": "Greet the world",
                "instruction": "Say hello, world",
                "paths": ["src/hello.txt"],
            },
        )
        assert r.status_code == 202, r.text
        task = await env.run_next(PATCH_OK)
        assert task.status == "ready", task.error
        assert task.branch and task.branch.startswith("aureon/task-")
        assert task.tests[0]["ok"] and task.tests[0]["argv"] == ["true"]
        # The change exists only in the worktree, on the task branch.
        wt = Path(str(task.worktree))
        assert (wt / "src" / "hello.txt").read_text() == "hello, world\n"
        main_checkout_untouched(env.repo)
        assert not (env.tmp / "HOOK_RAN").exists()  # repo hooks never run
        # The linked node_modules is visible to tests but never committed.
        assert (wt / "node_modules").is_symlink()
        assert git(env.repo, "show", "--stat", "--format=", task.branch).count("|") == 1
        assert git(env.repo, "log", "-1", "--format=%an", task.branch) == "Aureon coding agent"

        d = (await c.get(f"/api/code/tasks/{task.id}/diff")).json()
        assert "+hello, world" in d["diff"] and d["files"] == ["src/hello.txt"]
        got = (await c.get(f"/api/code/tasks/{task.id}")).json()
        assert "names the world" in got["result"]["explanation"]

        # Approval can never target a protected branch.
        bad = await c.post(
            f"/api/code/tasks/{task.id}/approve", headers=h, json={"feature_branch": "dev01"}
        )
        assert bad.status_code == 409
        ok = await c.post(
            f"/api/code/tasks/{task.id}/approve",
            headers=h,
            json={"feature_branch": "feature/greet"},
        )
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        assert git(env.repo, "rev-parse", "feature/greet") == task.commit_sha
        assert not await asyncio.to_thread(wt.exists)  # worktree removed, branch kept
        assert git(env.repo, "rev-parse", "--verify", task.branch)
        main_checkout_untouched(env.repo)
        again = await c.post(f"/api/code/tasks/{task.id}/approve", headers=h)
        assert again.status_code == 409
        # Nothing was pushed: the repo has no remotes at all.
        assert git(env.repo, "remote") == ""
    finally:
        await c.aclose()
        await mgr.__aexit__(None, None, None)
    async with env.maker() as db:
        events = (await db.scalars(select(AuditEvent.event_type))).all()
    assert "code_change_queued" in events and "code_task_approved" in events


@pytest.mark.parametrize(
    ("answer", "why"), [(PATCH_ESCAPE, "'..'"), (PATCH_LINKED, "escapes the repository")]
)
async def test_escaping_patches_fail_and_leave_nothing(env: Env, answer: str, why: str) -> None:
    async with env.maker() as db, db.begin():
        db.add(CodeTask(kind="change", repo="demo", title="x", instruction="x", base_ref="dev01"))
    task = await env.run_next(answer)
    assert (
        task.status == "failed"
        and "patch refused" in (task.error or "")
        and why in (task.error or "")
    )
    assert (env.tmp / "outside.txt").read_text() == "untouched\n"
    assert (env.repo / "node_modules" / "dep.js").read_text() == "module.exports = 1\n"
    assert task.worktree is None and not list((env.tmp / "worktrees").glob("*"))
    assert "aureon/task-" not in git(env.repo, "branch")
    main_checkout_untouched(env.repo)


async def test_failing_tests_block_approval_and_reject_deletes(env: Env) -> None:
    env.settings = make_settings(
        env.settings, env.settings.DATABASE_URL.get_secret_value(), env.repo, env.tmp, [["false"]]
    )
    async with env.maker() as db, db.begin():
        db.add(CodeTask(kind="change", repo="demo", title="x", instruction="x", base_ref="dev01"))
    task = await env.run_next(PATCH_OK)
    assert task.status == "ready" and task.tests[0]["ok"] is False
    async with env.maker() as db, db.begin():
        t = await db.get(CodeTask, task.id)
        admin_id = await db.scalar(select(User.id).where(User.email == "admin@spasta.online"))
        assert t and admin_id
        with pytest.raises(coding.DecisionRefused, match="failed"):
            await coding.approve(db, env.cfg, t, admin_id, None)
        await coding.reject(db, env.cfg, t, admin_id)
    assert "aureon/task-" not in git(env.repo, "branch")
    main_checkout_untouched(env.repo)


def test_repo_naming_a_non_allowlisted_command_is_refused(env: Env) -> None:
    with pytest.raises(Exception, match="not allowlisted"):
        coding.CodingConfig.from_settings(
            make_settings(
                env.settings,
                env.settings.DATABASE_URL.get_secret_value(),
                env.repo,
                env.tmp,
                [["bash", "-c", "true"]],
            )
        )


async def test_analyze_and_repo_reads(env: Env) -> None:
    mgr, c, csrf = await client(env)
    h = {"x-csrf-token": csrf}
    try:
        repos = (await c.get("/api/repos")).json()
        assert repos[0]["name"] == "demo" and repos[0]["available"] and repos[0]["base"] == "dev01"
        tree = (await c.get("/api/repos/demo/tree")).json()
        names = {i["name"] for i in tree}
        assert "src" in names and ".env" not in names  # secrets hidden from listings
        f = (await c.get("/api/repos/demo/file", params={"path": "src/hello.txt"})).json()
        assert f["content"] == "hello\n" and not f["binary"]
        for bad in (".env", "../outside.txt", "/etc/passwd", ".git/config"):
            r = await c.get("/api/repos/demo/file", params={"path": bad})
            assert r.status_code == 422, bad
        assert (await c.get("/api/repos/other/tree")).status_code == 404
        assert (
            await c.get("/api/repos/demo/tree", params={"ref": "dev01..main"})
        ).status_code == 422
        commits = (await c.get("/api/repos/demo/commits")).json()
        assert commits[0]["subject"] == "init"

        r = await c.post(
            "/api/code/analyze",
            headers=h,
            json={
                "repo": "demo",
                "question": "What does hello.txt say?",
                "paths": ["src/hello.txt"],
            },
        )
        assert r.status_code == 202
        bad = await c.post(
            "/api/code/analyze",
            headers=h,
            json={"repo": "demo", "question": "x", "paths": ["../outside.txt"]},
        )
        assert bad.status_code == 422
    finally:
        await c.aclose()
        await mgr.__aexit__(None, None, None)
    fake = FakeOllama("It says hello.")
    tid = await coding.claim(env.maker)
    assert tid
    await coding.run_task(env.maker, env.cfg, fake, tid)
    async with env.maker() as db:
        t = await db.get(CodeTask, tid)
    assert t and t.status == "ready" and t.result["answer"] == "It says hello."
    prompt = fake.prompts[0][1]["content"]
    assert (
        "hello\n" in prompt and "SECRET" not in prompt and ".env" not in prompt.split("Question")[0]
    )


async def test_non_admin_is_refused(env: Env) -> None:
    mgr, c, csrf = await client(env, "user@spasta.online")
    try:
        assert (await c.get("/api/repos")).status_code == 403
        r = await c.post(
            "/api/code/tasks",
            headers={"x-csrf-token": csrf},
            json={"repo": "demo", "title": "x", "instruction": "x"},
        )
        assert r.status_code == 403
    finally:
        await c.aclose()
        await mgr.__aexit__(None, None, None)


async def test_crash_recovery_cleans_worktree(env: Env) -> None:
    async with env.maker() as db, db.begin():
        db.add(CodeTask(kind="change", repo="demo", title="x", instruction="x", base_ref="dev01"))
    tid = await coding.claim(env.maker)
    assert tid
    assert await recover(env.maker, env.cfg) == 1
    async with env.maker() as db:
        t = await db.get(CodeTask, tid)
    assert t and t.status == "failed" and "restarted" in (t.error or "")


async def test_model_not_configured(env: Env) -> None:
    s = Settings.model_validate({**env.settings.model_dump(), "CODE_MODEL": None})
    async with env.maker() as db, db.begin():
        db.add(CodeTask(kind="analyze", repo="demo", title="x", instruction="x", base_ref="dev01"))
    tid = await coding.claim(env.maker)
    assert tid
    status = await coding.run_task(
        env.maker, coding.CodingConfig.from_settings(s), FakeOllama("x"), tid
    )
    assert status == "failed"
