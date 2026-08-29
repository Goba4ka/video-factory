#!/usr/bin/env bash
set -euo pipefail
umask 027

readonly EX_USAGE=64
readonly EX_DATAERR=65
readonly EX_NOINPUT=66
readonly EX_UNAVAILABLE=69
readonly EX_CANTCREAT=73
readonly SAFE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

apply=0
activate=0
replace_runtime_env=0
require_gpu=0
release_arg=
wheel_arg=
wheel_sha256=
wheelhouse_arg=
wheelhouse_manifest_arg=
wheelhouse_manifest_sha256=

# The root prefix exists only so the executable tests can exercise the real
# mutation and rollback paths without touching the host.  Production runs must
# not set either test variable.
test_mode=${VIDEO_FACTORY_BOOTSTRAP_TEST_MODE:-0}
test_root_arg=${VIDEO_FACTORY_BOOTSTRAP_TEST_ROOT:-}
case "$test_mode" in
  0|1) ;;
  *) printf 'bootstrap error: VIDEO_FACTORY_BOOTSTRAP_TEST_MODE must be 0 or 1\n' >&2; exit "$EX_USAGE" ;;
esac
if (( test_mode )); then
  [[ -n "$test_root_arg" && "$test_root_arg" == /* && "$test_root_arg" != / ]] || {
    printf 'bootstrap error: test mode requires an absolute non-root VIDEO_FACTORY_BOOTSTRAP_TEST_ROOT\n' >&2
    exit "$EX_USAGE"
  }
  root_prefix=${test_root_arg%/}
  root_prefix=$(realpath -e -- "$root_prefix") || {
    printf 'bootstrap error: cannot resolve test root\n' >&2
    exit "$EX_NOINPUT"
  }
  bootstrap_path=$PATH
else
  [[ -z "$test_root_arg" ]] || {
    printf 'bootstrap error: VIDEO_FACTORY_BOOTSTRAP_TEST_ROOT requires test mode\n' >&2
    exit "$EX_USAGE"
  }
  root_prefix=
  bootstrap_path=$SAFE_PATH
  PATH=$SAFE_PATH
  export PATH
fi
unset BASH_ENV ENV CDPATH

path_at_root() {
  printf '%s%s' "$root_prefix" "$1"
}

opt_root=$(path_at_root /opt/video-factory)
releases_root=$opt_root/releases
current=$opt_root/current
runtime_root=$(path_at_root /var/lib/video-factory)
artifact_root=$(path_at_root /srv/video-factory)
config_root=$(path_at_root /etc/video-factory)
runtime_env=$config_root/runtime.env
systemd_root=$(path_at_root /etc/systemd/system)
lock_root=$(path_at_root /run/lock)
lock_file=$lock_root/video-factory-bootstrap.lock

readonly -a MANAGED_UNITS=(
  video-factory-backup.service
  video-factory-backup.timer
  video-factory-metrics.service
  video-factory-metrics.timer
  video-factory-preflight.service
  video-factory-provider-worker@.service
  video-factory-recover.service
  video-factory-recover.timer
  video-factory-review-release.service
  video-factory-review-release.timer
  video-factory-runtime-worker@.service
  video-factory-voice.service
  video-factory-worker@.service
)

temporary_files=()
temporary_trees=()
candidate_link=
backup_dir=
transaction_active=0
transaction_committed=0
venv_created_this_run=0
test_lock_dir=

usage() {
  cat <<'EOF'
Usage:
  bootstrap_ubuntu_server.sh --release RELEASE --wheel WHEEL \
    --wheel-sha256 SHA256 --wheelhouse DIR \
    --wheelhouse-manifest FILE --wheelhouse-manifest-sha256 SHA256 \
    [--activate] [--require-gpu] [--replace-runtime-env] [--apply]

Default mode is a read-only dry run. --apply is required for every mutation.
Without --activate the script stages users, directories, the exact bound venv,
and a missing non-secret runtime.env only. It does not install units or change
current. --activate first validates the candidate through a temporary symlink,
then atomically commits runtime config, allowlisted units, and current; a failed
post-commit check restores their exact previous state.

--replace-runtime-env and --require-gpu are valid only with --activate.
Dependencies are installed only from the supplied flat, wheel-only wheelhouse.
Both the application wheel and the complete wheelhouse manifest are bound to
operator-supplied SHA-256 values. The script never creates credentials, fetches
models/media, enables services/timers, or invokes final_review/publisher.
EOF
}

die() {
  local code=$1
  shift
  printf 'bootstrap error: %s\n' "$*" >&2
  exit "$code"
}

quote_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

assert_no_symlink_chain() {
  local path=$1
  [[ "$path" == /* ]] || die "$EX_DATAERR" "path is not absolute: $path"
  local cursor=/ component
  local remainder=${path#/}
  local -a components=()
  IFS=/ read -r -a components <<<"$remainder"
  for component in "${components[@]}"; do
    [[ -n "$component" ]] || continue
    cursor=${cursor%/}/$component
    [[ ! -L "$cursor" ]] || die "$EX_DATAERR" "symlink path component is forbidden: $cursor"
  done
}

mode_is_not_group_world_writable() {
  local mode=$1
  (( (8#$mode & 0022) == 0 ))
}

assert_secure_file() {
  local path=$1 expected_uid=$2 label=$3
  [[ -f "$path" && ! -L "$path" ]] || die "$EX_DATAERR" "$label must be a regular non-symlink file: $path"
  [[ $(stat -c '%u' -- "$path") == "$expected_uid" ]] || die "$EX_DATAERR" "$label has an untrusted owner: $path"
  local mode
  mode=$(stat -c '%a' -- "$path")
  mode_is_not_group_world_writable "$mode" || die "$EX_DATAERR" "$label is group/world writable: $path"
}

assert_secure_directory() {
  local path=$1 expected_uid=$2 label=$3 mode
  [[ -d "$path" && ! -L "$path" ]] || die "$EX_DATAERR" "$label must be a real directory: $path"
  [[ $(stat -c '%u' -- "$path") == "$expected_uid" ]] || die "$EX_DATAERR" "$label has an untrusted owner: $path"
  mode=$(stat -c '%a' -- "$path")
  mode_is_not_group_world_writable "$mode" || die "$EX_DATAERR" "$label is group/world writable: $path"
}

assert_no_extended_acl() {
  local path=$1 label=$2 acl_output
  acl_output=$(getfacl -Rcp -- "$path") || die "$EX_UNAVAILABLE" "cannot inspect ACLs for $label: $path"
  if grep -E '^(default:)?(user|group):[^:]+' <<<"$acl_output" >/dev/null; then
    die "$EX_DATAERR" "$label contains a named/default ACL: $path"
  fi
}

assert_release_tree() {
  local path=$1 expected_uid=$2 excluded_venv=$3 bad
  bad=$(find "$path" -xdev \
    \( -path "$excluded_venv" -prune \) -o \
    \( -type l -o ! -uid "$expected_uid" -o -perm /022 \) -print -quit)
  [[ -z "$bad" ]] || die "$EX_DATAERR" "release tree contains a symlink, untrusted owner, or writable entry: $bad"
  assert_no_extended_acl "$path" release
}

assert_venv_tree() {
  local path=$1 expected_uid=$2 bad link resolved link_uid resolved_uid resolved_mode
  bad=$(find "$path" -xdev ! -type l \
    \( ! -uid "$expected_uid" -o -perm /022 \) -print -quit)
  [[ -z "$bad" ]] || die "$EX_DATAERR" "venv contains an untrusted or writable entry: $bad"
  while IFS= read -r link; do
    [[ -n "$link" ]] || continue
    link_uid=$(stat -c '%u' -- "$link")
    [[ "$link_uid" == "$expected_uid" ]] || die "$EX_DATAERR" "venv symlink has an untrusted owner: $link"
    resolved=$(realpath -e -- "$link") || die "$EX_DATAERR" "venv contains a broken symlink: $link"
    if [[ "$resolved" != "$path"/* && "$resolved" != /usr/bin/* && "$resolved" != /usr/local/bin/* ]]; then
      die "$EX_DATAERR" "venv symlink escapes to an unapproved prefix: $link -> $resolved"
    fi
    resolved_uid=$(stat -c '%u' -- "$resolved")
    resolved_mode=$(stat -c '%a' -- "$resolved")
    [[ "$resolved_uid" == "$expected_uid" ]] || die "$EX_DATAERR" "venv symlink target has an untrusted owner: $resolved"
    mode_is_not_group_world_writable "$resolved_mode" || die "$EX_DATAERR" "venv symlink target is writable: $resolved"
  done < <(find "$path" -xdev -type l -print)
  assert_no_extended_acl "$path" venv
}

safe_remove_tree() {
  local path=$1 allowed_parent=$2 allowed_name=$3
  [[ -n "$path" && -e "$path" && ! -L "$path" ]] || return 0
  [[ $(dirname -- "$path") == "$allowed_parent" && $(basename -- "$path") == "$allowed_name" ]] || {
    printf 'refuse unsafe cleanup target: %s\n' "$path" >&2
    return 1
  }
  find "$path" -xdev -depth -delete
}

atomic_install_file() {
  local mode=$1 owner=$2 group=$3 source=$4 target=$5 tmp
  assert_no_symlink_chain "$(dirname -- "$target")"
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" ]] || return 1
  fi
  tmp=$(mktemp "${target}.next.XXXXXX") || return 1
  temporary_files+=("$tmp")
  if (( test_mode )); then
    cp -- "$source" "$tmp" || return 1
  else
    install -m "$mode" -o "$owner" -g "$group" -- "$source" "$tmp" || return 1
  fi
  mv -Tf -- "$tmp" "$target"
}

atomic_symlink() {
  local target=$1 link=$2 tmp
  tmp="$(dirname -- "$link")/.${link##*/}.next.$$.$RANDOM"
  [[ ! -e "$tmp" && ! -L "$tmp" ]] || return 1
  temporary_files+=("$tmp")
  if (( test_mode )) && command -v cmd.exe >/dev/null 2>&1; then
    # Git Bash cannot create native symlinks without Developer Mode. A Windows
    # directory junction has the same lstat/realpath/atomic-mv behavior needed
    # by this sandbox; production never enters this branch.
    local tmp_windows target_windows
    tmp_windows=$(cygpath -w -- "$tmp") || return 1
    target_windows=$(cygpath -w -- "$target") || return 1
    MSYS2_ARG_CONV_EXCL='*' cmd.exe /d /c mklink /J \
      "$tmp_windows" "$target_windows" >/dev/null || return 1
  else
    ln -s -- "$target" "$tmp" || return 1
  fi
  mv -Tf -- "$tmp" "$link"
}

safe_unlink_regular_or_symlink() {
  local path=$1
  if [[ -e "$path" || -L "$path" ]]; then
    [[ -f "$path" || -L "$path" ]] || return 1
    unlink -- "$path"
  fi
}

rollback_transaction() {
  local ok=1 state mode uid gid name target
  printf 'Bootstrap transaction failed; restoring previous current/config/units from %s\n' "$backup_dir" >&2

  state=$(cat -- "$backup_dir/current.state" 2>/dev/null) || state=unknown
  if [[ "$state" == absent ]]; then
    safe_unlink_regular_or_symlink "$current" || ok=0
  elif [[ "$state" == /* ]]; then
    atomic_symlink "$state" "$current" || ok=0
  else
    printf 'invalid current rollback state: %s\n' "$state" >&2
    ok=0
  fi

  read -r state mode uid gid <"$backup_dir/runtime-env.state" 2>/dev/null || state=unknown
  if [[ "$state" == absent ]]; then
    safe_unlink_regular_or_symlink "$runtime_env" || ok=0
  elif [[ "$state" == present ]]; then
    atomic_install_file "$mode" "$uid" "$gid" "$backup_dir/runtime.env" "$runtime_env" || ok=0
  else
    printf 'invalid runtime.env rollback state: %s\n' "$state" >&2
    ok=0
  fi

  for name in "${MANAGED_UNITS[@]}"; do
    target=$systemd_root/$name
    read -r state mode uid gid <"$backup_dir/unit-state/$name" 2>/dev/null || state=unknown
    if [[ "$state" == absent ]]; then
      safe_unlink_regular_or_symlink "$target" || ok=0
    elif [[ "$state" == present ]]; then
      atomic_install_file "$mode" "$uid" "$gid" "$backup_dir/units/$name" "$target" || ok=0
    else
      printf 'invalid unit rollback state: %s (%s)\n' "$name" "$state" >&2
      ok=0
    fi
  done
  systemctl daemon-reload || ok=0
  (( ok ))
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  set +e
  local rollback_failed=0 path
  if (( transaction_active && ! transaction_committed )); then
    rollback_transaction || rollback_failed=1
  fi
  if [[ -n "$candidate_link" ]]; then
    safe_unlink_regular_or_symlink "$candidate_link" >/dev/null 2>&1 || true
  fi
  for path in "${temporary_files[@]}"; do
    [[ -e "$path" || -L "$path" ]] && safe_unlink_regular_or_symlink "$path" >/dev/null 2>&1
  done
  for path in "${temporary_trees[@]}"; do
    [[ -e "$path" && ! -L "$path" ]] && find "$path" -xdev -depth -delete >/dev/null 2>&1
  done
  if (( venv_created_this_run && status != 0 )); then
    safe_remove_tree "$venv" "$release" .venv >/dev/null 2>&1 || true
  fi
  if [[ -n "$test_lock_dir" && -d "$test_lock_dir" ]]; then
    rmdir -- "$test_lock_dir" >/dev/null 2>&1 || true
  fi
  if (( rollback_failed )); then
    printf 'bootstrap CRITICAL: automatic rollback was incomplete; keep all managed units disabled and use snapshot %s\n' "$backup_dir" >&2
    exit "$EX_CANTCREAT"
  fi
  exit "$status"
}
trap on_exit EXIT INT TERM HUP

while (( $# )); do
  case "$1" in
    --release)
      (( $# >= 2 )) || die "$EX_USAGE" '--release requires a value'
      release_arg=$2; shift 2 ;;
    --wheel)
      (( $# >= 2 )) || die "$EX_USAGE" '--wheel requires a value'
      wheel_arg=$2; shift 2 ;;
    --wheel-sha256)
      (( $# >= 2 )) || die "$EX_USAGE" '--wheel-sha256 requires a value'
      wheel_sha256=$2; shift 2 ;;
    --wheelhouse)
      (( $# >= 2 )) || die "$EX_USAGE" '--wheelhouse requires a value'
      wheelhouse_arg=$2; shift 2 ;;
    --wheelhouse-manifest)
      (( $# >= 2 )) || die "$EX_USAGE" '--wheelhouse-manifest requires a value'
      wheelhouse_manifest_arg=$2; shift 2 ;;
    --wheelhouse-manifest-sha256)
      (( $# >= 2 )) || die "$EX_USAGE" '--wheelhouse-manifest-sha256 requires a value'
      wheelhouse_manifest_sha256=$2; shift 2 ;;
    --activate) activate=1; shift ;;
    --replace-runtime-env) replace_runtime_env=1; shift ;;
    --require-gpu) require_gpu=1; shift ;;
    --apply) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "$EX_USAGE" "unknown argument: $1" ;;
  esac
done

[[ -n "$release_arg" ]] || die "$EX_USAGE" '--release is required'
[[ -n "$wheel_arg" ]] || die "$EX_USAGE" '--wheel is required'
[[ -n "$wheelhouse_arg" ]] || die "$EX_USAGE" '--wheelhouse is required for deterministic offline install'
[[ -n "$wheelhouse_manifest_arg" ]] || die "$EX_USAGE" '--wheelhouse-manifest is required'
[[ "$wheel_sha256" =~ ^[a-f0-9]{64}$ ]] || die "$EX_USAGE" '--wheel-sha256 must be a lowercase SHA-256'
[[ "$wheelhouse_manifest_sha256" =~ ^[a-f0-9]{64}$ ]] || die "$EX_USAGE" '--wheelhouse-manifest-sha256 must be a lowercase SHA-256'
(( ! replace_runtime_env || activate )) || die "$EX_USAGE" '--replace-runtime-env requires --activate'
(( ! require_gpu || activate )) || die "$EX_USAGE" '--require-gpu requires --activate'

for command_name in awk basename cat chmod chown cmp cp cut date dirname env find flock \
  getent getfacl grep groupadd head id install ln mkdir mktemp mv python3 \
  readlink realpath rmdir runuser sed sha256sum sort stat systemctl \
  systemd-analyze tr unlink useradd usermod; do
  command -v "$command_name" >/dev/null 2>&1 || die "$EX_UNAVAILABLE" "missing required command: $command_name"
done

if (( test_mode )); then
  [[ -d "$root_prefix" && ! -L "$root_prefix" ]] || die "$EX_NOINPUT" "test root is missing or unsafe: $root_prefix"
  trusted_uid=$(stat -c '%u' -- "$root_prefix")
  os_release=$root_prefix/etc/os-release
else
  trusted_uid=0
  os_release=/etc/os-release
  [[ -x /usr/sbin/nologin ]] || die "$EX_UNAVAILABLE" '/usr/sbin/nologin is missing or not executable'
fi

assert_secure_directory "$opt_root" "$trusted_uid" 'Video Factory opt root'
assert_secure_directory "$releases_root" "$trusted_uid" 'release root'
for existing_root in "$config_root" "$systemd_root"; do
  if [[ -e "$existing_root" || -L "$existing_root" ]]; then
    assert_no_symlink_chain "$existing_root"
    assert_secure_directory "$existing_root" "$trusted_uid" 'root-controlled bootstrap directory'
  fi
done

[[ -r "$os_release" ]] || die "$EX_UNAVAILABLE" "os-release is missing: $os_release"
ubuntu_id=$(sed -nE 's/^ID="?([^" ]+)"?$/\1/p' "$os_release" | head -n 1)
ubuntu_version=$(sed -nE 's/^VERSION_ID="?([^" ]+)"?$/\1/p' "$os_release" | head -n 1)
[[ "$ubuntu_id" == ubuntu ]] || die "$EX_UNAVAILABLE" 'Ubuntu is required'
[[ "$ubuntu_version" == 24.04 ]] || die "$EX_UNAVAILABLE" "Ubuntu 24.04 is required; found ${ubuntu_version:-unknown}"

if (( apply && ! test_mode && EUID != 0 )); then
  die "$EX_CANTCREAT" '--apply must run as root (use sudo)'
fi

[[ "$release_arg" == /* ]] || die "$EX_USAGE" '--release must be absolute'
[[ -d "$release_arg" ]] || die "$EX_NOINPUT" "release not found: $release_arg"
[[ ! -L "$release_arg" ]] || die "$EX_DATAERR" 'release must not be a symlink'
release=$(realpath -e -- "$release_arg")
[[ $(dirname -- "$release") == "$releases_root" ]] || die "$EX_DATAERR" "release must be a direct child of $releases_root"
release_id=$(basename -- "$release")
[[ "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "$EX_DATAERR" 'release directory name is unsafe'
venv=$release/.venv
[[ ! -L "$venv" ]] || die "$EX_DATAERR" "venv must not be a symlink: $venv"

assert_no_symlink_chain "$release"
assert_release_tree "$release" "$trusted_uid" "$venv"
for required_path in \
  "$release/factory/pyproject.toml" \
  "$release/factory/tools/server_preflight.py" \
  "$release/factory/deployment/server.env.example" \
  "$release/factory/deployment/systemd"; do
  [[ -e "$required_path" ]] || die "$EX_NOINPUT" "release is incomplete: $required_path"
  resolved_required_path=$(realpath -e -- "$required_path")
  [[ "$resolved_required_path" == "$release"/* ]] || die "$EX_DATAERR" "release path escapes: $required_path"
done

for tool in backup_server_state.sh collect_server_metrics.sh render_hyperframes.sh; do
  tool_path=$release/factory/tools/$tool
  assert_secure_file "$tool_path" "$trusted_uid" 'runtime tool'
  [[ -x "$tool_path" ]] || die "$EX_DATAERR" "runtime tool is not executable in the immutable release: $tool_path"
done

[[ "$wheel_arg" == /* ]] || die "$EX_USAGE" '--wheel must be absolute'
[[ -f "$wheel_arg" ]] || die "$EX_NOINPUT" "wheel not found: $wheel_arg"
assert_no_symlink_chain "$wheel_arg"
wheel=$(realpath -e -- "$wheel_arg")
assert_secure_file "$wheel" "$trusted_uid" wheel
assert_no_extended_acl "$wheel" wheel
[[ "$wheel" == *.whl ]] || die "$EX_DATAERR" '--wheel must name a .whl file'
actual_wheel_sha256=$(sha256sum -- "$wheel" | awk '{print $1}')
[[ "$actual_wheel_sha256" == "$wheel_sha256" ]] || die "$EX_DATAERR" "wheel SHA-256 mismatch: $actual_wheel_sha256"

[[ "$wheelhouse_arg" == /* ]] || die "$EX_USAGE" '--wheelhouse must be absolute'
[[ -d "$wheelhouse_arg" && ! -L "$wheelhouse_arg" ]] || die "$EX_NOINPUT" "wheelhouse is missing or a symlink: $wheelhouse_arg"
assert_no_symlink_chain "$wheelhouse_arg"
wheelhouse=$(realpath -e -- "$wheelhouse_arg")
assert_secure_directory "$wheelhouse" "$trusted_uid" wheelhouse
[[ "$wheelhouse_manifest_arg" == /* ]] || die "$EX_USAGE" '--wheelhouse-manifest must be absolute'
assert_no_symlink_chain "$wheelhouse_manifest_arg"
wheelhouse_manifest=$(realpath -e -- "$wheelhouse_manifest_arg") || die "$EX_NOINPUT" 'wheelhouse manifest not found'
assert_secure_file "$wheelhouse_manifest" "$trusted_uid" 'wheelhouse manifest'
assert_no_extended_acl "$wheelhouse_manifest" 'wheelhouse manifest'
actual_manifest_sha256=$(sha256sum -- "$wheelhouse_manifest" | awk '{print $1}')
[[ "$actual_manifest_sha256" == "$wheelhouse_manifest_sha256" ]] || die "$EX_DATAERR" "wheelhouse manifest SHA-256 mismatch: $actual_manifest_sha256"

declare -a manifest_names=() actual_names=()
declare -A manifest_digests=()
wheel_filename_re='^[A-Za-z0-9][-A-Za-z0-9._+]*[.]whl$'
while read -r digest filename extra; do
  [[ -n "${digest:-}" ]] || continue
  [[ -z "${extra:-}" && "$digest" =~ ^[a-f0-9]{64}$ ]] || die "$EX_DATAERR" 'invalid wheelhouse manifest line'
  filename=${filename#\*}
  filename=${filename%$'\r'}
  [[ "$filename" =~ $wheel_filename_re ]] || die "$EX_DATAERR" "unsafe/non-wheel manifest entry: $filename"
  [[ -z "${manifest_digests[$filename]+x}" ]] || die "$EX_DATAERR" "duplicate wheelhouse manifest entry: $filename"
  manifest_names+=("$filename")
  manifest_digests[$filename]=$digest
done <"$wheelhouse_manifest"
(( ${#manifest_names[@]} > 0 )) || die "$EX_DATAERR" 'wheelhouse manifest is empty'
mapfile -t manifest_names < <(printf '%s\n' "${manifest_names[@]}" | sort)
unsafe_wheelhouse_entry=$(find "$wheelhouse" -mindepth 1 -maxdepth 1 ! -type f -print -quit)
[[ -z "$unsafe_wheelhouse_entry" ]] || die "$EX_DATAERR" "wheelhouse must be flat and file-only: $unsafe_wheelhouse_entry"
mapfile -t actual_names < <(find "$wheelhouse" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | sort)
[[ "${actual_names[*]}" == "${manifest_names[*]}" ]] || die "$EX_DATAERR" 'wheelhouse files do not exactly match the signed manifest'
for filename in "${actual_names[@]}"; do
  dependency=$wheelhouse/$filename
  assert_secure_file "$dependency" "$trusted_uid" 'wheelhouse dependency'
  [[ $(sha256sum -- "$dependency" | awk '{print $1}') == "${manifest_digests[$filename]}" ]] || die "$EX_DATAERR" "wheelhouse dependency SHA-256 mismatch: $filename"
done
assert_no_extended_acl "$wheelhouse" wheelhouse

unit_source=$release/factory/deployment/systemd
mapfile -t discovered_units < <(find "$unit_source" -maxdepth 1 -type f \
  \( -name 'video-factory-*.service' -o -name 'video-factory-*.timer' \) \
  -printf '%f\n' | sort)
[[ "${discovered_units[*]}" == "${MANAGED_UNITS[*]}" ]] || die "$EX_DATAERR" 'release systemd unit inventory does not equal the bootstrap allowlist'
units=()
for name in "${MANAGED_UNITS[@]}"; do
  unit=$unit_source/$name
  assert_secure_file "$unit" "$trusted_uid" 'systemd unit'
  units+=("$unit")
done

runtime_template=$release/factory/deployment/server.env.example
assert_secure_file "$runtime_template" "$trusted_uid" 'runtime template'

if [[ -e "$runtime_env" || -L "$runtime_env" ]]; then
  assert_no_symlink_chain "$runtime_env"
  assert_secure_file "$runtime_env" "$trusted_uid" runtime.env
fi
if [[ -e "$current" || -L "$current" ]]; then
  [[ -L "$current" ]] || die "$EX_DATAERR" "$current must be a symlink"
  [[ $(stat -c '%u' -- "$current") == "$trusted_uid" ]] || die "$EX_DATAERR" "$current symlink has an untrusted owner"
  previous_current=$(realpath -e -- "$current") || die "$EX_DATAERR" "$current is a broken symlink"
  [[ -d "$previous_current" && $(dirname -- "$previous_current") == "$releases_root" ]] || die "$EX_DATAERR" "$current must resolve to a direct release directory (resolved=$previous_current, expected_parent=$releases_root)"
else
  previous_current=
fi
for name in "${MANAGED_UNITS[@]}"; do
  target=$systemd_root/$name
  if [[ -e "$target" || -L "$target" ]]; then
    assert_no_symlink_chain "$target"
    assert_secure_file "$target" "$trusted_uid" 'installed systemd unit'
  fi
done

assert_systemd_quiescent() {
  local active jobs unit_files line unit state
  active=$(systemctl list-units --all --state=active,activating,reloading,deactivating \
    --plain --no-legend 'video-factory-*.service' 'video-factory-*.timer') || die "$EX_UNAVAILABLE" 'cannot query Video Factory unit state'
  [[ -z "$active" ]] || die "$EX_DATAERR" "stop all Video Factory units before bootstrap:$(printf '\n%s' "$active")"
  jobs=$(systemctl list-jobs --plain --no-legend) || die "$EX_UNAVAILABLE" 'cannot query systemd jobs'
  while IFS= read -r line; do
    [[ "$line" != *video-factory-* ]] || die "$EX_DATAERR" "wait for Video Factory systemd job to finish: $line"
  done <<<"$jobs"
  unit_files=$(systemctl list-unit-files --plain --no-legend \
    'video-factory-*.service' 'video-factory-*.timer') || die "$EX_UNAVAILABLE" 'cannot query Video Factory enablement state'
  while read -r unit state _; do
    [[ -n "${unit:-}" ]] || continue
    case "$state" in
      enabled|enabled-runtime|linked|linked-runtime|alias)
        die "$EX_DATAERR" "disable managed unit before bootstrap: $unit ($state)" ;;
    esac
  done <<<"$unit_files"
}

if (( apply )); then
  if (( test_mode )); then
    mkdir -p -- "$lock_root"
    test_lock_dir=${lock_file}.d
    mkdir -- "$test_lock_dir" 2>/dev/null || die "$EX_UNAVAILABLE" 'another bootstrap holds the test lock'
  else
    assert_no_symlink_chain "$lock_root"
    [[ -d "$lock_root" ]] || die "$EX_UNAVAILABLE" "lock directory is missing: $lock_root"
    [[ ! -L "$lock_file" ]] || die "$EX_DATAERR" "bootstrap lock must not be a symlink: $lock_file"
    exec 9>"$lock_file"
    flock -n 9 || die "$EX_UNAVAILABLE" 'another bootstrap is already running'
  fi
fi
assert_systemd_quiescent

printf 'Video Factory Ubuntu bootstrap (%s, %s)\n' \
  "$([[ $apply == 1 ]] && printf APPLY || printf DRY-RUN)" \
  "$([[ $activate == 1 ]] && printf ACTIVATE || printf STAGE)"
printf '  release: %s\n  wheel: %s\n  wheel_sha256: %s\n  wheelhouse_manifest_sha256: %s\n' \
  "$release" "$wheel" "$actual_wheel_sha256" "$actual_manifest_sha256"

if (( ! apply )); then
  printf 'No changes were made. Planned operations:\n'
  quote_command install validated users groups and non-symlink runtime directories
  quote_command python3 -m venv "$venv"
  quote_command "$venv/bin/python" -m pip --no-index --only-binary=:all: --find-links "$wheelhouse" install "${wheel}[caption-observer,visual-qc]"
  if (( activate )); then
    quote_command candidate-preflight "$release" "$runtime_env"
    quote_command transactional-install allowlisted-units runtime.env current
    quote_command post-commit-verify-and-preflight
  else
    quote_command stage-only no-current no-systemd-units
  fi
  exit 0
fi

ensure_group() {
  local name=$1
  if getent group "$name" >/dev/null; then
    printf 'present group: %s\n' "$name"
  else
    groupadd --system "$name"
  fi
}

ensure_user() {
  local name=$1 group=$2 home=$3 create_home=$4 compatible_home=${5:-}
  if getent passwd "$name" >/dev/null; then
    local entry actual_group actual_home actual_shell
    entry=$(getent passwd "$name")
    actual_group=$(id -gn "$name")
    actual_home=$(cut -d: -f6 <<<"$entry")
    actual_shell=$(cut -d: -f7 <<<"$entry")
    [[ "$actual_group" == "$group" ]] || die "$EX_DATAERR" "$name has unexpected primary group: $actual_group"
    [[ "$actual_home" == "$home" || ( -n "$compatible_home" && "$actual_home" == "$compatible_home" ) ]] || die "$EX_DATAERR" "$name has unexpected home: $actual_home"
    [[ "$actual_shell" == /usr/sbin/nologin ]] || die "$EX_DATAERR" "$name must use /usr/sbin/nologin"
  elif [[ "$create_home" == yes ]]; then
    useradd --system --gid "$group" --create-home --home-dir "$home" --shell /usr/sbin/nologin "$name"
  else
    useradd --system --gid "$group" --no-create-home --home-dir "$home" --shell /usr/sbin/nologin "$name"
  fi
}

install_directory() {
  local mode=$1 owner=$2 group=$3 path=$4
  assert_no_symlink_chain "$path"
  if [[ -e "$path" ]]; then
    [[ -d "$path" && ! -L "$path" ]] || die "$EX_DATAERR" "runtime path is not a real directory: $path"
  fi
  if (( test_mode )); then
    mkdir -p -- "$path"
  else
    install -d -m "$mode" -o "$owner" -g "$group" -- "$path"
  fi
  assert_no_symlink_chain "$path"
}

if (( test_mode )); then
  printf 'test mode: identity creation is simulated under %s\n' "$root_prefix"
  root_owner=bootstrap-test-root
  root_group=bootstrap-test-root
  service_user=video-factory-test
  service_group=video-factory-test
  backup_user=video-factory-backup-test
  backup_group=video-factory-backup-test
  service_home=$runtime_root
else
  ensure_group video-factory
  ensure_group video-factory-backup
  ensure_user video-factory video-factory /var/lib/video-factory yes
  ensure_user video-factory-backup video-factory-backup /nonexistent no /home/video-factory-backup
  if ! id -nG video-factory-backup 2>/dev/null | tr ' ' '\n' | grep -x video-factory >/dev/null; then
    usermod --append --groups video-factory video-factory-backup
  fi
  root_owner=root
  root_group=root
  service_user=video-factory
  service_group=video-factory
  backup_user=video-factory-backup
  backup_group=video-factory-backup
  service_home=/var/lib/video-factory
fi

install_directory 0750 "$root_owner" "$service_group" "$config_root"
install_directory 0755 "$root_owner" "$root_group" "$systemd_root"
install_directory 0750 "$service_user" "$service_group" "$runtime_root"
for child in agent_outputs cache codex_workspace discovery frozen_media \
  hyperframes_projects media_inputs metrics qc_cache qc_evidence queue renders \
  review_outbox scratch source_audio bgm program_audio voice_approvals voices; do
  install_directory 0750 "$service_user" "$service_group" "$runtime_root/$child"
done
install_directory 0750 "$service_user" "$service_group" "$artifact_root"
install_directory 0750 "$service_user" "$service_group" "$artifact_root/artifacts"
install_directory 0750 "$service_user" "$service_group" "$artifact_root/artifacts/dedup"
install_directory 0750 root "$service_group" "$artifact_root/artifacts/rights-evidence"
install_directory 0750 "$backup_user" "$backup_group" "$artifact_root/backups"

binding_marker=$venv/.video-factory-bootstrap-binding
if [[ -e "$venv" ]]; then
  [[ -d "$venv" && ! -L "$venv" && -x "$venv/bin/python" ]] || die "$EX_DATAERR" "existing venv is incomplete or unsafe: $venv"
  assert_venv_tree "$venv" "$trusted_uid"
  assert_secure_file "$binding_marker" "$trusted_uid" 'venv binding marker'
  mapfile -t binding_lines <"$binding_marker"
  [[ ${#binding_lines[@]} == 2 \
     && "${binding_lines[0]}" == "wheel_sha256=$actual_wheel_sha256" \
     && "${binding_lines[1]}" == "wheelhouse_manifest_sha256=$actual_manifest_sha256" ]] || die "$EX_DATAERR" 'existing venv is not bound to the supplied wheel inputs; stage a new release'
  printf 'present exact-bound venv: %s\n' "$venv"
else
  venv_created_this_run=1
  python3 -m venv "$venv"
  env -i HOME="${service_home}" USER="$service_user" LOGNAME="$service_user" \
    PATH="$bootstrap_path" LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    "$venv/bin/python" -m pip --disable-pip-version-check --no-input \
    --no-cache-dir install --no-index --only-binary=:all: \
    --find-links "$wheelhouse" "${wheel}[caption-observer,visual-qc]"
  marker_tmp=$(mktemp "$venv/.binding.next.XXXXXX")
  temporary_files+=("$marker_tmp")
  printf 'wheel_sha256=%s\nwheelhouse_manifest_sha256=%s\n' \
    "$actual_wheel_sha256" "$actual_manifest_sha256" >"$marker_tmp"
  (( test_mode )) || chmod 0644 "$marker_tmp"
  mv -Tf -- "$marker_tmp" "$binding_marker"
  assert_venv_tree "$venv" "$trusted_uid"
  venv_created_this_run=0
fi

if (( ! activate )); then
  if [[ -e "$runtime_env" ]]; then
    printf 'preserve existing runtime env byte-for-byte: %s\n' "$runtime_env"
  else
    atomic_install_file 0640 "$root_owner" "$service_group" "$runtime_template" "$runtime_env" || die "$EX_CANTCREAT" 'cannot install staged runtime.env'
  fi
  printf 'Staging complete. current and systemd units were not changed. Provision models, corpus, toolchain/auth, edit runtime.env, then rerun with --activate.\n'
  exit 0
fi

desired_runtime_env=$runtime_env
if [[ ! -e "$runtime_env" || $replace_runtime_env == 1 ]]; then
  desired_runtime_env=$runtime_template
fi

backup_parent=$config_root/bootstrap-backups
install_directory 0700 "$root_owner" "$root_group" "$backup_parent"
backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
backup_dir=$(mktemp -d "$backup_parent/${backup_stamp}.XXXXXX")
if (( ! test_mode )); then
  chmod 0700 "$backup_dir"
fi
printf 'Recovery/diagnostic snapshot: %s\n' "$backup_dir"

candidate_link=$opt_root/.candidate.$$
[[ ! -e "$candidate_link" && ! -L "$candidate_link" ]] || die "$EX_CANTCREAT" "candidate path exists: $candidate_link"
atomic_symlink "$release" "$candidate_link" || die "$EX_CANTCREAT" 'cannot create candidate release link'
candidate_env=$(mktemp "$backup_dir/candidate-runtime.XXXXXX")
temporary_files+=("$candidate_env")
sed "s|/opt/video-factory/current|$candidate_link|g" "$desired_runtime_env" >"$candidate_env"
if (( test_mode )); then
  :
else
  chown root:video-factory "$candidate_env"
  chmod 0640 "$candidate_env"
fi

verify_dir=$(mktemp -d "$backup_dir/unit-verify.XXXXXX")
temporary_trees+=("$verify_dir")
verify_units=()
for name in "${MANAGED_UNITS[@]}"; do
  sed "s|/opt/video-factory/current|$candidate_link|g" "$unit_source/$name" >"$verify_dir/$name"
  (( test_mode )) || chmod 0644 "$verify_dir/$name"
  verify_units+=("$verify_dir/$name")
done
systemd-analyze verify "${verify_units[@]}"

preflight_base=(
  runuser -u "$service_user" -- env -i
  HOME="$service_home" USER="$service_user" LOGNAME="$service_user"
  PATH="$bootstrap_path" LANG=C.UTF-8 LC_ALL=C.UTF-8
  "$candidate_link/.venv/bin/python"
  "$candidate_link/factory/tools/server_preflight.py"
  --runtime-env "$candidate_env"
)
(( require_gpu )) && preflight_base+=(--require-gpu)
printf 'Run candidate preflight before any activation/unit/config mutation:\n'
if ! "${preflight_base[@]}" >"$backup_dir/preflight-candidate.json"; then
  cat "$backup_dir/preflight-candidate.json" >&2 || true
  die "$EX_DATAERR" "candidate preflight failed; current/config/units remain unchanged; report: $backup_dir/preflight-candidate.json"
fi
cat "$backup_dir/preflight-candidate.json"

install_directory 0700 "$root_owner" "$root_group" "$backup_dir/units"
install_directory 0700 "$root_owner" "$root_group" "$backup_dir/unit-state"
if [[ -e "$runtime_env" ]]; then
  printf 'present %s %s %s\n' \
    "$(stat -c '%a' -- "$runtime_env")" \
    "$(stat -c '%u' -- "$runtime_env")" \
    "$(stat -c '%g' -- "$runtime_env")" \
    >"$backup_dir/runtime-env.state"
  atomic_install_file 0600 "$root_owner" "$root_group" "$runtime_env" "$backup_dir/runtime.env" || die "$EX_CANTCREAT" 'cannot snapshot runtime.env'
else
  printf 'absent\n' >"$backup_dir/runtime-env.state"
fi
if [[ -n "$previous_current" ]]; then
  printf '%s\n' "$previous_current" >"$backup_dir/current.state"
else
  printf 'absent\n' >"$backup_dir/current.state"
fi
for name in "${MANAGED_UNITS[@]}"; do
  target=$systemd_root/$name
  if [[ -e "$target" ]]; then
    printf 'present %s %s %s\n' \
      "$(stat -c '%a' -- "$target")" \
      "$(stat -c '%u' -- "$target")" \
      "$(stat -c '%g' -- "$target")" \
      >"$backup_dir/unit-state/$name"
    atomic_install_file 0600 "$root_owner" "$root_group" "$target" "$backup_dir/units/$name" || die "$EX_CANTCREAT" "cannot snapshot unit: $name"
  else
    printf 'absent\n' >"$backup_dir/unit-state/$name"
  fi
done

assert_systemd_quiescent
transaction_active=1
if [[ ! -e "$runtime_env" || $replace_runtime_env == 1 ]]; then
  atomic_install_file 0640 "$root_owner" "$service_group" "$runtime_template" "$runtime_env" || die "$EX_CANTCREAT" 'cannot commit runtime.env'
else
  printf 'preserve existing runtime env byte-for-byte: %s\n' "$runtime_env"
fi
for name in "${MANAGED_UNITS[@]}"; do
  atomic_install_file 0644 "$root_owner" "$root_group" "$unit_source/$name" "$systemd_root/$name" || die "$EX_CANTCREAT" "cannot commit unit: $name"
done
if [[ "$previous_current" != "$release" ]]; then
  atomic_symlink "$release" "$current" || die "$EX_CANTCREAT" 'cannot atomically activate current'
fi
systemctl daemon-reload
installed_units=()
for name in "${MANAGED_UNITS[@]}"; do installed_units+=("$systemd_root/$name"); done
systemd-analyze verify "${installed_units[@]}"

postflight=(
  runuser -u "$service_user" -- env -i
  HOME="$service_home" USER="$service_user" LOGNAME="$service_user"
  PATH="$bootstrap_path" LANG=C.UTF-8 LC_ALL=C.UTF-8
  "$release/.venv/bin/python"
  "$release/factory/tools/server_preflight.py"
  --runtime-env "$runtime_env"
)
(( require_gpu )) && postflight+=(--require-gpu)
if ! "${postflight[@]}" >"$backup_dir/preflight-committed.json"; then
  cat "$backup_dir/preflight-committed.json" >&2 || true
  die "$EX_DATAERR" "post-commit preflight failed; automatic rollback required; report: $backup_dir/preflight-committed.json"
fi
cat "$backup_dir/preflight-committed.json"
transaction_committed=1
transaction_active=0

printf 'Bootstrap committed. Managed units remain disabled. No secret, model, media, final_review, publisher, or production writer was created or started.\n'
