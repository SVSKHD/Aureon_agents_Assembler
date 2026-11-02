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


# --- Phase 22 (docs/24-production-integration.md) -----------------------------------------

HOST_CPUS = 4  # the server: a 4-core box shared with Postgres and Ollama (outside compose)
CORE_MEM_BUDGET = 4 * 1024**3  # always-on services together (no profile)


def _mem_bytes(v: str) -> int:
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    v = str(v).strip().lower()
    return int(float(v[:-1]) * units[v[-1]]) if v[-1] in units else int(v)


def test_every_service_incl_phase_17_18_workers_is_supervised(
    services: dict[str, dict[str, Any]],
) -> None:
    # The webhook worker (Phase 17) and the coding worker, which also runs the self-repair
    # loop (Phase 18), are restarted, health-checked and ordered like every other worker.
    for name in ("github-webhook-worker", "coding-worker"):
        s = services[name]
        assert s["restart"] == "unless-stopped"
        assert s["healthcheck"]["test"][3] == "app.workers.healthcheck"
        assert s["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["coding-worker"]["command"] == ["python", "-m", "app.workers.coding"]
    src = (ROOT / "app" / "workers" / "coding.py").read_text()
    assert "repair_loop(" in src and src.count("async with HEAVY_MODEL_LOCK") == 2
    assert services["github-webhook-worker"]["command"][-1] == "app.workers.github_webhooks"


def test_resource_caps_and_budget(services: dict[str, dict[str, Any]]) -> None:
    core_mem, core_cpu = 0, 0.0
    for name, s in services.items():
        cpus, mem = float(s["cpus"]), _mem_bytes(s["mem_limit"])
        assert 0 < cpus <= HOST_CPUS - 1, name  # always leave a core for Postgres/Ollama
        assert 0 < mem <= 10 * 1024**3, name
        if not s.get("profiles") and s.get("restart") == "unless-stopped":
            core_mem += mem
            core_cpu += cpus
    assert core_mem <= CORE_MEM_BUDGET, core_mem
    assert core_cpu <= HOST_CPUS, core_cpu
    # The two heavy workers are opt-in and single-instance (fixed container names).
    for heavy in ("imaging-worker", "coding-worker"):
        assert services[heavy].get("profiles"), heavy
        assert services[heavy]["container_name"]
        assert "deploy" not in services[heavy]  # no replicas


def test_migrate_reads_the_migrator_url_from_its_own_env_file(
    services: dict[str, dict[str, Any]],
) -> None:
    env_files = services["migrate"]["env_file"]
    assert {"path": ".env.migrate", "required": False} in env_files
    for name, s in services.items():
        if name != "migrate":
            assert ".env.migrate" not in str(s.get("env_file")), name
    # The scheduler sees the HDD read-only (backup status for alerts), nothing else does new.
    vols = services["scheduler"]["volumes"]
    assert vols[0]["read_only"] is True and vols[0]["target"] == "/mnt/spasta-data"
