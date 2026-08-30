#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONFORMANCE_DIR = HERE.parent
DEFAULT_ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
DEFAULT_ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "config" / "summary.json"


@dataclass(frozen=True)
class Patch:
    section: str
    object_name: str
    member: str
    variable: str


SECTIONS = {
    "Services": "services",
    "Streams": "streams",
    "DataConnectors": "dataConnectors",
    "Endpoints": "endpoints",
    "Pools": "pools",
    "Links": "links",
    "Modules": "modules",
    "Types": "types",
}

SNAPSHOT_IGNORED_FIELDS = {
    "definitionFormat",
    "functionInitializerGroup",
    "functionModule",
    "functionPackage",
    "golangVersion",
    "implementation",
    "modulePath",
    "type",
    "typeDefinition",
    "typeImport",
    "typescriptVersion",
}

CALL_SEMANTICS = {
    "functionCall": 2,
    "taskPool": 3,
    "priorityTaskPool": 4,
    "parallelCall": 5,
}

GRPC_METHOD_TYPES = {
    "NoStreaming": 1,
    "ClientStreaming": 2,
    "ServerStreaming": 3,
    "BidirectionalStreaming": 4,
}


def upper_first(value: str) -> str:
    return value[:1].upper() + value[1:]


def env_name(value: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return words.upper()


def patches(config: str) -> list[Patch]:
    section = ""
    object_name = ""
    result: list[Patch] = []
    for line in config.splitlines():
        if match := re.fullmatch(r"([A-Za-z][A-Za-z0-9]*):", line):
            section = match.group(1)
            object_name = ""
            continue
        if match := re.fullmatch(r"  ([A-Za-z][A-Za-z0-9]*):", line):
            object_name = match.group(1)
            continue
        match = re.fullmatch(
            r"    ([A-Za-z][A-Za-z0-9]*): \$([A-Za-z][A-Za-z0-9]*)",
            line,
        )
        if match:
            if not section or not object_name:
                raise ValueError(f"variable outside typed config object: {line}")
            result.append(Patch(section, object_name, match.group(1), match.group(2)))
    return result


def yaml_scalar_paths(config: str) -> set[tuple[str, ...]]:
    """Return scalar mapping paths from the generated config YAML subset.

    Canonical service configuration contains nested mappings with scalar leaf
    values.  Keeping this reader deliberately small avoids making the static
    conformance gate depend on a third-party YAML package while still proving
    that every generated ``$variable`` has a concrete canonical override.
    """
    parents: list[tuple[int, str]] = []
    result: set[tuple[str, ...]] = set()
    for raw_line in config.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"( *)([A-Za-z][A-Za-z0-9]*):(?: (.*))?", raw_line)
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2)
        value = match.group(3)
        while parents and parents[-1][0] >= indent:
            parents.pop()
        path = tuple(parent for _, parent in parents) + (key,)
        if value is None or value == "":
            parents.append((indent, key))
        else:
            result.add(path)
    return result


def unresolved_override_paths(config: str, override: str) -> list[str]:
    override_paths = yaml_scalar_paths(override)
    return sorted(
        f"{patch.section}.{patch.object_name}.{patch.member}"
        for patch in patches(config)
        if (patch.section, patch.object_name, patch.member) not in override_paths
    )


def matching_brace(text: str, start: int) -> int:
    if text[start] != "{":
        raise ValueError(f"expected opening brace at {start}")
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ('"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"unterminated composite literal at {start}")


def split_top_level(text: str) -> list[str]:
    result: list[str] = []
    start = 0
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ('"', "`"):
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            part = text[start:index].strip()
            if part:
                result.append(part)
            start = index + 1
    part = text[start:].strip()
    if part:
        result.append(part)
    return result


def top_level_colon(text: str) -> int:
    depth = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ('"', "`"):
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == ":" and depth == 0:
            return index
    return -1


def lower_go_name(name: str) -> str:
    if name == "ID":
        return "id"
    if name.startswith("Id"):
        return "id" + name[2:]
    return name[:1].lower() + name[1:]


def go_id_symbols(source: str) -> dict[str, int]:
    symbols: dict[str, int] = {}
    for block in re.findall(r"const\s*\((.*?)\n\)", source, re.DOTALL):
        iota = 0
        expression = ""
        for raw_line in block.splitlines():
            line = raw_line.split("//", 1)[0].strip()
            if not line:
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*(.*))?", line)
            if match is None:
                continue
            if match.group(2) is not None:
                expression = match.group(2).strip()
            if re.fullmatch(r"iota\s*\+\s*1", expression):
                symbols[match.group(1)] = iota + 1
            iota += 1
    return symbols


def parse_go_value(text: str, symbols: dict[str, int]) -> Any:
    value = text.strip()
    if value.startswith('"'):
        return json.loads(value)
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "nil":
        return None
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", value):
        return float(value)
    if value in symbols:
        return symbols[value]
    opening = value.find("{")
    if opening >= 0 and matching_brace(value, opening) == len(value) - 1:
        body = value[opening + 1:-1]
        prefix = value[:opening].lstrip("&").strip()
        if prefix.startswith("[]"):
            return [parse_go_value(item, symbols) for item in split_top_level(body)]
        return parse_go_composite(body, symbols)
    return value


def parse_go_composite(body: str, symbols: dict[str, int]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in split_top_level(body):
        colon = top_level_colon(item)
        if colon < 0:
            raise ValueError(f"unkeyed generated Go literal item: {item[:80]}")
        key = item[:colon].strip()
        result[lower_go_name(key)] = parse_go_value(item[colon + 1:], symbols)
    return result


def go_default_snapshot(source: str) -> dict[str, Any]:
    symbols = go_id_symbols(source)
    make_config = source.index("func MakeConfig() *Config")
    source = source[make_config:]
    result: dict[str, Any] = {}
    for go_name, document_name in SECTIONS.items():
        match = re.search(rf"\n\t\t{go_name}:\s+struct\s*\{{", source)
        if match is None:
            raise ValueError(f"MakeConfig section {go_name} not found")
        type_open = source.index("{", match.start())
        type_close = matching_brace(source, type_open)
        value_open = source.index("{", type_close + 1)
        value_close = matching_brace(source, value_open)
        result[document_name] = parse_go_composite(
            source[value_open + 1:value_close], symbols
        )
    return result


def typescript_default_snapshot(source: str) -> dict[str, Any]:
    marker = "const DEFAULT_CONFIG = "
    start = source.index(marker) + len(marker)
    opening = source.index("{", start)
    closing = matching_brace(source, opening)
    value = json.loads(source[opening:closing + 1])
    if not isinstance(value, dict):
        raise ValueError("TypeScript DEFAULT_CONFIG is not an object")
    return value


def normalize_snapshot(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        if key in ("callSemantics", "defaultCallSemantics"):
            for semantics, kind in CALL_SEMANTICS.items():
                if semantics in value:
                    config = normalize_snapshot(value[semantics], semantics)
                    return {"kind": kind, **config}
        normalized: dict[str, Any] = {}
        for child_key, child in value.items():
            if child_key in SNAPSHOT_IGNORED_FIELDS or child_key.startswith("id"):
                continue
            normalized_child = normalize_snapshot(child, child_key)
            if normalized_child in ({}, [], None, "", False, 0):
                continue
            normalized[child_key] = normalized_child
        if set(normalized) == {"name", "path"}:
            normalized.pop("path")
        semantics = normalized.get("callSemantics")
        if isinstance(semantics, dict):
            for field in ("async", "poolName", "priority"):
                if field in normalized:
                    semantics[field] = normalized.pop(field)
        return normalized
    if isinstance(value, list):
        return [normalize_snapshot(child, key) for child in value]
    if key in ("callSemantics", "defaultCallSemantics") and isinstance(value, int):
        return {"kind": value}
    if key == "grpcMethodType" and isinstance(value, str):
        marker = "api.GrpcMethodType"
        if value.startswith(marker):
            return GRPC_METHOD_TYPES.get(value[len(marker):], value)
    if key == "httpMethodType" and isinstance(value, str):
        marker = "api.HTTPMethodType"
        if value.startswith(marker):
            return value[len(marker):]
    return value


def snapshot_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def difference_paths(left: Any, right: Any, path: str = "") -> list[str]:
    if type(left) is not type(right):
        return [path or "/"]
    if isinstance(left, dict):
        differences: list[str] = []
        for child in sorted(set(left) | set(right)):
            child_path = f"{path}/{child}"
            if child not in left or child not in right:
                differences.append(child_path)
            else:
                differences.extend(difference_paths(left[child], right[child], child_path))
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            return [path or "/"]
        differences = []
        for index, (left_child, right_child) in enumerate(zip(left, right)):
            differences.extend(
                difference_paths(left_child, right_child, f"{path}/{index}")
            )
        return differences
    return [] if left == right else [path or "/"]


def check_service(root: Path, service: str) -> dict[str, object]:
    go_root = root / "goexample" / service
    boost_root = root / "cppboostexample" / service
    typescript_root = root / "tsexample" / service
    errors: list[str] = []
    identical: dict[str, dict[str, bool]] = {}
    for relative in ("config/config.yaml", "config/overrides.yaml"):
        go_data = (go_root / relative).read_bytes()
        boost_data = (boost_root / relative).read_bytes()
        typescript_data = (typescript_root / relative).read_bytes()
        identical[relative] = {
            "cppboost": go_data == boost_data,
            "typescript": go_data == typescript_data,
        }
        if go_data != boost_data:
            errors.append(f"{service}:{relative}:Go/Boost bytes differ")
        if go_data != typescript_data:
            errors.append(f"{service}:{relative}:Go/TypeScript bytes differ")

    config_text = (boost_root / "config/config.yaml").read_text()
    unresolved = unresolved_override_paths(
        config_text, (boost_root / "config/overrides.yaml").read_text()
    )
    for path in unresolved:
        errors.append(f"{service}:config/overrides.yaml:missing value for {path}")
    cpp = (boost_root / "config/config.generated.hpp").read_text()
    go = (go_root / "internal/config/config.generated.go").read_text()
    typescript = (
        typescript_root / "src/internal/config/config.generated.ts"
    ).read_text()
    go_snapshot = normalize_snapshot(go_default_snapshot(go))
    typescript_snapshot = normalize_snapshot(typescript_default_snapshot(typescript))
    snapshot_equal = go_snapshot == typescript_snapshot
    snapshot_differences = difference_paths(go_snapshot, typescript_snapshot)
    if not snapshot_equal:
        errors.append(
            f"{service}:normalized Go/TypeScript default snapshots differ at "
            + ", ".join(snapshot_differences[:10])
        )
    patch_results: list[dict[str, object]] = []
    for patch in patches(config_text):
        cpp_target = (
            f"config.{patch.section}.{patch.object_name}.{patch.member} ="
        )
        cpp_property = (
            f'config.{patch.section}.{patch.object_name}.properties['
            f'"{patch.member}"]'
        )
        go_target = (
            f"c.{upper_first(patch.section)}."
            f"{upper_first(patch.object_name)}.{upper_first(patch.member)} ="
        )
        environment = env_name(patch.variable)
        typescript_path = re.compile(
            rf'environment: "{re.escape(environment)}", path: \['
            rf'"{re.escape(patch.section)}", '
            rf'"{re.escape(patch.object_name)}", '
            rf'"{re.escape(patch.member)}",\s*\]'
        )
        result = {
            "path": f"{patch.section}.{patch.object_name}.{patch.member}",
            "variable": patch.variable,
            "environment": environment,
            "cpp_direct_assignments": cpp.count(cpp_target),
            "cpp_property_assignments": cpp.count(cpp_property),
            "go_direct_assignments": go.count(go_target),
            "cpp_environment_mentions": cpp.count(f'"{environment}"'),
            "go_environment_mentions": go.count(f'"{environment}"'),
            "typescript_path_assignments": len(typescript_path.findall(typescript)),
            "typescript_environment_mentions": typescript.count(f'"{environment}"'),
        }
        if result["cpp_direct_assignments"] < 2:
            errors.append(
                f"{service}:{result['path']}:missing C++ YAML/env typed assignments"
            )
        if result["cpp_property_assignments"] != 0:
            errors.append(
                f"{service}:{result['path']}:canonical member routed through properties"
            )
        if result["go_direct_assignments"] < 1:
            errors.append(f"{service}:{result['path']}:missing Go env typed assignment")
        if result["cpp_environment_mentions"] < 1:
            errors.append(f"{service}:{result['path']}:missing C++ environment patch")
        if result["go_environment_mentions"] < 1:
            errors.append(f"{service}:{result['path']}:missing Go environment patch")
        if result["typescript_path_assignments"] != 1:
            errors.append(
                f"{service}:{result['path']}:expected one TypeScript typed path patch"
            )
        if result["typescript_environment_mentions"] != 1:
            errors.append(
                f"{service}:{result['path']}:expected one TypeScript environment patch"
            )
        patch_results.append(result)

    return {
        "identical_files": identical,
        "patch_count": len(patch_results),
        "patches": patch_results,
        "unresolved_override_paths": unresolved,
        "normalized_default_snapshot": {
            "equal": snapshot_equal,
            "go_sha256": snapshot_digest(go_snapshot),
            "typescript_sha256": snapshot_digest(typescript_snapshot),
            "differences": snapshot_differences,
        },
        "errors": errors,
    }


def check_automation_service(root: Path) -> dict[str, object]:
    implementations = (
        "goexample",
        "cppexample",
        "cppboostexample",
        "pyexample",
        "rustexample",
        "tsexample",
    )
    baseline = root / implementations[0] / "automationservice" / "config"
    config = (baseline / "config.yaml").read_bytes()
    override = (baseline / "overrides.yaml").read_bytes()
    identical: dict[str, dict[str, bool]] = {}
    errors: list[str] = []
    for implementation in implementations[1:]:
        config_root = root / implementation / "automationservice" / "config"
        config_equal = (config_root / "config.yaml").read_bytes() == config
        override_equal = (config_root / "overrides.yaml").read_bytes() == override
        identical[implementation] = {
            "config/config.yaml": config_equal,
            "config/overrides.yaml": override_equal,
        }
        if not config_equal:
            errors.append(
                "automationservice:config/config.yaml:"
                f"Go/{implementation} bytes differ"
            )
        if not override_equal:
            errors.append(
                "automationservice:config/overrides.yaml:"
                f"Go/{implementation} bytes differ"
            )
    unresolved = unresolved_override_paths(config.decode(), override.decode())
    for path in unresolved:
        errors.append(
            "automationservice:config/overrides.yaml:missing value for " + path
        )
    return {
        "identical_files": identical,
        "unresolved_override_paths": unresolved,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Differentially verify generated Go, Boost and TypeScript "
            "typed configuration."
        )
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_ARTIFACT)
    args = parser.parse_args()
    root = args.root.resolve()
    required = tuple(
        root / implementation / service
        for implementation in (
            "goexample", "cppexample", "cppboostexample",
            "pyexample", "rustexample", "tsexample",
        )
        for service in ("automationservice",)
    ) + tuple(
        root / implementation / service
        for implementation in ("goexample", "cppboostexample", "tsexample")
        for service in ("analyticsservice", "inventoryservice", "orderservice")
    )
    missing = [str(path) for path in required if not path.is_dir()]
    if missing:
        print("missing conformance input: " + ", ".join(missing), file=sys.stderr)
        return 2

    services = {
        service: check_service(root, service)
        for service in ("analyticsservice", "inventoryservice", "orderservice")
    }
    automation = check_automation_service(root)
    errors = [error for result in services.values() for error in result["errors"]]
    errors.extend(automation["errors"])
    summary = {
        "status": "pass" if not errors else "fail",
        "services": services,
        "automation_service": automation,
        "total_typed_patches": sum(
            int(result["patch_count"]) for result in services.values()
        ),
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
