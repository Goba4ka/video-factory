#!/usr/bin/env bash
set -Eeuo pipefail

: "${VIDEO_FACTORY_DB:?VIDEO_FACTORY_DB is required}"
: "${VIDEO_FACTORY_RUNTIME_ROOT:?VIDEO_FACTORY_RUNTIME_ROOT is required}"

cli="/opt/video-factory/current/.venv/bin/video-factory"
metrics_dir="$VIDEO_FACTORY_RUNTIME_ROOT/metrics"
install -d -m 0750 "$metrics_dir"

bucket="$(date -u +%Y%m%dT%H%M)"
host="$(hostname -s)"
"$cli" metrics-collect-queue \
  --db "$VIDEO_FACTORY_DB" \
  --idempotency-key "systemd-metrics:${host}:${bucket}" \
  --export "$metrics_dir/last-collection.json"
"$cli" analytics-summary \
  --db "$VIDEO_FACTORY_DB" \
  --export "$metrics_dir/latest-summary.json"
