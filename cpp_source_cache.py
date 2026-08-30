"""Shared, versioned Boost C++ dependency source cache for conformance builds."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


SOURCE_CACHE_VERSION = b"servicelib-conformance-source-cache-v3\0"
BUILD_CACHE_LAYOUT_VERSION = "v2"
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


def cache_root() -> Path:
    return Path(os.environ.get(
        "CPP_SOURCE_CACHE_DIR",
        CONFORMANCE_DIR / ".cpp-source-cache",
    )).expanduser().resolve()


def docker_url(url: str) -> tuple[str, str | None]:
    """Translate a host-local proxy URL for use inside a Docker container."""
    parsed = urlsplit(url)
    docker_host = os.environ.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        rendered_host = (
            f"[{docker_host}]" if ":" in docker_host else docker_host
        )
        netloc = rendered_host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    add_host = (
        "host.docker.internal:host-gateway"
        if parsed.hostname == "host.docker.internal"
        else None
    )
    return urlunsplit(parsed), add_host


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
    return cache_root() / cache_name(framework)


def invalidate() -> None:
    """Drop prepared sources and dependent CMake volumes, retaining ccache."""
    root = cache_root()
    forbidden = {Path("/").resolve(), Path.home().resolve(), CONFORMANCE_DIR}
    if root in forbidden:
        raise RuntimeError(f"refusing to remove unsafe source-cache path: {root}")
    if root.exists():
        shutil.rmtree(root)
        print(f"[source-cache] removed {root}")
    else:
        print(f"[source-cache] no cache at {root}")

    result = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[source-cache] Docker unavailable; no CMake volumes removed")
        return
    prefixes = (
        "cppexample_cpp-cmake-build",
        "cppboostexample_cpp-cmake-build",
        "cppboostservicelib-",
        "servicelib-",
    )
    volumes = [
        name for name in result.stdout.splitlines()
        if "cpp-cmake-build" in name and name.startswith(prefixes)
    ]
    for volume in volumes:
        subprocess.run(["docker", "volume", "rm", volume], check=True)
        print(f"[source-cache] removed CMake volume {volume}")
    print("[source-cache] ccache and Nexus data preserved")


def source_dir(framework: Path) -> Path:
    return cache_dir(framework) / "_deps"


def source_mount(framework: Path) -> str:
    return f"{source_dir(framework)}:{CONTAINER_SOURCE_DIR}:ro"


def build_volume_name(framework: Path, project: str = "cppboostexample") -> str:
    return (
        f"{project}_cpp-cmake-build-{BUILD_CACHE_LAYOUT_VERSION}-"
        f"{cache_name(framework)}"
    )


def build_volume_mount_args(
    framework: Path,
    project: str = "cppboostexample",
    *,
    readonly: bool = False,
) -> list[str]:
    """Return a Docker mount that never imports a host build directory.

    The framework source is bind-mounted at ``/workspace``.  Without
    ``volume-nocopy``, Docker may initialize a newly-created nested build
    volume from ``/workspace/build`` and copy host CMake caches into it.  Those
    caches contain absolute host paths and cannot be reused in the container.
    """
    return volume_mount_args(
        build_volume_name(framework, project), readonly=readonly
    )


def volume_mount_args(name: str, *, readonly: bool = False) -> list[str]:
    """Render a named build-volume mount without Docker's copy-up."""
    spec = f"type=volume,source={name},target=/workspace/build,volume-nocopy"
    if readonly:
        spec += ",readonly"
    return ["--mount", spec]


def configure_environment(
    environment: dict[str, str], framework: Path,
    project: str = "cppboostexample",
) -> Path:
    sources = ensure(framework)
    environment["CPPBOOST_SOURCE_CACHE_DIR"] = str(sources)
    environment["CPPBOOST_BUILD_VOLUME"] = build_volume_name(
        framework, project
    )
    # Runtime-image builds consume gRPC and asio-grpc through BuildKit named
    # contexts rather than the development container's bind mount. Point both
    # contexts at the same validated, versioned source cache so a stale CMake
    # cache cannot fall back to the example repository itself.
    environment["GRPC_SOURCE_CONTEXT"] = str(sources / "grpc-src")
    environment["ASIO_GRPC_SOURCE_CONTEXT"] = str(
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
        "-DCPPBOOSTSERVICELIB_BUILD_TESTS=ON "
        "-DCPPBOOSTSERVICELIB_GITHUB_ARCHIVE_BASE="
        '"${DEPENDENCY_GITHUB_RAW_URL:-https://github.com}"'
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
    github_raw_url = os.environ.get("DEPENDENCY_GITHUB_RAW_URL")
    if github_raw_url:
        github_raw_url, add_host = docker_url(github_raw_url)
        if add_host is not None:
            # Docker Desktop resolves this name natively; host-gateway makes
            # the same command work with Docker Engine on Linux.
            command.extend(["--add-host", add_host])
        command.extend(["--env", f"DEPENDENCY_GITHUB_RAW_URL={github_raw_url}"])
    command.extend([
        "cppboostservicelib-build:local", "/bin/bash", "-lc", run,
    ])
    return command


def ensure(framework: Path) -> Path:
    """Prepare and validate the shared sources, downloading only if incomplete."""
    sources = source_dir(framework)
    missing = [name for name in REQUIRED_SOURCE_DIRECTORIES if not (sources / name).is_dir()]
    if missing:
        build_proxy_args: list[str] = []
        add_host: str | None = None
        for name in (
            "DEPENDENCY_APT_UBUNTU_ARCHIVE_URL",
            "DEPENDENCY_APT_UBUNTU_SECURITY_URL",
            "DEPENDENCY_APT_UBUNTU_PORTS_URL",
        ):
            if value := os.environ.get(name):
                value, candidate = docker_url(value)
                add_host = add_host or candidate
                build_proxy_args.extend(["--build-arg", f"{name}={value}"])
        if add_host is not None:
            build_proxy_args[0:0] = ["--add-host", add_host]
        subprocess.run(
            [
                "docker", "build", *build_proxy_args,
                "-f", "Dockerfile.cmake", "-t",
                "cppboostservicelib-build:local", ".",
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


if __name__ == "__main__":
    if sys.argv[1:] != ["invalidate"]:
        raise SystemExit("usage: cpp_source_cache.py invalidate")
    invalidate()
