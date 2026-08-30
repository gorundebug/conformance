#!/usr/bin/env python3
"""Verify the canonical Go live config-reload contract beside Boost C++."""

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
from runtime_fixture import valid_override


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
OUTPUT = CONFORMANCE_DIR / ".artifacts" / "config-runtime-go" / "summary.json"
PROJECT = "servicelib-config-conformance-go"
PORT = 19092


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
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
        except Exception as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"Go service did not expose {path}: {last_error}")


def reload_count(metrics: str, event: str) -> int:
    pattern = re.compile(
        rf'^service_config_reloads_total\{{(?=[^}}]*event="{event}")'
        rf'(?=[^}}]*service="Order Service")[^}}]*\}} ([0-9]+)$',
        re.MULTILINE,
    )
    match = pattern.search(metrics)
    if not match:
        # The Go OTel exporter does not emit a counter time series before its
        # first increment; this is the observable zero value, not a missing
        # registration. Subsequent waits still require the series to appear.
        return 0
    return int(match.group(1))


def wait_count(event: str, minimum: int, deadline: float) -> int:
    last = -1
    while time.monotonic() < deadline:
        last = reload_count(fetch("/metrics"), event)
        if last >= minimum:
            return last
        time.sleep(0.25)
    raise RuntimeError(
        f"Go reload metric event={event} stayed at {last}, expected >= {minimum}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    example = ROOT / "goexample"
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
    command:
      - --config
      - /app/config/config.yaml
      - --values
      - /config-conformance/overrides.yaml
    volumes:
      - {artifact}:/config-conformance:ro
"""
    )
    command = [
        "docker", "compose", "--project-name", PROJECT,
        "-f", str(example / "docker-compose.yml"), "-f", str(compose),
    ]
    summary: dict[str, object] = {"status": "fail"}
    started = False
    try:
        if not args.skip_build:
            env = os.environ.copy()
            env["GOCACHE"] = env.get("GOCACHE", "/tmp/servicegen-go-build")
            env["GOSERVICELIB_SOURCE_CONTEXT"] = str(ROOT / "servicelib")
            run(["make", "docker-build"], example, env)
        run(command + ["up", "-d", "--no-deps", "orderservice"], example)
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
            raise RuntimeError("Go did not retain the previous valid snapshot")
        if reload_count(fetch("/metrics"), "success") != success:
            raise RuntimeError("Go published invalid YAML as a valid snapshot")

        override.write_text(valid_override(500))
        recovered = wait_count(
            "success", success + 1, time.monotonic() + args.timeout
        )
        summary = {
            "status": "pass",
            "initial": {"success": initial_success, "error": initial_error},
            "after_valid": {"success": success},
            "after_invalid": {"error": error},
            "after_recovery": {"success": recovered},
            "previous_snapshot_retained": True,
        }
    except Exception as error:
        summary = {"status": "fail", "error": str(error)}
    finally:
        OUTPUT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        if started and not args.keep:
            try:
                run(command + ["down", "--remove-orphans"], example)
            except Exception as error:
                print(f"warning: Go cleanup failed: {error}", file=sys.stderr)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
