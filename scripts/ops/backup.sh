#!/usr/bin/env bash
# Nightly backup of everything Aureon owns (docs/16-production-hardening.md, "Backups").
#
#   scripts/ops/backup.sh            # run by aureon-backup.timer
#   scripts/ops/backup.sh --dry-run  # print what would happen, write nothing
#
# Writes ${BACKUP_DIR}/aureon-<UTC stamp>/ on the HDD:
#   db-schemas.dump       pg_dump -Fc of the schemas this app owns (core audit ai agents
#                         trading dhan mt5 github spasta)
#   db-public.dump        pg_dump -Fc of the public finance tables + aureon_alembic_version
#   public-functions.sql  definitions of the functions those tables use as defaults
#   data.tar              DATA_ROOT subdirs (BACKUP_DATA_DIRS: images finance-attachments)
#   MANIFEST.json         row counts, alembic revision, sizes, checksums
#   SHA256SUMS            sha256 of every file above
# pg_dump runs inside the Postgres container (`docker compose exec -T`), so no port is needed.
# Status: BACKUP_STATUS_FILE (default $DATA_ROOT/ops-status/backup.json, mode 644, no
# secrets) records ok/failed and the last success, for the scheduler's backup alert.
# Retention: backup folders older than BACKUP_RETENTION_DAYS are removed. Nothing else is
# ever deleted, and only folders named aureon-<stamp> with a MANIFEST.json, strictly inside
# BACKUP_DIR.
set -euo pipefail
umask 077

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $arg" 2 ;;
  esac
done

require_cmd docker sha256sum tar python3 realpath
require_hdd
require_backup_dir
[[ "$BACKUP_RETENTION_DAYS" =~ ^[0-9]+$ && "$BACKUP_RETENTION_DAYS" -ge 1 ]] ||
  die "BACKUP_RETENTION_DAYS must be a whole number >= 1" 2
[[ "$BACKUP_PUBLIC_FUNCTIONS" =~ ^[a-z_][a-z0-9_,]*$ ]] ||
  die "BACKUP_PUBLIC_FUNCTIONS must be a comma-separated list of function names" 2
valid_db_name "$PG_DB" || die "invalid PG_DB" 2

STAMP="$(utc_stamp)"
DEST="$BACKUP_DIR/aureon-$STAMP"
PARTIAL="$DEST.partial"

schema_args=()
for s in "${OWNED_SCHEMAS[@]}"; do schema_args+=(-n "$s"); done
table_args=()
for t in "${PUBLIC_TABLES[@]}"; do table_args+=(-t "public.$t"); done

if [[ "$DRY_RUN" == 1 ]]; then
  log "dry run: would write $DEST"
  log "dry run: pg_dump -Fc -U $PG_USER -d $PG_DB ${schema_args[*]}"
  log "dry run: pg_dump -Fc -U $PG_USER -d $PG_DB ${table_args[*]}"
  log "dry run: tar of $DATA_ROOT/{${BACKUP_DATA_DIRS// /,}}"
  exit 0
fi

# Status file for alerts (Phase 22). Best effort: a status write never fails the backup.
write_status() {
  local status="$1" dir tmp
  path_is_under "$BACKUP_STATUS_FILE" "$DATA_ROOT" || {
    log "WARNING: BACKUP_STATUS_FILE is not under DATA_ROOT; status not written"
    return 0
  }
  dir="$(dirname -- "$BACKUP_STATUS_FILE")"
  tmp="$BACKUP_STATUS_FILE.tmp.$$"
  { mkdir -p -- "$dir" && chmod 755 -- "$dir"; } || return 0
  STATUS="$status" STAMP="$STAMP" PG_DB="$PG_DB" python3 - "$BACKUP_STATUS_FILE" "$tmp" <<'PY' ||
import json, os, sys
from datetime import datetime, timezone

prev = {}
try:
    prev = json.loads(open(sys.argv[1]).read())
except Exception:
    pass
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
ok = os.environ["STATUS"] == "ok"
doc = {
    "format": 1,
    "status": os.environ["STATUS"],
    "database": os.environ["PG_DB"],
    "stamp": os.environ["STAMP"],
    "finished_at": now,
    "last_success_at": now if ok else prev.get("last_success_at"),
    "last_success_backup": ("aureon-" + os.environ["STAMP"]) if ok else prev.get("last_success_backup"),
}
open(sys.argv[2], "w").write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
PY
    return 0
  if ! { chmod 644 -- "$tmp" && mv -f -- "$tmp" "$BACKUP_STATUS_FILE"; }; then
    rm -f -- "$tmp"
  fi
}

mkdir -p "$BACKUP_DIR"
mkdir "$PARTIAL"
cleanup_partial() {
  local rc=$?
  # Only the half-written folder of THIS run; its name is fixed above.
  if [[ -d "$PARTIAL" ]] && path_is_under "$PARTIAL" "$BACKUP_DIR"; then
    rm -rf -- "$PARTIAL"
  fi
  if [[ "$rc" != 0 ]]; then write_status failed; fi
  exit "$rc"
}
trap cleanup_partial EXIT

log "backup $STAMP: dumping owned schemas"
pg_exec pg_dump -Fc -U "$PG_USER" -d "$PG_DB" "${schema_args[@]}" >"$PARTIAL/db-schemas.dump"
log "backup $STAMP: dumping public finance tables"
pg_exec pg_dump -Fc -U "$PG_USER" -d "$PG_DB" "${table_args[@]}" >"$PARTIAL/db-public.dump"

fn_list="'${BACKUP_PUBLIC_FUNCTIONS//,/\',\'}'"
psql_q "$PG_DB" -f - >"$PARTIAL/public-functions.sql" <<SQL
SELECT pg_get_functiondef(p.oid) || ';'
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public' AND p.proname IN ($fn_list);
SQL

# Exact row counts at dump time; restore-drill.sh compares the restored DB against them.
row_count_sql | psql_q "$PG_DB" -f - >"$PARTIAL/row-counts.txt"
alembic_rev="$(psql_q "$PG_DB" -c 'SELECT version_num FROM public.aureon_alembic_version' || true)"

data_dirs=()
for d in $BACKUP_DATA_DIRS; do
  [[ "$d" =~ ^[A-Za-z0-9_-][A-Za-z0-9._-]*$ ]] || die "invalid BACKUP_DATA_DIRS entry: $d" 2
  if [[ -d "$DATA_ROOT/$d" ]]; then data_dirs+=("$d"); else log "skip missing $DATA_ROOT/$d"; fi
done
if ((${#data_dirs[@]})); then
  log "backup $STAMP: archiving ${data_dirs[*]}"
  tar -C "$DATA_ROOT" -cf "$PARTIAL/data.tar" -- "${data_dirs[@]}"
else
  tar -C "$DATA_ROOT" -cf "$PARTIAL/data.tar" --files-from /dev/null
fi

(cd "$PARTIAL" && sha256sum -- db-schemas.dump db-public.dump public-functions.sql \
  row-counts.txt data.tar >SHA256SUMS)

STAMP="$STAMP" PG_DB="$PG_DB" ALEMBIC_REV="$alembic_rev" RETENTION="$BACKUP_RETENTION_DAYS" \
  SCHEMAS="${OWNED_SCHEMAS[*]}" TABLES="${PUBLIC_TABLES[*]}" DATA_DIRS="${data_dirs[*]:-}" \
  python3 - "$PARTIAL" <<'PY'
import json, os, sys
from pathlib import Path

d = Path(sys.argv[1])
sums = dict(reversed(line.split(None, 1)) for line in (d / "SHA256SUMS").read_text().splitlines())
counts = {}
for line in (d / "row-counts.txt").read_text().splitlines():
    if "|" in line:
        name, n = line.rsplit("|", 1)
        counts[name] = int(n)
manifest = {
    "format": 1,
    "created_at": os.environ["STAMP"],
    "database": os.environ["PG_DB"],
    "alembic_revision": os.environ["ALEMBIC_REV"].strip() or None,
    "schemas": os.environ["SCHEMAS"].split(),
    "public_tables": os.environ["TABLES"].split(),
    "data_dirs": os.environ["DATA_DIRS"].split(),
    "retention_days": int(os.environ["RETENTION"]),
    "row_counts": counts,
    "files": {
        name.strip(): {"sha256": sha, "bytes": (d / name.strip()).stat().st_size}
        for name, sha in sums.items()
    },
}
(d / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY
(cd "$PARTIAL" && sha256sum -- MANIFEST.json >>SHA256SUMS)

mv -- "$PARTIAL" "$DEST"
trap - EXIT
write_status ok
log "backup $STAMP: done -> $DEST"

# --- retention ---------------------------------------------------------------------------
cutoff="$(date -u -d "-${BACKUP_RETENTION_DAYS} days" +%Y%m%dT%H%M%SZ)"
backup_root="$(real_path "$BACKUP_DIR")"
shopt -s nullglob
for old in "$backup_root"/aureon-*; do
  name="$(basename -- "$old")"
  [[ "$name" =~ $BACKUP_NAME_RE ]] || continue          # not one of ours
  [[ "${name#aureon-}" < "$cutoff" ]] || continue       # still inside retention
  [[ -d "$old" && ! -L "$old" ]] || continue             # real folders only, never links
  [[ -f "$old/MANIFEST.json" ]] || continue              # only complete backups
  path_is_under "$old" "$backup_root" || die "refusing to delete outside BACKUP_DIR: $old" 5
  [[ "$old" != "$DEST" ]] || continue
  log "retention: removing $name (older than ${BACKUP_RETENTION_DAYS} days)"
  rm -rf -- "$old"
done
