#!/usr/bin/env bash
# Pull-based auto-deploy (docs/deploy-automation.md). Run every minute by aureon-deploy.timer.
#
# For each target, compare the remote branch head (git ls-remote, read-only, no credentials:
# both repos are public) with the last good deploy; when it moved, run that repo's
# scripts/deploy.sh and record the deploy as a `deployer` agent run (Spasta agent monitor).
# Nothing from GitHub executes here except the merged code of the two repos themselves.
#
# Pause: `touch /mnt/spasta-data/logs/DEPLOY_PAUSED` (or stop aureon-deploy.timer).
set -euo pipefail

LOG_DIR="${DEPLOY_LOG_DIR:-/mnt/spasta-data/logs}"
LOG_FILE="$LOG_DIR/deploy.log"
BACKEND_DIR="${BACKEND_DIR:-/opt/private-ai/aureon}"
# target|repo dir|branch
TARGETS=(
  "backend|$BACKEND_DIR|main"
  "frontend|${FRONTEND_DIR:-/srv/projects/ASTRA-FRONTEND}|dev01"
)

log() { printf '%s poll %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE"; }

record() { # best effort: never blocks or fails a deploy
  { (cd "$BACKEND_DIR" && timeout 120 docker compose run --rm --no-deps -T api \
    python -m app.workers.deploy_record "$@") 2>>"$LOG_FILE" | tail -n 1; } || true
}

if [[ -e "$LOG_DIR/DEPLOY_PAUSED" ]]; then
  exit 0
fi

rc=0
for t in "${TARGETS[@]}"; do
  IFS='|' read -r name dir branch <<<"$t"
  [[ -d "$dir/.git" ]] || { log "$name: $dir is not a git checkout, skipped"; continue; }
  remote="$(git -C "$dir" ls-remote --quiet origin "refs/heads/$branch" | cut -f1)" || remote=""
  [[ "$remote" =~ ^[0-9a-f]{40}$ ]] || { log "$name: cannot read origin/$branch, skipped"; continue; }
  deployed="$(cat "$LOG_DIR/.deployed-$name" 2>/dev/null || true)"
  [[ "$remote" == "$deployed" ]] && continue
  # A commit that failed (and was rolled back) is not retried every minute; the next commit,
  # or `scripts/deploy.sh --force` by hand, tries again.
  failed="$(cat "$LOG_DIR/.failed-$name" 2>/dev/null || true)"
  [[ "$remote" == "$failed" ]] && continue

  script="$dir/scripts/deploy.sh"
  [[ -x "$script" ]] || git -C "$dir" fetch --quiet origin "$branch" || true
  if [[ ! -x "$script" ]] && git -C "$dir" cat-file -e "origin/$branch:scripts/deploy.sh" 2>/dev/null; then
    # First deploy of a repo whose deploy script only exists upstream so far.
    { git -C "$dir" checkout --quiet "$branch" && git -C "$dir" reset --quiet --hard "origin/$branch"; } || true
  fi
  [[ -x "$script" ]] || { log "$name: no scripts/deploy.sh on $branch, skipped"; continue; }

  log "$name: origin/$branch ${remote:0:12} != deployed ${deployed:0:12}"
  run_id="$(record start --target "$name" --sha "$remote" --from "${deployed:-none}" --log "$LOG_FILE")"
  [[ "$run_id" =~ ^[0-9]+$ ]] || { log "$name: deploy not recorded (recorder unavailable)"; run_id=""; }

  drc=0
  "$script" || drc=$?
  if ((drc == 0)); then
    log "$name: deployed ${remote:0:12}"
    rm -f "$LOG_DIR/.failed-$name"
    if [[ -n "$run_id" ]]; then record finish --run-id "$run_id" --status succeeded >/dev/null; fi
  else
    # deploy.sh: 1 = failed and rolled back healthy; 2 = the rollback is unhealthy too. Only
    # a healthy rollback is recorded as rolled back (the frontend script only uses 0/1).
    rolled=(--rolled-back)
    what="rolled back"
    if ((drc != 1)); then
      rolled=()
      what="ROLLBACK UNHEALTHY, needs a human"
    fi
    log "$name: deploy FAILED for ${remote:0:12}, $what (exit $drc; see $LOG_FILE)"
    echo "$remote" >"$LOG_DIR/.failed-$name"
    if [[ -n "$run_id" ]]; then
      record finish --run-id "$run_id" --status failed \
        --error "deploy.sh failed for ${remote:0:12} ($what); see $LOG_FILE" "${rolled[@]}" \
        >/dev/null
    fi
    rc=1
  fi
done
exit "$rc"
