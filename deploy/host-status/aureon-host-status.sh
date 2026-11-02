#!/bin/sh
# Writes `systemctl is-active` for a FIXED list of units to a JSON file that the API reads
# (GET /api/system/status → host_services). It only reads state: it never starts, stops or
# restarts anything, and takes no input from the API.
#
#   AUREON_HOST_UNITS        space-separated unit names (default: "cloudflared docker")
#   AUREON_HOST_STATUS_FILE  output (default: /mnt/spasta-data/status/host-services.json)
set -eu

OUT="${AUREON_HOST_STATUS_FILE:-/mnt/spasta-data/status/host-services.json}"
UNITS="${AUREON_HOST_UNITS:-cloudflared docker}"

for u in $UNITS; do
  case "$u" in
    *[!A-Za-z0-9@._-]*|"") echo "refusing unit name: '$u'" >&2; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$OUT")"
tmp="$(mktemp "$OUT.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  printf '{"generated_at":"%s","units":{' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sep=""
  for u in $UNITS; do
    state="$(systemctl is-active "$u" 2>/dev/null || true)"
    printf '%s"%s":"%s"' "$sep" "$u" "${state:-unknown}"
    sep=","
  done
  printf '}}\n'
} > "$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$OUT"
trap - EXIT
