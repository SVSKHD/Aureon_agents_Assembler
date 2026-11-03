"""Coding agent git ops (docs/05-agents.md, "Git ops"): the push branch rules, the repo
allowlist, the secrets-path refusal, and the token never reaching argv, git config or logs.

The git tests use a local bare repo as "GitHub" (file://, allowed only with ENV=test)."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from app.agents.coding import remote as remote_mod
from app.agents.coding.git import GitError
from app.agents.coding.remote import (
    ASKPASS_SCRIPT,
    TOKEN_ENV,
    PushRefused,
    RemoteGit,
    assert_no_credentials,
    check_push_branch,
    secret_paths,
)
from app.core.config import Settings
from app.integrations.github.client import GitHubNotFound
from app.models.agents import CodeGitOp, CodeTask
from app.models.auth import AuditEvent
from app.services import coding, coding_git
from app.services.audit import ClientInfo

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
PREFIXES = ("aureon/", "feat/")
PROTECTED = ("main", "master", "dev01", "dev", "develop", "production")


def rules(branch: str, **kw: Any) -> str:
    args: dict[str, Any] = {
        "default_branch": "trunk",
        "protected": PROTECTED,
        "prefixes": PREFIXES,
    }
    args.update(kw)
    return check_push_branch(branch, **args)


# --- branch refusal rules ------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["aureon/task-0123abcd", "feat/login-form", "aureon/x/y"])
def test_feature_branches_under_a_prefix_are_pushable(branch: str) -> None:
    assert rules(branch) == branch


@pytest.mark.parametrize(
    ("branch", "why"),
    [
        ("main", "never"),
        ("master", "never"),
        ("dev01", "never"),
        ("MAIN", "never"),
        ("Dev01", "never"),
        ("HEAD", "never"),
        ("trunk", "default branch"),
        ("develop", "protected"),
        ("production", "protected"),
        ("feature/x", "must start with"),
        ("fix/x", "must start with"),
        ("aureon/", "invalid"),
        ("feat", "must start with"),
        ("release/1.0", "must start with"),
        ("refs/heads/aureon/x", "invalid"),
        ("aureon/../main", "invalid"),
        ("aureon//x", "invalid"),
        ("aureon/x.lock", "invalid"),
        ("aureon/x@{1}", "invalid"),
        ("-aureon/x", "invalid"),
        ("aureon/x y", "invalid"),
        (" aureon/x", "invalid"),
        ("aureon/.hidden", "invalid"),
        ("", "invalid"),
    ],
)
def test_refused_branches(branch: str, why: str) -> None:
    with pytest.raises(PushRefused, match=why):
        rules(branch)


def test_default_branch_is_refused_even_under_a_prefix() -> None:
    with pytest.raises(PushRefused, match="default branch"):
        rules("aureon/main-line", default_branch="aureon/main-line")


def test_a_branch_protected_on_github_is_refused() -> None:
    with pytest.raises(PushRefused, match="protected on GitHub"):
        rules("feat/x", remote_protected=True)


def test_unknown_default_branch_fails_closed() -> None:
    with pytest.raises(PushRefused, match="default branch is unknown"):
        rules("feat/x", default_branch=None)


def test_no_prefixes_means_nothing_is_pushable() -> None:
    with pytest.raises(PushRefused, match="no push branch prefixes"):
        rules("feat/x", prefixes=())


def test_protected_setting_extends_the_refusals() -> None:
    with pytest.raises(PushRefused, match="protected"):
        rules("feat/keep", protected=(*PROTECTED, "feat/keep"))


# --- allowlist ------------------------------------------------------------------------------


def test_allowlist_defaults_to_nothing(settings: Settings) -> None:
    cfg = coding_git.GitOpsConfig.from_settings(settings)
    assert cfg.allowlist == frozenset()
    with pytest.raises(coding_git.GitOpRefused) as ei:
        cfg.require("SVSKHD/ASTRA-FRONTEND")
    assert ei.value.code == "repo_not_allowlisted"
    assert ei.value.status == 404


def test_allowlist_is_exact_and_case_insensitive(settings: Settings) -> None:
    s = settings.model_copy(update={"GITHUB_CODING_REPO_ALLOWLIST": ["SVSKHD/ASTRA-FRONTEND"]})
    cfg = coding_git.GitOpsConfig.from_settings(s)
    assert cfg.require("svskhd/astra-frontend") == "svskhd/astra-frontend"
    for other in ("svskhd/astra-frontend2", "other/astra-frontend", "svskhd/aureon"):
        with pytest.raises(coding_git.GitOpRefused):
            cfg.require(other)
    with pytest.raises(coding_git.GitOpRefused) as ei:
        cfg.require("../etc/passwd")
    assert ei.value.code == "invalid_repo"


def test_settings_parse_the_new_variables() -> None:
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        SECRET_KEY="t" * 40,
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        GITHUB_CODING_REPO_ALLOWLIST='["SVSKHD/ASTRA-FRONTEND"]',
        CODE_PUSH_BRANCH_PREFIXES="aureon/, feat/",
    )
    assert s.GITHUB_CODING_REPO_ALLOWLIST == ["SVSKHD/ASTRA-FRONTEND"]
    assert s.CODE_PUSH_BRANCH_PREFIXES == ["aureon/", "feat/"]
    empty = Settings(
        _env_file=None,
        SECRET_KEY="t" * 40,
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        GITHUB_CODING_REPO_ALLOWLIST="",
    )  # type: ignore[call-arg]
    assert empty.GITHUB_CODING_REPO_ALLOWLIST == []


def test_git_url_must_be_https_outside_tests() -> None:
    with pytest.raises(GitError, match="https"):
        RemoteGit("http://github.com", TOKEN)
    with pytest.raises(GitError, match="https"):
        RemoteGit("file:///tmp/x", TOKEN)
    with pytest.raises(GitError, match="credentials"):
        RemoteGit(f"https://x-access-token:{TOKEN}@github.com", TOKEN)
    assert RemoteGit("https://github.com/", TOKEN).remote_url("SVSKHD/X") == (
        "https://github.com/svskhd/x.git"
    )


# --- secrets paths --------------------------------------------------------------------------


def test_secret_paths() -> None:
    bad = [
        ".env",
        "app/.env.local",
        "config/secrets/db.json",
        "deploy/server.pem",
        "home/.ssh/id_rsa",
        "aws_credentials.json",
        ".npmrc",
        ".git/config",
    ]
    ok = [".env.example", "src/a.ts", "README.md", "app/core/config.py"]
    assert secret_paths(bad + ok) == sorted(bad)


# --- real git against a local bare "GitHub" -------------------------------------------------


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def github(tmp_path: Path) -> Path:
    """tmp/gh/svskhd/demo.git with one commit on `trunk` (the default branch)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    sh("init", "-q", "-b", "trunk", cwd=seed)
    (seed / "README.md").write_text("hi\n")
    (seed / ".env.example").write_text("A=\n")
    sh("add", ".", cwd=seed)
    sh("commit", "-q", "-m", "init", cwd=seed)
    root = tmp_path / "gh"
    (root / "svskhd").mkdir(parents=True)
    sh("clone", "-q", "--bare", str(seed), str(root / "svskhd" / "demo.git"), cwd=tmp_path)
    return root


@pytest.fixture
def git_settings(settings: Settings, tmp_path: Path, github: Path) -> Settings:
    return settings.model_copy(
        update={
            "GITHUB_CODING_REPO_ALLOWLIST": ["SVSKHD/demo"],
            "CODE_CLONE_ROOT": tmp_path / "clones",
            "GITHUB_GIT_URL": f"file://{github}",
        }
    )


def gone(path: str) -> bool:
    return not Path(path).exists()


class Spy:
    """Records every subprocess git is started with (argv + env), then runs it for real."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        real = asyncio.create_subprocess_exec

        async def spy(*argv: str, **kw: Any) -> Any:
            self.calls.append((list(argv), dict(kw.get("env") or {})))
            return await real(*argv, **kw)

        monkeypatch.setattr(remote_mod.asyncio, "create_subprocess_exec", spy)


def remote_for(s: Settings) -> RemoteGit:
    cfg = coding_git.GitOpsConfig.from_settings(s)
    assert cfg.allow_file  # ENV=test only
    return RemoteGit(cfg.git_url, TOKEN, timeout=60, allow_file=cfg.allow_file)


async def test_clone_push_and_pull_never_expose_the_token(
    git_settings: Settings, github: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch)
    cfg = coding_git.GitOpsConfig.from_settings(git_settings)
    remote = remote_for(git_settings)
    path = cfg.clone_path("svskhd/demo")
    with structlog.testing.capture_logs() as logs:
        await remote.clone("svskhd/demo", path)
        assert await remote.default_branch(path) == "trunk"

        # a feature branch with one commit, then the push
        sh("switch", "-q", "-c", "aureon/task-0123abcd", cwd=path)
        (path / "src.txt").write_text("change\n")
        sh("add", "src.txt", cwd=path)
        sh("commit", "-q", "-m", "change", cwd=path)
        sh("switch", "-q", "trunk", cwd=path)
        out = await coding_git.push_branch(
            cfg, remote, "svskhd/demo", "aureon/task-0123abcd",
            default_branch="trunk", remote_protected=False,
        )  # fmt: skip
        bare = github / "svskhd" / "demo.git"
        assert sh("rev-parse", "refs/heads/aureon/task-0123abcd", cwd=bare) == out["head_sha"]
        assert out["files"] == ["src.txt"]

        # someone else moves trunk on "GitHub"; pull fast-forwards the clone
        other = github.parent / "other"
        sh("clone", "-q", str(bare), str(other), cwd=github.parent)
        (other / "new.txt").write_text("x\n")
        sh("add", "new.txt", cwd=other)
        sh("commit", "-q", "-m", "upstream", cwd=other)
        sh("push", "-q", "origin", "trunk", cwd=other)
        sha = await remote.pull("svskhd/demo", path, "trunk")
        assert sha == sh("rev-parse", "trunk", cwd=bare)
        assert (path / "new.txt").exists()

    assert spy.calls
    network = 0
    for argv, env in spy.calls:
        assert all(TOKEN not in a for a in argv), argv  # never in argv (ps)
        assert "credential.helper=" in argv  # helpers cleared: nothing stored
        for k, v in env.items():
            if TOKEN in v:
                assert k == TOKEN_ENV  # only the one variable the askpass script reads
        if TOKEN_ENV in env:
            network += 1
            assert Path(env["GIT_ASKPASS"]).name == "askpass.sh"
            assert gone(env["GIT_ASKPASS"])  # removed after the call
            assert TOKEN not in ASKPASS_SCRIPT
    assert network == 4  # clone, fetch (push_branch), push, fetch (pull): nothing else
    # never in the repo's config or remote URL
    cfg_text = (path / ".git" / "config").read_text()
    assert TOKEN not in cfg_text and "@" not in cfg_text
    assert sh("remote", "get-url", "origin", cwd=path) == f"file://{github}/svskhd/demo.git"
    assert TOKEN not in repr(logs)


async def test_push_refuses_commits_touching_secrets(
    git_settings: Settings, github: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = coding_git.GitOpsConfig.from_settings(git_settings)
    remote = remote_for(git_settings)
    path = cfg.clone_path("svskhd/demo")
    await remote.clone("svskhd/demo", path)
    sh("switch", "-q", "-c", "feat/leak", cwd=path)
    (path / ".env").write_text("SECRET=1\n")
    sh("add", "-f", ".env", cwd=path)
    sh("commit", "-q", "-m", "oops", cwd=path)
    sh("rm", "-q", ".env", cwd=path)  # removed again: the history still has it
    sh("commit", "-q", "-m", "undo", cwd=path)
    with pytest.raises(PushRefused, match=r"secrets paths: \.env"):
        await coding_git.push_branch(
            cfg, remote, "svskhd/demo", "feat/leak", default_branch="trunk", remote_protected=False
        )
    bare = github / "svskhd" / "demo.git"
    assert "feat/leak" not in sh("branch", "--list", cwd=bare)


async def test_refused_branches_never_start_git(
    git_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = coding_git.GitOpsConfig.from_settings(git_settings)
    remote = remote_for(git_settings)
    spy = Spy(monkeypatch)
    for branch in ("main", "trunk", "dev01", "feature/x"):
        with pytest.raises(PushRefused):
            await coding_git.push_branch(
                cfg, remote, "svskhd/demo", branch, default_branch="trunk", remote_protected=False
            )
    with pytest.raises(PushRefused):
        await coding_git.push_branch(
            cfg, remote, "svskhd/demo", "feat/x", default_branch="trunk", remote_protected=True
        )
    assert spy.calls == []


async def test_an_empty_branch_is_not_pushed(git_settings: Settings) -> None:
    cfg = coding_git.GitOpsConfig.from_settings(git_settings)
    remote = remote_for(git_settings)
    path = cfg.clone_path("svskhd/demo")
    await remote.clone("svskhd/demo", path)
    sh("branch", "feat/nothing", cwd=path)
    with pytest.raises(PushRefused, match="no commits"):
        await coding_git.push_branch(
            cfg, remote, "svskhd/demo", "feat/nothing", default_branch="trunk",
            remote_protected=False,
        )  # fmt: skip


async def test_git_errors_are_redacted(tmp_path: Path) -> None:
    remote = RemoteGit("https://github.com", TOKEN)
    sh("init", "-q", cwd=tmp_path)
    with pytest.raises(GitError) as ei:
        await remote.run("rev-parse", TOKEN, cwd=tmp_path)
    assert TOKEN not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


def test_credentials_in_git_config_are_detected(tmp_path: Path) -> None:
    sh("init", "-q", cwd=tmp_path)
    assert_no_credentials(tmp_path)
    sh("remote", "add", "origin", f"https://x:{TOKEN}@github.com/a/b.git", cwd=tmp_path)
    with pytest.raises(GitError, match="credentials"):
        assert_no_credentials(tmp_path)


def test_askpass_script_reads_the_token_from_its_env(tmp_path: Path) -> None:
    script = tmp_path / "askpass.sh"
    script.write_text(ASKPASS_SCRIPT)
    script.chmod(0o700)
    env = {"PATH": "/usr/bin:/bin", TOKEN_ENV: TOKEN}
    user = subprocess.run([str(script), "Username for 'https://github.com': "], env=env,
                          capture_output=True, text=True, check=True)  # fmt: skip
    pw = subprocess.run([str(script), "Password for 'https://x@github.com': "], env=env,
                        capture_output=True, text=True, check=True)  # fmt: skip
    assert user.stdout.strip() == "x-access-token"
    assert pw.stdout.strip() == TOKEN


async def test_a_clone_becomes_a_coding_repo(git_settings: Settings) -> None:
    cfg = coding_git.GitOpsConfig.from_settings(git_settings)
    assert coding_git.clone_repos(git_settings) == {}
    await remote_for(git_settings).clone("svskhd/demo", cfg.clone_path("svskhd/demo"))
    repos = coding.CodingConfig.from_settings(git_settings).repos
    repo = repos["gh-svskhd-demo"]
    assert repo.base == "trunk"
    assert repo.path == cfg.clone_path("svskhd/demo")
    assert repo.tests == ()


# --- approval: recorded by the API, git work queued for the worker (D-24-12) ----------------


class FakeSession:
    """Just enough of an AsyncSession for approve() / run_op() without a database."""

    def __init__(self, store: dict[Any, Any]) -> None:
        self.store = store

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    def add(self, obj: Any) -> None:
        self.store.setdefault("added", []).append(obj)

    async def flush(self) -> None:
        for obj in self.store.get("added", []):
            if getattr(obj, "id", 1) is None:
                obj.id = uuid.uuid4()

    async def scalar(self, _stmt: Any) -> int:
        return 0  # no git op in progress

    async def get(self, cls: Any, oid: Any) -> Any:
        return self.store.get((cls.__name__, oid))

    async def execute(self, stmt: Any) -> Any:
        self.store.setdefault("updates", []).append(stmt.compile().params)


def ready_task(repo: str, sha: str = "a" * 40) -> CodeTask:
    return CodeTask(
        id=uuid.uuid4(),
        kind="change",
        repo=repo,
        title="Greet",
        instruction="say hello",
        status="ready",
        branch="aureon/task-0123abcd4567",
        commit_sha=sha,
        tests=[{"ok": True}],
        result={"diff_stat": " a | 1 +"},
        worktree=None,
    )


@pytest.fixture
def no_git_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any subprocess (git or otherwise) started from now on fails the test."""

    async def refuse(*argv: Any, **kw: Any) -> Any:
        raise AssertionError(f"the API process started a subprocess: {argv[:3]}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


async def test_approve_runs_no_git_and_queues_the_task_op(
    settings: Settings, tmp_path: Path, no_git_here: None
) -> None:
    s = settings.model_copy(
        update={"CODE_REPOS": {"demo": {"path": str(tmp_path / "nowhere"), "base": "dev01"}}}
    )
    store: dict[Any, Any] = {}
    task = ready_task("demo")
    user = uuid.uuid4()
    op = await coding.approve(FakeSession(store), s, task, user, "feature/greet", ClientInfo())  # type: ignore[arg-type]
    assert op.op == "task" and op.task_id == task.id and op.repo == "demo"
    assert op.branch == "feature/greet" and op.requested_by == user
    assert op.result["push"] is False  # a local CODE_REPOS checkout: nothing to push
    assert task.status == "approved" and task.result["git_op_id"] == str(op.id)
    events = [a.event_type for a in store["added"] if isinstance(a, AuditEvent)]
    assert events == ["code_git_task_queued"]
    # without a feature name the task branch itself is kept
    op2 = await coding.approve(FakeSession({}), s, ready_task("demo"), user, None, ClientInfo())  # type: ignore[arg-type]
    assert op2.branch == "aureon/task-0123abcd4567"


async def test_approve_refuses_protected_names_before_queueing(
    settings: Settings, tmp_path: Path, no_git_here: None
) -> None:
    s = settings.model_copy(
        update={"CODE_REPOS": {"demo": {"path": str(tmp_path / "nowhere"), "base": "dev01"}}}
    )
    store: dict[Any, Any] = {}
    for bad in ("dev01", "main", "release/x"):
        with pytest.raises(coding.DecisionRefused):
            await coding.approve(
                FakeSession(store), s, ready_task("demo"), uuid.uuid4(), bad, ClientInfo()
            )  # type: ignore[arg-type]
    assert "added" not in store


async def test_approving_a_clone_task_queues_a_push_with_the_rules_checked(
    git_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = git_settings.model_copy(update={"GITHUB_TOKEN": SecretStr(TOKEN)})
    cfg = coding_git.GitOpsConfig.from_settings(s)
    await remote_for(s).clone("svskhd/demo", cfg.clone_path("svskhd/demo"))

    async def refuse(*argv: Any, **kw: Any) -> Any:
        raise AssertionError("no git in the API process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    op = await coding.approve(
        FakeSession({}), s, ready_task("gh-svskhd-demo"), uuid.uuid4(), None, ClientInfo()
    )  # type: ignore[arg-type]
    assert op.repo == "svskhd/demo" and op.branch == "aureon/task-0123abcd4567"
    assert op.result["push"] is True and op.title == "aureon: Greet"
    # fix/x is a valid local feature name but not a push prefix: refused up front
    with pytest.raises(coding_git.GitOpRefused) as ei:
        await coding.approve(
            FakeSession({}), s, ready_task("gh-svskhd-demo"), uuid.uuid4(), "fix/x", ClientInfo()
        )  # type: ignore[arg-type]
    assert ei.value.code == "push_refused"


class FakeGitHub:
    def __init__(self, protected: bool = False) -> None:
        self.protected = protected
        self.paths: list[str] = []

    async def get_json(self, path: str, params: Any = None, *, cache: bool = True) -> Any:
        self.paths.append(path)
        if path == "/repos/svskhd/demo":
            return {
                "id": 1, "full_name": "SVSKHD/demo", "owner": {"login": "SVSKHD"},
                "name": "demo", "private": False, "default_branch": "trunk",
                "html_url": "https://github.com/SVSKHD/demo",
            }  # fmt: skip
        if path.startswith("/repos/svskhd/demo/branches/"):
            if self.protected:
                return {"name": path.rsplit("/", 1)[-1], "protected": True}
            raise GitHubNotFound("branch not found")
        raise AssertionError(f"unexpected GitHub call {path}")


async def _worker_task_op(
    s: Settings, branch: str, client: FakeGitHub
) -> tuple[str, CodeGitOp, str]:
    """A clone with an approved task commit, and the worker running its `task` op."""
    cfg = coding_git.GitOpsConfig.from_settings(s)
    path = cfg.clone_path("svskhd/demo")
    await remote_for(s).clone("svskhd/demo", path)
    sh("switch", "-q", "-c", "aureon/task-0123abcd4567", cwd=path)
    (path / "src.txt").write_text("change\n")
    sh("add", "src.txt", cwd=path)
    sh("commit", "-q", "-m", "change", cwd=path)
    sha = sh("rev-parse", "HEAD", cwd=path)
    sh("switch", "-q", "trunk", cwd=path)
    task = ready_task("gh-svskhd-demo", sha)
    task.status = "approved"
    row = CodeGitOp(
        id=uuid.uuid4(), op="task", repo="svskhd/demo", task_id=task.id, branch=branch,
        title="t", body="b", status="running", result={}, requested_by=None,
    )  # fmt: skip
    store: dict[Any, Any] = {("CodeGitOp", row.id): row, ("CodeTask", task.id): task}

    def maker() -> FakeSession:
        return FakeSession(store)

    status = await coding_git.run_op(maker, s, client, row.id, remote=remote_for(s))  # type: ignore[arg-type]
    err = next(u["error"] for u in store["updates"] if "error" in u)
    return status, row, str(err)


@pytest.mark.parametrize(
    ("branch", "protected", "why"),
    [
        ("fix/x", False, "must start with"),  # a local feature name, not a push prefix
        ("aureon/task-0123abcd4567", True, "protected on GitHub"),  # only GitHub knows
    ],
)
async def test_a_refused_branch_is_still_refused_in_the_worker(
    git_settings: Settings, github: Path, branch: str, protected: bool, why: str
) -> None:
    s = git_settings.model_copy(update={"GITHUB_ALLOWED_REPOS": ["svskhd/demo"]})
    client = FakeGitHub(protected=protected)
    status, _row, err = await _worker_task_op(s, branch, client)
    assert status == "failed" and why in str(err)
    bare = github / "svskhd" / "demo.git"
    assert sh("branch", "--list", cwd=bare) == "* trunk"  # nothing reached "GitHub"
    assert not any(p.endswith("/pulls") for p in client.paths)  # no PR either
