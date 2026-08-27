#!/usr/bin/env bash
set -euo pipefail

# Clones the sibling repos this conformance suite needs (if missing) and runs
# it with sensible defaults. Intended entry point for someone who just cloned
# `conformance` and wants to verify that Go/C++/Python/Rust/TypeScript are semantically
# equivalent (tracing spans and metrics) without first learning the
# multi-repo layout.
#
# Usage:
#   ./quickstart.sh                # clone what's missing, then run `make all`
#   ./quickstart.sh --clone-only   # only clone/update sibling repos, don't run
#   ./quickstart.sh --dependencies-dir /path/to/repos
#   ./quickstart.sh --skip-git-mirror-refresh       # trust cached Git revisions
#   ./quickstart.sh --profile current                # full suite with pools
#   ./quickstart.sh --profile current -- tracing     # tracing with pools
#
# Anything after the flags is forwarded to the run, e.g.:
#   ./quickstart.sh -- tracing                       # only the tracing suite
#   ./quickstart.sh -- metrics                       # only the metrics suite
#   ./quickstart.sh -- temporal                      # Temporal Schedule/Task Queue/DurableCall
#   ./quickstart.sh -- scenarios                     # framework/native scenarios
#   ./quickstart.sh -- call-semantics                # FunctionCall graph (default)
#   ./quickstart.sh --profile current -- call-semantics  # pooled graph
#   ./quickstart.sh -- standalone-components         # isolated local builds
#   ./quickstart.sh -- kubernetes                    # Helm + local k3s rollout
#   ./quickstart.sh -- benchmarks                    # full 12-variant benchmark
#   ./quickstart.sh -- profiling-all                 # CPU profiles for all variants
#   ./quickstart.sh -- fast                          # fast development gate
#   ./quickstart.sh -- integration                   # runtime/integration gate
#   ./quickstart.sh -- resume                        # retry failed/missing suites
#
# For diagnosing a single language, skip this script and call the runners
# directly, e.g. `python3 tracing/run.py --language cpp`.

ORG="https://github.com/gorundebug"
CONFORMANCE_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DEPENDENCIES_DIR="$CONFORMANCE_ROOT/.dependencies"
MANAGED_DEPENDENCIES=1
EXAMPLE_PROFILE="function-call"

REPOS=(goexample gonativeexample cppexample cppnativeexample cppboostexample cppboostnativeexample pyexample pynativeexample rustexample rustnativeexample tsexample tsnativeexample servicegen servicelib cppservicelib cppboostservicelib pyservicelib rustservicelib tsservicelib)

clone_only=0
refresh_git_mirror=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --clone-only)
      clone_only=1
      shift
      ;;
    --skip-git-mirror-refresh)
      refresh_git_mirror=0
      shift
      ;;
    --dependencies-dir)
      if [ "$#" -lt 2 ]; then
        echo "--dependencies-dir requires a path" >&2
        exit 2
      fi
      DEPENDENCIES_DIR="$2"
      MANAGED_DEPENDENCIES=0
      shift 2
      ;;
    --dependencies-dir=*)
      DEPENDENCIES_DIR="${1#*=}"
      MANAGED_DEPENDENCIES=0
      shift
      ;;
    --profile)
      if [ "$#" -lt 2 ]; then
        echo "--profile requires function-call or current" >&2
        exit 2
      fi
      EXAMPLE_PROFILE="$2"
      shift 2
      ;;
    --profile=*)
      EXAMPLE_PROFILE="${1#*=}"
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

case "$EXAMPLE_PROFILE" in
  function-call|current) ;;
  *)
    echo "Unsupported profile '$EXAMPLE_PROFILE'; expected function-call or current" >&2
    exit 2
    ;;
esac

mkdir -p "$DEPENDENCIES_DIR"
DEPENDENCIES_DIR="$(CDPATH= cd -- "$DEPENDENCIES_DIR" && pwd)"
export CONFORMANCE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR"
# Framework examples may later switch to a disposable generated workspace.
# Performance baselines must stay in one persistent, tag-pinned cache shared
# by benchmark and profiling runs.
export PERFORMANCE_NATIVE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR/performance-native"

echo "==> Checking prerequisites"
missing=0
for tool in git docker go python3 curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "  missing: $tool" >&2
    missing=1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "  missing: docker compose plugin (needs Docker Desktop or the compose-plugin package)" >&2
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  echo "Install the missing tools above and re-run." >&2
  exit 1
fi
echo "  git, docker, docker compose, go, python3, curl: OK"

# Managed checkouts are the first network operation performed by quickstart.
# Configure the shared Git mirror before fetch/clone; the complete Nexus
# environment is loaded below after goexample (which owns the generated proxy
# launcher) is available. Git keeps printing the original GitHub/GitLab URL,
# but url.*.insteadOf routes the transport through this mirror.
if [ -n "${DEPENDENCY_PROXY_DIR:-}" ]; then
  proxy_host="${DEPENDENCY_PROXY_HOST:-localhost}"
  git_mirror_port="${DEPENDENCY_GIT_MIRROR_PORT:-18084}"
  bootstrap_git_mirror="http://$proxy_host:$git_mirror_port/cgi-bin/git"
  export GIT_CONFIG_COUNT=2
  export GIT_CONFIG_KEY_0="url.$bootstrap_git_mirror/github.com/.insteadOf"
  export GIT_CONFIG_VALUE_0=https://github.com/
  export GIT_CONFIG_KEY_1="url.$bootstrap_git_mirror/gitlab.com/.insteadOf"
  export GIT_CONFIG_VALUE_1=https://gitlab.com/
  echo "==> Routing managed Git checkouts through $bootstrap_git_mirror"
  if [ "$refresh_git_mirror" -eq 1 ]; then
    echo "==> Refreshing every cached Git mirror before resolving revisions"
    curl --fail-with-body --show-error --silent --request POST \
      "$bootstrap_git_mirror/__servicegen_refresh"
  else
    echo "==> Trusting cached Git mirror revisions (--skip-git-mirror-refresh)"
  fi
fi

echo "==> Preparing repositories in $DEPENDENCIES_DIR"
for repo in "${REPOS[@]}"; do
  dir="$DEPENDENCIES_DIR/$repo"
  if [ -d "$dir/.git" ]; then
    if [ "$MANAGED_DEPENDENCIES" -eq 1 ]; then
      echo "  $repo: updating managed checkout"
      "$CONFORMANCE_ROOT/scripts/update-managed-checkout.sh" "$dir"
    else
      echo "  $repo: external checkout, leaving unchanged"
    fi
    continue
  fi
  echo "  cloning $repo"
  git clone --depth 1 "$ORG/$repo.git" "$dir"
done

PROXY_BIN_DIR=""
if [ -n "${DEPENDENCY_PROXY_DIR:-}" ]; then
  proxy_script="$DEPENDENCIES_DIR/goexample/scripts/dependency-cache.generated.sh"
  if [ ! -x "$proxy_script" ]; then
    echo "Shared dependency proxy requested, but $proxy_script is missing" >&2
    exit 1
  fi
  export DEPENDENCY_PROXY_CLIENT_HOST="${DEPENDENCY_PROXY_HOST:-localhost}"
  eval "$("$proxy_script" env)"
  proxy_resolver="$DEPENDENCIES_DIR/cppexample/scripts/dependency-proxy-env.generated.sh"
  if [ ! -f "$proxy_resolver" ]; then
    echo "Shared dependency proxy requested, but $proxy_resolver is missing" >&2
    exit 1
  fi
  source "$proxy_resolver"
  export SERVICEGEN_REAL_DOCKER="$(command -v docker)"
  # Keep the wrapper outside .artifacts. A profile switch deliberately clears
  # that directory before the suite starts; placing the wrapper there made
  # PATH silently fall back to the real Docker CLI and leaked host-side
  # localhost proxy URLs into container builds.
  PROXY_BIN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/servicelib-proxy-bin.XXXXXX")"
  ln -s "$DEPENDENCIES_DIR/cppexample/scripts/docker-dependency-proxy.generated.sh" "$PROXY_BIN_DIR/docker"
  export PATH="$PROXY_BIN_DIR:$PATH"
  echo "==> Using shared dependency proxy (host: $DEPENDENCY_PROXY_CLIENT_HOST, containers: ${DEPENDENCY_PROXY_DOCKER_HOST:-host.docker.internal})"
fi

# goexample/cppexample/pyexample each split their service/module code into
# further separate repos (orderservice, inventoryservice, order_service_api,
# inventory_service_api, model), restored via their own clone.generated.sh.
# Rust keeps the equivalent code force-added inside rustexample itself, so it
# needs no extra step.
echo "==> Restoring each example's own service/module repos"
for example in goexample cppexample cppboostexample pyexample tsexample; do
  script="$DEPENDENCIES_DIR/$example/clone.generated.sh"
  if [ -f "$script" ]; then
    echo "  $example"
    (cd "$DEPENDENCIES_DIR/$example" && bash clone.generated.sh)
  fi
done

if [ "$clone_only" -eq 1 ]; then
  echo "==> --clone-only requested, not running the conformance suite"
  exit 0
fi

SOURCE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR"
PROFILE_TEMP_DIR=""
LOCK_DIR=""
LOCK_ACQUIRED=0
cleanup_run() {
  if [ "$LOCK_ACQUIRED" -eq 1 ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  if [ -n "$PROFILE_TEMP_DIR" ]; then
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
      "$PROFILE_TEMP_DIR"
  fi
  if [ -n "$PROXY_BIN_DIR" ]; then
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
      "$PROXY_BIN_DIR"
  fi
}
trap cleanup_run EXIT INT TERM

PROFILE_MARKER="$CONFORMANCE_ROOT/.artifacts/example-profile.txt"
PREVIOUS_PROFILE="$(cat "$PROFILE_MARKER" 2>/dev/null || true)"
if [ -d "$CONFORMANCE_ROOT/.artifacts" ] && [ "$PREVIOUS_PROFILE" != "$EXAMPLE_PROFILE" ]; then
  DISPLAY_PREVIOUS_PROFILE="${PREVIOUS_PROFILE:-unknown}"
  echo "==> Profile changed ($DISPLAY_PREVIOUS_PROFILE -> $EXAMPLE_PROFILE); clearing incompatible suite artifacts"
  for artifact_dir in \
    "$CONFORMANCE_ROOT/.artifacts" \
    "$CONFORMANCE_ROOT/benchmarks/examples/.artifacts" \
    "$CONFORMANCE_ROOT/profiling/examples/.artifacts"; do
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' \
      "$artifact_dir"
  done

  # The canonical userver C++ example deliberately keeps its CMake build tree
  # in an external named volume. A generated profile workspace has newer source timestamps
  # than the checked-out function-call example, so reusing that build tree
  # after a profile switch can make CMake retain binaries from the previous
  # graph. Source caches and compiler caches remain compatible; remove only
  # the profile-dependent build trees.
  while IFS= read -r build_volume; do
    case "$build_volume" in
      cppexample_cpp-cmake-build)
        echo "  removing incompatible C++ build volume: $build_volume"
        docker volume rm "$build_volume" >/dev/null
        ;;
    esac
  done < <(docker volume ls --format '{{.Name}}')
fi
mkdir -p "$CONFORMANCE_ROOT/.artifacts"
printf '%s\n' "$EXAMPLE_PROFILE" > "$PROFILE_MARKER"
export CONFORMANCE_EXAMPLE_PROFILE="$EXAMPLE_PROFILE"
export SERVICEGEN_EXAMPLE_PROFILE="$EXAMPLE_PROFILE"

if [ "$EXAMPLE_PROFILE" = "current" ]; then
  PROFILE_TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/servicelib-conformance-current.XXXXXX")"
  PROFILE_WORKSPACE="$PROFILE_TEMP_DIR/workspace"
  echo "==> Preparing disposable '$EXAMPLE_PROFILE' generated examples"
  python3 "$CONFORMANCE_ROOT/profile_workspace.py" \
    --source-root "$SOURCE_DEPENDENCIES_DIR" \
    --workspace "$PROFILE_WORKSPACE" \
    --profile "$EXAMPLE_PROFILE"
  DEPENDENCIES_DIR="$PROFILE_WORKSPACE"
  export CONFORMANCE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR"
fi

LOCK_DIR="${TMPDIR:-/tmp}/servicelib-tooling.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    echo "Another ServiceLib benchmark/profiling/conformance run is active (pid $owner)." >&2
    echo "Run these tools sequentially." >&2
    exit 1
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "ServiceLib tooling lock is busy: $LOCK_DIR" >&2
    exit 1
  fi
fi
echo "$$" > "$LOCK_DIR/pid"
LOCK_ACQUIRED=1
export SERVICELIB_TOOLING_LOCK_HELD=1

echo "==> Running conformance with example profile '$EXAMPLE_PROFILE'"
cd "$CONFORMANCE_ROOT"
if [ "$#" -eq 0 ]; then
  make all
else
  make "$@"
fi
