"""Phase 17 units (docs/20-github-actions-webhooks.md): log sanitising, the zip caps, the log
host restriction, the blob download, webhook signatures and payload trimming, the dispatch
policy, and the scheduler wiring. No DB."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config import Settings
from app.integrations.github.client import GitHubClient, GitHubError
from app.integrations.github.logs import (
    LogArchiveError,
    log_host_allowed,
    redact_log,
    sanitize,
    strip_ansi,
    tail,
    unzip_logs,
)
from app.services import github_webhooks as hooks
from app.services.github_actions import DispatchPolicy, check_workflow, workflow_file
from app.services.github_webhooks import trim_payload, verify_signature
from app.workers.scheduler import TASKS, default_jobs

API = "https://api.github.test"
TOKEN = "github_pat_" + "Z1x2C3v4" * 10
BLOB = "https://pipelines.actions.githubusercontent.com/abc/logs/1?sig=SECRETSIG"
HOSTS = [
    "pipelines.actions.githubusercontent.com",
    "*.actions.githubusercontent.com",
    "productionresultssa*.blob.core.windows.net",
]


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- sanitising ---------------------------------------------------------------------------


def test_strip_ansi() -> None:
    raw = (
        "\x1b[36;1mRun tests\x1b[0m\r\n\x1b]0;title\x07ok \x1b[2K\x1b[1Gdone\x1b(B\r"
        "bell\x07 nul\x00 tab\tend"
    )
    assert strip_ansi(raw) == "Run tests\nok done\nbell nul tab\tend"


@pytest.mark.parametrize(
    "line",
    [
        "token ghp_" + "a1B2c3D4e5" * 4,
        "push with github_pat_" + "Q9w8E7r6" * 8,
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "found key AKIAIOSFODNN7EXAMPLE in env",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "Authorization: Bearer abc.def.ghi-123456789",
        "authorization: token 0123456789abcdef0123",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "curl -H 'X-Api-Key: k3y-v4lu3-12345'",
        "DB_PASSWORD=hunter2hunter2",
        "MY_SERVICE_KEY=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5",
        "export RANDOM_BLOB=q8Zr4LmX2vB7nT1wK9pY3sD6fH0jG5cE",
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n",
        "clone https://deploy:s3cr3tpassw0rd@github.com/me/app.git",
        "slack xoxb-123456789012-abcdefghijkl",
    ],
)
def test_redact_log_hides_secrets(line: str) -> None:
    out = redact_log(line)
    assert "[REDACTED]" in out
    secretish = [
        "a1B2c3D4e5a1B2", "Q9w8E7r6Q9w8", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI", "abc.def.ghi",
        "0123456789abcdef0123", "dXNlcjpwYXNzd29yZA", "k3y-v4lu3", "hunter2", "Zm9vYmFy",
        "q8Zr4LmX2vB7", "dozjgNryP4J3", "s3cr3tpassw0rd", "abcdefghijkl",
    ]  # fmt: skip
    assert not any(s in out for s in secretish), out


@pytest.mark.parametrize(
    "line",
    [
        "HEAD is now at 0123456789abcdef0123456789abcdef01234567",
        "sha=0123456789abcdef0123456789abcdef01234567",
        "commit_sha: 9fceb02d0ae598e95dc970b74767f19372d61af8",
        "Run actions/checkout@v4",
        "url: https://github.com/SVSKHD/Aureon_agents_Assembler/actions/runs/123456789",
        "path=/home/runner/work/Aureon_agents_Assembler/Aureon_agents_Assembler/tests",
        "2026-11-02T10:00:00.1234567Z ##[group]Run pytest -q",
        "SECRET_KEY=***",
        "===== 1 passed, 2 skipped in 3.21s =====",
    ],
)
def test_redact_log_keeps_ordinary_lines(line: str) -> None:
    assert redact_log(line) == line


def test_private_key_blocks_and_orphaned_tails() -> None:
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "A" * 64 + "\n-----END RSA PRIVATE KEY-----"
    assert redact_log(f"before\n{key}\nafter") == "before\n[REDACTED]\nafter"
    orphan = "MIIEow" + "B" * 64 + "\n-----END RSA PRIVATE KEY-----\nafter"
    assert redact_log(orphan) == "[REDACTED]\nafter"


def test_tail_cuts_at_a_line_start() -> None:
    text = "".join(f"line {i}\n" for i in range(1000))
    out, cut = tail(text, 100)
    assert cut and out.endswith("line 999\n") and out.startswith("line ")
    assert len(out.encode()) <= 100
    assert tail("short", 100) == ("short", False)


def test_sanitize_strips_then_redacts_then_caps() -> None:
    # The escape code inside the token would hide it from the redactor if stripped later.
    text = "x\n" * 5000 + "token ghp_\x1b[0m" + "a1B2c3D4e5" * 4 + "\nlast line\n"
    out = sanitize(text, max_bytes=2048, secrets=(TOKEN,))
    assert out.truncated and "a1B2c3D4e5" not in out.text and "[REDACTED]" in out.text
    assert out.text.startswith("[log truncated") and out.text.endswith("last line\n")
    assert len(out.text.encode()) <= 2048
    assert out.original_bytes == len(text.encode())
    small = sanitize("ok\n", max_bytes=2048)
    assert small.text == "ok\n" and not small.truncated


# --- zip ----------------------------------------------------------------------------------


def test_unzip_prefers_top_level_job_logs_in_order() -> None:
    data = make_zip({
        "2_test.txt": b"test log\n",
        "1_build.txt": b"build log\n",
        "build/1_Set up job.txt": b"step log\n",
        "notes.md": b"ignored",
    })  # fmt: skip
    text, capped = unzip_logs(data, max_total=10_000, max_entries=10)
    assert not capped
    assert text.index("1_build.txt") < text.index("2_test.txt")
    assert "step log" not in text and "ignored" not in text


def test_unzip_caps() -> None:
    big = make_zip({"1_a.txt": b"A" * 50_000, "2_b.txt": b"B" * 50_000})
    text, capped = unzip_logs(big, max_total=60_000, max_entries=10)
    assert capped and text.count("A") == 50_000 and 0 < text.count("B") <= 10_000
    many = make_zip({f"{i}_j.txt": b"x" for i in range(20)})
    with pytest.raises(LogArchiveError) as e:
        unzip_logs(many, max_total=10_000, max_entries=5)
    assert e.value.code == "log_archive_too_many_entries"
    with pytest.raises(LogArchiveError) as e:
        unzip_logs(b"not a zip", max_total=10, max_entries=5)
    assert e.value.code == "log_archive_invalid"
    # A zip bomb: 20 MB of zeros compresses to ~20 KB; the bounded read stops at the cap.
    bomb = make_zip({"1_bomb.txt": b"\0" * 20_000_000})
    assert len(bomb) < 100_000
    text, capped = unzip_logs(bomb, max_total=1_000_000, max_entries=5)
    assert capped and len(text) < 1_100_000


# --- the log host restriction -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://pipelines.actions.githubusercontent.com/x", True),
        ("https://results-receiver.actions.githubusercontent.com/x", True),
        ("https://productionresultssa7.blob.core.windows.net/actions-results/x?sig=1", True),
        ("http://pipelines.actions.githubusercontent.com/x", False),  # not https
        ("https://a.b.actions.githubusercontent.com/x", False),  # * is one label
        ("https://evil.com/x", False),
        ("https://pipelines.actions.githubusercontent.com.evil.com/x", False),
        ("https://evilblob.core.windows.net/x", False),
        ("https://user:pw@pipelines.actions.githubusercontent.com/x", False),
        ("https://pipelines.actions.githubusercontent.com:8443/x", False),
        ("https://api.github.com/x", False),
        ("not a url", False),
    ],
)
def test_log_host_allowed(url: str, ok: bool) -> None:
    assert log_host_allowed(url, HOSTS) is ok


async def test_blob_fetch_sends_no_token_and_refuses_other_hosts() -> None:
    client = GitHubClient(TOKEN, API, backoff_base=0, cache_ttl=0)
    try:
        with respx.mock(assert_all_called=False) as router:
            blob = router.get(BLOB).mock(return_value=httpx.Response(200, content=b"log body"))
            data, cut = await client.fetch_log_blob(BLOB, allowed_hosts=HOSTS, max_bytes=100)
            assert (data, cut) == (b"log body", False)
            assert "authorization" not in blob.calls[0].request.headers
            # a redirect hop to another host is refused before it is requested
            hop = "https://pipelines.actions.githubusercontent.com/hop"
            router.get(hop).mock(
                return_value=httpx.Response(302, headers={"location": "https://evil.com/x"})
            )
            evil = router.get("https://evil.com/x").mock(return_value=httpx.Response(200))
            with pytest.raises(GitHubError) as e:
                await client.fetch_log_blob(hop, allowed_hosts=HOSTS, max_bytes=100)
            assert e.value.code == "github_log_host_refused" and evil.call_count == 0
            assert "SECRETSIG" not in e.value.message
            # the cap, head-first and tail-first
            big = "https://pipelines.actions.githubusercontent.com/big"
            router.get(big).mock(return_value=httpx.Response(200, content=b"0123456789" * 10))
            assert await client.fetch_log_blob(big, allowed_hosts=HOSTS, max_bytes=15) == (
                b"012345678901234",
                True,
            )
            data, cut = await client.fetch_log_blob(
                big, allowed_hosts=HOSTS, max_bytes=1000, tail_bytes=5
            )
            assert (data, cut) == (b"56789", True)
    finally:
        await client.aclose()


async def test_off_host_redirect_returned_only_when_asked() -> None:
    client = GitHubClient(TOKEN, API, backoff_base=0, cache_ttl=0)
    try:
        with respx.mock(base_url=API) as router:
            router.get("/repos/me/app/actions/jobs/1/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB})
            )
            with pytest.raises(GitHubError) as e:
                await client.request("GET", "/repos/me/app/actions/jobs/1/logs")
            assert e.value.code == "github_redirect"
            resp = await client.request(
                "GET", "/repos/me/app/actions/jobs/1/logs", return_redirect=True
            )
            assert resp.status == 302 and resp.data == BLOB
    finally:
        await client.aclose()


# --- webhooks -----------------------------------------------------------------------------


def test_signature() -> None:
    body = b'{"zen":"hi"}'
    good = hooks.signature_for("s3cret", body)
    assert good.startswith("sha256=") and len(good) == 71
    assert verify_signature("s3cret", body, good)
    assert not verify_signature("s3cret", body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "")
    assert not verify_signature("s3cret", body, good[7:])  # no sha256= prefix
    assert not verify_signature("s3cret", body, "sha1=" + good[7:])
    assert not verify_signature("s3cret", body, "sha256=éé")
    assert not verify_signature("", body, good)


def test_trim_payload_keeps_what_the_worker_needs() -> None:
    p = {
        "action": "completed",
        "repository": {"full_name": "me/app", "id": 1, "description": "d" * 1000},
        "workflow_run": {"id": 9, "conclusion": "failure", "name": "CI"},
        "sender": {"login": "me", "bio": "b" * 5000},
        "commits": [{"message": "m" * 5000}],
        "note": TOKEN,
    }
    same, trimmed = trim_payload(p, 100_000)
    assert not trimmed and same["note"] == "[REDACTED]"
    small, trimmed = trim_payload(p, 2000)
    assert trimmed and small["workflow_run"] == p["workflow_run"]
    assert small["commits"] == [] and small["sender"] == {"login": "me"}
    tiny, trimmed = trim_payload(p, 120)
    assert trimmed and len(str(tiny)) < 400


def test_repo_of() -> None:
    assert hooks.repo_of({"repository": {"full_name": "me/app"}}) == "me/app"
    assert hooks.repo_of({"repository": {"full_name": "bad"}}) is None
    assert hooks.repo_of({}) is None


# --- dispatch policy ----------------------------------------------------------------------


def test_dispatch_policy(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "GITHUB_DISPATCH_ALLOWLIST": ["Me/App:ci.yml", "me/app:release.yml"],
            "GITHUB_PROD_WORKFLOWS": ["Release", "ship.yml"],
        }
    )
    p = DispatchPolicy.from_settings(s)
    assert p.allowed("me/app", "CI.yml") and not p.allowed("me/app", "other.yml")
    assert not p.allowed("me/other", "ci.yml") and not p.allowed("me/app", "")
    assert p.is_prod("Release", "release.yml") and p.is_prod("x", "ship.yml")
    assert p.is_prod("x", "ship.yaml") is False
    assert p.is_prod("Deploy site", "site.yml") and p.is_prod("CI", "prod-push.yml")
    assert not p.is_prod("CI", "ci.yml")
    assert workflow_file(".github/workflows/ci.yml@refs/heads/main") == "ci.yml"
    assert workflow_file(None) == ""
    for ok in ("ci.yml", "a.yaml", "12345"):
        assert check_workflow(ok) == ok
    for bad in ("../x.yml", "ci", "a/b.yml", "x.yml;rm"):
        with pytest.raises(ValueError):
            check_workflow(bad)


def test_settings_parse_json_lists() -> None:
    s = Settings(
        SECRET_KEY="k" * 48,
        DATABASE_URL="postgresql+asyncpg://u:p@db:5432/x",
        GITHUB_DISPATCH_ALLOWLIST='["me/app:ci.yml"]',  # type: ignore[arg-type]
        GITHUB_PROD_WORKFLOWS="",  # type: ignore[arg-type]
        GITHUB_WEBHOOK_SECRET=SecretStr("  "),
    )
    assert s.GITHUB_DISPATCH_ALLOWLIST == ["me/app:ci.yml"] and s.GITHUB_PROD_WORKFLOWS == []
    assert not s.github_webhook_configured
    assert s.GITHUB_LOG_MAX_BYTES == 1024 * 1024


def test_actions_sync_is_scheduled_only_with_a_token(settings: Settings) -> None:
    assert "github_actions_sync" in TASKS
    off = settings.model_copy(update={"GITHUB_TOKEN": None})
    assert all(j[1] != "github_actions_sync" for j in default_jobs(off))
    on = settings.model_copy(update={"GITHUB_TOKEN": SecretStr(TOKEN)})
    assert ("github-actions-sync", "github_actions_sync", on.GITHUB_ACTIONS_SYNC_CRON) in (
        default_jobs(on)
    )


async def test_callbacks_registry() -> None:
    seen: list[int] = []

    async def cb(f: hooks.WorkflowFailure) -> None:
        seen.append(f.run_id)

    async def boom(_f: hooks.WorkflowFailure) -> None:
        raise RuntimeError("callback bug")

    hooks.on_workflow_failed(cb)
    hooks.on_workflow_failed(cb)  # idempotent
    hooks.on_workflow_failed(boom)
    try:
        f = hooks.WorkflowFailure("d", "me/app", 7, 1, "CI", None, "main", "s", "push",
                                  "failure", 1, None)  # fmt: skip
        await hooks._fire(f)  # a raising callback does not stop the others
        assert seen == [7]
    finally:
        hooks.remove_workflow_failed(cb)
        hooks.remove_workflow_failed(boom)
