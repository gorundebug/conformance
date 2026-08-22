#!/usr/bin/env python3
"""Prepare disposable canonical examples generated with a selected profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


CONFORMANCE = Path(__file__).resolve().parent
ARTIFACTS = CONFORMANCE / ".artifacts"
VARIANTS = {
    "go": "goexample",
    "cpp": "cppexample",
    "cppboost": "cppboostexample",
    "python": "pyexample",
    "rust": "rustexample",
    "typescript": "tsexample",
}
FRAMEWORK_REPOSITORIES = {
    "servicelib",
    "cppservicelib",
    "cppboostservicelib",
    "pyservicelib",
    "rustservicelib",
    "tsservicelib",
}
CACHE_DIRECTORIES = (
    "build", "target", "node_modules", ".venv",
)


def run(
    command: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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


def copy_framework(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            ".git", ".artifacts", ".cache", ".ccache", ".idea",
            ".mypy_cache", ".pytest_cache", ".ruff_cache",
            "build*", "dist*", "node_modules", "target", ".venv",
            "__pycache__",
        ),
    )
    for name in CACHE_DIRECTORIES:
        cache = source / name
        if cache.exists():
            (destination / name).symlink_to(cache, target_is_directory=True)


def generate_archives(source_root: Path, archive_dir: Path, profile: str) -> str:
    servicegen = source_root / "servicegen"
    if not servicegen.is_dir():
        raise RuntimeError(f"missing servicegen source: {servicegen}")
    env = os.environ.copy()
    env.update(
        {
            "SERVICEGEN_EXAMPLE_ARCHIVE_DIR": str(archive_dir),
            "SERVICEGEN_EXAMPLE_PROFILE": profile,
            "GOCACHE": os.environ.get("GOCACHE", "/tmp/servicegen-go-build"),
            "GOWORK": "off",
        }
    )
    return run(
        [
            "go",
            "test",
            "./cmd/codegenerator",
            "-run",
            "^TestWriteCanonicalExampleArchives$",
            "-count=1",
            "-v",
        ],
        cwd=servicegen,
        env=env,
    ).stdout


def verify_current_graph(example: Path) -> dict[str, int]:
    graph = example / "graph" / "example.generated.yaml"
    if not graph.is_file():
        raise RuntimeError(f"generated graph is missing: {graph}")
    source = graph.read_text()
    actual = {
        "task_pool_links": source.count("callSemantics: TaskPool"),
        "priority_task_pool_links": source.count(
            "callSemantics: PriorityTaskPool"
        ),
        "parallel_call_links": source.count("callSemantics: ParallelCall"),
    }
    expected = {
        "task_pool_links": 1,
        "priority_task_pool_links": 1,
        "parallel_call_links": 3,
    }
    if actual != expected:
        raise RuntimeError(
            f"{example.name} profile graph differs: "
            f"actual={actual}, expected={expected}"
        )
    return actual


def initialize_git_snapshot(example: Path, profile: str) -> None:
    """Give read-only conformance checks an isolated tracked-file index."""
    run(["git", "init", "--quiet"], cwd=example)
    run(["git", "add", "--force", "."], cwd=example)
    run(
        [
            "git",
            "-c", "user.name=ServiceGen Conformance",
            "-c", "user.email=conformance@localhost",
            "commit", "--quiet", "-m", f"Generated {profile} profile",
        ],
        cwd=example,
    )


def merge_generated_command(archive: Path) -> list[str]:
    return [
        "bash", "scripts/merge.generated.sh", "--remove-stale", str(archive)
    ]


def prepare(source_root: Path, workspace: Path, profile: str) -> dict[str, object]:
    if workspace.exists() and any(workspace.iterdir()):
        raise RuntimeError(f"profile workspace must be empty: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    archive_dir = workspace.parent / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    profile_artifacts = ARTIFACTS / f"profile-{profile}"
    profile_artifacts.mkdir(parents=True, exist_ok=True)
    (profile_artifacts / "generation.log").write_text(
        generate_archives(source_root, archive_dir, profile)
    )

    generated: dict[str, object] = {}
    generated_repositories = set(VARIANTS.values())
    for language, repository in VARIANTS.items():
        source = source_root / repository
        destination = workspace / repository
        archive = archive_dir / f"{language}.zip"
        if not source.is_dir():
            raise RuntimeError(f"missing canonical example: {source}")
        if not archive.is_file() or archive.stat().st_size == 0:
            raise RuntimeError(f"missing generated profile archive: {archive}")
        copy_example(source, destination)
        merged = run(
            merge_generated_command(archive),
            cwd=destination,
        )
        (profile_artifacts / f"merge-{language}.log").write_text(merged.stdout)
        generated[language] = verify_current_graph(destination)
        initialize_git_snapshot(destination, profile)

    # Mirror every other managed dependency without copying large framework
    # and native-example repositories. The generated examples above are the
    # only repositories whose contents differ in the selected run profile.
    for source in source_root.iterdir():
        if (
            source.name in generated_repositories
            or source.name == "conformance"
            or source.name.startswith(".")
        ):
            continue
        destination = workspace / source.name
        if destination.exists() or destination.is_symlink():
            continue
        if source.name in FRAMEWORK_REPOSITORIES:
            copy_framework(source, destination)
        else:
            destination.symlink_to(source, target_is_directory=source.is_dir())

    # Scenario probes mount the dependency root and conformance sources at a
    # nested path; Docker needs the mount point to exist in the outer root.
    (workspace / "conformance").mkdir(exist_ok=True)
    summary = {
        "status": "pass",
        "profile": profile,
        "source_root": str(source_root),
        "workspace": str(workspace),
        "generated_graphs": generated,
    }
    (profile_artifacts / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile", choices=("current",), required=True)
    args = parser.parse_args()
    prepare(args.source_root.resolve(), args.workspace.resolve(), args.profile)
    print(f"Prepared {args.profile} profile workspace: {args.workspace}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Profile workspace preparation failed: {error}")
        raise SystemExit(1)
