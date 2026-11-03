"""Network git for the coding agent: clone, fetch/fast-forward and push of allowlisted GitHub
repos (docs/05-agents.md, "Git ops"). Everything else in the agent keeps
`protocol.allow=never` (app/agents/coding/git.py).

Token handling (D-24-2):
- the remote URL is always `<GITHUB_GIT_URL>/<owner>/<repo>.git`, never with credentials in
  it, so nothing secret is written to `.git/config` (clone stores the URL it was given);
- the token reaches git only through the environment of that one invocation
  (`AUREON_GIT_TOKEN`), read by a GIT_ASKPASS script that holds no secret itself. It never
  appears in argv (visible in `ps`), in any git config, or in a log line;
- credential helpers and user/system config are disabled, so git cannot store it either;
- every message that leaves this module goes through `redact()` with the token.

Pushes are plain fast-forward pushes of one explicit `refs/heads/<branch>` refspec: no
`--force`, no tags, no mirror, no deletes. Which branches may be pushed is decided by
`check_push_branch` (pure, tested) before any network call.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.agents.coding.git import EMPTY_HOOKS, GitError
from app.agents.coding.paths import is_blocked
from app.core.sensitive_paths import classify_path
from app.integrations.github.redact import redact

FULL_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
# A pushable branch name: conservative git ref rules (no "..", "@{", "//", leading "-").
PUSH_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
# Never pushed, whatever the settings say (compared case-insensitively).
ALWAYS_REFUSED = frozenset({"main", "master", "dev01", "head"})
TOKEN_ENV = "AUREON_GIT_TOKEN"  # noqa: S105 - an env var NAME, not a secret
# Holds no secret: it prints the token from its own environment, which git hands it.
ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  Username*) echo "x-access-token" ;;\n'
    "  *) printf '%s\\n' \"$" + TOKEN_ENV + '" ;;\n'
    "esac\n"
)
BASE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/nonexistent-aureon-home",
    "LC_ALL": "C",
}


class PushRefused(ValueError):
    """A push the rules forbid; the message is safe to show."""


def check_full_name(full: str) -> str:
    if not FULL_NAME.match(full) or ".." in full:
        raise PushRefused("repository must look like owner/name")
    return full.lower()


def check_push_branch(
    branch: str,
    *,
    default_branch: str | None,
    protected: Iterable[str],
    prefixes: Sequence[str],
    remote_protected: bool = False,
) -> str:
    """The branch if it may be pushed, else PushRefused with the reason.

    Refused: anything that is not a plain branch name; main, master, dev01; the repo's
    default branch; every name in `protected` (CODE_PROTECTED_BRANCHES); a branch GitHub
    reports as protected; anything not under one of `prefixes` (e.g. aureon/, feat/).
    """
    b = branch.strip()
    if b != branch or not PUSH_BRANCH.match(b):
        raise PushRefused("invalid branch name")
    if (
        ".." in b
        or "//" in b
        or "@{" in b
        or b.endswith(("/", ".", ".lock"))
        or b.startswith("refs/")
        or "/." in b
    ):
        raise PushRefused("invalid branch name")
    low = b.lower()
    if low in ALWAYS_REFUSED:
        raise PushRefused(f"pushing to {b} is never allowed")
    if default_branch is None:
        raise PushRefused("the repository's default branch is unknown; refusing to push")
    if low == default_branch.lower():
        raise PushRefused(f"{b} is the repository's default branch")
    if low in {p.strip().lower() for p in protected if p.strip()}:
        raise PushRefused(f"{b} is a protected branch")
    if remote_protected:
        raise PushRefused(f"{b} is protected on GitHub")
    ok = [p for p in prefixes if p and b.startswith(p) and len(b) > len(p)]
    if not ok:
        raise PushRefused(
            "branch must start with " + " or ".join(repr(p) for p in prefixes if p)
            if any(prefixes)
            else "no push branch prefixes are configured"
        )
    return b


def secret_paths(paths: Iterable[str]) -> list[str]:
    """Paths a pushed commit may never touch: the shared `secrets` category plus the coding
    agent's own blocklist (.git, .env*, keys). `.env.example`-style files are allowed."""
    out = []
    for p in paths:
        if not p:
            continue
        if classify_path(p) == "secrets" or is_blocked(p.lower()):
            out.append(p)
    return sorted(set(out))


@dataclass(frozen=True, slots=True)
class GitResult:
    stdout: str
    stderr: str


class RemoteGit:
    """Runs the few network git commands the agent needs, with the token only in the
    child's environment. `allow_file` exists for tests (a local bare repo as "GitHub")."""

    def __init__(
        self,
        git_url: str,
        token: str | None,
        *,
        timeout: float = 600.0,
        allow_file: bool = False,
    ) -> None:
        parts = urlsplit(git_url)
        if parts.scheme not in ("https", "file") or (parts.scheme == "file" and not allow_file):
            raise GitError("GITHUB_GIT_URL must be https")
        if parts.username or parts.password or "@" in parts.netloc:
            raise GitError("GITHUB_GIT_URL must not carry credentials")
        self.scheme = parts.scheme
        self.base = git_url.rstrip("/")
        self._token = token or None
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------------------
    def remote_url(self, full: str) -> str:
        return f"{self.base}/{check_full_name(full)}.git"

    def _config_args(self) -> list[str]:
        return [
            "-c", f"core.hooksPath={EMPTY_HOOKS}",
            "-c", "commit.gpgsign=false",
            "-c", "credential.helper=",  # an empty value clears every helper: nothing stored
            "-c", "protocol.allow=never",
            "-c", f"protocol.{self.scheme}.allow=always",
            "-c", "submodule.recurse=false",
            "-c", "push.followTags=false",
            "-c", "core.askPass=",  # GIT_ASKPASS below wins anyway; no config fallback
        ]  # fmt: skip

    def redact(self, text: str) -> str:
        return redact(text, [self._token])

    async def run(self, *args: str, cwd: Path, network: bool = False) -> GitResult:
        env = dict(BASE_ENV)
        askdir: str | None = None
        if network and self._token:
            askdir = tempfile.mkdtemp(prefix="aureon-askpass-")
            script = Path(askdir) / "askpass.sh"
            script.write_text(ASKPASS_SCRIPT)
            script.chmod(stat.S_IRWXU)
            env["GIT_ASKPASS"] = str(script)
            env[TOKEN_ENV] = self._token
        cmd = ["git", *self._config_args(), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    out, err = await proc.communicate()
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise GitError(f"git {args[0]} timed out") from exc
        finally:
            if askdir:
                shutil.rmtree(askdir, ignore_errors=True)
        o = self.redact(out.decode("utf-8", "replace"))
        e = self.redact(err.decode("utf-8", "replace"))
        if proc.returncode != 0:
            raise GitError(f"git {args[0]} failed: {e.strip()[:400]}")
        return GitResult(o, e)

    # -- operations --------------------------------------------------------------------
    async def clone(self, full: str, dest: Path) -> None:
        if await asyncio.to_thread(dest.exists):
            raise GitError("already cloned; pull instead")
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        await self.run(
            "clone", "--no-recurse-submodules", "--no-tags", "--origin", "origin",
            "--", self.remote_url(full), str(dest),
            cwd=dest.parent, network=True,
        )  # fmt: skip
        assert_no_credentials(dest)

    async def default_branch(self, repo: Path) -> str | None:
        """origin's HEAD as recorded by clone/fetch, else the checked-out branch."""
        for args in (
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
            ("symbolic-ref", "--short", "HEAD"),
        ):
            try:
                out = (await self.run(*args, cwd=repo)).stdout.strip()
            except GitError:
                continue
            if out:
                return out.removeprefix("origin/")
        return None

    async def pull(self, full: str, repo: Path, default_branch: str) -> str:
        """Fetch origin and fast-forward the local default branch. Returns its new sha."""
        self._check_origin(full, (await self.run("remote", "get-url", "origin", cwd=repo)).stdout)
        await self.run("fetch", "--prune", "--no-tags", "origin", cwd=repo, network=True)
        cur = (await self.run("symbolic-ref", "--short", "HEAD", cwd=repo)).stdout.strip()
        if cur == default_branch:
            await self.run("merge", "--ff-only", "--no-edit", f"origin/{default_branch}", cwd=repo)
        else:  # never touch a working tree that is not on the default branch
            await self.run(
                "fetch", ".", f"refs/remotes/origin/{default_branch}:refs/heads/{default_branch}",
                cwd=repo,
            )  # fmt: skip
        assert_no_credentials(repo)
        return (await self.run("rev-parse", default_branch, cwd=repo)).stdout.strip()

    async def touched_files(self, repo: Path, base: str, branch: str) -> list[str]:
        """Every path any commit in base..branch touches (not just the net diff, so a secret
        added and removed again is still caught)."""
        out = await self.run(
            "log", "--no-renames", "--name-only", "--format=", f"{base}..refs/heads/{branch}",
            "--", cwd=repo,
        )  # fmt: skip
        return sorted({line.strip() for line in out.stdout.splitlines() if line.strip()})

    async def push(self, full: str, repo: Path, branch: str) -> str:
        """Push one branch, fast-forward only. Returns the pushed sha."""
        self._check_origin(full, (await self.run("remote", "get-url", "origin", cwd=repo)).stdout)
        sha = (
            await self.run("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=repo)
        ).stdout.strip()
        await self.run(
            "push", "--no-verify", "--no-follow-tags", "--porcelain", "origin",
            f"refs/heads/{branch}:refs/heads/{branch}",
            cwd=repo, network=True,
        )  # fmt: skip
        assert_no_credentials(repo)
        return sha

    def _check_origin(self, full: str, url: str) -> None:
        if url.strip() != self.remote_url(full):
            raise GitError("origin is not the expected GitHub URL; refusing")


def assert_no_credentials(repo: Path) -> None:
    """Defence in depth: no URL with userinfo and no token shape in the repo's git config."""
    cfg = repo / ".git" / "config"
    if not cfg.is_file():
        return
    text = cfg.read_text(errors="replace")
    if redact(text) != text or re.search(r"://[^/\s]*@", text):
        raise GitError("credentials found in .git/config; refusing")
