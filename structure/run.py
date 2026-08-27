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
DEFAULT_ROOT = Path(os.environ.get("CONFORMANCE_DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
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
        description="Compare Boost C++ public paths and example layout with canonical C++."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--policy", type=Path, default=HERE / "deviations.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    args = parser.parse_args()

    root = args.root.resolve()
    canonical_headers = root / "cppservicelib" / "include" / "servicelib"
    boost_headers = root / "cppboostservicelib" / "include" / "servicelib"
    canonical_example = root / "cppexample"
    boost_example = root / "cppboostexample"
    language_examples = {
        name: root / name
        for name in (
            "goexample",
            "cppexample",
            "cppboostexample",
            "pyexample",
            "rustexample",
            "tsexample",
        )
    }
    required_roots = (
        canonical_headers,
        boost_headers,
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
    boost_public = files_below(boost_headers)
    public_layout = exact_difference(
        left=canonical_public,
        right=boost_public,
        allowed_left_only=set(public_policy["omitted_userver_boundaries"]),
        allowed_right_only=set(public_policy["boost_boundary_replacements"]),
    )
    actual_changed = changed_common(canonical_headers, boost_headers)
    allowed_changed = set(public_policy["changed_shared_paths"])
    public_content = {
        "unrecorded_changed_shared_paths": sorted(actual_changed - allowed_changed),
        "stale_changed_shared_allowance": sorted(allowed_changed - actual_changed),
    }

    canonical_layout = example_files(canonical_example)
    boost_layout = example_files(boost_example)
    example_layout = exact_difference(
        left=canonical_layout,
        right=boost_layout,
        allowed_left_only=set(example_policy["omitted_userver_boundaries"]),
        allowed_right_only=set(example_policy["boost_boundary_replacements"]),
    )
    canonical_interfaces = interface_files(canonical_example)
    boost_interfaces = interface_files(boost_example)
    shared_interfaces = canonical_interfaces & boost_interfaces
    changed_interfaces = {
        relative
        for relative in shared_interfaces
        if (canonical_example / relative).read_bytes()
        != (boost_example / relative).read_bytes()
    }
    allowed_interface_changes = set(
        example_policy["changed_interface_boundaries"]
    )
    required_tokens = example_policy["required_boundary_interface_tokens"]
    missing_tokens: list[str] = []
    for relative, tokens in required_tokens.items():
        for implementation, base in (
            ("canonical", canonical_example),
            ("boost", boost_example),
        ):
            contents = (base / relative).read_text()
            for token in tokens:
                if token not in contents:
                    missing_tokens.append(f"{implementation}:{relative}:{token}")
    interface_contract = {
        "missing_canonical_interfaces": sorted(
            canonical_interfaces - boost_interfaces
        ),
        "unexpected_boost_interfaces": sorted(
            boost_interfaces - canonical_interfaces
        ),
        "unrecorded_changed_interfaces": sorted(
            changed_interfaces - allowed_interface_changes
        ),
        "stale_changed_interface_allowance": sorted(
            allowed_interface_changes - changed_interfaces
        ),
        "missing_boundary_interface_tokens": sorted(missing_tokens),
    }
    required_tracked_paths = policy["required_tracked_example_paths"]
    missing_tracked_paths = [
        f"{example}:{relative}"
        for example, paths in required_tracked_paths.items()
        for relative in paths
        if relative not in tracked_files(language_examples[example])
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
        "generated_example_layout": example_layout,
        "graph_function_interfaces": interface_contract,
        "required_tracked_example_artifacts": {
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
        "canonical_public_paths": len(canonical_public),
        "boost_public_paths": len(boost_public),
        "byte_identical_shared_paths": len(
            (canonical_public & boost_public) - actual_changed
        ),
        "recorded_changed_shared_paths": len(actual_changed),
        "canonical_example_files": len(canonical_layout),
        "boost_example_files": len(boost_layout),
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
