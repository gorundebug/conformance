"""Load one generated dependency environment at direct runner boundaries."""

from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Sequence, TextIO
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

TRANSIENT_NETWORK_MARKERS = (
    "context deadline exceeded",
    "could not resolve host",
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "couldn't connect to server",
    "failed to connect to",
    "failed to do request",
    "network is unreachable",
    "no route to host",
    "temporary failure in name resolution",
    "tls handshake timeout",
    "unexpected eof",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "status code: 429",
    "status code: 502",
    "status code: 503",
    "status code: 504",
    "status_code: 429",
    "status_code: 502",
    "status_code: 503",
    "status_code: 504",
)


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


def for_host(environment: dict[str, str]) -> dict[str, str]:
    """Translate a generated container dependency contract for host commands.

    Generated projects deliberately expose container-reachable proxy URLs,
    because their normal consumer is Docker/Compose.  A conformance runner may
    also need to execute a dependency command directly on the host.  Keep the
    same proxy route and credentials, changing only the address used to reach
    that proxy from the host.
    """
    docker_host = environment.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    proxy_host = environment.get("DEPENDENCY_PROXY_HOST", "localhost")
    return {
        name: _host_value(value, docker_host, proxy_host)
        for name, value in environment.items()
    }


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


def run_dependency_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    attempts: int = 10,
    retry_delay_seconds: float = 2.0,
    output_stream: TextIO | None = None,
    echo: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a dependency download with retries and no alternate route.

    Proxy mode must keep using the configured proxy on every attempt; direct
    mode must keep using the normal upstream route.  Retrying the unchanged
    environment preserves that contract instead of silently introducing a
    fallback when a registry or proxy has a transient failure.
    """
    if attempts < 1:
        raise ValueError("dependency command attempts must be positive")
    rendered = " ".join(command)
    for attempt in range(1, attempts + 1):
        output: list[str] = []
        tail: deque[str] = deque(maxlen=80)
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("dependency command output pipe was not created")
        for line in process.stdout:
            if echo:
                print(line, end="", flush=True)
            if output_stream is not None:
                output_stream.write(line)
                output_stream.flush()
            output.append(line)
            tail.append(line)
        return_code = process.wait()
        completed = subprocess.CompletedProcess(
            list(command), return_code, stdout="".join(output), stderr=None,
        )
        if return_code == 0:
            return completed
        if (
            attempt == attempts
            or not is_transient_network_failure(tail)
        ):
            raise subprocess.CalledProcessError(
                return_code,
                list(command),
                output=completed.stdout,
            )
        delay = min(retry_delay_seconds * attempt, 15.0)
        notice = (
            f"[dependency] transient network failure; retrying the same "
            f"command and route in {delay:g}s "
            f"({attempt}/{attempts}): {rendered}\n"
        )
        if echo:
            print(notice, end="", flush=True)
        if output_stream is not None:
            output_stream.write(notice)
            output_stream.flush()
        time.sleep(delay)

    raise AssertionError("dependency retry loop terminated unexpectedly")


def is_transient_network_failure(output: Iterable[str]) -> bool:
    """Return whether recent command output proves a retryable network fault."""
    return any(
        marker in line.lower()
        for line in output
        for marker in TRANSIENT_NETWORK_MARKERS
    )


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


def _host_value(value: str, docker_host: str, proxy_host: str) -> str:
    if value == docker_host:
        return proxy_host
    parsed = urlsplit(value)
    if parsed.scheme and parsed.hostname == docker_host:
        rendered_host = f"[{proxy_host}]" if ":" in proxy_host else proxy_host
        netloc = rendered_host
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        return urlunsplit(parsed._replace(netloc=netloc))
    return value.replace(f"://{docker_host}:", f"://{proxy_host}:")
