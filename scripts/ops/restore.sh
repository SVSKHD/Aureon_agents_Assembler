#!/usr/bin/env bash
# Restore an Aureon backup (docs/16-production-hardening.md, "Restore").
#
#   scripts/ops/restore.sh <backup>                         # into a NEW db spasta_restore_<ts>
#   scripts/ops/restore.sh <backup> --target-db NAME        # into a NEW db NAME (must not exist)
#   scripts/ops/restore.sh <backup> --with-data             # also unpack data.tar into
#                                                           #   DATA_ROOT/restore-<ts>/ (never
#                                                           #   over the live folders)
#   scripts/ops/restore.sh <backup> --into-live --yes-i-am-sure
#                                                           # over the LIVE database (stop the
#                                                           #   stack first; see the runbook)
#
# <backup> is a folder name under BACKUP_DIR (aureon-<stamp>), "latest", or a path.
# Checksums are verified before anything is touched. Prints the target DB name on stdout.
set -euo pipefail
umask 077

# shellcheck source=scripts/ops/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
  sed -n '2,15p' "$0"
  exit "${1:-0}"
}

SRC=""
TARGET=""
INTO_LIVE=0
SURE=0
WITH_DATA=0
while (($#)); do
  case "$1" in
    --target-db)
      [[ $# -ge 2 ]] || die "--target-db needs a name" 2
      TARGET="$2"
      shift
      ;;
    --into-live) INTO_LIVE=1 ;;
    --yes-i-am-sure) SURE=1 ;;
    --with-data) WITH_DATA=1 ;;
    -h | --help) usage 0 ;;
    -*) die "unknown option: $1" 2 ;;
    *)
      [[ -z "$SRC" ]] || die "only one backup may be given" 2
      SRC="$1"
      ;;
  esac
  shift
done
[[ -n "$SRC" ]] || usage 2

require_cmd docker sha256sum tar realpath
valid_db_name "$PG_DB" || die "invalid PG_DB" 2

# --- resolve the backup ------------------------------------------------------------------
SRC="$(resolve_backup "$SRC")"
[[ -d "$SRC" ]] || die "backup not found: $SRC" 3
for f in SHA256SUMS MANIFEST.json db-schemas.dump db-public.dump; do
  [[ -f "$SRC/$f" ]] || die "backup is incomplete: $f missing" 3
done

# --- target database ---------------------------------------------------------------------
if [[ "$INTO_LIVE" == 1 ]]; then
  [[ "$SURE" == 1 ]] || die "--into-live overwrites the LIVE database; add --yes-i-am-sure" 3
  [[ -z "$TARGET" || "$TARGET" == "$PG_DB" ]] || die "--into-live conflicts with --target-db" 2
  TARGET="$PG_DB"
else
  TARGET="${TARGET:-spasta_restore_$(date -u +%Y%m%d_%H%M%S)}"
  [[ "$TARGET" != "$PG_DB" ]] ||
    die "refusing to restore over the live database $PG_DB without --into-live --yes-i-am-sure" 3
fi
valid_db_name "$TARGET" || die "invalid target database name: $TARGET" 2

# --- verify checksums first --------------------------------------------------------------
log "verifying checksums in $SRC"
(cd "$SRC" && sha256sum --quiet --strict -c SHA256SUMS) || die "checksum verification failed" 3

# --- restore -----------------------------------------------------------------------------
if [[ "$INTO_LIVE" == 1 ]]; then
  log "restoring INTO THE LIVE DATABASE $TARGET (--into-live --yes-i-am-sure)"
  restore_opts=(--clean --if-exists --single-transaction --exit-on-error)
else
  exists="$(psql_q postgres -c "SELECT 1 FROM pg_database WHERE datname = '$TARGET'")"
  [[ -z "$exists" ]] || die "database $TARGET already exists; pick another --target-db" 3
  log "creating database $TARGET"
  psql_q postgres -c "CREATE DATABASE \"$TARGET\" TEMPLATE template0"
  restore_opts=()
fi

if [[ -s "$SRC/public-functions.sql" ]]; then
  log "restoring public helper functions"
  {
    echo "SET check_function_bodies = off;"
    cat -- "$SRC/public-functions.sql"
  } | pg_exec psql -X -q -v ON_ERROR_STOP=0 -U "$PG_USER" -d "$TARGET" -f - >/dev/null
fi

errors=0
for dump in db-schemas.dump db-public.dump; do
  log "pg_restore $dump -> $TARGET"
  # Non-fatal errors (e.g. Supabase roles named in RLS policies that do not exist on this
  # server) are reported, not fatal: restore-drill.sh verifies the row counts afterwards.
  if ! pg_exec pg_restore --no-owner --no-privileges "${restore_opts[@]}" \
    -U "$PG_USER" -d "$TARGET" <"$SRC/$dump"; then
    errors=$((errors + 1))
    log "pg_restore reported errors for $dump"
    [[ "$INTO_LIVE" == 0 ]] || die "restore into the live database failed and was rolled back" 1
  fi
done

if [[ "$WITH_DATA" == 1 && -s "$SRC/data.tar" ]]; then
  require_hdd
  data_dest="$DATA_ROOT/restore-$(date -u +%Y%m%d_%H%M%S)"
  mkdir -- "$data_dest"
  tar -C "$data_dest" --no-same-owner -xf "$SRC/data.tar"
  log "data files unpacked into $data_dest (move them into place by hand)"
fi

log "restore finished: database=$TARGET dumps_with_errors=$errors"
printf '%s\n' "$TARGET"
