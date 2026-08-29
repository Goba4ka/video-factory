#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 -m pip install -e factory
python3 -m pip install -r factory/deployment/requirements-cloud.lock

python3 -m video_factory lanes --registry factory/lanes/registry.json
