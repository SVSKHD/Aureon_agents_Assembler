"""DevOps monitoring without a database: status mapping, payload normalization, the deploy
record CLI, the runtime report contract, the host collector script and the API contract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app
from app.models.devops import Deployment, Service
from app.services import devops, devops_read
from app.workers import deploy_record, runtime_record
from tests.test_v161_deploy_scripts import READY, Deploy

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(UTC)


# --- normalization ----------------------------------------------------------------------------


def _pr(**kw: Any) -> dict[str, Any]:
    return {"number": 4, "title": "Fix it", "html_url": "https://x/4", "user": {"login": "dev"},
            "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "c" * 40},
            "created_at": "2026-11-08T10:00:00Z", "closed_at": "2026-11-08T11:00:00Z",
            "merged_at": "2026-11-08T11:00:00Z", "merge_commit_sha": "d" * 40,
            "merged_by": {"login": "lead"}, **kw}  # fmt: skip


def test_webhook_and_backfill_produce_the_same_keys() -> None:
    hook = devops.pr_facts("me/app", _pr(), "closed", "webhook", "d-1")
    back = devops.pr_facts("me/app", _pr(), None, "backfill", None)
    assert [f["dedup_key"] for f in hook] == [f["dedup_key"] for f in back]
    merged = next(f for f in hook if f["kind"] == "pr.merged")
    assert merged["sha"] == "d" * 40 and merged["actor"] == "lead" and merged["branch"] == "main"


def test_closed_unmerged_and_reopened_are_distinct_facts() -> None:
    closed = devops.pr_facts("me/app", _pr(merged_at=None), "closed", "webhook", None)
    assert [f["kind"] for f in closed] == ["pr.opened", "pr.closed"]
    reopened = devops.pr_facts("me/app", _pr(merged_at=None), "reopened", "webhook", None)
    assert [f["kind"] for f in reopened] == ["pr.opened", "pr.reopened"]
    assert devops.pr_facts("me/app", {"title": "no number"}, "opened", "webhook", None) == []


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("queued", None, "queued"), ("in_progress", None, "running"),
        ("completed", "success", "succeeded"), ("completed", "failure", "failed"),
        ("completed", "timed_out", "failed"), ("completed", "cancelled", "cancelled"),
        ("completed", "weird", "unknown"), (None, None, "unknown"),
    ],
)  # fmt: skip
def test_build_state(status: str | None, conclusion: str | None, state: str) -> None:
    assert devops.build_state(status, conclusion) == state


@pytest.mark.parametrize(
    ("code", "phases", "outcome"),
    [
        (0, {"health_ok": NOW}, ("succeeded", False, None)),
        (0, {}, ("unverified", False, None)),  # never "success" without a verification
        (1, {}, ("rolled_back", True, "healthy")),
        (2, {}, ("failed", True, "unhealthy")),
        (3, {}, ("failed", False, None)),
    ],
)
def test_server_outcome(code: int, phases: dict[str, Any], outcome: tuple[Any, ...]) -> None:
    assert devops.server_outcome(code, phases) == outcome


def test_phases_keep_known_keys_and_one_line_failure() -> None:
    p = devops.parse_phases({"build_started": "2026-11-08T10:00:00Z", "evil": "x",
                             "health_ok": "not a date", "failure": "a\nb   c"})  # fmt: skip
    assert set(p) == {"build_started", "failure"} and p["failure"] == "a b c"


def test_deploy_record_cli() -> None:
    p = deploy_record.build_parser()
    a = p.parse_args(["finish", "--run-id", "3", "--status", "failed", "--rolled-back"])
    assert deploy_record.exit_code_of(a) == 1 and a.phases == {}
    a = p.parse_args(["finish", "--run-id", "3", "--status", "failed"])
    assert deploy_record.exit_code_of(a) == 3
    a = p.parse_args(["finish", "--run-id", "3", "--status", "succeeded", "--exit-code", "0",
                      "--phases-json", '{"health_ok": "2026-11-08T10:00:00Z"}'])  # fmt: skip
    assert deploy_record.exit_code_of(a) == 0 and "health_ok" in a.phases
    for bad in ("[1]", "{not json", "x" * 5000):
        with pytest.raises(SystemExit):
            p.parse_args(["finish", "--run-id", "3", "--status", "failed", "--phases-json", bad])
    with pytest.raises(SystemExit):
        p.parse_args(["start", "--target", "backend", "--sha", "a" * 40, "--log", "/x",
                      "--environment", "prod; rm -rf"])  # fmt: skip


# --- runtime ----------------------------------------------------------------------------------


def _obs(**kw: Any) -> devops.ServiceObservation:
    return devops.ServiceObservation(service=kw.pop("service", "api"), **kw)


ONESHOT = frozenset({"migrate"})


@pytest.mark.parametrize(
    ("obs", "ready", "status"),
    [
        (dict(state="running", health="healthy"), None, "healthy"),
        (dict(state="running", health=""), None, "healthy"),
        (dict(state="running", health="unhealthy"), None, "unhealthy"),
        (dict(state="running", health="starting"), None, "starting"),
        (dict(state="restarting"), None, "unhealthy"),
        (dict(state="exited", exit_code=137), None, "stopped"),
        (dict(state="missing"), None, "stopped"),
        (dict(state="weird"), None, "unknown"),
        (dict(service="migrate", state="exited", exit_code=0), None, "healthy"),
        (dict(service="migrate", state="exited", exit_code=1), None, "unhealthy"),
        (dict(state="running", health="healthy"), {"ok": False, "http_status": 503}, "unhealthy"),
    ],
)  # fmt: skip
def test_service_status(obs: dict[str, Any], ready: dict[str, Any] | None, status: str) -> None:
    readiness = devops.Readiness(**ready) if ready else None
    assert devops.service_status(_obs(**obs), ONESHOT, readiness)[0] == status


def test_runtime_report_is_validated() -> None:
    good = {
        "schema": "aureon.runtime/v1",
        "host": "srv",
        "observed_at": NOW.isoformat(),
        "services": [{"service": "api", "state": "Running", "revision": "<no value>"}],
    }
    r = runtime_record.parse(json.dumps(good).encode())
    assert r.services[0].state == "running" and r.services[0].revision is None
    for bad in (b"not json", json.dumps({**good, "schema": "other"}).encode(),
                json.dumps({**good, "host": "a b"}).encode(),
                json.dumps({**good, "services": [{"service": "x;y"}]}).encode(),
                b"x" * (runtime_record.MAX_INPUT + 1)):  # fmt: skip
        with pytest.raises(ValueError):
            runtime_record.parse(bad)


def test_runtime_record_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", argparse.Namespace(buffer=io.BytesIO(b"{}")))
    assert runtime_record.main() == 2


# --- read mapping -----------------------------------------------------------------------------


def _dep(**kw: Any) -> Deployment:
    base = dict(
        id=1,
        source="server",
        repo="o/r",
        target="backend",
        environment="production",
        branch="main",
        sha="a" * 40,
        previous_sha=None,
        status="running",
        health_verified_at=None,
        rolled_back=False,
        rollback_status=None,
        failure_reason=None,
        started_at=NOW,
        finished_at=None,
        build_started_at=None,
        build_finished_at=None,
        deploy_started_at=None,
        deploy_finished_at=None,
        url=None,
    )
    return Deployment(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "status", "health"),
    [
        ({}, "RUNNING", "UNKNOWN"),
        ({"status": "succeeded", "health_verified_at": NOW}, "SUCCEEDED", "VERIFIED"),
        ({"status": "unverified"}, "UNVERIFIED", "NOT_VERIFIED"),
        ({"status": "rolled_back"}, "ROLLED_BACK", "FAILED"),
        ({"status": "succeeded", "source": "github"}, "SUCCEEDED", "UNKNOWN"),
    ],
)
def test_deployment_mapping(kw: dict[str, Any], status: str, health: str) -> None:
    out = devops_read.deployment_out(_dep(**kw))
    assert (out.status, out.health) == (status, health)


def test_stale_service_is_unknown(settings: Settings) -> None:
    s = Service(
        id=1,
        host="h",
        service="api",
        status="healthy",
        status_since=NOW,
        last_seen_at=NOW - timedelta(seconds=settings.DEVOPS_RUNTIME_STALE_SECONDS + 1),
    )
    out = devops_read.service_out(s, settings)
    assert out.status == "UNKNOWN" and out.stale is True
    s.last_seen_at = NOW
    assert devops_read.service_out(s, settings).status == "HEALTHY"


def test_parse_cursor() -> None:
    assert devops_read.parse_cursor("42") == 42
    assert devops_read.parse_cursor(None) is None
    for bad in ("-1", "abc", "1" * 30, "1;2"):
        assert devops_read.parse_cursor(bad) is None


# --- API contract (OpenAPI -> ASTRA-FRONTEND types) -------------------------------------------


def test_openapi_has_four_distinct_status_vocabularies(settings: Settings) -> None:
    spec = create_app(settings).openapi()
    s = spec["components"]["schemas"]
    enum = lambda model, field: set(s[model]["properties"][field]["enum"])  # noqa: E731
    assert enum("PullRequestOut", "status") == {"OPEN", "MERGED", "CLOSED", "UNKNOWN"}
    assert enum("BuildOut", "status") == {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED",
                                          "CANCELLED", "UNKNOWN"}  # fmt: skip
    assert enum("DeploymentOut", "status") == {"RUNNING", "SUCCEEDED", "UNVERIFIED", "FAILED",
                                               "ROLLED_BACK", "UNKNOWN"}  # fmt: skip
    assert enum("ServiceOut", "status") == {"HEALTHY", "UNHEALTHY", "STOPPED", "STARTING",
                                            "UNKNOWN"}  # fmt: skip
    assert enum("DeploymentOut", "health") == {"VERIFIED", "FAILED", "NOT_VERIFIED", "UNKNOWN"}
    # No progress percentage anywhere in the build contract.
    assert not any("percent" in k or "progress" in k for k in s["BuildOut"]["properties"])
    paths = spec["paths"]
    for p in (
        "overview",
        "repositories",
        "events",
        "builds",
        "deployments",
        "services",
        "feed",
        "stream",
        "notifications",
        "notifications/read",
        "notifications/preferences",
    ):
        assert f"/api/v1/devops/{p}" in paths, p


# --- host scripts -----------------------------------------------------------------------------

COLLECT_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE/docker.log"
if [[ "$*" == *"ps --all --format json"* ]]; then
  printf '%s\n' "$PS_ROWS"
elif [[ "$1" == "inspect" ]]; then
  echo "abc123def456aaaa|sha256:img1|$REV|2026-11-08T10:00:00.1Z"
  echo "fff000fff000bbbb|sha256:img1|$REV|0001-01-01T00:00:00Z"
elif [[ "$*" == *"ps --status running --quiet api"* ]]; then
  [[ -n "${API_UP:-}" ]] && echo abc123
elif [[ "$*" == *"runtime_record"* ]]; then
  cat >"$FAKE/stdin.json"
fi
exit 0
"""
PS_ROWS = "\n".join(
    json.dumps(r)
    for r in (
        {"Service": "api", "Name": "aureon-api", "ID": "abc123def456", "State": "running",
         "Health": "healthy", "ExitCode": 0, "Image": "aureon-agentic:dev"},
        {"Service": "migrate", "Name": "aureon-migrate", "ID": "fff000fff000",
         "State": "exited", "Health": "", "ExitCode": 0, "Image": "aureon-agentic:dev"},
    )
)  # fmt: skip
COLLECT_CURL = r"""#!/usr/bin/env bash
printf '%s\n%s' "${READY_BODY}" "${READY_CODE}"
"""


@pytest.fixture
def collector(tmp_path: Path) -> Any:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, body in (("docker", COLLECT_DOCKER), ("curl", COLLECT_CURL)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)

    def go(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / "ops" / "runtime-collect.sh"), *args],
            env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE": str(tmp_path),
                 "OPS_ENV_FILE": str(tmp_path / "none.env"), "AUREON_DIR": str(ROOT),
                 "RUNTIME_HOST": "srv1", "REV": "e" * 40, "READY_BODY": READY,
                 "READY_CODE": "200", "PS_ROWS": PS_ROWS, **env},
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip

    go.tmp = tmp_path  # type: ignore[attr-defined]
    return go


def test_collector_builds_a_valid_report_without_secrets(collector: Any) -> None:
    r = collector("--print", RUNTIME_EXPECTED_SERVICES="api,scheduler")
    assert r.returncode == 0, r.stderr
    report = runtime_record.parse(r.stdout.encode())
    api = next(s for s in report.services if s.service == "api")
    assert api.revision == "e" * 40 and api.image_id == "sha256:img1"
    assert api.started_at is not None
    migrate = next(s for s in report.services if s.service == "migrate")
    assert migrate.started_at is None  # docker's zero time is dropped
    assert report.expected == ["api", "scheduler"] and report.readiness and report.readiness.ok
    log = (collector.tmp / "docker.log").read_text()
    assert "Config.Env" not in log and "{{json" not in log  # a fixed field list only


def test_collector_reports_failing_readiness(collector: Any) -> None:
    bad = json.dumps({"status": "not_ready", "checks": {"postgres": {"ok": False}}})
    r = collector("--print", READY_BODY=bad, READY_CODE="503")
    report = runtime_record.parse(r.stdout.encode())
    assert report.readiness is not None
    assert (report.readiness.ok, report.readiness.failing) == (False, ["postgres"])


@pytest.mark.parametrize(
    ("api_up", "how"), [("1", "exec -T api"), ("", "run --rm --no-deps -T api")]
)
def test_collector_records_through_the_api_image(collector: Any, api_up: str, how: str) -> None:
    r = collector(API_UP=api_up)
    assert r.returncode == 0, r.stderr
    log = (collector.tmp / "docker.log").read_text()
    assert f"{how} python -m app.workers.runtime_record" in log
    runtime_record.parse((collector.tmp / "stdin.json").read_bytes())


def test_collector_is_strict_and_shellcheck_clean() -> None:
    script = ROOT / "scripts" / "ops" / "runtime-collect.sh"
    assert "set -euo pipefail" in script.read_text() and os.access(script, os.X_OK)
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck not installed")
    assert subprocess.run([sc, "-x", str(script)], cwd=ROOT).returncode == 0


def test_systemd_units() -> None:
    svc = (ROOT / "deploy/systemd/aureon-runtime-collect.service").read_text()
    timer = (ROOT / "deploy/systemd/aureon-runtime-collect.timer").read_text()
    assert "Group=docker" in svc and "runtime-collect.sh" in svc and "Type=oneshot" in svc
    assert "OnUnitInactiveSec=60s" in timer


@pytest.fixture
def dep(tmp_path: Path) -> Deploy:
    if not shutil.which("flock"):
        pytest.skip("flock not installed")
    return Deploy(tmp_path)


def _phases(d: Deploy) -> dict[str, str]:
    lines = (d.logs / ".deploy-backend.phases").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if not line.startswith("stage="))


def _stages(d: Deploy) -> list[tuple[str, str]]:
    lines = (d.logs / ".deploy-backend.phases").read_text().splitlines()
    return [tuple(line[6:].split("|")[:2]) for line in lines if line.startswith("stage=")]  # type: ignore[misc]


def test_deploy_writes_health_ok_only_after_readiness(dep: Deploy) -> None:
    r = dep.run([f"200|{READY}"])
    assert r.returncode == 0, r.stdout
    p = _phases(dep)
    assert list(p) == ["sha", "build_started", "build_finished", "migrate_finished",
                       "deploy_started", "deploy_finished", "health_ok"]  # fmt: skip
    assert p["sha"] == dep.new
    assert _stages(dep) == [
        (n, st) for n in ("pulling", "validating", "building", "migrating", "restarting",
                          "health_checking", "verifying") for st in ("running", "succeeded")
    ]  # fmt: skip
    assert "AUREON_GIT_SHA" not in r.stdout  # exported to compose, never printed


def test_failed_deploy_phases_have_no_health_ok_and_a_reason(dep: Deploy) -> None:
    r = dep.run(['503|{"status": "not_ready", "checks": {"postgres": {"ok": false}}}'])
    assert r.returncode == 1
    p = _phases(dep)
    assert "health_ok" not in p
    assert p["failure"] == "readiness: HTTP 503" and "rollback_finished" in p
    parsed = devops.parse_phases(p)
    assert devops.server_outcome(1, parsed) == ("rolled_back", True, "healthy")


def test_migrate_failure_reason(dep: Deploy) -> None:
    r = dep.run([f"200|{READY}"], MIGRATE_RC="1")
    assert r.returncode == 1
    p = _phases(dep)
    assert p["failure"] == "migrate failed" and "deploy_started" not in p
