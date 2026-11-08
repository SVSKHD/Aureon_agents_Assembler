#!/usr/bin/env bash
# Backend deploy (docs/deploy-automation.md): bring the server to origin/main.
#
#   scripts/deploy.sh            # deploy if origin/main moved since the last good deploy
#   scripts/deploy.sh --force    # rebuild and redeploy even if nothing moved
#
# fetch -> reset to origin/main -> docker compose build -> migrate -> verify the schema is at
# head (`migrate --check`, read-only) -> up -d -> wait up to 60 s for /api/health/ready to pass
# READY_PASSES (2) checks in a row. A pass is HTTP 200 AND a JSON body with status "ready" and
# every check ok: a 2xx alone (a proxy page, a half-started app) never promotes a release.
# On any failure the previous image and commit come back (`aureon-agentic:rollback` -> `:dev`,
# git reset to the old SHA, up -d) and the last-good marker is left untouched.
# Exit: 0 deployed | 1 failed, rolled back healthy | 2 failed AND the rollback is unhealthy.
# Phase timestamps (build, migrate, deploy, health_ok, rollback) and a one-line failure reason
# go to $DEPLOY_LOG_DIR/.deploy-backend.phases; deploy-poll.sh hands them to the deploy record
# (docs/32-devops-monitoring.md). The built images carry the commit as their OCI revision.
# Lock-protected (flock): a second deploy waits for the first. Server-local files (.env,
# .env.migrate, compose.override.yaml, scripts/ops/ops.env) are untracked and never touched;
# local edits to tracked files are stashed (named), never lost. Migrations are additive, so the
# old image runs on the new schema. Never touches Postgres data or TRADING_LIVE_ENABLED.
set -euo pipefail
umask 027

main() {
  local repo branch log_dir log_file lock state force=0
  phases="" # global: phase() appends to it
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  branch="${DEPLOY_BRANCH:-main}"
  log_dir="${DEPLOY_LOG_DIR:-/mnt/spasta-data/logs}"
  log_file="$log_dir/deploy.log"
  lock="$log_dir/.deploy-backend.lock"
  state="$log_dir/.deployed-backend"
  phases="$log_dir/.deploy-backend.phases"
  [[ "${1:-}" == "--force" ]] && force=1

  mkdir -p "$log_dir"
  exec 9>"$lock"
  flock 9
  # Timestamped into deploy.log and, unchanged, to stdout (the journal when run by systemd).
  exec > >(while IFS= read -r l; do
    printf '%s backend %s\n' "$(date -u +%FT%TZ)" "$l" >>"$log_file"
    printf '%s\n' "$l"
  done) 2>&1

  cd "$repo"
  local cur want
  git fetch --quiet origin "$branch"
  want="$(git rev-parse "origin/$branch")"
  cur="$(cat "$state" 2>/dev/null || git rev-parse HEAD)"
  if [[ "$force" == 0 && "$cur" == "$want" && -f "$state" ]]; then
    echo "up to date at ${want:0:12}"
    return 0
  fi
  echo "deploy ${cur:0:12} -> ${want:0:12} (force=$force)"

  [[ "$(git rev-parse --abbrev-ref HEAD)" == "$branch" ]] || git checkout --quiet "$branch"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    git stash push --quiet -m "deploy.sh $(date -u +%FT%TZ): local edits before ${want:0:12}"
    echo "WARN: stashed local edits to tracked files (git stash list)"
  fi

  # Keep the running images for rollback (each one that exists). Profile workers (coding,
  # imaging) are built and updated too when .env sets COMPOSE_PROFILES (docs/deploy-automation.md).
  local img
  for img in aureon-agentic aureon-coding aureon-imaging; do
    if docker image inspect "$img:dev" >/dev/null 2>&1; then
      docker tag "$img:dev" "$img:rollback"
    fi
  done

  git reset --quiet --hard "$want"
  # Phase timestamps for the deploy record (deploy-poll.sh -> deploy_record --phases-json):
  # a release is recorded as verified only when `health_ok` is written below.
  : >"$phases"
  local fail=""
  export AUREON_GIT_SHA="$want" # the images' OCI revision label
  phase build_started
  if ! docker compose build --quiet; then
    fail="docker compose build failed"
  else
    phase build_finished
    if ! docker compose run --rm migrate; then
      fail="migrate failed"
    else
      phase migrate_finished
      if ! migrations_at_head; then
        fail="migrations not at head after migrate"
      else
        phase deploy_started
        if ! docker compose up -d; then
          fail="docker compose up failed"
        else
          phase deploy_finished
          if ! wait_ready "${DEPLOY_READY_SECONDS:-60}"; then
            fail="readiness: $last_ready"
          fi
        fi
      fi
    fi
  fi
  if [[ -z "$fail" ]]; then
    phase health_ok
    echo "$want" >"$state"
    echo "OK ${want:0:12} healthy"
    return 0
  fi

  phase failure "$fail"
  echo "FAILED ${want:0:12} ($fail): rolling back to ${cur:0:12}"
  phase rollback_started
  git reset --quiet --hard "$cur"
  for img in aureon-agentic aureon-coding aureon-imaging; do
    if docker image inspect "$img:rollback" >/dev/null 2>&1; then
      docker tag "$img:rollback" "$img:dev"
    fi
  done
  if docker compose up -d && wait_ready "${DEPLOY_ROLLBACK_READY_SECONDS:-90}"; then
    phase rollback_finished
    echo "ROLLED BACK to ${cur:0:12} (healthy)"
    return 1
  fi
  phase rollback_finished
  echo "ROLLBACK UNHEALTHY at ${cur:0:12}: needs a human (scripts/ops/healthcheck.sh)"
  return 2
}

# phase NAME [VALUE]: one `name=value` line (default value: now, UTC) in the phases file.
phase() {
  printf '%s=%s\n' "$1" "$(printf '%s' "${2:-$(date -u +%FT%TZ)}" | tr -d '\n\r' | cut -c1-300)" \
    >>"$phases"
}

# Read-only: the database's recorded revision must be the image's head. `migrate` already
# verifies this after upgrading; checked again here so a deploy never proceeds on a pending
# (exit 3) or diverged (exit 4) schema even if the migrate service is overridden locally.
migrations_at_head() {
  local out rc=0
  out="$(docker compose run --rm --no-deps -T migrate python -m app.workers.migrate --check 2>&1)" \
    || rc=$?
  printf '%s\n' "$out" | grep -A1 -F '=== aureon migration check ===' | tail -n 1 || true
  case "$rc" in
    0) echo "migrations: at head" ;;
    3) echo "migrations: PENDING after migrate (see the report above)" ;;
    4) echo "migrations: DIVERGED (the database records revisions this build lacks)" ;;
    *) echo "migrations: check failed (exit $rc)" ;;
  esac
  return "$rc"
}

# One readiness probe: HTTP 200 and {"status": "ready", "checks": {name: {"ok": true}, ...}}.
ready_once() {
  local body code
  body="$(mktemp)"
  code="$(curl -sS -o "$body" -w '%{http_code}' --max-time 4 "$READY_URL" 2>/dev/null)" || code=000
  if [[ "$code" != "200" ]]; then
    last_ready="HTTP ${code:-000}"
    rm -f "$body"
    return 1
  fi
  if last_ready="$(python3 - "$body" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("HTTP 200 but the body is not JSON"); sys.exit(1)
if not isinstance(doc, dict) or doc.get("status") != "ready":
    status = doc.get("status") if isinstance(doc, dict) else None
    print(f"HTTP 200 but status={status!r}"); sys.exit(1)
checks = doc.get("checks")
if not isinstance(checks, dict) or not checks:
    print("HTTP 200 but no checks in the body"); sys.exit(1)
bad = sorted(k for k, v in checks.items() if not (isinstance(v, dict) and v.get("ok") is True))
if bad:
    print("HTTP 200 but failing: " + ", ".join(bad)); sys.exit(1)
print("ready")
PY
  )"; then
    rm -f "$body"
    return 0
  fi
  rm -f "$body"
  return 1
}

wait_ready() { # $1 = seconds; needs READY_PASSES consecutive passing probes
  local deadline=$((SECONDS + $1)) passes=0 need="${READY_PASSES:-2}"
  last_ready="no answer"
  while ((SECONDS < deadline)); do
    if ready_once; then
      passes=$((passes + 1))
      ((passes >= need)) && return 0
    else
      passes=0
    fi
    sleep "${DEPLOY_READY_INTERVAL:-3}"
  done
  echo "health: $READY_URL not ready within $1 s (last: $last_ready)"
  return 1
}

READY_URL="${DEPLOY_READY_URL:-http://127.0.0.1:8000/api/health/ready}"
last_ready=""

# Whole file is parsed before main runs, so `git reset` replacing this file mid-deploy is safe.
main "$@"
exit $?
