#!/usr/bin/env python3
"""Verify the canonical TypeScript live config-reload contract beside Go."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_fixture import valid_override
import dependency_environment


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
ROOT = Path(
    os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)
).expanduser().resolve()
OUTPUT = (
    CONFORMANCE_DIR
    / ".artifacts"
    / "config-runtime-typescript"
    / "summary.json"
)
PROJECT = "servicelib-config-conformance-typescript"
PORT = 19093


def run(
    command: list[str], cwd: Path, env: dict[str, str],
    *, retry_network: bool = False,
) -> None:
    print("+", " ".join(command), flush=True)
    if retry_network:
        dependency_environment.run_dependency_command(command, cwd=cwd, env=env)
        return
    subprocess.run(command, cwd=cwd, env=env, check=True)


def fetch(path: str, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}{path}", timeout=timeout
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"GET {path} returned HTTP {response.status}")
        return response.read().decode()


def wait_fetch(path: str, deadline: float) -> str:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return fetch(path)
        except Exception as error:  # service may still be starting
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"TypeScript service did not expose {path}: {last_error}")


def reload_count(metrics: str, event: str) -> int:
    pattern = re.compile(
        rf'^service_config_reloads_total\{{(?=[^}}]*event="{event}")'
        rf'(?=[^}}]*service="Order Service")[^}}]*\}} ([0-9]+)$',
        re.MULTILINE,
    )
    match = pattern.search(metrics)
    return 0 if match is None else int(match.group(1))


def wait_count(event: str, minimum: int, deadline: float) -> int:
    last = -1
    while time.monotonic() < deadline:
        last = reload_count(fetch("/metrics"), event)
        if last >= minimum:
            return last
        time.sleep(0.25)
    raise RuntimeError(
        f"TypeScript reload metric event={event} stayed at {last}, "
        f"expected >= {minimum}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    example = ROOT / "tsexample"
    framework = ROOT / "tsservicelib"
    generated_service = (
        example / "orderservice/src/internal/app/service.generated.ts"
    )
    required = (
        example / "docker-compose.yml",
        example / "docker-compose.typescript-runtime.generated.yml",
        generated_service,
        framework,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("missing conformance input: " + ", ".join(missing), file=sys.stderr)
        return 2

    artifact = OUTPUT.parent
    artifact.mkdir(parents=True, exist_ok=True)
    override = artifact / "overrides.yaml"
    compose = artifact / "compose.yml"
    override.write_text(valid_override(1000))
    compose.write_text(
        f"""services:
  orderservice:
    ports: !override
      - "{PORT}:9091"
    volumes:
      - {override}:/app/config/docker_overrides.yaml:ro
"""
    )
    command = [
        "docker", "compose", "--project-name", PROJECT,
        "-f", str(example / "docker-compose.yml"),
        "-f", str(example / "docker-compose.typescript-runtime.generated.yml"),
        "-f", str(compose),
    ]
    env = {
        **os.environ,
        "TSSERVICELIB_SOURCE_CONTEXT": str(framework),
    }
    summary: dict[str, object] = {"status": "fail"}
    started = False
    try:
        if not args.skip_build:
            run(
                command + ["build", "orderservice"], example, env,
                retry_network=True,
            )
        run(command + ["up", "-d", "--no-deps", "orderservice"], example, env)
        started = True
        initial_metrics = wait_fetch(
            "/metrics", time.monotonic() + args.timeout
        )
        initial_success = reload_count(initial_metrics, "success")
        initial_error = reload_count(initial_metrics, "error")

        override.write_text(valid_override(750))
        success = wait_count(
            "success", initial_success + 1, time.monotonic() + args.timeout
        )
        override.write_text("dataConnectors: [broken\n")
        error = wait_count(
            "error", initial_error + 1, time.monotonic() + args.timeout
        )
        if "Order Service" not in fetch("/status/graph"):
            raise RuntimeError("TypeScript did not retain the previous snapshot")
        if reload_count(fetch("/metrics"), "success") != success:
            raise RuntimeError("TypeScript published invalid YAML as a valid snapshot")

        override.write_text(valid_override(500))
        recovered = wait_count(
            "success", success + 1, time.monotonic() + args.timeout
        )
        source = generated_service.read_text()
        wiring = {
            "paths": "Config.configPaths(arguments_)" in source,
            "reload_source": "configReload:" in source,
            "reload_load": (
                "load: async () => (await Config.load(arguments_)).runtime"
                in source
            ),
        }
        if not all(wiring.values()):
            raise RuntimeError(f"generated reload lifecycle is incomplete: {wiring}")
        summary = {
            "status": "pass",
            "initial": {"success": initial_success, "error": initial_error},
            "after_valid": {"success": success},
            "after_invalid": {"error": error},
            "after_recovery": {"success": recovered},
            "previous_snapshot_retained": True,
            "generated_lifecycle": wiring,
        }
    except Exception as error:
        summary = {"status": "fail", "error": str(error)}
    finally:
        OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        if started and not args.keep:
            try:
                run(command + ["down", "--remove-orphans"], example, env)
            except Exception as error:
                print(
                    f"warning: TypeScript cleanup failed: {error}",
                    file=sys.stderr,
                )

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
