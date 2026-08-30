#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BENCHMARK_ROOT = HERE.parent
DEPENDENCIES = Path(
    os.environ.get(
        "DEPENDENCIES_DIR", str(BENCHMARK_ROOT.parent.parent),
    )
).expanduser().resolve()
EXAMPLE = DEPENDENCIES / "tsexample"
ARTIFACTS = HERE / ".artifacts"
SCRIPT = HERE / "scripts" / "graph-only.mjs"
OVERLAY = HERE / "compose.typescript-graph.yml"


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)s", value)
    if match is None:
        raise argparse.ArgumentTypeError("duration must be a positive whole number of seconds")
    return int(match.group(1))


def parse_result(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("scenario") == "typescript_graph_without_grpc":
            if not isinstance(value.get("requests"), int) or value["requests"] < 1:
                raise RuntimeError("graph-only benchmark completed without requests")
            rate = value.get("requests_per_second")
            if not isinstance(rate, (int, float)) or rate <= 0:
                raise RuntimeError("graph-only benchmark returned an invalid request rate")
            return value
    raise RuntimeError("graph-only benchmark did not emit its JSON result")


def compose(project: str, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(EXAMPLE),
        "--file",
        str(EXAMPLE / "docker-compose.yml"),
        "--file",
        str(EXAMPLE / "docker-compose.typescript-runtime.generated.yml"),
        "--file",
        str(OVERLAY),
        *arguments,
    ]


def run_streaming(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    print("+ " + " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return_code = process.wait()
    output = "".join(lines)
    if return_code != 0:
        raise RuntimeError(
            f"command failed with exit code {return_code}: {' '.join(command)}"
        )
    return output


def build(env: dict[str, str]) -> None:
    if not EXAMPLE.is_dir():
        raise RuntimeError(f"TypeScript example is missing: {EXAMPLE}")
    local_framework = DEPENDENCIES / "tsservicelib"
    if local_framework.is_dir():
        env["TSSERVICELIB_SOURCE_CONTEXT"] = str(local_framework)
    run_streaming(
        ["make", "docker-build", "RUNTIME_IMAGE=1"],
        cwd=EXAMPLE,
        env=env,
    )


def write_results(result: dict[str, Any], cores: int) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    json_path = ARTIFACTS / "typescript-graph-without-grpc.json"
    markdown_path = ARTIFACTS / "typescript-graph-without-grpc.md"
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    markdown_path.write_text(
        "# TypeScript graph without gRPC\n\n"
        "This benchmark invokes the canonical generated Order stream graph in "
        "the production runtime image. The Inventory sink is replaced after graph "
        "construction by an immediate in-memory result, and no service lifecycle or "
        "network transport is started. Production code paths are unchanged.\n\n"
        f"- Service CPU quota: `{cores}` cores\n"
        f"- Virtual users: `{result['vus']}`\n"
        f"- Measurement: `{result['duration_seconds']}s`\n"
        f"- Requests: `{result['requests']}`\n"
        f"- Requests/s: `{result['requests_per_second']:.2f}`\n"
    )
    print(f"wrote {json_path} and {markdown_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical TypeScript Order graph without gRPC/network I/O"
    )
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--vus", type=int, default=256)
    parser.add_argument("--duration", type=duration_seconds, default=20)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if args.cores < 1 or args.vus < 1:
        parser.error("cores and vus must be positive")
    if not SCRIPT.is_file() or not OVERLAY.is_file():
        raise RuntimeError("graph-only benchmark assets are missing")

    env = os.environ.copy()
    env.update(
        {
            "BENCHMARK_DURATION_SECONDS": str(args.duration),
            "BENCHMARK_SERVICE_CORES": str(args.cores),
            "BENCHMARK_VUS": str(args.vus),
        }
    )
    if not args.skip_build:
        build(env)

    project = f"servicelib-example-benchmark-typescript-graph-{os.getpid()}"
    command = compose(
        project,
        "run",
        "--rm",
        "--no-deps",
        "--volume",
        f"{SCRIPT}:/app/graph-only.mjs:ro",
        "--entrypoint",
        "node",
        "orderservice",
        "/app/graph-only.mjs",
    )
    try:
        output = run_streaming(command, cwd=EXAMPLE, env=env)
        result = parse_result(output)
        write_results(result, args.cores)
    finally:
        subprocess.run(
            compose(project, "down", "--volumes", "--remove-orphans"),
            cwd=EXAMPLE,
            env=env,
            check=False,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
