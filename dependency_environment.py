"""Load one generated dependency environment at direct runner boundaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DOCKER_PASSTHROUGH_NAMES = frozenset({
    "CARGO_REGISTRIES_CRATES_IO_INDEX",
    "COREPACK_NPM_REGISTRY",
    "GOPROXY",
    "GOSUMDB",
    "NPM_CONFIG_REGISTRY",
    "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "UV_INDEX_URL",
})


def from_framework(framework: Path) -> dict[str, str]:
    """Return the framework-owned environment for a direct subprocess.

    Generated Make targets already load this contract themselves.  Conformance
    runners that invoke Docker directly must do the same so proxy and direct
    modes have exactly one source of dependency URLs and pinned Git contexts.
    """
    script = framework / "scripts" / "dependency-proxy-env.sh"
    if not script.is_file():
        raise RuntimeError(f"dependency environment is missing: {script}")
    return _read_environment([
        "/bin/bash",
        "-c",
        'source "$1"; env -0',
        "framework-dependency-environment",
        str(script),
    ])


def from_project(project: Path) -> dict[str, str]:
    """Return the generated project's canonical dependency environment."""
    script = project / "scripts" / "docker-dependency-proxy.generated.sh"
    if not script.is_file():
        raise RuntimeError(f"project dependency environment is missing: {script}")
    return _read_environment([str(script), "--print-environment"])


def _read_environment(command: list[str]) -> dict[str, str]:
    process = subprocess.run(
        command,
        check=True,
        capture_output=True,
        env=os.environ,
    )
    environment: dict[str, str] = {}
    for entry in process.stdout.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        name, value = entry.split(b"=", 1)
        environment[name.decode()] = value.decode()
    return environment


def docker_arguments(framework: Path) -> list[str]:
    """Pass the framework dependency contract through a direct docker run.

    Docker Compose and generated Make targets already do this themselves. A
    conformance runner that invokes ``docker run`` directly must translate
    host-local proxy URLs and pass them into the container explicitly.
    """
    environment = from_framework(framework)
    if not environment.get("DEPENDENCY_PROXY_DIR"):
        return []

    proxy_host = environment.get("DEPENDENCY_PROXY_HOST", "localhost")
    docker_host = environment.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    selected: dict[str, str] = {}
    for name, value in environment.items():
        is_dependency_url = name.startswith("DEPENDENCY_") and name.endswith("_URL")
        is_git_config = name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        )
        is_download_mirror = name.startswith("npm_config_") and name.endswith(
            "_binary_host_mirror"
        )
        if not (
            is_dependency_url
            or is_git_config
            or is_download_mirror
            or name in DOCKER_PASSTHROUGH_NAMES
        ):
            continue
        selected[name] = _docker_value(value, proxy_host, docker_host)

    arguments = ["--add-host", "host.docker.internal:host-gateway"]
    for name, value in sorted(selected.items()):
        arguments.extend(["--env", f"{name}={value}"])
    return arguments


def _docker_value(value: str, proxy_host: str, docker_host: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname in {proxy_host, "localhost", "127.0.0.1", "::1"}:
        rendered_host = f"[{docker_host}]" if ":" in docker_host else docker_host
        netloc = rendered_host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return urlunsplit(parsed._replace(netloc=netloc))
    return value.replace(f"://{proxy_host}:", f"://{docker_host}:")
