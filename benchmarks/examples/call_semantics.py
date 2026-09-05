#!/usr/bin/env python3
"""Benchmark generated framework examples with the current call-semantics profile."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
BENCHMARK_ROOT = HERE.parent
ROOT = Path(
    os.environ.get(
        "DEPENDENCIES_DIR", BENCHMARK_ROOT.parent.parent,
    )
).expanduser().resolve()
ARTIFACTS = HERE / ".artifacts" / "call-semantics"
SERVICEGEN = ROOT / "servicegen"

VARIANTS = {
    "go": "goexample",
    "cpp": "cppexample",
    "cpp-boost": "cppboostexample",
    "python": "pyexample",
    "rust": "rustexample",
    "typescript": "tsexample",
}

FRAMEWORKS = (
    "servicelib",
    "cppservicelib",
    "cppboostservicelib",
    "pyservicelib",
    "rustservicelib",
    "tsservicelib",
)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def copy_example(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".servicegen",
            ".artifacts",
            ".cache",
            ".ccache",
            ".idea",
            ".mypy_cache",
            ".pyservicelib",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "bin",
            "build*",
            "dist*",
            "node_modules",
            "target",
            "tools",
            "tmp",
            "__pycache__",
        ),
    )


def generate_archives(archive_dir: Path) -> str:
    env = os.environ.copy()
    env.update(
        {
            "SERVICEGEN_EXAMPLE_ARCHIVE_DIR": str(archive_dir),
            "EXAMPLE_PROFILE": "current",
            "GOCACHE": os.environ.get("GOCACHE", "/tmp/servicegen-go-build"),
            "GOWORK": "off",
        }
    )
    completed = run(
        [
            "go",
            "test",
            "./cmd/codegenerator",
            "-run",
            "^TestWriteCanonicalExampleArchives$",
            "-count=1",
            "-v",
        ],
        cwd=SERVICEGEN,
        env=env,
        capture=True,
    )
    return completed.stdout


def verify_graph(example: Path) -> None:
    graph = example / "graph" / "example.generated.yaml"
    source = graph.read_text()
    expected = {
        "TaskPool": 1,
        "PriorityTaskPool": 1,
        "ParallelCall": 3,
    }
    actual = {
        name: source.count(f"callSemantics: {name}") for name in expected
    }
    if actual != expected:
        raise RuntimeError(
            f"{example.name} current profile differs: "
            f"actual={actual}, expected={expected}"
        )


def prepare_workspace(
    workspace: Path, archive_dir: Path, selected: list[str]
) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "generation.log").write_text(generate_archives(archive_dir))
    for language in selected:
        repository = VARIANTS[language]
        source = ROOT / repository
        destination = workspace / repository
        archive = archive_dir / f"{language.replace('cpp-boost', 'cppboost')}.zip"
        if not source.is_dir():
            raise RuntimeError(f"missing canonical example: {source}")
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError(f"missing generated current-profile archive: {archive}")
        copy_example(source, destination)
        completed = run(
            ["bash", "scripts/merge.generated.sh", str(archive)],
            cwd=destination,
            capture=True,
        )
        (ARTIFACTS / f"merge-{language}.log").write_text(completed.stdout)
        verify_graph(destination)

    for repository in FRAMEWORKS:
        source = ROOT / repository
        if not source.is_dir():
            raise RuntimeError(f"missing framework source: {source}")
        (workspace / repository).symlink_to(source, target_is_directory=True)


def benchmark_command(args: argparse.Namespace, selected: list[str]) -> list[str]:
    command = [
        sys.executable,
        str(HERE / "run.py"),
        "--cores",
        str(args.cores),
        "--loadgen-cores",
        str(args.loadgen_cores),
        "--vus",
        str(args.vus),
        "--duration",
        args.duration,
        "--warmup",
        args.warmup,
        "--runs",
        str(args.runs),
        "--max-map-count",
        str(args.max_map_count),
        "--scenario",
        "process_order_out_of_stock",
        "--graph-profile",
        "current",
        "--result-prefix",
        "call-semantics",
    ]
    if args.grpc_connections is not None:
        command.extend(("--grpc-connections", str(args.grpc_connections)))
    if args.build_only:
        command.append("--build-only")
    for language in selected:
        command.extend(("--language", language))
    return command


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
    parser.add_argument("--language", action="append", choices=tuple(VARIANTS))
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    selected = [name for name in VARIANTS if not args.language or name in args.language]
    if not SERVICEGEN.is_dir():
        raise RuntimeError(f"missing servicegen source: {SERVICEGEN}")

    temporary = Path(tempfile.mkdtemp(prefix="servicelib-call-semantics-benchmark-"))
    workspace = temporary / "workspace"
    archives = temporary / "archives"
    workspace.mkdir()
    archives.mkdir()
    try:
        prepare_workspace(workspace, archives, selected)
        env = os.environ.copy()
        env.update(
            {
                "DEPENDENCIES_DIR": str(workspace),
                "UPDATE_MANAGED_DEPENDENCIES": "0",
                "EXAMPLE_PROFILE": "current",
            }
        )
        run(benchmark_command(args, selected), cwd=HERE, env=env)
        return 0
    finally:
        if args.keep_workspace:
            print(f"Kept generated workspace: {workspace}", flush=True)
        else:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
