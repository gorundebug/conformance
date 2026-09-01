#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
profile="${EXAMPLE_PROFILE:-function-call}"
state_dir="$root/.artifacts/cold-gates"
state_file="$state_dir/$profile.tsv"
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
  profiling
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

record_status() {
  local gate="$1"
  local status="$2"
  local temporary="$state_file.tmp.$$"
  awk -F '\t' -v gate="$gate" '$1 != gate' "$state_file" > "$temporary"
  printf '%s\t%s\t%s\n' "$gate" "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$temporary"
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
  echo "==> [cold-gates:$profile] clearing all BuildKit cache"
  if ! docker builder prune --all --force; then
    record_status "$gate" FAIL
    echo "==> [cold-gates:$profile] FAIL $gate: BuildKit cleanup failed" >&2
    exit 1
  fi

  status=0
  if [ "$gate" = "dependency-manifests" ]; then
    "$make_command" --no-print-directory "$gate" || status=$?
  else
    # dependency-manifests is the first explicit gate. Marking that phony
    # prerequisite old prevents every later one-gate invocation from silently
    # running a second suite before the named gate.
    "$make_command" --no-print-directory -o dependency-manifests "$gate" || status=$?
  fi
  if [ "$status" -ne 0 ]; then
    record_status "$gate" FAIL
    echo "==> [cold-gates:$profile] FAIL $index/$total $gate (exit $status)" >&2
    echo "==> [cold-gates:$profile] resume with: bash ./quickstart.sh --profile $profile -- cold-gates-resume" >&2
    exit "$status"
  fi

  docker ps -a --format '{{.ID}}' | sort > "$current_containers"
  comm -13 "$baseline_containers" "$current_containers" > "$new_containers"
  if [ -s "$new_containers" ]; then
    record_status "$gate" FAIL
    echo "==> [cold-gates:$profile] FAIL $gate: containers were left behind" >&2
    while IFS= read -r container_id; do
      docker ps -a --filter "id=$container_id" --format '  {{.ID}} {{.Names}} {{.Status}}' >&2
    done < "$new_containers"
    exit 1
  fi

  record_status "$gate" PASS
  echo "==> [cold-gates:$profile] PASS $index/$total $gate"
done

echo "==> [cold-gates:$profile] PASS all $total gates"
