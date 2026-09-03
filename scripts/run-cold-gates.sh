#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
profile="${EXAMPLE_PROFILE:-function-call}"
state_dir="$root/.artifacts/cold-gates"
state_file="$state_dir/$profile.tsv"
run_id="${COLD_GATES_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
log_dir="$state_dir/$profile/runs/$run_id"
resume=0

if [ "${1:-}" = "--resume" ]; then
  resume=1
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: $0 [--resume]" >&2
  exit 2
fi

gates=(
  dependency-manifests
  tooling
  structure
  signatures
  config-core
  config-schema
  pools
  operators
  serde
  config-runtime-core
  config-runtime-go
  config-runtime-typescript
  dependencies
  standalone-components
  published-components
  transports
  kafka
  temporal
  tracing
  metrics
  dashboards-core
  logging
  scenarios
  call-semantics
  sanitizers
  generation
  kubernetes
  benchmarks
)

if [ "$resume" -eq 0 ]; then
  # quickstart has already created the disposable profile workspace. Remove
  # prior suite outcomes without deleting the profile marker or the generation
  # diagnostics that describe that workspace.
  shopt -s nullglob dotglob
  for artifact in "$root/.artifacts"/*; do
    case "$(basename "$artifact")" in
      example-profile.txt|profile-current|cold-gates) ;;
      *) rm -rf "$artifact" ;;
    esac
  done
  shopt -u nullglob dotglob
  rm -rf "$root/benchmarks/examples/.artifacts" "$root/profiling/examples/.artifacts"
  mkdir -p "$state_dir"
  rm -f "$state_file"
fi
mkdir -p "$state_dir"
touch "$state_file"
mkdir -p "$log_dir"

run_logged() {
  local log_file="$1"
  shift

  set +e
  "$@" 2>&1 \
    | tee -a "$log_file" \
    | awk '
        /^==>/ ||
        /^\[progress\]/ ||
        /^\[[^]]+\] (START|PASS|FAIL)/ ||
        /conformance (passed|failed)/ {
          print
          fflush()
        }
      '
  local command_status="${PIPESTATUS[0]}"
  set -e
  return "$command_status"
}

proxy_log_size() {
  local path="$1"
  if [ -r "$path" ]; then
    wc -c < "$path" | tr -d '[:space:]'
  else
    printf '0\n'
  fi
}

write_proxy_configuration() {
  local log_file="$1"
  if [ -z "${DEPENDENCY_PROXY_DIR:-}" ]; then
    echo "[proxy-audit] mode=direct" >> "$log_file"
    return
  fi

  {
    echo "[proxy-audit] mode=proxy"
    printf '[proxy-audit] data_dir=%s\n' "$DEPENDENCY_PROXY_DIR"
    env | LC_ALL=C sort | awk -F= '
      $1 == "DEPENDENCY_PROXY_CLIENT_HOST" ||
      $1 == "DEPENDENCY_PROXY_DOCKER_HOST" ||
      $1 == "DEPENDENCY_GIT_MIRROR_URL" ||
      $1 == "DEPENDENCY_CONAN_REMOTE_URL" ||
      $1 == "DEPENDENCY_DOCKER_REGISTRY" ||
      $1 == "GOPROXY" ||
      $1 == "NPM_CONFIG_REGISTRY" ||
      $1 == "PIP_INDEX_URL" ||
      $1 == "UV_INDEX_URL" ||
      $1 == "CARGO_REGISTRIES_CRATES_IO_INDEX" {
        print "[proxy-audit] route " $0
      }
    '
  } | sed -E 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1***:***@#g' >> "$log_file"
}

write_proxy_log_delta() {
  local source_file="$1"
  local initial_size="$2"
  local audit_file="$3"
  local label="$4"
  local gate_log="$5"

  if [ ! -r "$source_file" ]; then
    printf '[proxy-audit] %s unavailable: %s\n' "$label" "$source_file" >> "$gate_log"
    return
  fi

  local final_size
  final_size="$(proxy_log_size "$source_file")"
  if [ "$final_size" -ge "$initial_size" ]; then
    tail -c "+$((initial_size + 1))" "$source_file" > "$audit_file"
  else
    # Nexus rotated the active log during the gate. Preserve the complete new
    # active file and make the rotation explicit instead of reporting a false
    # zero-request result.
    printf '[proxy-audit] active log rotated during gate; new active file follows\n' > "$audit_file"
    cat "$source_file" >> "$audit_file"
  fi

  local entries
  entries="$(wc -l < "$audit_file" | tr -d '[:space:]')"
  printf '[proxy-audit] %s entries=%s file=%s\n' \
    "$label" "$entries" "$audit_file" >> "$gate_log"
}

record_status() {
  local gate="$1"
  local status="$2"
  local log_file="${3:-}"
  local temporary="$state_file.tmp.$$"
  awk -F '\t' -v gate="$gate" '$1 != gate' "$state_file" > "$temporary"
  printf '%s\t%s\t%s\t%s\n' \
    "$gate" "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$log_file" >> "$temporary"
  mv "$temporary" "$state_file"
}

passed() {
  local gate="$1"
  awk -F '\t' -v gate="$gate" '$1 == gate && $2 == "PASS" { found = 1 } END { exit !found }' "$state_file"
}

baseline_containers="$(mktemp "${TMPDIR:-/tmp}/conformance-cold-containers.XXXXXX")"
current_containers="$(mktemp "${TMPDIR:-/tmp}/conformance-cold-containers.XXXXXX")"
new_containers="$(mktemp "${TMPDIR:-/tmp}/conformance-cold-containers.XXXXXX")"
cleanup() {
  rm -f "$baseline_containers" "$current_containers" "$new_containers"
}
trap cleanup EXIT INT TERM
docker ps -a --format '{{.ID}}' | sort > "$baseline_containers"

make_command="${MAKE_COMMAND:-make}"
total="${#gates[@]}"
index=0
for gate in "${gates[@]}"; do
  index=$((index + 1))
  if [ "$resume" -eq 1 ] && passed "$gate"; then
    echo "==> [cold-gates:$profile] SKIP PASS $index/$total $gate"
    continue
  fi

  echo "==> [cold-gates:$profile] START $index/$total $gate"
  gate_log="$log_dir/$(printf '%02d' "$index")-$gate.log"
  proxy_request_audit="$log_dir/$(printf '%02d' "$index")-$gate.proxy-requests.log"
  proxy_outbound_audit="$log_dir/$(printf '%02d' "$index")-$gate.proxy-outbound.log"
  : > "$gate_log"
  nexus_request_log="${DEPENDENCY_PROXY_DIR:-}/nexus/log/request.log"
  nexus_outbound_log="${DEPENDENCY_PROXY_DIR:-}/nexus/log/outbound-request.log"
  nexus_request_offset="$(proxy_log_size "$nexus_request_log")"
  nexus_outbound_offset="$(proxy_log_size "$nexus_outbound_log")"
  write_proxy_configuration "$gate_log"
  echo "==> [cold-gates:$profile] log $gate_log"
  echo "==> [cold-gates:$profile] clearing all BuildKit cache"
  if ! run_logged "$gate_log" docker builder prune --all --force; then
    record_status "$gate" FAIL "$gate_log"
    echo "==> [cold-gates:$profile] FAIL $gate: BuildKit cleanup failed" >&2
    echo "==> [cold-gates:$profile] full log: $gate_log" >&2
    tail -n 200 "$gate_log" >&2
    exit 1
  fi

  status=0
  gate_target="$gate"
  if [ "$resume" -eq 1 ] && [ "$gate" = "standalone-components" ]; then
    gate_target="standalone-components-resume"
  fi
  if [ "$gate" = "dependency-manifests" ]; then
    run_logged "$gate_log" "$make_command" --no-print-directory "$gate_target" || status=$?
  else
    # dependency-manifests is the first explicit gate. Marking that phony
    # prerequisite old prevents every later one-gate invocation from silently
    # running a second suite before the named gate.
    run_logged "$gate_log" "$make_command" --no-print-directory -o dependency-manifests "$gate_target" || status=$?
  fi
  if [ -n "${DEPENDENCY_PROXY_DIR:-}" ]; then
    write_proxy_log_delta "$nexus_request_log" "$nexus_request_offset" \
      "$proxy_request_audit" requests "$gate_log"
    write_proxy_log_delta "$nexus_outbound_log" "$nexus_outbound_offset" \
      "$proxy_outbound_audit" outbound-requests "$gate_log"
  fi
  if [ "$status" -ne 0 ]; then
    record_status "$gate" FAIL "$gate_log"
    echo "==> [cold-gates:$profile] FAIL $index/$total $gate (exit $status)" >&2
    echo "==> [cold-gates:$profile] full log: $gate_log" >&2
    tail -n 200 "$gate_log" >&2
    echo "==> [cold-gates:$profile] resume with: bash ./quickstart.sh --profile $profile -- cold-gates-resume" >&2
    exit "$status"
  fi

  docker ps -a --format '{{.ID}}' | sort > "$current_containers"
  comm -13 "$baseline_containers" "$current_containers" > "$new_containers"
  if [ -s "$new_containers" ]; then
    record_status "$gate" FAIL "$gate_log"
    echo "==> [cold-gates:$profile] FAIL $gate: containers were left behind" >&2
    while IFS= read -r container_id; do
      docker ps -a --filter "id=$container_id" --format '  {{.ID}} {{.Names}} {{.Status}}' >&2
    done < "$new_containers"
    exit 1
  fi

  record_status "$gate" PASS "$gate_log"
  echo "==> [cold-gates:$profile] PASS $index/$total $gate (log: $gate_log)"
done

echo "==> [cold-gates:$profile] PASS all $total gates"
