#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 1 ]]; then
  echo "usage: $0 BACKUP_ROOT" >&2
  exit 64
fi

for command_name in realpath sqlite3 grep sha256sum find sort tail; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "missing required command: $command_name" >&2
    exit 69
  }
done

backup_root=$(realpath -m -- "$1")
[[ "$backup_root" != "/" ]] || {
  echo "refusing to use filesystem root as backup root" >&2
  exit 64
}
factory_db=${VIDEO_FACTORY_DB:-/var/lib/video-factory/queue/factory.sqlite3}
fish_db=${FISH_USAGE_DB:-/var/lib/video-factory/fish_audio_usage.sqlite3}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$backup_root/$stamp"
mkdir -p "$target"

for db in "$factory_db" "$fish_db"; do
  [[ -f "$db" ]] || { echo "required database missing: $db" >&2; exit 66; }
  name=$(basename "$db")
  sqlite3 "$db" ".timeout 10000" ".backup '$target/$name'"
  sqlite3 "$target/$name" "PRAGMA integrity_check;" | grep -qx ok
done

sha256sum "$target"/*.sqlite3 >"$target/SHA256SUMS"
printf '{"created_at":"%s","factory_db":"%s","fish_usage_db":"%s"}\n' \
  "$stamp" "$factory_db" "$fish_db" >"$target/manifest.json"

# Retention: keep 30 daily directories. Off-host encrypted replication is a
# deployment concern and must complete before local deletion is enabled.
mapfile -t old < <(find "$backup_root" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | tail -n +31)
for name in "${old[@]}"; do
  [[ "$name" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
  candidate=$(realpath -m -- "$backup_root/$name")
  [[ $(dirname "$candidate") == "$backup_root" ]] || {
    echo "refusing unsafe retention target: $candidate" >&2
    exit 65
  }
  rm -rf -- "$candidate"
done

printf '%s\n' "$target"
