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

# The `name=value` lines deploy.sh wrote, as one JSON object ("{}" when there are none, e.g. a
# deploy script that records no phases: the record then says health was not verified).
phases_json() {
  [[ -s "$1" ]] || { echo '{}'; return 0; }
  python3 -c '
import json, sys
out = {}
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    k, sep, v = line.rstrip("\n").partition("=")
    if sep and k.isidentifier():
        out[k] = v[:300]
print(json.dumps(out)[:4000])
' "$1" 2>/dev/null || echo '{}'
}

# One aureon.deploy.progress/v1 report (docs/33-deploy-progress.md): the stage lines the deploy
# script wrote so far, the commit it checked out, and this deploy's own lines of deploy.log
# since `offset` (lines tagged with the target name; the poller's own lines are left out).
progress_json() { # name run_id phases_file log_offset
  python3 - "$1" "$2" "$3" "$4" "$LOG_FILE" <<'PY' 2>/dev/null
import json, os, sys

name, run_id, phases, offset, log_file = sys.argv[1:6]
stages, sha, pr = [], None, None
try:
    with open(phases, encoding="utf-8", errors="replace") as f:
        for line in f:
            k, _, v = line.rstrip("\n").partition("=")
            if k == "stage":
                parts = v.split("|", 3)
                if len(parts) == 4:
                    stages.append({"name": parts[0], "status": parts[1], "at": parts[2],
                                   "detail": parts[3] or None})
            elif k == "sha":
                sha = v
            elif k == "pr" and v.isdigit():
                pr = int(v)
except OSError:
    pass
lines, truncated = [], False
try:
    with open(log_file, "rb") as f:
        f.seek(int(offset))
        data = f.read(4 * 1024 * 1024)
        truncated = bool(f.read(1))
    for raw in data.decode("utf-8", "replace").splitlines():
        parts = raw.split(" ", 2)
        if len(parts) == 3 and parts[1] == name:
            lines.append(parts[0] + " " + parts[2])
except (OSError, ValueError):
    pass
if len(lines) > 600:
    lines, truncated = lines[-600:], True
doc = {"schema": "aureon.deploy.progress/v1", "run_id": int(run_id), "target": name,
       "stages": stages[-200:], "log": "\n".join(lines)[-256 * 1024:],
       "log_truncated": truncated}
if sha:
    doc["sha"] = sha
if pr:
    doc["pr"] = pr
print(json.dumps(doc))
PY
}

# Send a progress report; best effort, never blocks or fails a deploy. `live`: into the running
# API container (a backend deploy restarts it: missed reports are healed by the next one).
# `final`: through a one-shot container, so the end of a deploy is always recorded.
report_progress() { # live|final name run_id phases_file log_offset
  local doc mode="$1"
  shift
  doc="$(progress_json "$@")" || return 0
  [[ -n "$doc" ]] || return 0
  if [[ "$mode" == "live" ]]; then
    printf '%s' "$doc" | (cd "$BACKEND_DIR" && timeout 20 docker compose exec -T api \
      python -m app.workers.deploy_progress) >/dev/null 2>&1 || true
  else
    printf '%s' "$doc" | (cd "$BACKEND_DIR" && timeout 120 docker compose run --rm --no-deps -T \
      api python -m app.workers.deploy_progress) >/dev/null 2>>"$LOG_FILE" || true
  fi
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

  phases_file="$LOG_DIR/.deploy-$name.phases"
  rm -f "$phases_file" # never report a previous deploy's phases
  offset="$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)"
  drc=0
  # Run the deploy and report its progress while it runs (every PROGRESS_SECONDS when the
  # stages or the log changed, and at least once a minute so it never looks stale).
  "$script" &
  dpid=$!
  last_sig=""
  last_sent=$SECONDS
  while kill -0 "$dpid" 2>/dev/null; do
    sleep "${PROGRESS_SECONDS:-4}"
    [[ -n "$run_id" ]] || continue
    sig="$(stat -c '%s.%Y' "$phases_file" "$LOG_FILE" 2>/dev/null | tr '\n' ' ' || true)"
    if [[ "$sig" != "$last_sig" ]] || ((SECONDS - last_sent >= 60)); then
      report_progress live "$name" "$run_id" "$phases_file" "$offset"
      last_sig="$sig"
      last_sent=$SECONDS
    fi
  done
  wait "$dpid" || drc=$?
  if [[ -n "$run_id" ]]; then
    report_progress final "$name" "$run_id" "$phases_file" "$offset"
  fi
  phases_json="$(phases_json "$phases_file")"
  if ((drc == 0)); then
    log "$name: deployed ${remote:0:12}"
    rm -f "$LOG_DIR/.failed-$name"
    if [[ -n "$run_id" ]]; then
      record finish --run-id "$run_id" --status succeeded --exit-code 0 \
        --phases-json "$phases_json" >/dev/null
    fi
  else
    # deploy.sh: 1 = failed and rolled back healthy; 2 = the rollback is unhealthy too;
    # 3 = failed before anything changed. Only a healthy rollback is recorded as rolled back.
    rolled=()
    case "$drc" in
      1)
        rolled=(--rolled-back)
        what="rolled back"
        ;;
      2) what="ROLLBACK UNHEALTHY, needs a human" ;;
      3) what="nothing changed, the previous release is still live" ;;
      *) what="exit $drc" ;;
    esac
    log "$name: deploy FAILED for ${remote:0:12}, $what (exit $drc; see $LOG_FILE)"
    echo "$remote" >"$LOG_DIR/.failed-$name"
    if [[ -n "$run_id" ]]; then
      record finish --run-id "$run_id" --status failed \
        --error "deploy.sh failed for ${remote:0:12} ($what); see $LOG_FILE" "${rolled[@]}" \
        --exit-code "$drc" --phases-json "$phases_json" >/dev/null
    fi
    rc=1
  fi
done
exit "$rc"
