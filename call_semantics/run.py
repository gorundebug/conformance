#!/usr/bin/env python3
"""Run canonical framework scenarios with the selected call-semantics profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
ROOT = Path(
    os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)
).expanduser().resolve()
ARTIFACTS = CONFORMANCE_DIR / ".artifacts" / "call-semantics"
SERVICEGEN = ROOT / "servicegen"

VARIANTS = {
    "go": "goexample",
    "cpp": "cppexample",
    "cppboost": "cppboostexample",
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

EXPECTED_CURRENT_GRAPH_MARKERS = (
    "callSemantics: TaskPool",
    "callSemantics: PriorityTaskPool",
    "callSemantics: ParallelCall",
    "poolName: Default Pool",
    "poolName: Inventory Priority Workers",
)
# Kept as the public runner-test fixture name used by test_paths.py.
EXPECTED_GRAPH_MARKERS = EXPECTED_CURRENT_GRAPH_MARKERS


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
            "SERVICEGEN_EXAMPLE_PROFILE": "current",
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


def verify_graph(example: Path, profile: str) -> dict[str, object]:
    graph = example / "graph" / "example.generated.yaml"
    if not graph.is_file():
        raise RuntimeError(f"generated graph is missing: {graph}")
    source = graph.read_text()
    function_count = source.count("callSemantics: FunctionCall")
    task_count = source.count("callSemantics: TaskPool")
    priority_count = source.count("callSemantics: PriorityTaskPool")
    parallel_count = source.count("callSemantics: ParallelCall")
    if profile == "function-call":
        if function_count != 6 or task_count or priority_count or parallel_count:
            raise RuntimeError(
                f"{example.name} did not preserve the function-call profile: "
                f"function={function_count}, task={task_count}, "
                f"priority={priority_count}, parallel={parallel_count}"
            )
    elif profile == "current":
        missing = [
            marker for marker in EXPECTED_CURRENT_GRAPH_MARKERS
            if marker not in source
        ]
        if missing or task_count != 1 or priority_count != 1 or parallel_count != 3:
            raise RuntimeError(
                f"{example.name} did not preserve the current call-semantics profile: "
                f"missing={missing}, task={task_count}, priority={priority_count}, "
                f"parallel={parallel_count}"
            )
    else:
        raise RuntimeError(
            f"unsupported call-semantics profile: {profile}"
        )
    return {
        "graph": str(graph.relative_to(example)),
        "function_call_links": function_count,
        "task_pool_links": task_count,
        "priority_task_pool_links": priority_count,
        "parallel_call_links": parallel_count,
    }


def prepare_workspace(
    workspace: Path, archive_dir: Path, selected: list[str]
) -> dict[str, object]:
    # The scenario gRPC probe mounts the dependency root read-only at /repo and
    # overlays the conformance sources at /repo/conformance. Docker cannot
    # create that nested mountpoint inside an already read-only bind.
    (workspace / "conformance").mkdir(exist_ok=True)
    generation_log = generate_archives(archive_dir)
    (ARTIFACTS / "generation.log").write_text(generation_log)

    prepared: dict[str, object] = {}
    for language in selected:
        repository = VARIANTS[language]
        source = ROOT / repository
        destination = workspace / repository
        archive = archive_dir / f"{language}.zip"
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
        prepared[language] = verify_graph(destination, "current")

    # Every language uses the shared Go gRPC probe. Its go.mod replaces the
    # generated API module with /repo/goexample/inventory_service_api, so a
    # filtered run still needs that small module even when Go is not selected.
    probe_api = workspace / "goexample" / "inventory_service_api"
    if not probe_api.exists():
        copy_example(ROOT / "goexample" / "inventory_service_api", probe_api)

    for repository in FRAMEWORKS:
        source = ROOT / repository
        if not source.is_dir():
            raise RuntimeError(f"missing framework source: {source}")
        (workspace / repository).symlink_to(source, target_is_directory=True)
    return prepared


def execute_scenarios(
    workspace: Path,
    selected: list[str],
    profile: str,
    *,
    skip_build: bool = False,
) -> dict[str, object]:
    runtime_artifacts = ARTIFACTS / "runtime"
    runtime_artifacts.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "CONFORMANCE_DEPENDENCIES_DIR": str(workspace),
        "SERVICELIB_SCENARIO_ARTIFACTS_DIR": str(runtime_artifacts),
        "SERVICELIB_SCENARIO_PROJECT_SUFFIX": f"-{profile}",
        "SERVICELIB_SCENARIO_REQUIRE_POOLS": "1" if profile == "current" else "0",
    })
    command = [sys.executable, str(CONFORMANCE_DIR / "scenarios" / "run.py")]
    if skip_build:
        command.append("--skip-build")
    for language in selected:
        command.extend(("--language", language))
    run(command, cwd=CONFORMANCE_DIR, env=env)
    summary_path = runtime_artifacts / "summary.json"
    if not summary_path.is_file():
        raise RuntimeError(f"{profile} scenario runner did not write its summary")
    summary = json.loads(summary_path.read_text())
    if summary.get("status") != "passed":
        raise RuntimeError(f"{profile} scenario runner failed: {summary}")
    implementations = summary.get("implementations")
    if not isinstance(implementations, dict) or set(implementations) != set(selected):
        raise RuntimeError(
            f"{profile} scenario language matrix differs: "
            f"actual={sorted(implementations or {})}, expected={sorted(selected)}"
        )
    if profile == "current":
        for language, result in implementations.items():
            activity = result.get("pool_activity") if isinstance(result, dict) else None
            if not isinstance(activity, dict) or any(
                not isinstance(value, (int, float)) or value <= 0
                for value in activity.values()
            ):
                raise RuntimeError(
                    f"{language} has no proven pool activity: {activity}"
                )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", action="append", choices=tuple(VARIANTS))
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="generate, merge and validate the pooled graph without Docker runtime",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse already-built runtime images and build volumes",
    )
    args = parser.parse_args()

    selected = [name for name in VARIANTS if not args.language or name in args.language]
    if not SERVICEGEN.is_dir():
        raise RuntimeError(f"missing servicegen source: {SERVICEGEN}")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    active_profile = os.environ.get("CONFORMANCE_EXAMPLE_PROFILE", "")
    if active_profile in {"function-call", "current"}:
        started = time.monotonic()
        summary: dict[str, object] = {
            "status": "fail",
            "profile": active_profile,
            "languages": selected,
            "workspace": "prepared-by-quickstart",
        }
        try:
            summary["generated_graphs"] = {
                language: verify_graph(ROOT / VARIANTS[language], active_profile)
                for language in selected
            }
            if not args.prepare_only:
                runtime = execute_scenarios(
                    ROOT, selected, active_profile, skip_build=args.skip_build
                )
                summary["implementations"] = runtime["implementations"]
            summary["status"] = "pass"
            summary["duration_seconds"] = round(time.monotonic() - started, 3)
            (ARTIFACTS / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n"
            )
            print(
                f"{active_profile} call-semantics conformance passed:",
                ", ".join(selected),
            )
            return 0
        except Exception as error:
            summary["error"] = str(error)
            summary["duration_seconds"] = round(time.monotonic() - started, 3)
            (ARTIFACTS / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n"
            )
            raise

    temporary = Path(tempfile.mkdtemp(prefix="servicelib-pooled-conformance-"))
    workspace = temporary / "workspace"
    archives = temporary / "archives"
    workspace.mkdir()
    archives.mkdir()
    started = time.monotonic()
    summary: dict[str, object] = {
        "status": "fail",
        "profile": "current",
        "languages": selected,
        "workspace": str(workspace) if args.keep_workspace else "disposable",
    }
    try:
        summary["generated_graphs"] = prepare_workspace(
            workspace, archives, selected
        )
        if not args.prepare_only:
            runtime = execute_scenarios(
                workspace, selected, "current", skip_build=args.skip_build
            )
            summary["implementations"] = runtime["implementations"]
        summary["status"] = "pass"
        summary["duration_seconds"] = round(time.monotonic() - started, 3)
        (ARTIFACTS / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        print(
            "Pooled call-semantics conformance passed:",
            ", ".join(selected),
        )
        return 0
    except Exception as error:
        summary["error"] = str(error)
        summary["duration_seconds"] = round(time.monotonic() - started, 3)
        (ARTIFACTS / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        raise
    finally:
        if not args.keep_workspace:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Call-semantics conformance failed: {error}", file=sys.stderr)
        raise SystemExit(1)
