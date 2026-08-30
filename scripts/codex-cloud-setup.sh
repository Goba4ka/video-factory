#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

install_apt_package() {
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y "$1"
  elif [[ "$(id -u)" == "0" ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y "$1"
  else
    echo "error: $1 is missing and apt requires root privileges" >&2
    return 1
  fi
}

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "error: ffmpeg/ffprobe are missing and apt-get is unavailable" >&2
    exit 1
  fi
  install_apt_package ffmpeg
fi

ffmpeg -hide_banner -version >/dev/null
ffprobe -hide_banner -version >/dev/null

python3 -m pip install -e factory
python3 -m pip install -r factory/deployment/requirements-cloud.lock

python3 -m video_factory lanes --registry factory/lanes/registry.json
