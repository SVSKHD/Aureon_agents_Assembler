#!/usr/bin/env bash
# Post-deploy / post-reboot health check (docs/16-production-hardening.md, "Reboot order").
#
#   scripts/ops/healthcheck.sh             # exit 0 = everything healthy
#   scripts/ops/healthcheck.sh --wait 300  # retry every 10 s for up to 300 s (boot)
#   API_URL=http://127.0.0.1:8000 scripts/ops/healthcheck.sh
#
# Checks: GET /api/health (liveness), GET /api/health/ready (Postgres, Redis, Ollama, HDD),
# and `docker compose ps`: every running service with a healthcheck must be "healthy", and
# the one-shots (mount-check, migrate) must have exited 0.
set -euo pipefail

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

API_URL="${API_URL:-http://127.0.0.1:8000}"
CURL_MAX_TIME="${CURL_MAX_TIME:-10}"
WAIT=0
RETRY_EVERY="${HEALTHCHECK_RETRY_SECONDS:-10}"
case "${1:-}" in
  "") ;;
  --wait)
    [[ "${2:-}" =~ ^[0-9]+$ ]] || die "--wait needs a number of seconds" 2
    WAIT="$2"
    ;;
  *) die "usage: healthcheck.sh [--wait SECONDS]" 2 ;;
esac
require_cmd curl docker python3

# Judges `docker compose ps --format json` (an array, or one object per line).
read -r -d '' PS_CHECK <<'PY' || true
import json, sys
raw = sys.stdin.read().strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x]
bad = 0
for r in rows:
    svc, state = r.get("Service", "?"), r.get("State", "")
    health, code = r.get("Health", ""), r.get("ExitCode", 0)
    if svc in ("mount-check", "migrate"):
        ok = state == "exited" and code == 0
    else:
        ok = state == "running" and health in ("", "healthy")
    shown = health or "-"
    print(("ok:  " if ok else "BAD: ") + f"{svc:16} state={state} health={shown} exit={code}")
    bad += not ok
sys.exit(1 if bad else 0)
PY

failures=0
fail() {
  log "FAIL: $*"
  failures=$((failures + 1))
}

check_once() {
  failures=0
  if curl -fsS --max-time "$CURL_MAX_TIME" "$API_URL/api/health" >/dev/null; then
    log "ok: /api/health"
  else
    fail "/api/health did not answer 200"
  fi

  local ready_body ready_code ps_json
  ready_body="$(curl -sS --max-time "$CURL_MAX_TIME" -w '\n%{http_code}' \
    "$API_URL/api/health/ready" || true)"
  ready_code="${ready_body##*$'\n'}"
  if [[ "$ready_code" == "200" ]]; then
    log "ok: /api/health/ready"
  else
    fail "/api/health/ready answered ${ready_code:-nothing}: ${ready_body%$'\n'*}"
  fi

  ps_json="$(docker compose --project-directory "$AUREON_DIR" ps --all --format json || true)"
  if [[ -z "$ps_json" ]]; then
    fail "docker compose ps returned nothing (is the stack up?)"
  elif ! printf '%s\n' "$ps_json" | python3 -c "$PS_CHECK"; then
    fail "one or more compose services are not healthy"
  fi
}

deadline=$((SECONDS + WAIT))
while true; do
  check_once
  ((failures)) || break
  if ((SECONDS + RETRY_EVERY > deadline)); then
    log "healthcheck: $failures problem(s)"
    exit 1
  fi
  log "healthcheck: $failures problem(s); retrying in ${RETRY_EVERY}s"
  sleep "$RETRY_EVERY"
done
log "healthcheck: all good"
