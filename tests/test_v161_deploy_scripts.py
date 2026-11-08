"""V1.6.1 A3/A4: scripts/deploy.sh never promotes an unhealthy release.

The real deploy.sh runs in a throwaway clone (origin = a local bare repo) with a fake `docker`
and a fake `curl` on PATH. A release is promoted (last-good marker written) only when the
schema check passes AND /api/health/ready answers HTTP 200 with {"status": "ready"} and every
check ok, twice in a row. Anything else rolls back to the previous commit and image and leaves
the marker on the previous good deploy. Exit 1 = rolled back healthy, 2 = rollback unhealthy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
READY = json.dumps({"status": "ready", "checks": {"postgres": {"ok": True}, "redis": {"ok": True}}})
NOT_READY = json.dumps(
    {"status": "not_ready", "checks": {"postgres": {"ok": False}, "redis": {"ok": True}}}
)
LYING = json.dumps(  # 200 + "ready", but a check failed: still not promotable
    {"status": "ready", "checks": {"postgres": {"ok": False}, "redis": {"ok": True}}}
)

DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE/docker.log"
case "$*" in
  "image inspect"*) exit 0 ;;
  "tag aureon-agentic:rollback aureon-agentic:dev") touch "$FAKE/rolled"; exit 0 ;;
  *"app.workers.migrate --check"*)
    echo '=== aureon migration check ==='
    echo "{\"state\": \"${CHECK_STATE:-at_head}\"}"
    exit "${CHECK_RC:-0}" ;;
  *"run --rm migrate"*) exit "${MIGRATE_RC:-0}" ;;
  *"app.workers.deploy_record start"*) echo 17; exit 0 ;;
  *) exit 0 ;;
esac
"""

# Answers from $FAKE/new.seq before the rollback and $FAKE/old.seq after it. One "code|body"
# per line; each call consumes a line, the last line repeats.
CURL = r"""#!/usr/bin/env bash
out=/dev/stdout
while (($#)); do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w|--max-time) shift 2 ;;
    *) shift ;;
  esac
done
seq="$FAKE/new.seq"; [[ -e "$FAKE/rolled" ]] && seq="$FAKE/old.seq"
line="$(head -n 1 "$seq")"
if (( $(wc -l <"$seq") > 1 )); then tail -n +2 "$seq" >"$seq.t" && mv "$seq.t" "$seq"; fi
echo "${line%%|*}" >>"$FAKE/curl.log"
printf '%s' "${line#*|}" >"$out"
printf '%s' "${line%%|*}"
"""


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class Deploy:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.logs = tmp / "logs"
        for d in (self.fake, self.bin, self.logs):
            d.mkdir()
        for name, body in (("docker", DOCKER), ("curl", CURL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        origin = tmp / "origin.git"
        _git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        seed = tmp / "seed"
        _git(tmp, "clone", "-q", str(origin), str(seed))
        _git(seed, "checkout", "-q", "-b", "main")
        (seed / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "deploy.sh", seed / "scripts" / "deploy.sh")
        (seed / "VERSION").write_text("old\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "old")
        _git(seed, "push", "-q", "origin", "main")
        self.old = _git(seed, "rev-parse", "HEAD")
        self.repo = tmp / "server"
        _git(tmp, "clone", "-q", str(origin), str(self.repo))
        (seed / "VERSION").write_text("new\n")
        _git(seed, "commit", "-q", "-am", "new")
        _git(seed, "push", "-q", "origin", "main")
        self.new = _git(seed, "rev-parse", "HEAD")
        self.marker = self.logs / ".deployed-backend"
        self.marker.write_text(self.old + "\n")

    def run(self, new: list[str], old: list[str] | None = None, **env: str) -> Any:
        (self.fake / "new.seq").write_text("\n".join(new) + "\n")
        (self.fake / "old.seq").write_text("\n".join(old or [f"200|{READY}"]) + "\n")
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "deploy.sh")],
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FAKE": str(self.fake),
                "DEPLOY_LOG_DIR": str(self.logs),
                "DEPLOY_READY_SECONDS": "3",
                "DEPLOY_ROLLBACK_READY_SECONDS": "3",
                "DEPLOY_READY_INTERVAL": "0.05",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )

    def docker(self) -> list[str]:
        log = self.fake / "docker.log"
        return log.read_text().splitlines() if log.exists() else []

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def promoted(self) -> str:
        return self.marker.read_text().strip()


@pytest.fixture
def dep(tmp_path: Path) -> Deploy:
    if not shutil.which("flock"):
        pytest.skip("flock not installed")
    return Deploy(tmp_path)


def test_healthy_release_is_promoted(dep: Deploy) -> None:
    r = dep.run([f"200|{READY}"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert dep.promoted() == dep.new and dep.head() == dep.new
    calls = dep.docker()
    check = next(i for i, c in enumerate(calls) if "--check" in c)
    up = next(i for i, c in enumerate(calls) if c == "compose up -d")
    assert check < up, calls  # the schema is verified before the new release starts
    assert not (dep.fake / "rolled").exists()


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (f"503|{NOT_READY}", "HTTP 503"),
        ("200|<html>nginx default page</html>", "not JSON"),
        (f"200|{NOT_READY}", "status='not_ready'"),
        (f"200|{LYING}", "failing: postgres"),
        ('200|{"status": "ready"}', "no checks"),
        ("000|", "HTTP 000"),
    ],
)
def test_unready_release_is_never_promoted(dep: Deploy, answer: str, why: str) -> None:
    r = dep.run([answer])
    assert r.returncode == 1, r.stdout + r.stderr
    assert why in r.stdout, r.stdout
    assert "ROLLED BACK" in r.stdout
    assert dep.promoted() == dep.old  # the previous good deploy stays recorded
    assert dep.head() == dep.old
    assert (dep.fake / "rolled").exists()  # :rollback image re-tagged :dev


def test_flapping_readiness_is_not_promoted(dep: Deploy) -> None:
    flap = [f"200|{READY}", f"503|{NOT_READY}"] * 40
    r = dep.run(flap)
    assert r.returncode == 1, r.stdout
    assert dep.promoted() == dep.old


def test_needs_consecutive_passes_then_promotes(dep: Deploy) -> None:
    r = dep.run([f"503|{NOT_READY}", f"200|{READY}", f"200|{READY}"])
    assert r.returncode == 0, r.stdout
    assert dep.promoted() == dep.new
    codes = (dep.fake / "curl.log").read_text().split()
    assert codes[:3] == ["503", "200", "200"]


@pytest.mark.parametrize(("rc", "state", "msg"), [("3", "pending", "PENDING"),
                                                 ("4", "diverged", "DIVERGED")])  # fmt: skip
def test_schema_not_at_head_blocks_the_release(dep: Deploy, rc: str, state: str, msg: str) -> None:
    r = dep.run([f"200|{READY}"], CHECK_RC=rc, CHECK_STATE=state)
    assert r.returncode == 1, r.stdout
    assert f"migrations: {msg}" in r.stdout
    calls = dep.docker()
    # The new release never started: the only `up -d` is the rollback's.
    rolled = calls.index("tag aureon-agentic:rollback aureon-agentic:dev")
    ups = [i for i, c in enumerate(calls) if c == "compose up -d"]
    assert ups and all(i > rolled for i in ups), calls
    assert dep.promoted() == dep.old


def test_failed_migration_rolls_back(dep: Deploy) -> None:
    r = dep.run([f"200|{READY}"], MIGRATE_RC="1")
    assert r.returncode == 1
    assert not any("--check" in c for c in dep.docker())
    assert dep.promoted() == dep.old


def test_unhealthy_rollback_exits_2(dep: Deploy) -> None:
    r = dep.run([f"503|{NOT_READY}"], old=[f"503|{NOT_READY}"])
    assert r.returncode == 2, r.stdout
    assert "ROLLBACK UNHEALTHY" in r.stdout
    assert dep.promoted() == dep.old


# --- deploy-poll.sh: only a healthy rollback is recorded as rolled back -------------------


def _poll(tmp: Path, deploy_rc: int) -> list[str]:
    fake, bin_, logs = tmp / "fake", tmp / "bin", tmp / "logs"
    for d in (fake, bin_, logs):
        d.mkdir(exist_ok=True)
    (bin_ / "docker").write_text(DOCKER)
    (bin_ / "docker").chmod(0o755)
    origin = tmp / "o.git"
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp / "backend"
    _git(tmp, "clone", "-q", str(origin), str(repo))
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    stub = repo / "scripts" / "deploy.sh"
    stub.write_text(f"#!/usr/bin/env bash\nexit {deploy_rc}\n")
    stub.chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "x")
    _git(repo, "push", "-q", "origin", "main")
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "ops" / "deploy-poll.sh")],
        env={
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "FAKE": str(fake),
            "DEPLOY_LOG_DIR": str(logs),
            "BACKEND_DIR": str(repo),
            "FRONTEND_DIR": str(tmp / "absent"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == (0 if deploy_rc == 0 else 1), r.stdout + r.stderr
    return [c for c in (fake / "docker.log").read_text().splitlines() if "finish" in c]


def test_poll_records_a_healthy_rollback_as_rolled_back(tmp_path: Path) -> None:
    (finish,) = _poll(tmp_path, 1)
    assert "--status failed" in finish and "--rolled-back" in finish


def test_poll_never_records_an_unhealthy_rollback_as_rolled_back(tmp_path: Path) -> None:
    (finish,) = _poll(tmp_path, 2)
    assert "--status failed" in finish and "--rolled-back" not in finish
    assert "ROLLBACK UNHEALTHY" in finish


def test_poll_records_success(tmp_path: Path) -> None:
    (finish,) = _poll(tmp_path, 0)
    assert "--status succeeded" in finish
