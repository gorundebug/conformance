#!/usr/bin/env python3
"""Generate, merge and build the canonical Boost C++ example."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_source_cache
import go_toolchain


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
SERVICEGEN = ROOT / "servicegen"
CANONICAL = ROOT / "cppboostexample"
FRAMEWORK = ROOT / "cppboostservicelib"
TYPESCRIPT_CANONICAL = ROOT / "tsexample"
TYPESCRIPT_FRAMEWORK = ROOT / "tsservicelib"
ARTIFACTS = CONFORMANCE_DIR / ".artifacts" / "generation"

CANONICAL_VARIANTS = {
    "go": ROOT / "goexample",
    "cpp": ROOT / "cppexample",
    "cppboost": ROOT / "cppboostexample",
    "python": ROOT / "pyexample",
    "rust": ROOT / "rustexample",
    "typescript": ROOT / "tsexample",
}

GENERATOR_PREFLIGHT = (
    "^(TestCodeGeneration|TestExampleLanguageMatrixGeneration|"
    "TestGeneratedGitLabRepositoryTooling|TestCanonicalExampleProfiles|"
    "TestCanonicalExampleProfileSelection|TestGeneratedEnvironmentVariableParity|"
    "TestExampleExecutableFilesAreReplaceable)$"
)
SUPPORTED_EXAMPLE_PROFILES = {"function-call", "current"}


def active_example_profile() -> str:
    profile = os.environ.get(
        "CONFORMANCE_EXAMPLE_PROFILE", "function-call"
    )
    if profile not in SUPPORTED_EXAMPLE_PROFILES:
        raise RuntimeError(f"unsupported conformance example profile: {profile}")
    return profile


def write_local_go_work(path: Path, modules: tuple[Path, ...]) -> None:
    path.write_text(
        go_toolchain.render_workspace(go_toolchain.example_version(ROOT), modules)
    )


def generator_preflight_environment(environment: dict[str, str]) -> dict[str, str]:
    clean = environment.copy()
    clean.pop("SERVICEGEN_EXAMPLE_PROFILE", None)
    return clean

# These are generator-owned or generator-published artifact families, not a
# snapshot of the whole example. Keep this list explicit so a successful C++
# compile cannot hide an accidentally missing operations artifact.
REQUIRED_ARCHIVE_SUFFIXES = (
    "/CMakeLists.txt",
    "/CMakePresets.json",
    "/Dockerfile.cmake",
    "/docker-compose.yml",
    "/docker-compose.cmake.generated.yml",
    "/docker-compose.integration.generated.yml",
    "/docker-compose.kubernetes.yml",
    "/kubernetes/registries.generated.yaml",
    "/kubernetes/redpanda-values.generated.yaml",
    "/.github/workflows/cpp-ci.generated.yml",
    "/.gitlab-ci.cpp.generated.yml",
    "/orderservice/config/config.yaml",
    "/orderservice/config/overrides.yaml",
    "/orderservice/config/config.generated.cpp",
    "/orderservice/config/config.generated.hpp",
    "/inventoryservice/config/config.yaml",
    "/inventoryservice/config/overrides.yaml",
    "/inventoryservice/config/config.generated.cpp",
    "/inventoryservice/config/config.generated.hpp",
    "/scripts/build.generated.sh",
    "/scripts/test.generated.sh",
    "/scripts/integration-test.generated.sh",
    "/scripts/package-cpp-service.generated.sh",
    "/scripts/kubernetes.generated.sh",
    "/scripts/quickstart.generated.sh",
    "/scripts/sanitizer-test.generated.sh",
    "/scripts/sanitizer-integration.generated.sh",
    "/orderservice/grafana/generate.generated.sh",
    "/inventoryservice/grafana/generate.generated.sh",
    "/grafana/provisioning/dashboards/servicelib.yml",
    "/grafana/provisioning/datasources/prometheus.yml",
    "/orderservice/helm/Chart.yaml",
    "/orderservice/helm/values.generated.yaml",
    "/orderservice/helm/values.schema.json",
    "/orderservice/helm/templates/configmap.generated.yaml",
    "/orderservice/helm/templates/extra-objects.generated.yaml",
    "/orderservice/helm/templates/service.generated.yaml",
    "/orderservice/helm/templates/workload.generated.yaml",
)

TYPESCRIPT_REQUIRED_ARCHIVE_SUFFIXES = (
    "/package.json",
    "/pnpm-workspace.yaml",
    "/tsconfig.json",
    "/Dockerfile.typescript",
    "/.github/workflows/typescript-ci.generated.yml",
    "/docker-compose.yml",
    "/docker-compose.typescript-runtime.generated.yml",
    "/docker-compose.kubernetes.yml",
    "/kubernetes/registries.generated.yaml",
    "/kubernetes/redpanda-values.generated.yaml",
    "/make.generated.mk",
    "/make.typescript.generated.mk",
    "/scripts/merge.generated.sh",
    "/scripts/merge-overwrite.txt",
    "/scripts/merge_validate.generated.py",
    "/scripts/package-typescript-service.generated.sh",
    "/scripts/kubernetes.generated.sh",
    "/orderservice/config/config.yaml",
    "/orderservice/config/overrides.yaml",
    "/orderservice/.github/workflows/ci.yml",
    "/orderservice/.github/dependabot.yml",
    "/orderservice/.gitlab-ci.yml",
    "/orderservice/scripts/pre-commit.generated.sh",
    "/orderservice/scripts/pre-push.generated.sh",
    "/orderservice/src/internal/app/service.generated.ts",
    "/order_service_api/openapi-ts.config.generated.mjs",
    "/order_service_api/src/generated/http/types.generated.ts",
    "/inventoryservice/src/internal/app/service.generated.ts",
    "/analyticsservice/src/internal/app/service.generated.ts",
    "/orderservice/helm/Chart.yaml",
    "/orderservice/helm/values.generated.yaml",
    "/orderservice/helm/values.schema.json",
    "/orderservice/helm/templates/configmap.generated.yaml",
    "/orderservice/helm/templates/extra-objects.generated.yaml",
    "/orderservice/helm/templates/service.generated.yaml",
    "/orderservice/helm/templates/workload.generated.yaml",
)

TYPESCRIPT_CANONICAL_SERVICE_MAKEFILE = "include make.generated.mk\n"


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    print("+", " ".join(command), flush=True)
    process = subprocess.Popen(
        command, cwd=cwd, env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output.append(line)
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            f"command failed with exit code {return_code}: {' '.join(command)}"
        )
    return "".join(output)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def isolate_compose_volumes(project: Path) -> None:
    """Let a temporary merge use Compose project-scoped build volumes."""
    fixed_name_prefixes = (
        "cppboostexample_cpp-cmake-build",
        "cppboostexample_cpp-ccache",
        "${SERVICEGEN_CPPBOOST_BUILD_VOLUME:-cppboostexample_cpp-cmake-build",
    )
    for name in (
        "docker-compose.yml",
        "docker-compose.cmake.generated.yml",
        "docker-compose.integration.generated.yml",
    ):
        path = project / name
        lines = path.read_text().splitlines(keepends=True)
        path.write_text(
            "".join(
                line
                for line in lines
                if not (
                    line.startswith("    name: ")
                    and line.strip().removeprefix("name: ").startswith(
                        fixed_name_prefixes
                    )
                )
            )
        )


def boost_source_cache_build_dir() -> str:
    return cpp_source_cache.build_dir(FRAMEWORK)


def prepare_boost_source_cache() -> Path:
    source_cache = cpp_source_cache.source_dir(FRAMEWORK)
    required = (
        "boost-src", "yaml-cpp-src", "googletest-src", "grpc-src",
        "asio-grpc-src", "librdkafka-src", "opentelemetry-cpp-src",
        "opentelemetry-cpp-build/opentelemetry-proto-prefix/src/opentelemetry-proto",
    )
    run(cpp_source_cache.prepare_command(FRAMEWORK), cwd=FRAMEWORK)
    missing = [name for name in required if not (source_cache / name).is_dir()]
    if missing:
        raise RuntimeError(
            "shared Boost source cache is incomplete: " + ", ".join(missing)
        )
    return source_cache


def attach_boost_source_cache(project: Path, source_cache: Path) -> None:
    container_cache = "/servicegen-cpp-source-cache"
    cmake_cache = project / "conformance-source-cache.generated.cmake"
    cmake_cache.write_text(cpp_source_cache.cmake_cache_contents(container_cache))
    override = project / "docker-compose.source-cache.generated.yml"
    write_json(override, {
        "services": {
            "cpp-build": {
                "environment": {
                    "SERVICEGEN_CPPBOOST_SOURCE_CACHE": "1",
                },
                "volumes": [f"{source_cache}:{container_cache}:ro"],
            },
        },
    })
    compose = (
        "docker compose -f docker-compose.cmake.generated.yml "
        "-f docker-compose.source-cache.generated.yml"
    )
    for relative in (
        "scripts/build.generated.sh",
        "scripts/test.generated.sh",
        "scripts/integration-test.generated.sh",
    ):
        path = project / relative
        contents = path.read_text()
        replaced = contents.replace(
            "docker compose -f docker-compose.cmake.generated.yml", compose
        )
        if replaced == contents:
            raise RuntimeError(f"cannot attach source cache to {relative}")
        if relative != "scripts/integration-test.generated.sh":
            replaced = replaced.replace(
                'cmake --preset "$SERVICEGEN_CPP_CMAKE_PRESET"',
                'cmake --preset "$SERVICEGEN_CPP_CMAKE_PRESET" '
                '-C /workspace/conformance-source-cache.generated.cmake',
            )
        else:
            cleanup = (
                "cleanup() {\n"
                "  docker compose -f docker-compose.integration.generated.yml "
                "down --timeout 30\n"
                "}"
            )
            diagnostic_cleanup = (
                "cleanup() {\n"
                "  status=$?\n"
                "  if [ \"$status\" -ne 0 ]; then\n"
                "    docker compose -f docker-compose.integration.generated.yml "
                "ps || true\n"
                "    docker compose -f docker-compose.integration.generated.yml "
                "logs --no-color --tail 200 || true\n"
                "  fi\n"
                "  docker compose -f docker-compose.integration.generated.yml "
                "down --timeout 30\n"
                "}"
            )
            if cleanup not in replaced:
                raise RuntimeError(
                    "cannot add integration failure diagnostics to " + relative
                )
            replaced = replaced.replace(cleanup, diagnostic_cleanup)
        path.write_text(replaced)


def overwrite_paths(root: Path) -> set[str]:
    source = root / "scripts" / "merge-overwrite.txt"
    if not source.is_file():
        return set()
    return {
        line.removeprefix("./")
        for raw_line in source.read_text().splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    }


def archive_overwrite_paths(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as source:
        candidates = [
            name
            for name in source.namelist()
            if name.endswith("/scripts/merge-overwrite.txt")
            or name == "scripts/merge-overwrite.txt"
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one scripts/merge-overwrite.txt in {archive}, "
                f"found {len(candidates)}"
            )
        contents = source.read(candidates[0]).decode("utf-8")
    return {
        line.removeprefix("./")
        for raw_line in contents.splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    }


def manifest(
    root: Path,
    *,
    generated: bool,
    overwritten: set[str] | None = None,
) -> dict[str, dict[str, int | str]]:
    overwritten = overwritten or set()
    result: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(
            part in {".git", ".servicegen", "build", "tmp"}
            for part in relative.parts
        ):
            continue
        generator_owned = "generated" in path.name or relative.as_posix() in overwritten
        if generator_owned != generated:
            continue
        result[relative.as_posix()] = {
            "sha256": digest(path),
            "mode": path.stat().st_mode & 0o777,
        }
    return result


def complete_manifest(
    root: Path, *, include: set[str] | None = None,
) -> dict[str, dict[str, int | str]]:
    """Digest the publishable project tree, including business-owned files."""
    result: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if include is not None and relative.as_posix() not in include:
            continue
        if any(
            part in {
                ".git", ".servicegen", "build", "dist", "node_modules",
                "target", "tmp", "tools", ".venv", "__pycache__",
            }
            for part in relative.parts
        ):
            continue
        result[relative.as_posix()] = {
            "sha256": digest(path),
            "mode": path.stat().st_mode & 0o777,
        }
    return result


def archive_project_paths(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as source:
        files = [name for name in source.namelist() if not name.endswith("/")]
    roots = {name.split("/", 1)[0] for name in files}
    if len(roots) != 1 or not all("/" in name for name in files):
        raise RuntimeError(f"archive has no single project root: {archive}")
    return {name.split("/", 1)[1] for name in files}


def tracked_paths(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True, capture_output=True,
    )
    return {
        value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    }


def verify_canonical_examples_are_generated(
    archive_dir: Path, temporary: Path,
) -> dict[str, object]:
    """Prove canonical examples are exact merge results, not hand-fixed trees."""
    results: dict[str, object] = {}
    for variant, canonical in CANONICAL_VARIANTS.items():
        archive = archive_dir / f"{variant}.zip"
        if not archive.is_file():
            raise RuntimeError(f"missing canonical archive for {variant}: {archive}")
        candidate = temporary / f"canonical-{variant}"
        shutil.copytree(
            canonical,
            candidate,
            ignore=shutil.ignore_patterns(
                ".git", ".servicegen", "build", "build-*", "dist",
                "node_modules", "target", "tmp", "tools", ".venv",
                "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                "conformance",
            ),
        )
        # Archive paths are generator output even when the canonical root
        # intentionally ignores independently publishable service modules.
        # Git-tracked paths catch obsolete generated files.  Ignored local
        # compiler/codegen products (for example protoc output) are excluded:
        # they are recreated by tooling and are not part of the published
        # canonical source tree.
        publishable = tracked_paths(canonical) | archive_project_paths(archive)
        # `.env` is a generated local convenience file and is deliberately
        # ignored by every canonical repository.  Its presence in a developer
        # checkout must not be required for published-tree parity.
        publishable.discard(".env")
        before = complete_manifest(candidate, include=publishable)
        merge_output = run(
            ["bash", "scripts/merge.generated.sh", "--remove-stale", str(archive)],
            cwd=candidate,
        )
        after = complete_manifest(candidate, include=publishable)
        differences = changed(before, after)
        (ARTIFACTS / f"canonical-{variant}-merge.log").write_text(merge_output)
        write_json(ARTIFACTS / f"canonical-{variant}-diff.json", {
            "changed": differences,
        })
        if differences:
            raise RuntimeError(
                f"canonical {variant} example differs from a clean generated "
                "archive merge: " + ", ".join(differences)
            )
        results[variant] = {
            "files": len(after),
            "changed_by_clean_merge": [],
        }
    return results


def changed(before: dict[str, object], after: dict[str, object]) -> list[str]:
    return sorted(
        path for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def classify_user_changes(
    before: dict[str, object], after: dict[str, object]
) -> tuple[list[str], list[str]]:
    preserved_changes = sorted(
        path for path in before
        if before.get(path) != after.get(path)
    )
    additions = sorted(path for path in after if path not in before)
    return preserved_changes, additions


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def verify_archive_artifacts(
    archive: Path,
    required_suffixes: tuple[str, ...] = REQUIRED_ARCHIVE_SUFFIXES,
    artifact_name: str = "artifact-matrix.json",
) -> dict[str, object]:
    with zipfile.ZipFile(archive) as source:
        names = sorted(name for name in source.namelist() if not name.endswith("/"))
    missing = [
        suffix for suffix in required_suffixes
        if not any(("/" + name.lstrip("/")).endswith(suffix) for name in names)
    ]
    result = {
        "archive_files": len(names),
        "required_artifacts": list(required_suffixes),
        "missing_artifacts": missing,
    }
    write_json(ARTIFACTS / artifact_name, result)
    if missing:
        raise RuntimeError(
            "generated archive is missing required artifact(s): " + ", ".join(missing)
        )
    return result


def wait_http(url: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for {url}: {last_error}")


def send_typescript_request() -> None:
    request = urllib.request.Request(
        "http://localhost:9091/v1/processorder",
        data=json.dumps({
            "customer_id": "generation-customer",
            "items": [{
                "item_id": "generation-item",
                "sku": "SKU-001",
                "quantity": 2,
                "unit_price": 799.0,
            }],
        }).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    if response.status != 200 or payload.get("status") != "CONFIRMED":
        raise RuntimeError(
            f"TypeScript generated integration returned {response.status}: {payload}"
        )


def verify_typescript_generation(
    archive: Path,
    merged: Path,
    *,
    skip_docker: bool,
) -> dict[str, object]:
    artifact_matrix = verify_archive_artifacts(
        archive,
        TYPESCRIPT_REQUIRED_ARCHIVE_SUFFIXES,
        "typescript-artifact-matrix.json",
    )
    overwritten = overwrite_paths(merged) | archive_overwrite_paths(archive)
    user_before = manifest(merged, generated=False, overwritten=overwritten)
    generated_before = manifest(merged, generated=True, overwritten=overwritten)
    merge_output = run(
        ["bash", "scripts/merge.generated.sh", str(archive)], cwd=merged
    )
    (ARTIFACTS / "typescript-merge.log").write_text(merge_output)
    user_after = manifest(merged, generated=False, overwritten=overwritten)
    generated_after = manifest(merged, generated=True, overwritten=overwritten)
    user_changes, user_additions = classify_user_changes(user_before, user_after)
    generated_changes = changed(generated_before, generated_after)
    write_json(ARTIFACTS / "typescript-merge-diff.json", {
        "user_owned_changes": user_changes,
        "user_owned_additions": user_additions,
        "generator_owned_changes": generated_changes,
    })
    if user_changes:
        raise RuntimeError(
            "TypeScript merge modified or removed existing user-owned files: "
            + ", ".join(user_changes)
        )

    for service in ("analyticsservice", "inventoryservice", "orderservice"):
        makefile = merged / service / "Makefile"
        if makefile.read_text() != TYPESCRIPT_CANONICAL_SERVICE_MAKEFILE:
            raise RuntimeError(
                f"TypeScript {service}/Makefile must delegate to "
                "make.generated.mk so a packaged service retains its standalone "
                "build and Docker targets"
            )

    checks: list[str] = []
    if not skip_docker:
        env = os.environ.copy()
        test_image = f"typescript-generation-tests:{os.getpid()}"
        env.update({
            "COMPOSE_PROJECT_NAME": f"typescript-generation-{os.getpid()}",
            "TSSERVICELIB_SOURCE_CONTEXT": str(TYPESCRIPT_FRAMEWORK),
        })
        compose = [
            "docker", "compose",
            "-f", "docker-compose.yml",
            "-f", "docker-compose.typescript-runtime.generated.yml",
        ]
        try:
            run(["make", "docker-build", "RUNTIME_IMAGE=1"], cwd=merged, env=env)
            checks.append("docker-build")
            run(
                [
                    "docker", "build",
                    "--file", "Dockerfile.typescript",
                    "--target", "development",
                    "--build-context", f"tsservicelib-source={TYPESCRIPT_FRAMEWORK}",
                    "--tag", test_image,
                    ".",
                ],
                cwd=merged,
                env=env,
            )
            run(
                [
                    "docker", "run", "--rm", "--entrypoint", "corepack",
                    test_image,
                    "pnpm", "--recursive", "test",
                ],
                cwd=merged,
                env=env,
            )
            checks.append("unit")
            run(
                [*compose, "up", "--detach", "redpanda", "analyticsservice", "inventoryservice"],
                cwd=merged,
                env=env,
            )
            wait_http("http://localhost:9092/status/data")
            wait_http("http://localhost:9093/status/data")
            run([*compose, "up", "--detach", "orderservice"], cwd=merged, env=env)
            wait_http("http://localhost:9091/status/data")
            send_typescript_request()
            checks.append("integration")
        finally:
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=merged,
                env=env,
                check=False,
            )

    return {
        "archive_sha256": digest(archive),
        "archive_bytes": archive.stat().st_size,
        "artifact_matrix": artifact_matrix,
        "user_owned_files": len(user_after),
        "user_owned_changes": [],
        "user_owned_additions": user_additions,
        "generator_owned_changes": generated_changes,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-docker", action="store_true",
        help="verify generation and merge only; not valid for the release gate",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()
    profile = active_example_profile()

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for old in ARTIFACTS.iterdir():
        if old.is_file():
            old.unlink()

    temporary = Path(tempfile.mkdtemp(prefix="cppboost-generation-merge-"))
    archive_dir = temporary / "archives"
    merged = temporary / "cppboostexample"
    typescript_merged = temporary / "tsexample"
    archive_dir.mkdir()
    shutil.copytree(
        CANONICAL, merged,
        ignore=shutil.ignore_patterns(".git", "build", "tmp"),
    )
    shutil.copytree(
        TYPESCRIPT_CANONICAL,
        typescript_merged,
        ignore=shutil.ignore_patterns(
            ".git", ".servicegen", "build", "dist", "dist-test", "node_modules", "tmp"
        ),
    )

    summary: dict[str, object] = {
        "status": "fail",
        "canonical": str(CANONICAL),
        "workspace": str(merged) if args.keep_workspace else "disposable",
        "profile": profile,
        "docker": not args.skip_docker,
    }
    started = time.monotonic()
    project = f"cppboost-generation-{os.getpid()}"
    docker_env: dict[str, str] | None = None
    down: list[str] | None = None
    docker_started = False

    try:
        local_go_work = temporary / "go.work"
        write_local_go_work(
            local_go_work, (SERVICEGEN, ROOT / "servicelib")
        )
        generation_env = os.environ.copy()
        generation_env.update({
            "SERVICEGEN_EXAMPLE_ARCHIVE_DIR": str(archive_dir),
            "SERVICEGEN_EXAMPLE_PROFILE": profile,
            "GOCACHE": os.environ.get("GOCACHE", "/tmp/servicegen-go-build"),
            "GOWORK": str(local_go_work),
        })
        preflight_env = generator_preflight_environment(generation_env)
        internal_preflight = run(
            ["go", "test", "./internal/codegenerator/cpp", "-count=1"],
            cwd=SERVICEGEN, env=preflight_env,
        )
        merge_hook_preflight = run(
            [
                "go", "test", "./internal/codegenerator", "-run",
                "^(TestMergeHooksCustomizeFilesAndRespectDryRun|"
                "TestFailingMergeFileHookDoesNotPartiallyApplyFiles)$",
                "-count=1",
            ],
            cwd=SERVICEGEN, env=preflight_env,
        )
        command_preflight = run(
            ["go", "test", "./cmd/codegenerator", "-run",
             GENERATOR_PREFLIGHT, "-count=1"],
            cwd=SERVICEGEN, env=preflight_env,
        )
        (ARTIFACTS / "preflight.log").write_text(
            internal_preflight + merge_hook_preflight + command_preflight
        )
        generation_output = run(
            ["go", "test", "./cmd/codegenerator", "-run",
             "^TestWriteCanonicalExampleArchives$", "-count=1", "-v"],
            cwd=SERVICEGEN, env=generation_env,
        )
        (ARTIFACTS / "generation.log").write_text(generation_output)

        summary["canonical_merge_parity"] = (
            verify_canonical_examples_are_generated(archive_dir, temporary)
        )

        archive = archive_dir / "cppboost.zip"
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError(f"generator did not create a non-empty {archive}")
        artifact_matrix = verify_archive_artifacts(archive)
        typescript_archive = archive_dir / "typescript.zip"
        if not typescript_archive.is_file() or typescript_archive.stat().st_size == 0:
            raise RuntimeError(
                f"generator did not create a non-empty {typescript_archive}"
            )

        overwritten = overwrite_paths(merged) | archive_overwrite_paths(archive)
        user_before = manifest(merged, generated=False, overwritten=overwritten)
        generated_before = manifest(merged, generated=True, overwritten=overwritten)
        merge_output = run(
            ["bash", "scripts/merge.generated.sh", str(archive)], cwd=merged
        )
        (ARTIFACTS / "merge.log").write_text(merge_output)

        user_after = manifest(merged, generated=False, overwritten=overwritten)
        generated_after = manifest(merged, generated=True, overwritten=overwritten)
        user_changes, user_additions = classify_user_changes(
            user_before, user_after
        )
        generated_changes = changed(generated_before, generated_after)
        write_json(ARTIFACTS / "user-owned.before.json", user_before)
        write_json(ARTIFACTS / "user-owned.after.json", user_after)
        write_json(ARTIFACTS / "generated.before.json", generated_before)
        write_json(ARTIFACTS / "generated.after.json", generated_after)
        write_json(ARTIFACTS / "merge-diff.json", {
            "user_owned_changes": user_changes,
            "user_owned_additions": user_additions,
            "generator_owned_changes": generated_changes,
        })
        if user_changes:
            raise RuntimeError(
                "merge modified or removed existing user-owned files: "
                + ", ".join(user_changes)
            )

        build_results: list[dict[str, str]] = []
        if not args.skip_docker:
            isolate_compose_volumes(merged)
            source_cache = prepare_boost_source_cache()
            attach_boost_source_cache(merged, source_cache)
            docker_env = os.environ.copy()
            docker_env.update({
                "COMPOSE_PROJECT_NAME": project,
                "SERVICELIB_SOURCE_CONTEXT": str(FRAMEWORK),
                "SERVICEGEN_FETCH_CPP_DEPENDENCIES": "OFF",
            })
            down = ["docker", "compose", "-f",
                    "docker-compose.cmake.generated.yml", "down", "--volumes",
                    "--remove-orphans"]
            run(down, cwd=merged, env=docker_env)
            docker_started = True
            unit_output = run(
                ["bash", "scripts/test.generated.sh", "docker-release"],
                cwd=merged, env=docker_env,
            )
            (ARTIFACTS / "unit-release.log").write_text(unit_output)
            if "100% tests passed" not in unit_output:
                raise RuntimeError(
                    "generated Release unit run did not report passing tests"
                )
            build_results.append({"suite": "unit", "preset": "docker-release"})

            integration_output = run(
                ["bash", "scripts/integration-test.generated.sh", "docker-release"],
                cwd=merged, env=docker_env,
            )
            (ARTIFACTS / "integration.log").write_text(integration_output)
            if "cppboost generated integration scenario: PASS" not in integration_output:
                raise RuntimeError(
                    "generated integration run did not execute the live scenario"
                )
            build_results.append({"suite": "integration", "preset": "docker-release"})
            run(down, cwd=merged, env=docker_env)
            docker_started = False

        typescript_result = verify_typescript_generation(
            typescript_archive,
            typescript_merged,
            skip_docker=args.skip_docker,
        )

        summary.update({
            "status": "pass",
            "archive_sha256": digest(archive),
            "archive_bytes": archive.stat().st_size,
            "user_owned_files": len(user_after),
            "user_owned_changes": [],
            "user_owned_additions": user_additions,
            "generator_owned_changes": generated_changes,
            "generator_preflight": "pass",
            "artifact_matrix": artifact_matrix,
            "build_results": build_results,
            "typescript": typescript_result,
            "duration_seconds": time.monotonic() - started,
        })
        write_json(ARTIFACTS / "summary.json", summary)
        print(
            f"Generation merge conformance passed: {len(user_before)} existing "
            f"user-owned files unchanged, {len(user_additions)} added"
        )
        return 0
    except Exception as error:
        summary["error"] = str(error)
        summary["duration_seconds"] = time.monotonic() - started
        write_json(ARTIFACTS / "summary.json", summary)
        raise
    finally:
        if docker_started and down is not None and docker_env is not None:
            try:
                run(down, cwd=merged, env=docker_env)
            except Exception as cleanup_error:
                print(f"warning: generated Docker cleanup failed: {cleanup_error}")
        if not args.keep_workspace:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
