"""Self-repair: incident -> sandboxed worktree -> local-model patch -> allowlisted tests -> PR
(docs/21-self-repair.md).

- Reuses the coding agent's machinery (app/agents/coding): the repo allowlist, a git worktree
  on a `repair/<id>` branch with hooks disabled, the patch extractor/validator, symlink-safe
  path checks and the argv command allowlist with a scrubbed environment.
- The model is the local Ollama REPAIR_MODEL. Unset = repairs disabled: tasks stay `queued`
  with the reason in `blocked_reason`.
- HARD BLOCKS (`hard_block`): trading / paper / research / risk code, auth and security,
  secrets, and infra (compose, Dockerfile, deploy, ops scripts, alembic, CI workflows), plus
  the repair system itself. Checked on the target files before the model is called and on
  the patch's files (and added secret-looking lines) after; a hit makes the task `blocked`
  and nothing is pushed.
- Only when every allowlisted test passes is anything sent to GitHub: the changed files go
  up through the REST API (blobs -> tree -> commit -> `refs/heads/repair/<id>`), never with
  git credentials, and a PR is opened. Nothing is ever merged here: merging is the Phase 16
  `github.pr.merge` approval.
- Recursion guard: incidents from the repair system itself (or a repair PR's own CI) never
  get a repair, a retry's parent may not itself be a retry (depth <= 1), at most
  REPAIR_MAX_PER_FINGERPRINT_PER_DAY repairs per fingerprint, and more than
  REPAIR_BREAKER_MAX_FAILURES_PER_HOUR failures in an hour pauses the `repair` agent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coding import patch as patches
from app.agents.coding.commands import run_allowed
from app.agents.coding.git import Git, GitError
from app.agents.coding.paths import PathRejected, normalize, safe_path
from app.agents.coding.repos import RepoConfig
from app.agents.registry import AgentRegistry, AgentSpec
from app.core.config import Settings
from app.core.logging import get_logger, scrub
from app.core.sensitive_paths import classify_repair_path
from app.integrations.github.client import GitHubClient, GitHubNotFound
from app.models.agents import Agent
from app.models.repairs import Incident, RepairTask
from app.services import audit, coding
from app.services import github as gh
from app.services import github_ops as ops
from app.services.audit import ClientInfo

log = get_logger("services.repairs")

REPAIR = AgentSpec("repair", "Self-repair agent", "repair")
SYSTEM_CLIENT = ClientInfo(user_agent="aureon-self-repair")
ACTIVE = ("queued", "running", "patch_ready", "tests_passed")
FAILED = ("failed", "tests_failed")
RETRYABLE = ("failed", "tests_failed", "blocked", "cancelled")
CANCELLABLE = ("queued", "running", "patch_ready", "tests_passed", "blocked")

# --- hard blocks ----------------------------------------------------------------------------

# The path list is shared with the Phase 19 review filter: app/core/sensitive_paths.py
# (secrets, infra, trading_risk, auth), plus its repair-only additions (migrations, ops
# scripts, research, `*key*` names, and the repair system / sandbox itself).
CONFIG_FILE = "app/core/config.py"
# A changed line in settings that names a secret field.
SECRET_FIELD = re.compile(
    r"SecretStr|SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL", re.IGNORECASE
)
# Anything that looks like a credential being added anywhere.
SECRET_VALUE = re.compile(
    r"github_pat_[A-Za-z0-9_]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{8,}"
)


def hard_block(path: str) -> str | None:
    """The reason `path` may never be touched by a repair, or None."""
    cat = classify_repair_path(path)
    return f"hard block ({cat}): {path}" if cat else None


def hard_block_any(paths: list[str] | tuple[str, ...]) -> str | None:
    for p in paths:
        if why := hard_block(p):
            return why
    return None


def _changed_lines(patch_text: str) -> dict[str, list[str]]:
    """file -> added/removed lines of a unified diff."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            current = line[4:].split("\t", 1)[0].removeprefix("b/")
            continue
        if line.startswith("--- "):
            continue
        if line.startswith(("+", "-")) and current:
            out.setdefault(current, []).append(line[1:])
    return out


def patch_block(p: patches.Patch) -> str | None:
    """The after-model check: the patch's files, secret fields in settings, and added
    credential-looking strings."""
    if why := hard_block_any(p.files):
        return why
    for file, lines in _changed_lines(p.text).items():
        if file == CONFIG_FILE and any(SECRET_FIELD.search(x) for x in lines):
            return f"hard block (settings secret fields): {file}"
        if any(SECRET_VALUE.search(x) for x in lines):
            return f"hard block (secret-looking value): {file}"
    return None


# --- config ---------------------------------------------------------------------------------


class RepairError(RuntimeError):
    """A task-level failure with a message that is safe to show."""


class RepairRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True, slots=True)
class RepairConfig:
    coding: coding.CodingConfig
    model: str | None
    repo: str | None
    github_repo: str | None
    model_timeout: float
    output_max: int

    @classmethod
    def from_settings(cls, s: Settings) -> RepairConfig:
        return cls(
            coding=coding.CodingConfig.from_settings(s),
            model=(s.REPAIR_MODEL or "").strip() or None,
            repo=(s.REPAIR_REPO or "").strip() or None,
            github_repo=(s.REPAIR_GITHUB_REPO or "").strip() or None,
            model_timeout=s.REPAIR_MODEL_TIMEOUT_SECONDS,
            output_max=s.REPAIR_TEST_OUTPUT_MAX_CHARS,
        )


def disabled_reason(settings: Settings) -> str | None:
    """Why repairs cannot run right now (they stay queued), or None."""
    if not (settings.REPAIR_MODEL or "").strip():
        return "repairs are disabled: REPAIR_MODEL is not set"
    repo = (settings.REPAIR_REPO or "").strip()
    if not repo or repo not in (settings.CODE_REPOS or {}):
        return "repairs are disabled: REPAIR_REPO must name a CODE_REPOS entry"
    if "/" not in (settings.REPAIR_GITHUB_REPO or ""):
        return "repairs are disabled: REPAIR_GITHUB_REPO (owner/repo) is not set"
    if not settings.github_configured:
        return "repairs are disabled: GitHub is not configured (GITHUB_TOKEN)"
    return None


def repair_branch(rid: uuid.UUID) -> str:
    return f"repair/{rid.hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


# --- creating -------------------------------------------------------------------------------


def target_paths(incident: Incident, paths: list[str] | None) -> list[str]:
    raw = list(paths or [])
    if not raw:
        loc = (incident.details or {}).get("location") or {}
        if isinstance(loc, dict) and loc.get("file"):
            raw = [str(loc["file"])]
    out: list[str] = []
    for p in raw[:10]:
        try:
            clean = normalize(p)
        except PathRejected as exc:
            raise RepairRefused(422, "invalid_path", f"{p}: {exc}") from None
        if not clean:
            raise RepairRefused(422, "invalid_path", "a file path is required")
        if clean not in out:
            out.append(clean)
    return out


async def create_repair(
    db: AsyncSession,
    settings: Settings,
    incident: Incident,
    *,
    created_by: uuid.UUID | None = None,
    parent: RepairTask | None = None,
    paths: list[str] | None = None,
    client: ClientInfo = SYSTEM_CLIENT,
) -> RepairTask:
    """Queue a repair for `incident` (in the caller's transaction), after every guard."""
    if incident.status in ("resolved", "ignored"):
        raise RepairRefused(409, "incident_closed", f"the incident is {incident.status}")
    if (incident.details or {}).get("self_origin"):
        raise RepairRefused(
            409, "recursion_guard", "incidents from the repair system itself are never repaired"
        )
    if parent is not None and parent.parent_repair_id is not None:
        raise RepairRefused(409, "recursion_guard", "a retry of a retry is not allowed (depth 1)")
    active = await db.scalar(
        select(func.count())
        .select_from(RepairTask)
        .where(RepairTask.incident_id == incident.id, RepairTask.status.in_(ACTIVE))
    )
    if active:
        raise RepairRefused(409, "repair_active", "this incident already has an active repair")
    per_day = await db.scalar(
        select(func.count())
        .select_from(RepairTask)
        .join(Incident, Incident.id == RepairTask.incident_id)
        .where(
            Incident.fingerprint == incident.fingerprint,
            RepairTask.created_at > func.now() - timedelta(days=1),
        )
    )
    if int(per_day or 0) >= settings.REPAIR_MAX_PER_FINGERPRINT_PER_DAY:
        raise RepairRefused(
            429,
            "repair_daily_limit",
            f"at most {settings.REPAIR_MAX_PER_FINGERPRINT_PER_DAY} repairs per incident per day",
        )
    queued = await db.scalar(
        select(func.count())
        .select_from(RepairTask)
        .where(RepairTask.status.in_(("queued", "running")))
    )
    if int(queued or 0) >= settings.REPAIR_QUEUE_MAX:
        raise RepairRefused(429, "repair_queue_full", "the repair queue is full")
    targets = target_paths(incident, paths)
    if not targets:
        raise RepairRefused(422, "no_code_location", "the incident maps to no code location")
    task = RepairTask(
        incident_id=incident.id,
        repo=(settings.REPAIR_REPO or "").strip() or "-",
        github_repo=(settings.REPAIR_GITHUB_REPO or "").strip() or None,
        target_paths=targets,
        model=(settings.REPAIR_MODEL or "").strip() or None,
        parent_repair_id=parent.id if parent is not None else None,
        created_by=created_by,
        status="queued",
    )
    blocked = hard_block_any(targets)  # check 1 of 2 (again in the worker, before the model)
    if blocked:
        task.status = "blocked"
        task.blocked_reason = blocked
        task.finished_at = _now()
    else:
        task.blocked_reason = disabled_reason(settings)
        incident.status = "repair_queued"
    db.add(task)
    await db.flush()
    incident.details = incident.details | {"_repair_id": str(task.id)}
    audit.record(
        db,
        "repair_blocked" if blocked else "repair_queued",
        client,
        actor_user_id=created_by,
        details={
            "repair_id": str(task.id),
            "incident_id": str(incident.id),
            "targets": targets,
            "parent_repair_id": str(parent.id) if parent else None,
        }
        | ({"reason": blocked} if blocked else {}),
    )
    log.info("repair_created", repair=str(task.id), status=task.status, targets=targets)
    return task


# --- breaker / agent ------------------------------------------------------------------------


async def is_paused(maker: async_sessionmaker[AsyncSession]) -> bool:
    return await AgentRegistry(maker).is_paused(REPAIR.id)


async def check_breaker(maker: async_sessionmaker[AsyncSession], settings: Settings) -> bool:
    """Open the circuit (pause the `repair` agent) after too many failures in an hour.
    Returns True when it is (now) open."""
    async with maker() as db, db.begin():
        failures = int(
            await db.scalar(
                select(func.count())
                .select_from(RepairTask)
                .where(
                    RepairTask.status.in_(FAILED),
                    RepairTask.finished_at > func.now() - timedelta(hours=1),
                )
            )
            or 0
        )
        if failures <= settings.REPAIR_BREAKER_MAX_FAILURES_PER_HOUR:
            return False
        ins = insert(Agent).values(
            id=REPAIR.id, name=REPAIR.name, kind=REPAIR.kind, state="idle", paused=True
        )
        was = await db.scalar(select(Agent.paused).where(Agent.id == REPAIR.id))
        await db.execute(
            ins.on_conflict_do_update(index_elements=[Agent.id], set_={"paused": True})
        )
        if not was:
            audit.record(
                db,
                "repair_circuit_open",
                SYSTEM_CLIENT,
                details={
                    "failures_last_hour": failures,
                    "limit": settings.REPAIR_BREAKER_MAX_FAILURES_PER_HOUR,
                },
            )
            log.warning("repair_circuit_open", failures=failures)
    if not was:
        await AgentRegistry(maker).log(
            REPAIR.id,
            "error",
            f"circuit breaker: {failures} failed repairs in the last hour; agent paused "
            "(resume with POST /api/agents/repair/resume)",
        )
    return True


async def claim(maker: async_sessionmaker[AsyncSession], settings: Settings) -> uuid.UUID | None:
    """The oldest queued repair, when repairs are enabled, the agent is not paused, the
    breaker is closed and nothing else is running (one repair at a time)."""
    if disabled_reason(settings) or await is_paused(maker):
        return None
    if await check_breaker(maker, settings):
        return None
    async with maker() as db, db.begin():
        running = await db.scalar(
            select(func.count())
            .select_from(RepairTask)
            .where(RepairTask.status.in_(("running", "patch_ready", "tests_passed")))
        )
        if running:
            return None
        rid = await db.scalar(
            select(RepairTask.id)
            .where(RepairTask.status == "queued")
            .order_by(RepairTask.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if rid is None:
            return None
        await db.execute(
            update(RepairTask)
            .where(RepairTask.id == rid)
            .values(
                status="running",
                started_at=func.now(),
                blocked_reason=None,
                attempts=RepairTask.attempts + 1,
            )
        )
    return rid


# --- running --------------------------------------------------------------------------------

SYSTEM_REPAIR = (
    "You are a careful senior engineer fixing a production incident in a Python service. "
    "Find the root cause in the files provided and fix it with the smallest safe change. "
    "Reply with ONE unified diff in a ```diff fenced block, with paths relative to the "
    "repository root using a/ and b/ prefixes, followed by a short explanation. Never touch "
    "trading, risk, authentication, secrets, settings secret fields, Docker/compose, deploy "
    "scripts, migrations or CI configuration, never add binary files or symlinks, and never "
    "disable or delete tests."
)


def _incident_brief(inc: Incident) -> str:
    d: dict[str, Any] = scrub(dict(inc.details or {}))
    lines = [f"Incident: {inc.title}", f"Source: {inc.source}; seen {inc.count} time(s)"]
    for key in ("type", "message", "event", "location"):
        if d.get(key):
            lines.append(f"{key}: {d[key]}")
    frames = d.get("frames") or []
    if frames:
        lines.append("App frames (innermost last):\n" + "\n".join(f"  {f}" for f in frames))
    return "\n".join(lines)


async def _set(
    maker: async_sessionmaker[AsyncSession], rid: uuid.UUID, *, force: bool = False, **values: Any
) -> bool:
    """Update the task unless an admin cancelled it meanwhile (then nothing changes and False
    is returned). `force` is only for facts that already happened (an opened PR)."""
    q = update(RepairTask).where(RepairTask.id == rid)
    if not force:
        q = q.where(RepairTask.status != "cancelled")
    async with maker() as db, db.begin():
        res = await db.execute(q.values(**values))
    return bool(res.rowcount)  # type: ignore[attr-defined]


async def _finish_incident(
    maker: async_sessionmaker[AsyncSession], incident_id: uuid.UUID, status: str, extra: Any
) -> None:
    """A finished repair: a PR keeps the incident `repair_queued` (an admin resolves it after
    merging); anything else hands it back to `open`, already escalated (no automatic loop)."""
    async with maker() as db, db.begin():
        inc = await db.get(Incident, incident_id, with_for_update=True)
        if inc is None:
            return
        inc.details = inc.details | {"_repair_result": extra}
        if status != "pr_opened" and inc.status == "repair_queued":
            inc.status = "open"


class Cancelled(Exception):
    pass


async def _cleanup(git: Git, worktree: Path | None, branch: str | None) -> None:
    if worktree is not None:
        await coding._cleanup(git, worktree, branch, delete_branch=True)
    elif branch:
        await coding._cleanup(git, Path("/nonexistent"), branch, delete_branch=True)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 4] + "\n…[output truncated]…\n" + text[-(limit * 3 // 4) + 40 :]


async def run_repair(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    cfg: RepairConfig,
    ollama: Any,
    github: GitHubClient | None,
    rid: uuid.UUID,
) -> str:
    """Execute one claimed repair; returns its final status. The caller holds the heavy
    model lock for the whole call."""
    async with maker() as db:
        task = await db.get(RepairTask, rid)
        assert task is not None
        incident = await db.get(Incident, task.incident_id)
        assert incident is not None
    worktree: Path | None = None
    branch: str | None = None
    git: Git | None = None
    status = "failed"
    extra: dict[str, Any] = {"repair_id": str(rid)}
    try:
        if not cfg.model:
            raise RepairError("REPAIR_MODEL is not set")
        targets = list(task.target_paths)
        if why := hard_block_any(targets):  # check 1 (again): before the model is called
            await _set(maker, rid, status="blocked", blocked_reason=why, finished_at=func.now())
            status = "blocked"
            extra |= {"status": status, "reason": why}
            return status
        repo: RepoConfig | None = cfg.coding.repos.get(task.repo)
        if repo is None:
            raise RepairError(f"repo {task.repo!r} is not in CODE_REPOS")
        git = Git(repo.path)
        base_sha = await git.head(coding.check_ref(repo.base))
        context, _shown = await coding.gather_context(
            git, base_sha, targets, cfg.coding.max_context
        )
        prompt = (
            f"{_incident_brief(incident)}\n\n{context}\n\n"
            f"Fix the incident. Files you may change: {', '.join(targets)} (and their tests)."
        )
        digest = hashlib.sha256(f"{SYSTEM_REPAIR}\n{prompt}".encode()).hexdigest()
        branch = repair_branch(rid)
        worktree = cfg.coding.worktree_root / f"repair-{rid.hex}"
        await asyncio.to_thread(cfg.coding.worktree_root.mkdir, parents=True, exist_ok=True)
        await git.worktree_add(worktree, branch, base_sha)
        if not await _set(
            maker, rid, base_sha=base_sha, model=cfg.model, prompt_digest=digest,
            branch=branch, worktree=str(worktree),
        ):  # fmt: skip
            raise Cancelled
        for d in repo.link:  # reuse installed deps so tests run offline; never committed
            src, dst = repo.path / d, worktree / d
            if await asyncio.to_thread(src.is_dir) and not await asyncio.to_thread(dst.exists):
                await asyncio.to_thread(dst.symlink_to, src, True)

        answer = await coding.ask_model(
            ollama, cfg.model, SYSTEM_REPAIR, prompt, limit_s=cfg.model_timeout
        )
        try:
            patch = patches.validate(patches.extract(answer), cfg.coding.max_patch)
        except patches.PatchRejected as exc:
            raise RepairError(f"patch refused: {exc}") from exc
        if why := patch_block(patch):  # check 2: after the model, on what it touched
            await _cleanup(git, worktree, branch)
            worktree = branch = None
            await _set(
                maker, rid, status="blocked", blocked_reason=why, patch=patch.text[:200_000],
                files=list(patch.files), worktree=None, branch=None, finished_at=func.now(),
            )  # fmt: skip
            status = "blocked"
            extra |= {"status": status, "reason": why}
            log.warning("repair_blocked", repair=str(rid), reason=why)
            return status
        try:
            for f in patch.files:  # symlinks resolved: no writing through a linked dir
                safe_path(worktree, f)
                if f.split("/", 1)[0] in repo.link:
                    raise PathRejected(f"{f} is inside a linked directory")
            await git.apply(worktree, patch.text, check=True)
        except (PathRejected, GitError) as exc:
            raise RepairError(f"patch refused: {exc}") from exc
        await git.apply(worktree, patch.text, check=False)
        await git.run("add", "--", *patch.files, cwd=worktree)
        commit = await git.commit_all_staged(
            worktree, f"repair: {incident.title[:120]}\n\nIncident {incident.id}"
        )
        if not await _set(
            maker, rid, status="patch_ready", patch=patch.text, files=list(patch.files)
        ):
            raise Cancelled
        extra["commit"] = commit
        if not repo.tests:
            raise _TestsFailed("no allowlisted test commands are configured for this repo", "")
        outputs: list[str] = []
        ok = True
        home = cfg.coding.worktree_root / f"repair-{rid.hex}.home"
        await asyncio.to_thread(home.mkdir, exist_ok=True)
        try:
            for argv in repo.tests:
                r = await run_allowed(
                    argv, allowed=cfg.coding.allowed, cwd=worktree,
                    limit_s=cfg.coding.test_timeout, home=home,
                )  # fmt: skip
                outputs.append(
                    f"$ {' '.join(r.argv)}  -> exit {r.exit_code}"
                    f"{' (timed out)' if r.timed_out else ''}\n{r.output}"
                )
                ok = ok and r.ok
        finally:
            await asyncio.to_thread(shutil.rmtree, home, True)
        output = _cap(scrub("\n\n".join(outputs)), cfg.output_max)
        if not ok:
            raise _TestsFailed("allowlisted tests failed", output)
        if not await _set(maker, rid, status="tests_passed", test_output=output):
            raise Cancelled
        if github is None:
            raise RepairError("GitHub is not configured; nothing was pushed")
        url, number = await publish(
            maker, settings, github, repo, git, worktree, base_sha, list(patch.files),
            branch, incident, rid,
        )  # fmt: skip
        await _cleanup(git, worktree, branch)
        worktree = None
        await _set(
            maker, rid, status="pr_opened", pr_url=url, pr_number=number, worktree=None,
            finished_at=func.now(), force=True,
        )  # fmt: skip
        status = "pr_opened"
        extra |= {"status": status, "pr_url": url}
        async with maker() as db, db.begin():
            audit.record(
                db,
                "repair_pr_opened",
                SYSTEM_CLIENT,
                details={"repair_id": str(rid), "pr_url": url, "branch": branch},
            )
        log.info("repair_pr_opened", repair=str(rid), pr=url)
        return status
    except Cancelled:
        if git is not None:
            await _cleanup(git, worktree, branch)
        await _set(maker, rid, worktree=None, branch=None, finished_at=func.now(), force=True)
        status = "cancelled"
        extra |= {"status": status}
        return status
    except _TestsFailed as exc:
        if git is not None:
            await _cleanup(git, worktree, branch)
        await _set(
            maker, rid, status="tests_failed", test_output=exc.output or None,
            error=exc.message, worktree=None, branch=None, finished_at=func.now(),
        )  # fmt: skip
        status = "tests_failed"
        extra |= {"status": status}
        log.warning("repair_tests_failed", repair=str(rid))
        return status
    except Exception as exc:
        msg = (
            str(exc)
            if isinstance(exc, RepairError | PathRejected | GitError)
            else f"{type(exc).__name__}: {exc}"
        )
        msg = str(scrub(msg))[:2000]
        log.warning("repair_failed", repair=str(rid), error=msg[:200])
        if git is not None:
            await _cleanup(git, worktree, branch)
        await _set(
            maker, rid, status="failed", error=msg, worktree=None, branch=None,
            finished_at=func.now(),
        )  # fmt: skip
        status = "failed"
        extra |= {"status": status, "error": msg[:300]}
        return status
    finally:
        if status != "pr_opened":  # the sandbox is gone whatever happened (even if cancelled)
            await _set(maker, rid, worktree=None, force=True)
        await _finish_incident(maker, incident.id, status, extra)
        if status in FAILED:
            await check_breaker(maker, settings)


class _TestsFailed(Exception):
    def __init__(self, message: str, output: str) -> None:
        super().__init__(message)
        self.message, self.output = message, output


# --- publishing (GitHub REST only; never git push) ------------------------------------------


async def _file_mode(git: Git, base_sha: str, path: str) -> str:
    try:
        out = await git.run("ls-tree", base_sha, "--", path)
    except GitError:
        return "100644"
    mode = out.split(" ", 1)[0] if out.strip() else "100644"
    return "100755" if mode == "100755" else "100644"


async def publish(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: GitHubClient,
    repo: RepoConfig,
    git: Git,
    worktree: Path,
    base_sha: str,
    files: list[str],
    branch: str,
    incident: Incident,
    rid: uuid.UUID,
) -> tuple[str, int | None]:
    """Upload the changed files as one commit on `refs/heads/<branch>` and open a PR."""
    owner, _, name = (settings.REPAIR_GITHUB_REPO or "").partition("/")
    async with maker() as db:
        full = await gh.require_allowed(db, client, settings, owner, name)
    try:
        base = await client.get_json(f"/repos/{full}/git/commits/{base_sha}", cache=False)
    except GitHubNotFound:
        raise RepairError(
            f"base commit {base_sha[:12]} is not on GitHub; push {repo.base} first"
        ) from None
    tree_sha = str((base.get("tree") or {}).get("sha") or "") if isinstance(base, dict) else ""
    if not tree_sha:
        raise RepairError("could not read the base tree from GitHub")
    entries: list[dict[str, Any]] = []
    for f in files:
        path = worktree / f
        if await asyncio.to_thread(path.is_file):
            content = await asyncio.to_thread(path.read_bytes)
            blob = await client.request(
                "POST",
                f"/repos/{full}/git/blobs",
                json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            mode = await _file_mode(git, base_sha, f)
            entries.append({"path": f, "mode": mode, "type": "blob", "sha": blob.data["sha"]})
        else:  # deleted by the patch
            entries.append({"path": f, "mode": "100644", "type": "blob", "sha": None})
    tree = await client.request(
        "POST", f"/repos/{full}/git/trees", json={"base_tree": tree_sha, "tree": entries}
    )
    commit = await client.request(
        "POST",
        f"/repos/{full}/git/commits",
        json={
            "message": f"repair: {incident.title[:120]}\n\nAureon self-repair {rid}",
            "tree": tree.data["sha"],
            "parents": [base_sha],
        },
    )
    actor = ops.Actor(None, SYSTEM_CLIENT)
    await ops.tracked_write(
        maker, client, actor, op="repair.branch_create", repo=full, target=f"branch:{branch}",
        method="POST", path=f"/repos/{full}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": commit.data["sha"]},
    )  # fmt: skip
    body = (
        f"Automated repair for incident `{incident.id}`: **{incident.title}**\n\n"
        f"- source: {incident.source}, seen {incident.count}x\n"
        f"- files: {', '.join(files)}\n"
        "- the repo's allowlisted tests passed in a sandboxed worktree\n\n"
        "Generated by the local self-repair agent. Review it like any other change; merging "
        "goes through the approval gate (`github.pr.merge`)."
    )
    data = await ops.tracked_write(
        maker, client, actor, op="pr.create", repo=full, target=f"branch:{branch}",
        method="POST", path=f"/repos/{full}/pulls",
        json={
            "title": f"repair: {incident.title[:200]}",
            "head": branch,
            "base": repo.base,
            "body": body,
            "maintainer_can_modify": False,
        },
    )  # fmt: skip
    pr = data if isinstance(data, dict) else {}
    number = pr.get("number") if isinstance(pr.get("number"), int) else None
    return str(pr.get("html_url") or f"https://github.com/{full}/pull/{number}"), number


# --- admin actions --------------------------------------------------------------------------


async def cancel(db: AsyncSession, cfg: RepairConfig, task: RepairTask) -> RepairTask:
    if task.status not in CANCELLABLE:
        raise RepairRefused(409, "not_cancellable", f"a {task.status} repair cannot be cancelled")
    was = task.status
    task.status = "cancelled"
    task.finished_at = _now()
    if was in ("queued", "blocked"):
        repo = cfg.coding.repos.get(task.repo)
        if repo is not None and (task.worktree or task.branch):
            try:
                await _cleanup(
                    Git(repo.path), Path(task.worktree) if task.worktree else None, task.branch
                )
            except GitError:
                pass
            task.worktree = None
        inc = await db.get(Incident, task.incident_id)
        if inc is not None and inc.status == "repair_queued":
            inc.status = "open"
    # running / patch_ready / tests_passed: the worker sees `cancelled`, cleans up, stops.
    return task


async def retry(
    db: AsyncSession,
    settings: Settings,
    task: RepairTask,
    user_id: uuid.UUID,
    client: ClientInfo = SYSTEM_CLIENT,
) -> RepairTask:
    if task.status not in RETRYABLE:
        raise RepairRefused(409, "not_retryable", f"a {task.status} repair cannot be retried")
    inc = await db.get(Incident, task.incident_id, with_for_update=True)
    if inc is None:
        raise RepairRefused(404, "incident_not_found", "incident not found")
    return await create_repair(
        db, settings, inc, created_by=user_id, parent=task, paths=list(task.target_paths),
        client=client,
    )  # fmt: skip


async def recover_stuck(maker: async_sessionmaker[AsyncSession], cfg: RepairConfig) -> int:
    """At worker start: repairs a crash left mid-flight are failed and cleaned up."""
    async with maker() as db, db.begin():
        stuck = (
            await db.scalars(
                select(RepairTask).where(
                    RepairTask.status.in_(("running", "patch_ready", "tests_passed"))
                )
            )
        ).all()
        for t in stuck:
            repo = cfg.coding.repos.get(t.repo)
            if repo is not None and (t.worktree or t.branch):
                try:
                    await _cleanup(
                        Git(repo.path), Path(t.worktree) if t.worktree else None, t.branch
                    )
                except GitError:
                    pass
            t.status = "failed"
            t.error = "worker restarted during the repair"
            t.worktree = None
            t.finished_at = _now()
    return len(stuck)
