#!/usr/bin/env bash
# Host runtime collector (docs/32-devops-monitoring.md, "Runtime collector").
# Run every minute by aureon-runtime-collect.timer, as the deploy user (group docker).
#
#   scripts/ops/runtime-collect.sh            # observe + record
#   scripts/ops/runtime-collect.sh --print    # observe and print the JSON, record nothing
#
# Reads, on the host: `docker compose ps --all`, a FIXED field list from `docker inspect`
# (id, image id, the OCI revision label, start time; never Config.Env, which holds secrets),
# and GET /api/health/ready. Builds one aureon.runtime/v1 document and pipes it to
# `python -m app.workers.runtime_record` inside the running API container (`exec`), or a
# one-shot API container when the API is down, so an outage is still recorded.
# The API process gets no Docker access and no new endpoint; nothing here writes to Docker.
#
# Env: RUNTIME_HOST (default: short hostname), RUNTIME_EXPECTED_SERVICES (comma list of
# services that must exist; a missing one is recorded as stopped), API_URL.
set -euo pipefail

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

API_URL="${API_URL:-http://127.0.0.1:8000}"
HOST_NAME="${RUNTIME_HOST:-$(hostname -s 2>/dev/null || echo host)}"
EXPECTED="${RUNTIME_EXPECTED_SERVICES:-}"
PRINT_ONLY=0
case "${1:-}" in
  "") ;;
  --print) PRINT_ONLY=1 ;;
  *) die "usage: runtime-collect.sh [--print]" 2 ;;
esac
require_cmd docker python3 curl

compose() { docker compose --project-directory "$AUREON_DIR" "$@"; }

PS_JSON="$(compose ps --all --format json)" || die "docker compose ps failed" 1
IDS="$(printf '%s' "$PS_JSON" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x]
print(" ".join(r["ID"] for r in rows if r.get("ID")))
')"
INSPECT=""
if [[ -n "$IDS" ]]; then
  # shellcheck disable=SC2086 # word splitting of the id list is intended
  INSPECT="$(docker inspect --format \
    '{{.Id}}|{{.Image}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.State.StartedAt}}' \
    $IDS 2>/dev/null || true)"
fi
READY="$(curl -sS --max-time 5 -w '\n%{http_code}' "$API_URL/api/health/ready" 2>/dev/null || true)"

DOC="$(PS_JSON="$PS_JSON" INSPECT="$INSPECT" READY="$READY" HOST_NAME="$HOST_NAME" \
  EXPECTED="$EXPECTED" python3 -c '
import json, os
from datetime import datetime, timezone

raw = os.environ["PS_JSON"].strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x]
inspect = {}
for line in os.environ["INSPECT"].splitlines():
    parts = line.split("|")
    if len(parts) == 4:
        inspect[parts[0][:12]] = parts
services = []
for r in rows:
    cid = str(r.get("ID", ""))
    info = inspect.get(cid[:12], ["", "", "", ""])
    services.append({
        "service": r.get("Service"), "container_id": cid[:80] or None,
        "container_name": r.get("Name"), "state": r.get("State"),
        "health": r.get("Health") or "", "exit_code": r.get("ExitCode"),
        "image": r.get("Image"), "image_id": info[1][:100] or None,
        "revision": info[2] or None,
        "started_at": info[3] if info[3] and not info[3].startswith("0001") else None,
    })
body, _, code = os.environ["READY"].rpartition("\n")
readiness = {"service": "api", "http_status": int(code) if code.isdigit() else None,
             "ok": False, "failing": []}
try:
    doc = json.loads(body)
    checks = doc.get("checks") or {}
    readiness["failing"] = sorted(k for k, v in checks.items()
                                  if not (isinstance(v, dict) and v.get("ok") is True))[:20]
    readiness["ok"] = (code == "200" and doc.get("status") == "ready"
                       and bool(checks) and not readiness["failing"])
except Exception:
    readiness["failing"] = ["no readiness answer"]
print(json.dumps({
    "schema": "aureon.runtime/v1",
    "host": os.environ["HOST_NAME"],
    "observed_at": datetime.now(timezone.utc).isoformat(),
    "services": services,
    "expected": [s.strip() for s in os.environ["EXPECTED"].split(",") if s.strip()],
    "readiness": readiness,
}))
')"

if ((PRINT_ONLY)); then
  printf '%s\n' "$DOC"
  exit 0
fi

record=(python -m app.workers.runtime_record)
if [[ -n "$(compose ps --status running --quiet api 2>/dev/null || true)" ]]; then
  printf '%s' "$DOC" | compose exec -T api "${record[@]}"
else
  log "api is not running: recording through a one-shot api container"
  printf '%s' "$DOC" | compose run --rm --no-deps -T api "${record[@]}"
fi
