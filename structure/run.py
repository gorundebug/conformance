#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
DEFAULT_ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
DEFAULT_ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "structure" / "summary.json"


def files_below(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def example_files(root: Path) -> set[str]:
    ignored_parts = {
        ".git",
        ".servicegen",
        "build",
        "conformance",
        "dist",
        "tmp",
        "tools",
        "__pycache__",
    }
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in ignored_parts for part in path.relative_to(root).parts)
    }


def tracked_files(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return {
        path.decode()
        for path in result.stdout.split(b"\0")
        if path
    }


def mapped_example_paths(paths: set[str], mappings: dict[str, str]) -> set[str]:
    result: dict[str, str] = {}
    for path in sorted(paths):
        mapped = path
        for source, destination in mappings.items():
            if path.startswith(source):
                mapped = destination + path[len(source):]
                break
        if mapped in result:
            raise ValueError(f"example path collision: {result[mapped]} and {path} -> {mapped}")
        result[mapped] = path
    return set(result)


def interface_tokens(required: dict[str, list[str]], implementation: str) -> list[str]:
    return [*required["shared"], *required[implementation]]


def exact_difference(
    *,
    left: set[str],
    right: set[str],
    allowed_left_only: set[str],
    allowed_right_only: set[str],
) -> dict[str, list[str]]:
    left_only = left - right
    right_only = right - left
    return {
        "unexpected_left_only": sorted(left_only - allowed_left_only),
        "stale_left_only_allowance": sorted(allowed_left_only - left_only),
        "unexpected_right_only": sorted(right_only - allowed_right_only),
        "stale_right_only_allowance": sorted(allowed_right_only - right_only),
    }


def changed_common(left_root: Path, right_root: Path) -> set[str]:
    left = files_below(left_root)
    right = files_below(right_root)
    return {
        relative
        for relative in left & right
        if (left_root / relative).read_bytes() != (right_root / relative).read_bytes()
    }


def interface_files(root: Path) -> set[str]:
    result = {
        path.relative_to(root).as_posix()
        for path in root.glob("*service/internal/functions/**/*.hpp")
        if path.is_file()
    }
    result.update(
        path.relative_to(root).as_posix()
        for path in root.glob("*service/internal/app/service.hpp")
        if path.is_file()
    )
    return result


def failures(value: Any, prefix: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            result.extend(failures(child, child_prefix))
    elif isinstance(value, list) and value:
        result.append(f"{prefix}: {', '.join(str(item) for item in value)}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Coro C++ public paths and example layout with canonical C++."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--policy", type=Path, default=HERE / "deviations.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--artifact-state", choices=("tracked", "workspace"), default="tracked",
        help="Default tracked verifies Git publication readiness; workspace checks local files without claiming they are tracked.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    canonical_headers = root / "cppservicelib" / "include" / "servicelib"
    coro_headers = root / "cppcoroservicelib" / "include" / "servicelib"
    canonical_example = root / "cppexample"
    coro_example = root / "cppcoroexample"
    language_examples = {
        name: root / name
        for name in (
            "goexample",
            "cppexample",
            "cppcoroexample",
            "pyexample",
            "rustexample",
            "tsexample",
        )
    }
    required_roots = (
        canonical_headers,
        coro_headers,
        *language_examples.values(),
    )
    missing_roots = [str(path) for path in required_roots if not path.is_dir()]
    if missing_roots:
        print("missing conformance input: " + ", ".join(missing_roots), file=sys.stderr)
        return 2

    policy = json.loads(args.policy.read_text())
    public_policy = policy["public_headers"]
    example_policy = policy["example_layout"]

    canonical_public = files_below(canonical_headers)
    coro_public = files_below(coro_headers)
    public_layout = exact_difference(
        left=canonical_public,
        right=coro_public,
        allowed_left_only=set(public_policy["omitted_userver_boundaries"]),
        allowed_right_only=set(public_policy["coro_boundary_replacements"]),
    )
    actual_changed = changed_common(canonical_headers, coro_headers)
    allowed_changed = set(public_policy["changed_shared_paths"])
    public_content = {
        "unrecorded_changed_shared_paths": sorted(actual_changed - allowed_changed),
        "stale_changed_shared_allowance": sorted(allowed_changed - actual_changed),
    }
    retired_tokens = [
        f"{relative}:{token}"
        for relative, tokens in public_policy["forbidden_coro_tokens"].items()
        for token in tokens
        if token in (coro_headers / relative).read_text()
    ]

    path_mappings = example_policy["path_mappings"]
    canonical_layout = mapped_example_paths(example_files(canonical_example), path_mappings["canonical"])
    coro_layout = mapped_example_paths(example_files(coro_example), path_mappings["coro"])
    example_layout = exact_difference(
        left=canonical_layout,
        right=coro_layout,
        allowed_left_only=set(example_policy["omitted_userver_boundaries"]),
        allowed_right_only=set(example_policy["coro_boundary_replacements"]),
    )
    canonical_interfaces = interface_files(canonical_example)
    coro_interfaces = interface_files(coro_example)
    shared_interfaces = canonical_interfaces & coro_interfaces
    changed_interfaces = {
        relative
        for relative in shared_interfaces
        if (canonical_example / relative).read_bytes()
        != (coro_example / relative).read_bytes()
    }
    allowed_interface_changes = set(
        example_policy["changed_interface_boundaries"]
    )
    required_tokens = example_policy["required_boundary_interface_tokens"]
    missing_tokens: list[str] = []
    for relative, tokens_by_runtime in required_tokens.items():
        for implementation, base in (
            ("canonical", canonical_example),
            ("coro", coro_example),
        ):
            contents = (base / relative).read_text()
            for token in interface_tokens(tokens_by_runtime, implementation):
                if token not in contents:
                    missing_tokens.append(f"{implementation}:{relative}:{token}")
    interface_contract = {
        "missing_canonical_interfaces": sorted(
            canonical_interfaces - coro_interfaces
        ),
        "coro_interface_boundaries": exact_difference(
            left=canonical_interfaces,
            right=coro_interfaces,
            allowed_left_only=set(),
            allowed_right_only=set(example_policy["coro_interface_boundaries"]),
        ),
        "unrecorded_changed_interfaces": sorted(
            changed_interfaces - allowed_interface_changes
        ),
        "stale_changed_interface_allowance": sorted(
            allowed_interface_changes - changed_interfaces
        ),
        "missing_boundary_interface_tokens": sorted(missing_tokens),
    }
    required_tracked_paths = policy["required_example_paths"]
    artifact_files = tracked_files if args.artifact_state == "tracked" else example_files
    available_artifacts = {
        example: artifact_files(language_examples[example])
        for example in required_tracked_paths
    }
    missing_tracked_paths = [
        f"{example}:{relative}"
        for example, paths in required_tracked_paths.items()
        for relative in paths
        if relative not in available_artifacts[example]
    ]

    typescript_framework = root / "tsservicelib"
    required_typescript_paths = {
        "src/api",
        "src/datasource/http",
        "src/datasource/grpc",
        "src/datasource/kafka",
        "src/datasink/http",
        "src/datasink/grpc",
        "src/datasink/kafka",
        "src/operators",
        "src/runtime/config",
        "src/runtime/pool",
        "src/runtime/serde",
        "src/runtime/status",
        "src/runtime/store",
        "src/runtime/telemetry",
        "src/transformation",
    }
    missing_typescript_paths = sorted(
        relative
        for relative in required_typescript_paths
        if not (typescript_framework / relative).is_dir()
    )
    package = json.loads((typescript_framework / "package.json").read_text())
    required_typescript_exports = {
        ".",
        "./api",
        "./datasource",
        "./datasource/http",
        "./datasource/grpc",
        "./datasource/kafka",
        "./datasink",
        "./datasink/http",
        "./datasink/grpc",
        "./datasink/kafka",
        "./operators",
        "./runtime",
        "./runtime/config",
        "./runtime/pool",
        "./runtime/serde",
        "./runtime/status",
        "./runtime/store",
        "./runtime/telemetry",
        "./transformation",
    }
    missing_typescript_exports = sorted(
        required_typescript_exports - set(package.get("exports", {}))
    )

    checks = {
        "public_header_layout": public_layout,
        "public_shared_content": public_content,
        "retired_runtime_tokens": {"unexpected": retired_tokens},
        "generated_example_layout": example_layout,
        "graph_function_interfaces": interface_contract,
        "required_example_artifacts": {
            "missing": sorted(missing_tracked_paths),
        },
        "typescript_package_taxonomy": {
            "missing_paths": missing_typescript_paths,
            "missing_exports": missing_typescript_exports,
        },
    }
    errors = failures(checks)
    summary = {
        "status": "pass" if not errors else "fail",
        "artifact_state": args.artifact_state,
        "canonical_public_paths": len(canonical_public),
        "coro_public_paths": len(coro_public),
        "byte_identical_shared_paths": len(
            (canonical_public & coro_public) - actual_changed
        ),
        "recorded_changed_shared_paths": len(actual_changed),
        "canonical_example_files": len(canonical_layout),
        "coro_example_files": len(coro_layout),
        "byte_identical_graph_function_interfaces": len(
            shared_interfaces - changed_interfaces
        ),
        "recorded_changed_graph_function_interfaces": len(changed_interfaces),
        "checks": checks,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
