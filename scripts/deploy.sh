#!/usr/bin/env bash
# Backend deploy (docs/deploy-automation.md): bring the server to origin/main.
#
#   scripts/deploy.sh            # deploy if origin/main moved since the last good deploy
#   scripts/deploy.sh --force    # rebuild and redeploy even if nothing moved
#
# fetch -> reset to origin/main -> docker compose build -> migrate -> up -d -> wait up to 60 s
# for /api/health/ready. If the health check fails, the previous image and commit come back
# (`aureon-agentic:rollback` -> `:dev`, git reset to the old SHA, up -d) and it exits 1.
# Lock-protected (flock): a second deploy waits for the first. Server-local files (.env,
# .env.migrate, compose.override.yaml, scripts/ops/ops.env) are untracked and never touched;
# local edits to tracked files are stashed (named), never lost. Migrations are additive, so the
# old image runs on the new schema. Never touches Postgres data or TRADING_LIVE_ENABLED.
set -euo pipefail
umask 027

main() {
  local repo branch log_dir log_file lock state force=0
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  branch="${DEPLOY_BRANCH:-main}"
  log_dir="${DEPLOY_LOG_DIR:-/mnt/spasta-data/logs}"
  log_file="$log_dir/deploy.log"
  lock="$log_dir/.deploy-backend.lock"
  state="$log_dir/.deployed-backend"
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
  if docker compose build --quiet && docker compose run --rm migrate && docker compose up -d \
    && wait_ready 60; then
    echo "$want" >"$state"
    echo "OK ${want:0:12} healthy"
    return 0
  fi

  echo "FAILED ${want:0:12}: rolling back to ${cur:0:12}"
  git reset --quiet --hard "$cur"
  for img in aureon-agentic aureon-coding aureon-imaging; do
    if docker image inspect "$img:rollback" >/dev/null 2>&1; then
      docker tag "$img:rollback" "$img:dev"
    fi
  done
  if docker compose up -d && wait_ready 90; then
    echo "ROLLED BACK to ${cur:0:12} (healthy)"
  else
    echo "ROLLBACK UNHEALTHY at ${cur:0:12}: needs a human (scripts/ops/healthcheck.sh)"
  fi
  return 1
}

wait_ready() { # $1 = seconds
  local deadline=$((SECONDS + $1))
  while ((SECONDS < deadline)); do
    if curl -fsS -o /dev/null --max-time 4 http://127.0.0.1:8000/api/health/ready; then
      return 0
    fi
    sleep 3
  done
  echo "health: /api/health/ready not ok within $1 s"
  return 1
}

# Whole file is parsed before main runs, so `git reset` replacing this file mid-deploy is safe.
main "$@"
exit $?
