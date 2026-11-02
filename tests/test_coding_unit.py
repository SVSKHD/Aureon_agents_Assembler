"""Coding agent guards: path escapes, command allowlist, patch validation, repo config."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.coding.commands import (
    DEFAULT_ALLOWED,
    CommandRejected,
    check_allowed,
    run_allowed,
    scrubbed_env,
)
from app.agents.coding.patch import PatchRejected, extract, validate
from app.agents.coding.paths import PathRejected, normalize, safe_path
from app.agents.coding.repos import RepoNotAllowed, get_repo, parse_repos


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("x")
    (root / ".git").mkdir()
    (root / ".env").write_text("SECRET=1")
    (tmp_path / "outside.txt").write_text("nope")
    return root


@pytest.mark.parametrize(
    "rel",
    [
        "../outside.txt",
        "src/../../outside.txt",
        "/etc/passwd",
        ".git/config",
        "src/.git/HEAD",
        ".env",
        "app/.env.local",
        "deploy/server.pem",
        "home/.ssh/id_rsa",
        "a\x00b",
        "src\\..\\x",
        "config/secrets/db.json",
    ],
)
def test_path_escapes_and_secrets_are_rejected(repo: Path, rel: str) -> None:
    with pytest.raises(PathRejected):
        safe_path(repo, rel)


def test_symlink_out_of_the_repo_is_rejected(repo: Path, tmp_path: Path) -> None:
    os.symlink(tmp_path / "outside.txt", repo / "src" / "link.txt")
    os.symlink(repo / ".git", repo / "gitlink")
    with pytest.raises(PathRejected, match="escapes"):
        safe_path(repo, "src/link.txt")
    with pytest.raises(PathRejected, match="off limits"):
        safe_path(repo, "gitlink/config")


def test_allowed_paths(repo: Path) -> None:
    assert safe_path(repo, "src/a.ts") == (repo / "src" / "a.ts").resolve()
    assert safe_path(repo, "./src//a.ts") == (repo / "src" / "a.ts").resolve()
    assert normalize("") == ""
    assert normalize(".env.example") == ".env.example"  # documentation, not a secret


@pytest.mark.parametrize(
    "argv",
    [
        ["npm", "run", "deploy"],
        ["npm", "test", "--", "--reporter", "x"],  # extra args are not the same command
        ["bash", "-c", "npm test"],
        ["sh", "-c", "rm -rf /"],
        ["npm test"],
        ["python", "-c", "import os"],
        ["git", "push"],
        ["curl", "https://example.com"],
        [],
    ],
)
def test_non_allowlisted_commands_are_rejected(argv: list[str]) -> None:
    with pytest.raises(CommandRejected):
        check_allowed(argv, DEFAULT_ALLOWED)


def test_allowlisted_command_matches_exactly() -> None:
    assert check_allowed(["npm", "test"], DEFAULT_ALLOWED) == ("npm", "test")


async def test_run_scrubs_env_and_enforces_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    env = scrubbed_env(tmp_path)
    assert "DATABASE_URL" not in env and env["CI"] == "1"
    allowed = [("env",), ("sleep", "5")]
    r = await run_allowed(["env"], allowed=allowed, cwd=tmp_path, limit_s=10, home=tmp_path)
    assert r.ok and "DATABASE_URL" not in r.output and "CI=1" in r.output
    slow = await run_allowed(
        ["sleep", "5"], allowed=allowed, cwd=tmp_path, limit_s=0.3, home=tmp_path
    )
    assert slow.timed_out and not slow.ok and slow.duration_ms < 4000
    with pytest.raises(CommandRejected):
        await run_allowed(["sleep", "1"], allowed=allowed, cwd=tmp_path, limit_s=5, home=tmp_path)


GOOD = """Here is the change:

```diff
diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-x
+y
```
"""


def test_patch_extract_and_validate() -> None:
    p = validate(extract(GOOD), 10_000)
    assert p.files == ("src/a.ts",)
    assert p.text.startswith("diff --git")


@pytest.mark.parametrize(
    ("diff", "why"),
    [
        ("--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n", "'..'"),
        ("--- /dev/null\n+++ b/.git/hooks/pre-commit\n@@ -0,0 +1 @@\n+x\n", "off limits"),
        ("--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n", "off limits"),
        ("diff --git a/x b/x\nnew file mode 120000\n--- /dev/null\n+++ b/x\n", "120000"),
        ("diff --git a/x b/x\nGIT binary patch\nliteral 1\n", "binary"),
        ("diff --git a/run.sh b/run.sh\nnew mode 100755\n", "100755"),
        ("diff --git a/a b/a\nrename from a\nrename to ../b\n", "'..'"),
    ],
)
def test_dangerous_patches_are_rejected(diff: str, why: str) -> None:
    with pytest.raises(PatchRejected, match=why if why != "binary" else "binary"):
        validate(diff, 10_000)


def test_patch_size_and_no_diff() -> None:
    with pytest.raises(PatchRejected, match="larger"):
        validate(extract(GOOD), 10)
    with pytest.raises(PatchRejected, match="unified diff"):
        extract("Sorry, I cannot do that.")


def test_repo_config() -> None:
    repos = parse_repos(
        {
            "astra-frontend": {
                "path": "/srv/a",
                "base": "dev01",
                "tests": [["npm", "test"]],
                "link": ["node_modules"],
            }
        }
    )
    assert repos["astra-frontend"].tests == (("npm", "test"),)
    with pytest.raises(RepoNotAllowed):
        get_repo(repos, "other")
    for bad in (
        {"x": {"path": "relative/path"}},
        {"Bad Name": {"path": "/a"}},
        {"x": {"path": "/a", "base": "../main"}},
        {"x": {"path": "/a", "link": ["../etc"]}},
    ):
        with pytest.raises(ValueError):
            parse_repos(bad)
