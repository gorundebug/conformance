"""Shared, versioned Boost C++ dependency source cache for conformance builds."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
from pathlib import Path


SOURCE_CACHE_VERSION = b"servicelib-conformance-source-cache-v2\0"
CONFORMANCE_DIR = Path(__file__).resolve().parent
CONTAINER_SOURCE_DIR = "/servicegen-cpp-source-cache"
SOURCE_DIRECTORIES = {
    "BOOST": "boost-src",
    "YAML-CPP": "yaml-cpp-src",
    "GOOGLETEST": "googletest-src",
    "SERVICEGEN_GOOGLETEST": "googletest-src",
    "GRPC": "grpc-src",
    "ASIO-GRPC": "asio-grpc-src",
    "LIBRDKAFKA": "librdkafka-src",
    "OPENTELEMETRY-CPP": "opentelemetry-cpp-src",
}
REQUIRED_SOURCE_DIRECTORIES = tuple(dict.fromkeys(SOURCE_DIRECTORIES.values())) + (
    "opentelemetry-cpp-build/opentelemetry-proto-prefix/src/opentelemetry-proto",
)


def cache_name(framework: Path) -> str:
    digest = hashlib.sha256()
    digest.update(SOURCE_CACHE_VERSION)
    for relative in (
        Path("cmake/DependencyVersions.cmake"),
        Path("cmake/Dependencies.cmake"),
    ):
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update((framework / relative).read_bytes())
        digest.update(b"\0")
    architecture = re.sub(r"[^A-Za-z0-9_.-]", "-", platform.machine())
    return f"conformance-source-cache-{architecture}-{digest.hexdigest()[:12]}"


def cache_dir(framework: Path) -> Path:
    root = Path(os.environ.get(
        "CONFORMANCE_CPP_SOURCE_CACHE_DIR",
        CONFORMANCE_DIR / ".cpp-source-cache",
    )).expanduser().resolve()
    return root / cache_name(framework)


def source_dir(framework: Path) -> Path:
    return cache_dir(framework) / "_deps"


def source_mount(framework: Path) -> str:
    return f"{source_dir(framework)}:{CONTAINER_SOURCE_DIR}:ro"


def build_volume_name(framework: Path, project: str = "cppboostexample") -> str:
    return f"{project}_cpp-cmake-build-{cache_name(framework)}"


def configure_environment(
    environment: dict[str, str], framework: Path,
    project: str = "cppboostexample",
) -> Path:
    sources = ensure(framework)
    environment["SERVICEGEN_CPPBOOST_SOURCE_CACHE_DIR"] = str(sources)
    environment["SERVICEGEN_CPPBOOST_BUILD_VOLUME"] = build_volume_name(
        framework, project
    )
    # Runtime-image builds consume gRPC and asio-grpc through BuildKit named
    # contexts rather than the development container's bind mount. Point both
    # contexts at the same validated, versioned source cache so a stale CMake
    # cache cannot fall back to the example repository itself.
    environment["SERVICEGEN_GRPC_SOURCE_CONTEXT"] = str(sources / "grpc-src")
    environment["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"] = str(
        sources / "asio-grpc-src"
    )
    return sources


def cmake_cache_contents(
    container_cache: str = CONTAINER_SOURCE_DIR,
) -> str:
    lines = [
        'set(FETCHCONTENT_UPDATES_DISCONNECTED ON CACHE BOOL "" FORCE)',
    ]
    lines.extend(
        f'set(FETCHCONTENT_SOURCE_DIR_{name} "{container_cache}/{directory}" '
        'CACHE PATH "" FORCE)'
        for name, directory in SOURCE_DIRECTORIES.items()
    )
    lines.append(
        f'set(OTELCPP_PROTO_PATH "{container_cache}/opentelemetry-cpp-build/'
        'opentelemetry-proto-prefix/src/opentelemetry-proto" '
        'CACHE PATH "" FORCE)'
    )
    return "\n".join(lines) + "\n"


def build_dir(framework: Path) -> str:
    """Compatibility alias for callers that display the versioned cache key."""
    return cache_name(framework)


def cmake_args(framework: Path, container_cache: str = CONTAINER_SOURCE_DIR) -> str:
    cache = container_cache
    otel_proto = (
        f"{cache}/opentelemetry-cpp-build/"
        "opentelemetry-proto-prefix/src/opentelemetry-proto"
    )
    arguments = [
        "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
        f"-DOTELCPP_PROTO_PATH={otel_proto}",
    ]
    arguments.extend(
        f"-DFETCHCONTENT_SOURCE_DIR_{name}={cache}/{directory}"
        for name, directory in SOURCE_DIRECTORIES.items()
    )
    return " ".join(arguments) + " "


def prepare_command(framework: Path) -> list[str]:
    host_cache = cache_dir(framework)
    host_cache.mkdir(parents=True, exist_ok=True)
    container_cache = "/servicegen-cpp-source-cache-build"
    ready = f"{container_cache}/.ready"
    configure = (
        f"cmake -S . -B {container_cache} -G Ninja "
        "-DCMAKE_BUILD_TYPE=Release "
        "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON "
        "-DCPPBOOSTSERVICELIB_DEPENDENCY_MODE=FETCH "
        "-DCPPBOOSTSERVICELIB_ENABLE_GRPC=ON "
        "-DCPPBOOSTSERVICELIB_ENABLE_KAFKA=ON "
        "-DCPPBOOSTSERVICELIB_ENABLE_OTEL=ON "
        "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON"
    )
    # Every source, including opentelemetry-proto, is populated during
    # configure from immutable archives. No dependency build target is needed
    # merely to complete the source cache.
    populate = configure
    locked = (
        f"if [ -f {ready} ]; then "
        f"echo \"[source-cache] reuse {cache_name(framework)}\"; "
        "else attempt=1; max_attempts=6; "
        f"until {populate}; do "
        "if [ \"$attempt\" -ge \"$max_attempts\" ]; then "
        "echo \"[source-cache] failed after $attempt attempts\" >&2; exit 1; "
        "fi; delay=$((attempt * 10)); "
        "echo \"[source-cache] attempt $attempt failed; retrying in ${delay}s\" >&2; "
        "sleep \"$delay\"; attempt=$((attempt + 1)); done; "
        f"touch {ready}; fi"
    )
    run = (
        f"flock {container_cache}/.lock -c '{locked}'"
    )
    command = [
        "docker", "run", "--rm", "-v", f"{framework}:/workspace",
        "-v", f"{host_cache}:{container_cache}", "-w",
        "/workspace",
    ]
    github_raw_url = os.environ.get("SERVICEGEN_GITHUB_RAW_URL")
    if github_raw_url:
        command.extend(["--add-host", "host.docker.internal:host-gateway"])
        command.extend(["--env", f"SERVICEGEN_GITHUB_RAW_URL={github_raw_url}"])
    command.extend([
        "cppboostservicelib-build:latest", "/bin/bash", "-lc", run,
    ])
    return command


def ensure(framework: Path) -> Path:
    """Prepare and validate the shared sources, downloading only if incomplete."""
    sources = source_dir(framework)
    missing = [name for name in REQUIRED_SOURCE_DIRECTORIES if not (sources / name).is_dir()]
    if missing:
        subprocess.run(
            [
                "docker", "build", "-f", "Dockerfile.cmake", "-t",
                "cppboostservicelib-build:latest", ".",
            ],
            cwd=framework,
            check=True,
        )
        subprocess.run(prepare_command(framework), cwd=framework, check=True)
        missing = [
            name for name in REQUIRED_SOURCE_DIRECTORIES
            if not (sources / name).is_dir()
        ]
    if missing:
        raise RuntimeError(
            "shared Boost source cache is incomplete: " + ", ".join(missing)
        )
    # Consumers that invoke the framework's own Docker test script need a
    # portable way to select these already-populated sources.  Keep the cache
    # file beside _deps so the read-only mount is sufficient and a new build
    # volume never falls back to downloading GoogleTest/gRPC again.
    (sources / "conformance-cache.cmake").write_text(
        cmake_cache_contents()
    )
    print(f"[source-cache] reuse {cache_name(framework)}", flush=True)
    return sources
