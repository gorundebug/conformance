#!/usr/bin/env python3
"""Reject userver build declarations and linked runtime dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFORMANCE_DIR))
import cpp_source_cache

ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT_DIR = CONFORMANCE_DIR / ".artifacts" / "dependencies"
SOURCE_ROOTS = (
    ROOT / "cppboostservicelib",
    ROOT / "cppboostexample",
    ROOT / "cppboostnativeexample",
    ROOT / "servicegen" / "internal" / "codegenerator" / "templates" / "cppboost",
)
BUILD_FILENAMES = {"CMakeLists.txt", "Dockerfile", "Dockerfile.cmake"}
BUILD_SUFFIXES = {".cmake", ".sh", ".yml", ".yaml", ".tmpl"}
FORBIDDEN = re.compile(
    r"(?:find_package\s*\([^)]*userver|FetchContent[^\n]*userver|"
    r"target_link_libraries[^\n]*userver|userver::|libuserver|userver-core|"
    r"<userver/)",
    re.IGNORECASE,
)
BINARIES = {
    "orderservice": "/workspace/build/orderservice/example_order_service",
    "inventoryservice": "/workspace/build/inventoryservice/example_inventory_service",
}
CPPBOOST_BUILD_IMAGE = "cppboostexample-cpp-build:local"
NATIVE_BINARIES = {
    "orderservice-native": (
        "cppboostnativeexample-orderservice:local",
        "/usr/local/bin/orderservice",
    ),
    "inventoryservice-native": (
        "cppboostnativeexample-inventoryservice:local",
        "/usr/local/bin/inventoryservice",
    ),
}

CPPBOOST_SNAPSHOT = {
    "boost": "BOOST",
    "grpc": "GRPC",
    "protobuf": "PROTOBUF",
    "asio-grpc": "ASIO_GRPC",
    "yaml-cpp": "YAML_CPP",
    "librdkafka": "RDKAFKA",
    "opentelemetry-cpp": "OPENTELEMETRY",
    "googletest": "GOOGLETEST",
}
SHARED_NATIVE_CONAN_PACKAGES = (
    (
        "cppservicelib",
        "cppnativeexample",
        {
            "userver": "@gorundebug/userver#",
            "librdkafka": "@gorundebug/userver#",
        },
    ),
    (
        "cppboostservicelib",
        "cppboostnativeexample",
        {"grpc": "@gorundebug/boost#"},
    ),
)


def command(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stdout}")
    return result.stdout


def is_build_input(path: Path) -> bool:
    if any(part in {"build", ".artifacts", ".git"} for part in path.parts):
        return False
    return path.name in BUILD_FILENAMES or path.suffix in BUILD_SUFFIXES


def static_findings() -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for source_root in SOURCE_ROOTS:
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or not is_build_input(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in FORBIDDEN.finditer(text):
                findings.append(
                    {
                        "path": str(path.relative_to(ROOT)),
                        "line": text.count("\n", 0, match.start()) + 1,
                        "match": match.group(0),
                    }
                )
    return findings


def manifest_dependencies(path: Path) -> dict[str, dict[str, object]]:
    dependencies: dict[str, dict[str, object]] = {}
    current: str | None = None
    current_sequence: str | None = None
    in_dependencies = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line == "dependencies:":
            in_dependencies = True
            continue
        if in_dependencies and raw_line and not raw_line.startswith(" "):
            break
        match = re.fullmatch(r"    (\S[^:]*):", raw_line)
        if in_dependencies and match:
            current = match.group(1)
            dependencies[current] = {}
            current_sequence = None
            continue
        match = re.fullmatch(
            r"        (repository|revision|conanVersion|conanPackage):\s+(.+)", raw_line
        )
        if in_dependencies and current and match:
            current_sequence = None
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = ast.literal_eval(value)
            dependencies[current][match.group(1)] = value
            continue
        match = re.fullmatch(r"        (conanScopes):", raw_line)
        if in_dependencies and current and match:
            current_sequence = match.group(1)
            dependencies[current][current_sequence] = []
            continue
        match = re.fullmatch(r"            -\s+(.+)", raw_line)
        if in_dependencies and current and current_sequence and match:
            sequence = dependencies[current][current_sequence]
            assert isinstance(sequence, list)
            sequence.append(match.group(1))
    return dependencies


def conan_dependencies_for_scope(
    manifest: dict[str, dict[str, object]], scope: str
) -> set[str]:
    return {
        name
        for name, dependency in manifest.items()
        if scope in dependency.get("conanScopes", [])
    }


def dependency_snapshot_errors() -> list[str]:
    manifest = manifest_dependencies(
        ROOT / "servicegen" / "internal" / "codegenerator" / "dependencies.yaml"
    )
    errors: list[str] = []
    boost_snapshot = runpy.run_path(
        str(ROOT / "cppboostservicelib" / "conan" / "dependencies_generated.py")
    )
    expected_boost_dependencies = conan_dependencies_for_scope(manifest, "cppboost")
    actual_boost_dependencies = set(boost_snapshot["VERSIONS"]) - {"conan"}
    if actual_boost_dependencies != expected_boost_dependencies:
        errors.append(
            "cppboostservicelib generated Conan dependency set differs: "
            f"actual={sorted(actual_boost_dependencies)!r}, "
            f"expected={sorted(expected_boost_dependencies)!r}"
        )
    for dependency in CPPBOOST_SNAPSHOT:
        expected = manifest.get(dependency, {})
        for field, generated_map in (
            ("conanVersion", "VERSIONS"),
            ("repository", "REPOSITORIES"),
            ("revision", "REVISIONS"),
        ):
            actual = boost_snapshot[generated_map].get(dependency)
            if actual != expected.get(field):
                errors.append(
                    f"cppboostservicelib {generated_map}[{dependency!r}]={actual!r} "
                    "differs from dependencies.yaml "
                    f"{dependency}.{field}={expected.get(field)!r}"
                )

    userver_snapshot = runpy.run_path(
        str(ROOT / "cppservicelib" / "conan" / "dependencies_generated.py")
    )["VERSIONS"]
    expected_userver_dependencies = conan_dependencies_for_scope(manifest, "userver")
    actual_userver_dependencies = set(userver_snapshot) - {"conan"}
    if actual_userver_dependencies != expected_userver_dependencies:
        errors.append(
            "cppservicelib generated Conan dependency set differs: "
            f"actual={sorted(actual_userver_dependencies)!r}, "
            f"expected={sorted(expected_userver_dependencies)!r}"
        )
    for dependency in sorted(expected_userver_dependencies):
        actual = userver_snapshot.get(dependency)
        expected = manifest.get(dependency, {}).get("conanVersion")
        if actual != expected:
            errors.append(
                f"cppservicelib VERSIONS[{dependency!r}]={actual!r} differs "
                f"from dependencies.yaml {dependency}.conanVersion={expected!r}"
            )

    for relative in (
        Path("cppservicelib/docker/userver-packages-ubuntu-24.04.txt"),
        Path("servicegen/internal/codegenerator/templates/cpp/docker/userver_packages.tmpl"),
    ):
        if "librdkafka-dev" in (ROOT / relative).read_text(encoding="utf-8"):
            errors.append(f"{relative} still permits unpinned system librdkafka")
    return errors


def native_jemalloc_contract_errors() -> list[str]:
    root = ROOT / "cppboostnativeexample"
    conanfile = (root / "conanfile.py").read_text(encoding="utf-8")
    cmake = (root / "CMakeLists.txt").read_text(encoding="utf-8")
    errors: list[str] = []
    for marker in (
        'self.options["jemalloc"].shared = True',
        'self.requires(f"jemalloc/{VERSIONS[\'jemalloc\']}")',
    ):
        if marker not in conanfile:
            errors.append(f"cppboostnativeexample/conanfile.py misses {marker}")
    for target in ("${service}", "inventoryservice_cq"):
        if not re.search(
            rf"target_link_libraries\({re.escape(target)}\s+PRIVATE\s+"
            r"jemalloc::jemalloc\b",
            cmake,
        ):
            errors.append(
                f"cppboostnativeexample does not link jemalloc into {target}"
            )
    for lockfile in sorted((root / "conan" / "locks").glob("*.lock")):
        if '"jemalloc/' not in lockfile.read_text(encoding="utf-8"):
            errors.append(f"{lockfile.relative_to(ROOT)} does not lock jemalloc")
    return errors


def shared_native_conan_contract_errors() -> list[str]:
    """Require every supported profile to reuse framework recipe revisions."""
    def recipe_identity(reference: str) -> str:
        # Conan appends a local cache timestamp after ``%``.  Independently
        # exporting byte-identical recipes may therefore produce different
        # timestamps while retaining the same immutable recipe revision.
        return reference.split("%", 1)[0]

    errors: list[str] = []
    for framework_name, native_name, packages in SHARED_NATIVE_CONAN_PACKAGES:
        framework_locks = ROOT / framework_name / "conan" / "locks"
        native_locks = ROOT / native_name / "conan" / "locks"
        for framework_lock in sorted(framework_locks.glob("*.lock")):
            native_lock = native_locks / framework_lock.name
            if not native_lock.is_file():
                errors.append(
                    f"{native_lock.relative_to(ROOT)} is missing for framework lock"
                )
                continue
            framework = json.loads(framework_lock.read_text(encoding="utf-8"))
            native = json.loads(native_lock.read_text(encoding="utf-8"))
            for package, required_marker in packages.items():
                prefix = f"{package}/"
                native_found = False
                for section in ("requires", "build_requires"):
                    framework_refs = [
                        value for value in framework.get(section, [])
                        if value.startswith(prefix)
                    ]
                    native_refs = [
                        value for value in native.get(section, [])
                        if value.startswith(prefix)
                    ]
                    native_found = native_found or bool(native_refs)
                    if framework_refs and any(
                        required_marker not in value for value in framework_refs
                    ):
                        errors.append(
                            f"{framework_lock.relative_to(ROOT)} uses a non-framework "
                            f"{package} recipe in {section}: {framework_refs!r}"
                        )
                    if native_refs and [
                        recipe_identity(value) for value in native_refs
                    ] != [recipe_identity(value) for value in framework_refs]:
                        errors.append(
                            f"{native_lock.relative_to(ROOT)} does not reuse the exact "
                            f"{framework_name} {package} recipe revision in {section}: "
                            f"framework={framework_refs!r}, native={native_refs!r}"
                        )
                if not any(
                    value.startswith(prefix)
                    for section in ("requires", "build_requires")
                    for value in framework.get(section, [])
                ):
                    errors.append(
                        f"{framework_lock.relative_to(ROOT)} does not lock {package}"
                    )
                if not native_found:
                    errors.append(
                        f"{native_lock.relative_to(ROOT)} does not lock {package}"
                    )
    return errors


def linked_dependencies(skip_build: bool) -> dict[str, dict[str, object]]:
    example = ROOT / "cppboostexample"
    native_example = ROOT / "cppboostnativeexample"
    framework_env = os.environ.copy()
    framework_env.setdefault(
        "CPPBOOST_BUILD_VOLUME",
        cpp_source_cache.build_volume_name(ROOT / "cppboostservicelib"),
    )
    if not skip_build:
        framework_env["SERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "cppboostservicelib")
        source_cache = cpp_source_cache.configure_environment(
            framework_env, ROOT / "cppboostservicelib"
        )
        # Keep the subsequent Compose inspection on the exact build volume
        # selected by scripts/build.generated.sh. Environment exported inside
        # that script cannot propagate back into this Python process.
        framework_env["GRPC_SOURCE_CONTEXT"] = str(
            source_cache / "grpc-src"
        )
        framework_env["ASIO_GRPC_SOURCE_CONTEXT"] = str(
            source_cache / "asio-grpc-src"
        )
        command(
            ["./scripts/build.generated.sh", "docker-release"],
            cwd=example,
            env=framework_env,
        )
        command(
            ["docker", "compose", "build", "inventoryservice", "orderservice"],
            cwd=native_example,
            env=framework_env,
        )

    results: dict[str, dict[str, object]] = {}
    build_volume = framework_env["CPPBOOST_BUILD_VOLUME"]
    compose_source = (example / "docker-compose.cmake.generated.yml").read_text()
    if f"image: {CPPBOOST_BUILD_IMAGE}" not in compose_source:
        raise RuntimeError(
            "generated C++ build image identity differs from the dependency "
            f"inspector: expected {CPPBOOST_BUILD_IMAGE}"
        )
    for service, binary in BINARIES.items():
        output = command(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "ldd",
                *cpp_source_cache.volume_mount_args(
                    build_volume, readonly=True
                ),
                CPPBOOST_BUILD_IMAGE,
                binary,
            ],
            cwd=example,
        )
        libraries = [line.strip() for line in output.splitlines() if "=>" in line or line.lstrip().startswith("/")]
        userver = [line for line in libraries if "userver" in line.casefold()]
        results[service] = {
            "binary": binary,
            "libraries": libraries,
            "userver_libraries": userver,
            "jemalloc_libraries": [line for line in libraries if "jemalloc" in line.casefold()],
        }
    for service, (image, binary) in NATIVE_BINARIES.items():
        output = command(
            ["docker", "run", "--rm", "--entrypoint", "ldd", image, binary],
            cwd=native_example,
        )
        libraries = [line.strip() for line in output.splitlines() if "=>" in line or line.lstrip().startswith("/")]
        userver = [line for line in libraries if "userver" in line.casefold()]
        results[service] = {
            "binary": binary,
            "image": image,
            "libraries": libraries,
            "userver_libraries": userver,
            "jemalloc_libraries": [line for line in libraries if "jemalloc" in line.casefold()],
            "jemalloc_linkage": "shared-runtime",
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true", help="reuse the existing Docker Release build")
    parser.add_argument("--static-only", action="store_true", help="skip the linked-binary check")
    args = parser.parse_args()

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    findings = static_findings()
    runtime = {} if args.static_only else linked_dependencies(args.skip_build)
    linked_userver = {
        service: details["userver_libraries"]
        for service, details in runtime.items()
        if details["userver_libraries"]
    }
    missing_jemalloc = {
        service: details["binary"]
        for service, details in runtime.items()
        if not details["jemalloc_libraries"]
        and details.get("jemalloc_linkage") != "static-conan"
    }
    errors: list[str] = []
    snapshot_errors = dependency_snapshot_errors()
    native_contract_errors = native_jemalloc_contract_errors()
    shared_native_contract_errors = shared_native_conan_contract_errors()
    if findings:
        errors.append(f"{len(findings)} forbidden userver build declaration(s)")
    if linked_userver:
        errors.append(f"userver linked into: {', '.join(sorted(linked_userver))}")
    if missing_jemalloc:
        errors.append(f"jemalloc missing from: {', '.join(sorted(missing_jemalloc))}")
    errors.extend(snapshot_errors)
    errors.extend(native_contract_errors)
    errors.extend(shared_native_contract_errors)

    summary = {
        "status": "pass" if not errors else "fail",
        "static_files_scanned_roots": [str(path.relative_to(ROOT)) for path in SOURCE_ROOTS],
        "static_findings": findings,
        "dependency_snapshot_errors": snapshot_errors,
        "native_jemalloc_contract_errors": native_contract_errors,
        "shared_native_conan_contract_errors": shared_native_contract_errors,
        "runtime_checked": not args.static_only,
        "binaries": runtime,
        "errors": errors,
    }
    (ARTIFACT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
