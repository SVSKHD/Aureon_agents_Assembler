"""compose.yaml hardening (docs/16-production-hardening.md): asserted on the parsed file, and
with `docker compose config` when the docker CLI is installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
ONE_SHOTS = {"mount-check", "migrate"}
WORKERS = {
    "scheduler",
    "dhan-feed",
    "research-worker",
    "coding-worker",
    "imaging-worker",
    "github-webhook-worker",
}


@pytest.fixture(scope="module")
def services() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(COMPOSE.read_text())  # SafeLoader resolves `<<:` merge keys
    svcs: dict[str, dict[str, Any]] = data["services"]
    return svcs


def test_expected_services_and_profiles_kept(services: dict[str, dict[str, Any]]) -> None:
    assert set(services) == {"api", "redis", "docker-proxy", "migrate", *ONE_SHOTS, *WORKERS}
    profiles = {n: tuple(s.get("profiles", ())) for n, s in services.items()}
    assert profiles["imaging-worker"] == ("imaging",)
    assert profiles["coding-worker"] == ("coding",)
    assert profiles["dhan-feed"] == ("dhan",)
    assert profiles["research-worker"] == ("research",)
    assert profiles["github-webhook-worker"] == ("github",)
    for core in ("api", "redis", "scheduler", "docker-proxy", "migrate", "mount-check"):
        assert profiles[core] == (), core


def test_only_the_api_publishes_and_only_on_loopback(services: dict[str, dict[str, Any]]) -> None:
    published = {n: s["ports"] for n, s in services.items() if s.get("ports")}
    assert published == {"api": ["127.0.0.1:8000:8000"]}
    assert all("expose" not in s for s in services.values())
    text = COMPOSE.read_text()
    assert "0.0.0.0" not in text
    assert "network_mode: host" not in text


def test_restart_policies(services: dict[str, dict[str, Any]]) -> None:
    for name, s in services.items():
        want = "no" if name in ONE_SHOTS else "unless-stopped"
        assert s.get("restart") == want, name


def test_hardening_on_every_service(services: dict[str, dict[str, Any]]) -> None:
    for name, s in services.items():
        assert s.get("read_only") is True, name
        assert s.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in s.get("security_opt", []), name
        assert "cap_add" not in s and not s.get("privileged"), name
        assert s.get("mem_limit") and s.get("cpus"), name
        opts = s["logging"]["options"]
        assert s["logging"]["driver"] == "json-file" and opts["max-size"] and opts["max-file"], name
        if name != "mount-check":
            assert s.get("tmpfs"), name


def test_healthchecks(services: dict[str, dict[str, Any]]) -> None:
    for name, s in services.items():
        if name in ONE_SHOTS:
            continue
        hc = s.get("healthcheck")
        assert hc and hc.get("test") and not hc.get("disable"), name
    for w in WORKERS:
        assert services[w]["healthcheck"]["test"][:4] == [
            "CMD",
            "python",
            "-m",
            "app.workers.healthcheck",
        ]
    assert "/api/health" in " ".join(services["api"]["healthcheck"]["test"])


def test_start_order(services: dict[str, dict[str, Any]]) -> None:
    def deps(name: str) -> dict[str, str]:
        return {k: v["condition"] for k, v in services[name].get("depends_on", {}).items()}

    assert deps("redis") == {"mount-check": "service_completed_successfully"}
    assert deps("migrate") == {"redis": "service_healthy"}
    assert deps("api") == {
        "mount-check": "service_completed_successfully",
        "redis": "service_healthy",
        "migrate": "service_completed_successfully",
    }
    for w in WORKERS:
        assert deps(w)["api"] == "service_healthy", w
        assert deps(w)["migrate"] == "service_completed_successfully", w


def test_no_docker_socket_except_the_read_only_proxy(services: dict[str, dict[str, Any]]) -> None:
    for name, s in services.items():
        mounts = [str(v) for v in s.get("volumes", [])]
        if name == "docker-proxy":
            assert mounts == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
            env = s["environment"]
            assert env["POST"] == "0" and env["EXEC"] == "0"
        else:
            assert not any("docker.sock" in m for m in mounts), name


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_docker_compose_config_validates(tmp_path: Path) -> None:
    for f in ("compose.yaml", ".env.example"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / ".env").write_text((ROOT / ".env.example").read_text())
    profiles = ["--profile", "imaging", "--profile", "coding", "--profile", "dhan"]
    profiles += ["--profile", "research", "--profile", "github"]
    r = subprocess.run(
        ["docker", "compose", *profiles, "config", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0 and "Cannot connect" in r.stderr:
        pytest.skip("docker daemon required for config")
    assert r.returncode == 0, r.stderr


def test_network_isolation(services: dict[str, dict[str, Any]]) -> None:
    def nets(name: str) -> set[str]:
        return set(services[name].get("networks", []))

    # Only the API shares a network with the Docker proxy (container env holds every secret).
    assert nets("docker-proxy") == {"aureon-docker"}
    assert {n for n in services if "aureon-docker" in nets(n)} == {"api", "docker-proxy"}
    net = yaml.safe_load(COMPOSE.read_text())["networks"]["aureon-docker"]
    assert net["internal"] is True
    # The coding worker runs model-patched test code: no Redis, no Docker proxy.
    assert nets("coding-worker") == {"private-ai"}
    assert services["mount-check"]["network_mode"] == "none"
