#!/usr/bin/env python3
"""Run and validate the complete framework/native benchmark matrix."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


CONFORMANCE = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get(
        "DEPENDENCIES_DIR",
        str(CONFORMANCE.parent),
    )
).expanduser().resolve()
BENCHMARKS = CONFORMANCE / "benchmarks"
RUNNER = BENCHMARKS / "examples" / "run.py"
RUNNER_ARTIFACTS = BENCHMARKS / "examples" / ".artifacts"
ARTIFACTS = CONFORMANCE / ".artifacts" / "benchmarks"
LANGUAGES = {
    "go", "go-native", "cpp", "cpp-native", "cpp-boost",
    "cpp-boost-native", "python", "python-native", "rust", "rust-native",
    "typescript", "typescript-native",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_results() -> dict[str, Any]:
    path = RUNNER_ARTIFACTS / "results.json"
    require(path.is_file(), f"missing benchmark result: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise RuntimeError(f"malformed benchmark result {path}: {error}") from error
    require(isinstance(value, dict), "benchmark result must be an object")
    return value


def validate(results: dict[str, Any], args: argparse.Namespace) -> None:
    profile = os.environ.get("EXAMPLE_PROFILE", "function-call")
    require(results.get("graph_profile") == profile, "benchmark graph profile differs")
    parameters = results.get("parameters")
    require(isinstance(parameters, dict), "benchmark parameters are missing")
    expected = {
        "service_cores": args.cores,
        "loadgen_cores": args.loadgen_cores,
        "grpc_connections": args.grpc_connections or args.cores,
        "vus": args.vus,
        "duration": args.duration,
        "warmup": args.warmup,
        "runs": args.runs,
        "max_map_count": args.max_map_count,
    }
    for name, value in expected.items():
        require(parameters.get(name) == value,
                f"benchmark {name}={parameters.get(name)!r}, expected {value!r}")
    rows = results.get("results")
    require(isinstance(rows, list), "benchmark results matrix is missing")
    actual = {
        row.get("language") for row in rows if isinstance(row, dict)
    }
    require(actual == LANGUAGES,
            f"benchmark language matrix differs: {sorted(actual)}")
    logs = results.get("logs")
    require(isinstance(logs, dict), "benchmark per-language logs are missing")
    require(set(logs) == LANGUAGES, "benchmark log language matrix differs")
    for language, path in logs.items():
        require(Path(path).is_file(), f"{language}: benchmark log is missing: {path}")
    for row in rows:
        require(isinstance(row, dict), "benchmark row must be an object")
        language = row.get("language")
        rps = row.get("requests_per_second")
        error_rate = row.get("error_rate")
        require(isinstance(rps, (int, float)) and math.isfinite(float(rps))
                and float(rps) > 0, f"{language}: invalid requests_per_second")
        require(error_rate == 0, f"{language}: benchmark has errors")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--loadgen-cores", type=int, default=6)
    parser.add_argument("--grpc-connections", type=int)
    parser.add_argument("--vus", type=int, default=256)
    parser.add_argument("--duration", default="20s")
    parser.add_argument("--warmup", default="5s")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-map-count", type=int, default=0)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args()

    command = [
        sys.executable, str(RUNNER),
        "--graph-profile",
        os.environ.get("EXAMPLE_PROFILE", "function-call"),
        "--cores", str(args.cores),
        "--loadgen-cores", str(args.loadgen_cores),
        "--grpc-connections", str(args.grpc_connections or args.cores),
        "--vus", str(args.vus),
        "--duration", args.duration,
        "--warmup", args.warmup,
        "--runs", str(args.runs),
        "--max-map-count", str(args.max_map_count),
    ]
    if args.skip_build:
        command.append("--skip-build")
    if not args.skip_run:
        print("+", " ".join(command), flush=True)
        subprocess.run(command, cwd=BENCHMARKS, check=True)

    results = read_results()
    validate(results, args)
    summary = {
        "status": "pass",
        "graph_profile": results["graph_profile"],
        "languages": sorted(LANGUAGES),
        "parameters": results["parameters"],
        "results": results["results"],
        "logs": results["logs"],
        "artifacts": {
            "json": str(RUNNER_ARTIFACTS / "results.json"),
            "csv": str(RUNNER_ARTIFACTS / "results.csv"),
            "markdown": str(RUNNER_ARTIFACTS / "results.md"),
        },
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
