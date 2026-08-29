#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ $# -lt 2 || $# -gt 5 ]]; then
  echo "usage: $0 PROJECT OUTPUT [QUALITY=high] [FPS=30] [CRF=16]" >&2
  exit 64
fi

project=$1
output=$2
quality=${3:-high}
fps=${4:-30}
crf=${5:-16}
runtime_root=${VIDEO_FACTORY_RUNTIME_ROOT:-/var/lib/video-factory}
frame_cache=${HYPERFRAMES_FRAMES_CACHE:-$runtime_root/hyperframes-frames}
lock_file=${VIDEO_FACTORY_RENDER_LOCK:-$runtime_root/hyperframes-render.lock}
lock_timeout=${VIDEO_FACTORY_RENDER_LOCK_TIMEOUT_SECONDS:-60}
render_timeout=${VIDEO_FACTORY_RENDER_TIMEOUT_SECONDS:-3600}
hyperframes_bin=${HYPERFRAMES_BIN:-/opt/video-factory/toolchain/node_modules/.bin/hyperframes}

for command_name in ffmpeg ffprobe flock jq sha256sum timeout; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "missing required command: $command_name" >&2
    exit 69
  }
done

[[ -x "$hyperframes_bin" ]] || {
  echo "pinned HyperFrames binary not found: $hyperframes_bin" >&2
  exit 69
}
[[ -d "$project" ]] || { echo "project directory not found: $project" >&2; exit 66; }
[[ ! -e "$output" ]] || { echo "refusing to overwrite existing output: $output" >&2; exit 73; }

output_dir=$(dirname "$output")
output_base=$(basename "$output")
mkdir -p "$output_dir" "$frame_cache" "$runtime_root"
tmp_output=$(mktemp --tmpdir="$output_dir" ".${output_base}.rendering.XXXXXX.mp4")
cleanup() { rm -f -- "$tmp_output"; }
trap cleanup EXIT INT TERM

# One lock is authoritative for all callers on this host. A caller should not
# hold a queue lease while waiting here; claim only after the GPU slot is free.
exec 9>"$lock_file"
if ! flock -w "$lock_timeout" 9; then
  echo "render lock busy after ${lock_timeout}s" >&2
  exit 75
fi

args=(
  render "$project"
  --output "$tmp_output"
  --quality "$quality"
  --fps "$fps"
  --crf "$crf"
  --workers "${HYPERFRAMES_WORKERS:-1}"
  --max-concurrent-renders 1
  --frames-cache-dir "$frame_cache"
  --strict-all
  --no-best-effort
)

if [[ ${HYPERFRAMES_BROWSER_GPU:-1} == 1 ]]; then
  args+=(--browser-gpu)
else
  args+=(--no-browser-gpu)
fi
if [[ ${HYPERFRAMES_GPU_ENCODING:-0} == 1 ]]; then
  args+=(--gpu)
fi

start_epoch=$(date +%s)
timeout --signal=TERM --kill-after=60s "$render_timeout" \
  "$hyperframes_bin" "${args[@]}"

probe=$(ffprobe -v error -show_entries \
  stream=codec_type,codec_name,width,height,pix_fmt,sample_rate \
  -show_entries format=duration -of json "$tmp_output")

jq -e '
  ([.streams[] | select(.codec_type=="video" and .codec_name=="h264" and .width==1080 and .height==1920 and .pix_fmt=="yuv420p")] | length) == 1
  and ([.streams[] | select(.codec_type=="audio" and .codec_name=="aac" and .sample_rate=="48000")] | length) == 1
  and ((.format.duration | tonumber) > 0)
' >/dev/null <<<"$probe" || {
  echo "rendered master failed codec/geometry/audio preflight" >&2
  jq . <<<"$probe" >&2
  exit 65
}

sha256=$(sha256sum "$tmp_output" | awk '{print $1}')
mv -- "$tmp_output" "$output"
trap - EXIT INT TERM
elapsed=$(( $(date +%s) - start_epoch ))
jq -n \
  --arg output "$output" \
  --arg sha256 "$sha256" \
  --argjson duration_seconds "$elapsed" \
  '{ok:true, output:$output, sha256:$sha256, duration_seconds:$duration_seconds}'
