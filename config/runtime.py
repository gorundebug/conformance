#!/usr/bin/env python3

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
import cpp_source_cache
from runtime_fixture import valid_override


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
DEFAULT_ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
DEFAULT_ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "config-runtime" / "summary.json"
PROJECT = "servicelib-config-conformance"
PORT = 19091


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
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
    raise RuntimeError(f"service did not expose {path}: {last_error}")


def reload_count(metrics: str, event: str) -> int:
    pattern = re.compile(
        rf'^service_config_reloads_total\{{(?=[^}}]*event="{event}")'
        rf'(?=[^}}]*service="Order Service")[^}}]*\}} ([0-9]+)$',
        re.MULTILINE,
    )
    match = pattern.search(metrics)
    if not match:
        raise RuntimeError(f"missing config reload metric for event={event}")
    return int(match.group(1))


def wait_count(event: str, minimum: int, deadline: float) -> tuple[int, str]:
    last = -1
    metrics = ""
    while time.monotonic() < deadline:
        metrics = fetch("/metrics")
        last = reload_count(metrics, event)
        if last >= minimum:
            return last, metrics
        time.sleep(0.25)
    raise RuntimeError(
        f"config reload metric event={event} stayed at {last}, expected >= {minimum}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify generated cppboost config reload and retention in Docker."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--timeout", type=float, default=35.0)
    args = parser.parse_args()

    root = args.root.resolve()
    example = root / "cppboostexample"
    framework = root / "cppboostservicelib"
    main_source = example / "orderservice" / "cmd" / "service" / "main.cpp"
    required = (example / "docker-compose.yml", framework, main_source)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("missing conformance input: " + ", ".join(missing), file=sys.stderr)
        return 2

    output = args.output.resolve()
    artifact = output.parent
    artifact.mkdir(parents=True, exist_ok=True)
    override = artifact / "overrides.yaml"
    compose = artifact / "compose.yml"
    env = os.environ.copy()
    env["SERVICELIB_SOURCE_CONTEXT"] = str(framework)
    env["CPPBOOST_BUILD_VOLUME"] = (
        cpp_source_cache.build_volume_name(framework)
    )
    override.write_text(valid_override(1000))
    compose.write_text(
        f"""services:
  orderservice:
    ports: !override
      - "{PORT}:9091"
    volumes:
      - {override}:/workspace/orderservice/config/overrides.yaml:ro
volumes:
  cpp-cmake-build:
    external: true
    name: {env["CPPBOOST_BUILD_VOLUME"]}
"""
    )

    compose_command = [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "-f",
        str(example / "docker-compose.yml"),
        "-f",
        str(compose),
    ]
    summary: dict[str, object] = {"status": "fail"}
    started = False
    try:
        if not args.skip_build:
            cpp_source_cache.configure_environment(env, framework)
            run(["./scripts/build.generated.sh", "docker-debug"], example, env)

        run(
            compose_command + ["up", "-d", "--no-deps", "orderservice"],
            example,
            env,
        )
        started = True
        deadline = time.monotonic() + args.timeout
        initial_metrics = wait_fetch("/metrics", deadline)
        initial_success = reload_count(initial_metrics, "success")
        initial_error = reload_count(initial_metrics, "error")

        override.write_text(valid_override(750))
        success_after_valid, _ = wait_count(
            "success", initial_success + 1, time.monotonic() + args.timeout
        )

        override.write_text("dataConnectors: [broken\n")
        error_after_invalid, _ = wait_count(
            "error", initial_error + 1, time.monotonic() + args.timeout
        )
        graph_while_invalid = fetch("/status/graph")
        metrics_while_invalid = fetch("/metrics")
        if reload_count(metrics_while_invalid, "success") != success_after_valid:
            raise RuntimeError("invalid YAML was published as a successful snapshot")
        if "Order Service" not in graph_while_invalid:
            raise RuntimeError("previous runtime snapshot was not retained")

        override.write_text(valid_override(500))
        success_after_recovery, final_metrics = wait_count(
            "success", success_after_valid + 1, time.monotonic() + args.timeout
        )
        final_error = reload_count(final_metrics, "error")

        source = main_source.read_text()
        wiring = {
            "telemetry": bool(
                re.search(
                    r"ConfigLoader<Config>\s+loader\([\s\S]*?"
                    r"\{\},\s*bootstrap_logger,\s*metrics,\s*\"\"\s*\);",
                    source,
                )
            ),
            "polling": "loader.Start(" in source,
            "owned_publication": (
                "std::shared_ptr<const servicelib::config::RuntimeConfig>" in source
                and "RuntimeConfigRegistry::Publish(" in source
            ),
            "stop_before_service": source.find("loader.Stop()")
            < source.find("service.stop()"),
        }
        if not all(wiring.values()):
            raise RuntimeError(f"generated reload lifecycle is incomplete: {wiring}")

        summary = {
            "status": "pass",
            "initial": {"success": initial_success, "error": initial_error},
            "after_valid": {"success": success_after_valid},
            "after_invalid": {"error": error_after_invalid},
            "after_recovery": {
                "success": success_after_recovery,
                "error": final_error,
            },
            "previous_snapshot_retained": True,
            "generated_lifecycle": wiring,
        }
    except Exception as error:
        summary = {"status": "fail", "error": str(error)}
    finally:
        output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        if started and not args.keep:
            try:
                run(
                    compose_command + ["down", "--remove-orphans"],
                    example,
                    env,
                )
            except Exception as error:
                print(f"warning: compose cleanup failed: {error}", file=sys.stderr)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
