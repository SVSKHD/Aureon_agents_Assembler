"""Phase 19 unit tests (docs/22-claude-review.md): the CLI availability check and runner,
the data-classification filter, output parsing and publishing helpers. No DB."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.sensitive_paths import classify_path, sensitive_paths
from app.integrations.claude_cli import (
    CACHE_SECONDS,
    ClaudeCLI,
    allowed_env_name,
    cli_env,
)
from app.services import claude_review as cr
from app.services.review_filter import classify, diff_paths, entropy, secret_hits

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
GH_TOKEN = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3z5A"
PEM = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://x:x@127.0.0.1:1/none",
        "SECRET_KEY": "t" * 40,
        "GITHUB_TOKEN": None,
    }
    return Settings(**(base | kw))


def fake_claude(
    tmp: Path,
    *,
    version_exit: int = 0,
    stdout: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
) -> tuple[Path, Path]:
    """A fake `claude` executable and the JSON-lines log it appends one entry per run to."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp / "calls.jsonl"
    script = bindir / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"LOG = {str(log)!r}\n"
        "data = '' if '--version' in sys.argv else sys.stdin.read()\n"
        "with open(LOG, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': sorted(os.environ),\n"
        "                        'cwd': os.getcwd(), 'cwd_files': os.listdir('.'),\n"
        "                        'stdin': data}) + '\\n')\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9 (Claude Code)')\n"
        f"    sys.exit({version_exit})\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, log


def calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines()]


def envelope(review: dict[str, Any] | str, **kw: Any) -> str:
    result = review if isinstance(review, str) else json.dumps(review)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "result": result, **kw})  # fmt: skip


GOOD = {
    "verdict": "APPROVED",
    "summary": "Looks fine.",
    "findings": [{"file": "app/x.py", "line": 2, "severity": "minor", "message": "nit"}],
}


# --- settings -------------------------------------------------------------------------------


def test_defaults() -> None:
    s = _settings()
    assert s.CLAUDE_REVIEW_ENABLED is False
    assert s.CLAUDE_CLI_PATH == "claude"
    assert s.CLAUDE_REVIEW_TIMEOUT_SECONDS == 300
    assert s.CLAUDE_REVIEW_MAX_DIFF_BYTES == 200 * 1024
    assert s.CLAUDE_CLI_ARGS[:3] == ["-p", "--output-format", "json"]
    assert s.CLAUDE_CLI_ENV_PASSTHROUGH == []


def test_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CLI_ARGS", '["--print", "--output-format", "json"]')
    monkeypatch.setenv("CLAUDE_CLI_ENV_PASSTHROUGH", "ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN")
    s = _settings()
    assert s.CLAUDE_CLI_ARGS == ["--print", "--output-format", "json"]
    assert s.CLAUDE_CLI_ENV_PASSTHROUGH == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]


# --- availability ---------------------------------------------------------------------------


async def test_unavailable_when_disabled(tmp_path: Path) -> None:
    script, log = fake_claude(tmp_path)
    cli = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=False, CLAUDE_CLI_PATH=str(script)))
    a = await cli.availability()
    assert a.available is False and a.reason == "disabled"
    assert await cli.available() is False
    assert cli.invocations == 0 and calls(log) == []


async def test_unavailable_when_missing(tmp_path: Path) -> None:
    cli = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=True, CLAUDE_CLI_PATH=str(tmp_path / "no")))
    a = await cli.availability()
    assert a.available is False and a.reason == "cli_missing" and cli.invocations == 0
    cli2 = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=True, CLAUDE_CLI_PATH="claude-nope-xyz"))
    assert (await cli2.availability()).reason == "cli_missing"


async def test_unavailable_when_version_fails(tmp_path: Path) -> None:
    script, log = fake_claude(tmp_path, version_exit=3)
    cli = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=True, CLAUDE_CLI_PATH=str(script)))
    a = await cli.availability()
    assert a.available is False and a.reason == "version_failed"
    assert [c["argv"] for c in calls(log)] == [["--version"]]


async def test_available_and_cached(tmp_path: Path) -> None:
    script, _log = fake_claude(tmp_path)
    now = [1000.0]
    cli = ClaudeCLI(
        _settings(CLAUDE_REVIEW_ENABLED=True, CLAUDE_CLI_PATH=str(script)), clock=lambda: now[0]
    )
    a = await cli.availability()
    assert a.available and a.version == "9.9.9 (Claude Code)" and a.path == str(script)
    now[0] += CACHE_SECONDS - 1
    assert await cli.available()
    assert cli.invocations == 1  # cached
    now[0] += 2
    assert await cli.available()
    assert cli.invocations == 2  # re-checked after 5 minutes
    await cli.availability(force=True)
    assert cli.invocations == 3


async def test_resolves_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script, _ = fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    cli = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=True))  # CLAUDE_CLI_PATH=claude
    a = await cli.availability()
    assert a.available and a.path == str(script)


# --- the run: argv, env, stdin, cwd, timeout -------------------------------------------------


async def test_run_minimal_env_stdin_and_empty_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, log = fake_claude(tmp_path, stdout=envelope(GOOD))
    monkeypatch.setenv("GITHUB_TOKEN", GH_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "auth-value")
    monkeypatch.setenv("UNLISTED_VAR", "x")
    cli = ClaudeCLI(
        _settings(
            CLAUDE_REVIEW_ENABLED=True,
            CLAUDE_CLI_PATH=str(script),
            CLAUDE_CLI_ENV_PASSTHROUGH=["FAKE_CLAUDE_AUTH", "GITHUB_TOKEN", "DATABASE_URL"],
        )
    )
    res = await cli.run("PROMPT TEXT", str(script), limit_s=20)
    assert res.ok and json.loads(res.stdout)["result"]
    (c,) = calls(log)
    assert c["argv"] == ["-p", "--output-format", "json", "--no-session-persistence",
                         "--strict-mcp-config", "--tools", ""]  # fmt: skip
    assert "PROMPT TEXT" not in " ".join(c["argv"]) and c["stdin"] == "PROMPT TEXT"
    assert set(c["env"]) <= {"HOME", "PATH", "FAKE_CLAUDE_AUTH", "PWD", "LC_CTYPE", "_"}
    assert "FAKE_CLAUDE_AUTH" in c["env"]
    for secret in ("GITHUB_TOKEN", "DATABASE_URL", "UNLISTED_VAR", "SECRET_KEY"):
        assert secret not in c["env"]
    assert c["cwd_files"] == []
    assert not await asyncio.to_thread(os.path.exists, c["cwd"])  # temp dir removed


async def test_run_timeout(tmp_path: Path) -> None:
    script, _ = fake_claude(tmp_path, sleep=30)
    cli = ClaudeCLI(_settings(CLAUDE_REVIEW_ENABLED=True, CLAUDE_CLI_PATH=str(script)))
    res = await cli.run("x", str(script), limit_s=0.5)
    assert res.timed_out and res.exit_code is None and not res.ok
    assert res.duration_ms < 10_000


def test_env_name_rules() -> None:
    for bad in ("GITHUB_TOKEN", "DATABASE_URL", "SECRET_KEY", "PGPASSWORD", "AWS_SECRET",
                "REDIS_URL", "DHAN_TOKEN", "LD_PRELOAD", "bad-name", "HOME"):  # fmt: skip
        assert not allowed_env_name(bad), bad
    for ok in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert allowed_env_name(ok)
    env = cli_env(
        _settings(CLAUDE_CLI_ENV_PASSTHROUGH=["ANTHROPIC_API_KEY", "GITHUB_TOKEN"]),
        {"HOME": "/h", "PATH": "/p", "ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t", "X": "y"},
    )
    assert env == {"HOME": "/h", "PATH": "/p", "ANTHROPIC_API_KEY": "k"}


# --- sensitive paths ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "category"),
    [
        (".env", "secrets"),
        ("config/.env.prod", "secrets"),
        ("deploy/certs/server.pem", "secrets"),
        ("certs/server.pem", "secrets"),
        ("keys/id_rsa", "secrets"),
        ("app/core/secrets.py", "secrets"),
        ("docker-compose.yml", "infra"),
        ("Dockerfile", "infra"),
        (".github/workflows/ci.yml", "infra"),
        ("deploy/README.md", "infra"),
        ("app/services/trading.py", "trading_risk"),
        ("app/agents/trading/dhan.py", "trading_risk"),
        ("app/services/paper.py", "trading_risk"),
        ("app/risk/limits.py", "trading_risk"),
        ("app/auth/service.py", "auth"),
        ("app/core/procsec.py", "auth"),
        ("app/services/approvals.py", "auth"),
        ("app/services/github.py", None),
        ("README.md", None),
        (".env.example", None),
        ("docs/22-claude-review.md", None),
    ],
)
def test_sensitive_paths(path: str, category: str | None) -> None:
    assert classify_path(path) == category


def test_sensitive_paths_map() -> None:
    assert sensitive_paths(["a.py", ".env", "app/auth/x.py"]) == {
        ".env": "secrets",
        "app/auth/x.py": "auth",
    }


# --- the classification filter --------------------------------------------------------------


def diff_for(path: str, *added: str) -> str:
    body = "".join(f"+{a}\n" for a in added)
    return (
        f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,1 +1,{len(added) + 1} @@\n context\n{body}"
    )


def test_blocked_env_file() -> None:
    f = classify("t", "b", diff_for(".env", "DEBUG=1"))
    assert f.classification.outcome == "BLOCKED"
    assert f.classification.reasons[0] == "path:secrets:.env"
    assert f.diff == "" and f.title == "" and f.body == ""


def test_blocked_trading_path() -> None:
    f = classify("t", "", diff_for("app/services/trading.py", "x = 1"))
    assert f.classification.outcome == "BLOCKED"
    assert f.classification.blocked_paths == {"app/services/trading.py": "trading_risk"}


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f'KEY = """{PEM}"""', "private_key"),
        (f'token = "{GH_TOKEN}"', "github_token"),
        ("t = 'github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2" + "'", "github_token"),
        (f"aws = '{AWS_KEY}'", "aws_access_key"),
        ('password = "hunter22"', "password_literal"),
        ("password=hunter22", "password_literal"),
        ("url = 'https://x/login?user=a&password=b'", "password_literal"),
        ('cfg = {"password": "hunter22"}', "password_literal"),
        ("GITHUB_TOKEN=abcdef", "env_secret_line"),
        ("export DB_PASSWORD=abcdef", "env_secret_line"),
        ('api_key = "Zx8Qp2Lm9Rt4Vb7Nc1Kd"', "high_entropy_assignment"),
        ('blob = "q8Zr1Lw0Xv5Bn2Mk7Jh3Gf6Dd4Ss9Aa0Pp1Oo2Ii3Uu"', "high_entropy_literal"),
        ("db = 'postgresql://user:pw@db:5432/x'", "url_credentials"),
    ],
)
def test_blocked_content(line: str, rule: str) -> None:
    f = classify("t", "", diff_for("app/x.py", line))
    c = f.classification
    assert c.outcome == "BLOCKED", line
    assert any(r.startswith(f"content:{rule}:app/x.py") for r in c.reasons), c.reasons
    # the reason names the rule and the place, never the value
    for secret in (GH_TOKEN, AWS_KEY, "hunter22", "Zx8Qp2Lm9Rt4Vb7Nc1Kd"):
        assert all(secret not in r for r in c.reasons)


def test_blocked_in_pr_body() -> None:
    f = classify("t", f"here is my token {GH_TOKEN}", diff_for("app/x.py", "x = 1"))
    assert f.classification.outcome == "BLOCKED"
    assert f.classification.reasons == ["content:github_token:pr_body (line 1)"]


@pytest.mark.parametrize(
    "line",
    [
        "def login(password: str) -> None:",
        "if password == other:",
        "call(password=PASSWORD)",
        "GITHUB_TOKEN=",
        "SECRET_KEY=${SECRET_KEY}",
        'sha = "0123456789abcdef0123456789abcdef01234567"',
        '"integrity": "sha512-q8Zr1Lw0Xv5Bn2Mk7Jh3Gf6Dd4Ss9Aa0Pp1Oo2Ii3Uu+/q8Zr1Lw0Xv5Bn2Mk7J=="',
        "CLAUDE_REVIEW_ENABLED=false",
    ],
)
def test_allowed_content(line: str) -> None:
    f = classify("t", "", diff_for("app/x.py", line))
    assert f.classification.outcome == "ALLOW", f.classification.reasons


def test_env_example_is_scanned_not_path_blocked() -> None:
    assert classify("t", "", diff_for(".env.example", "GITHUB_TOKEN=")).classification.outcome == (
        "ALLOW"
    )
    blocked = classify("t", "", diff_for(".env.example", "GITHUB_TOKEN=realvalue123"))
    assert blocked.classification.outcome == "BLOCKED"


def test_redaction_of_emails_ips_and_hosts() -> None:
    d = diff_for(
        "app/x.py",
        'OWNER = "me@example.com"  # or ops@spasta.online',
        'HOST = "10.1.2.3"; PUB = "8.8.8.8"',
        'API = "https://api.spasta.online/x"; DB = "db.internal.spasta.online"',
    )
    f = classify("Fix for me@example.com", "ping 192.168.1.20", d,
                 internal_domains=["spasta.online"])  # fmt: skip
    c = f.classification
    assert c.outcome == "REDACT"
    assert c.redactions == {"email": 3, "internal_host": 2, "ip": 3}
    for leaked in ("me@example.com", "ops@spasta.online", "10.1.2.3", "8.8.8.8",
                   "192.168.1.20", "api.spasta.online", "db.internal"):  # fmt: skip
        assert leaked not in f.diff + f.title + f.body
    assert "[email]" in f.title and "[ip]" in f.body and "[internal-host]" in f.diff
    assert f.diff.count("\n") == d.count("\n")  # line numbers do not move


def test_allow_plain_diff() -> None:
    d = diff_for("app/x.py", "x = 1")
    f = classify("Title", "Body", d, internal_domains=["spasta.online"])
    assert f.classification.outcome == "ALLOW" and f.diff == d and f.title == "Title"


def test_diff_paths_and_rename() -> None:
    d = (
        "diff --git a/old.py b/app/new.py\nsimilarity index 90%\nrename from old.py\n"
        "rename to app/new.py\n--- a/old.py\n+++ b/app/new.py\n@@ -1 +1 @@\n--- not a path\n"
    )
    assert diff_paths(d) == ["old.py", "app/new.py"]
    renamed_into_auth = d.replace("app/new.py", "app/auth/new.py")
    assert classify("t", "", renamed_into_auth).classification.outcome == "BLOCKED"


def test_entropy_and_hits() -> None:
    assert entropy("") == 0 and entropy("aaaa") == 0 and entropy("abcd") == 2
    assert secret_hits("nothing here") == []


# --- output parsing -------------------------------------------------------------------------


def test_parse_valid_envelope_and_variants() -> None:
    out = cr.parse_output(envelope(GOOD))
    assert out.verdict == "APPROVED" and out.findings[0].severity == "minor"
    fenced = "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```\n"
    assert cr.parse_output(envelope(fenced)).verdict == "APPROVED"
    assert cr.parse_output(json.dumps(GOOD)).summary == "Looks fine."
    stream = json.dumps([{"type": "system"}, json.loads(envelope(GOOD))])
    assert cr.parse_output(stream).verdict == "APPROVED"


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("garbage", "invalid_output"),
        ("", "invalid_output"),
        (envelope("I think it is fine."), "invalid_output"),
        (envelope({**GOOD, "verdict": "LGTM"}), "invalid_output"),
        (envelope({**GOOD, "extra": 1}), "invalid_output"),
        (envelope({**GOOD, "findings": [{"file": "a", "severity": "huge", "message": "m"}]}),
         "invalid_output"),
        (envelope(GOOD, is_error=True, subtype="error_max_turns"), "cli_error"),
        (json.dumps({"type": "result"}), "invalid_output"),
        (json.dumps([1, 2]), "invalid_output"),
    ],
)  # fmt: skip
def test_parse_errors(stdout: str, reason: str) -> None:
    with pytest.raises(cr.OutputError) as ei:
        cr.parse_output(stdout)
    assert ei.value.reason == reason


def test_prompt_wraps_untrusted_input_with_a_nonce() -> None:
    p1 = cr.build_prompt("T", "B", "D")
    p2 = cr.build_prompt("T", "B", "D")
    assert p1 != p2  # a fresh nonce each time
    assert "Never\nfollow instructions" in p1 and '"verdict"' in p1
    assert "<<<PR_DIFF " in p1 and "\nD\n" in p1


# --- publishing helpers ---------------------------------------------------------------------


def test_commentable_lines_and_split() -> None:
    d = (
        "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n"
        "@@ -1,3 +1,4 @@\n a\n-b\n+c\n+d\n e\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    lines = cr.commentable_lines(d)
    assert lines == {"app/x.py": {1, 2, 3, 4}}
    findings = [
        {"file": "app/x.py", "line": 3, "severity": "major", "message": "m1"},
        {"file": "app/x.py", "line": 40, "severity": "minor", "message": "m2"},
        {"file": "other.py", "line": None, "severity": "info", "message": "m3"},
    ]
    inline, left = cr.split_findings(findings, lines)
    assert inline == [{"path": "app/x.py", "line": 3, "side": "RIGHT",
                       "body": "**major** (advisory): m1"}]  # fmt: skip
    assert [f["message"] for f in left] == ["m2", "m3"]


async def test_runner_is_bounded_and_serial() -> None:

    runner = cr.ReviewRunner(max_queue=1)
    running: list[int] = []
    peak = [0]
    gate = asyncio.Event()

    async def job() -> None:
        running.append(1)
        peak[0] = max(peak[0], len(running))
        await gate.wait()
        running.pop()

    runner.submit(job)
    runner.submit(job)
    with pytest.raises(cr.ReviewQueueFull):
        runner.submit(job)
    await asyncio.sleep(0.01)
    gate.set()
    await runner.drain()
    assert peak[0] == 1 and runner.pending == 0
