#!/usr/bin/env python3
"""Compare every typed Go configuration field with all typed runtimes."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


CONFORMANCE_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("DEPENDENCIES_DIR", CONFORMANCE_DIR.parent)).expanduser().resolve()
ARTIFACT = CONFORMANCE_DIR / ".artifacts" / "config-schema" / "summary.json"
GO_FILES = (
    "config.go",
    "dataconnector_types.go",
    "endpoint_types.go",
    "link_types.go",
    "stream_types.go",
    "type_types.go",
)
CPP_FILES = (
    "config.hpp",
    "dataconnector_types.hpp",
    "endpoint_types.hpp",
    "link_types.hpp",
    "stream_types.hpp",
    "type_types.hpp",
)
TYPESCRIPT_STRUCTURAL_CONFIGS = {
    "CanonicalConfig",
    "DataConnectorConfig",
    "EndpointConfig",
    "FunctionConfig",
    "StreamConfig",
}


def go_structs() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    base = ROOT / "servicelib" / "runtime" / "config"
    for filename in GO_FILES:
        source = (base / filename).read_text()
        for match in re.finditer(
            r"^type\s+(\w+)\s+struct\s*\{(.*?)^\}",
            source,
            re.MULTILINE | re.DOTALL,
        ):
            name, body = match.groups()
            if not (name.endswith("Config") or name == "CallSemanticsGroup"):
                continue
            if name == "RuntimeConfig":
                continue
            fields: set[str] = set()
            for yaml_tag in re.findall(r'`[^`]*yaml:"([^" ]+)[^`]*`', body):
                key = yaml_tag.split(",", 1)[0]
                if key:
                    fields.add(key)
            result[name] = fields
    return result


def cpp_structs(runtime: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    base = ROOT / runtime / "include" / "servicelib" / "runtime" / "config"
    for filename in CPP_FILES:
        source = (base / filename).read_text()
        for match in re.finditer(
            r"^struct\s+(\w+)\s*\{(.*?)^\};",
            source,
            re.MULTILINE | re.DOTALL,
        ):
            name, body = match.groups()
            if not (name.endswith("Config") or name == "CallSemanticsGroup"):
                continue
            fields: set[str] = set()
            depth = 0
            declaration = ""
            for line in body.splitlines():
                line = line.split("//", 1)[0].strip()
                if depth == 0 and line:
                    declaration = line
                elif declaration:
                    declaration += " " + line
                depth += line.count("{") - line.count("}")
                if depth == 0 and declaration and "(" not in declaration and declaration.endswith(";"):
                    field = re.search(
                        r"\b([A-Za-z_]\w*)\s*(?:\{.*\})?;$", declaration
                    )
                    if field and field.group(1) != "properties":
                        fields.add(field.group(1))
                if declaration.endswith(";"):
                    declaration = ""
            if name == "TemporalDataConnectorConfig" and "namespaceName" in fields:
                fields.remove("namespaceName")
                fields.add("namespace")
            result[name] = fields
    return result


def typescript_structs() -> tuple[dict[str, set[str]], set[str]]:
    source = (
        ROOT / "tsservicelib" / "src" / "runtime" / "config" / "types.ts"
    ).read_text()
    own_fields: dict[str, set[str]] = {}
    parents: dict[str, tuple[str, ...]] = {}
    for match in re.finditer(
        r"^export interface\s+(\w+)(?:\s+extends\s+([^\{]+))?\s*\{(.*?)^\}",
        source,
        re.MULTILINE | re.DOTALL,
    ):
        name, inherited, body = match.groups()
        own_fields[name] = set(
            re.findall(r"^\s*readonly\s+([A-Za-z_]\w*)\??\s*:", body, re.MULTILINE)
        )
        parents[name] = tuple(
            value.strip() for value in (inherited or "").split(",") if value.strip()
        )

    resolved: dict[str, set[str]] = {}

    def fields(name: str, stack: tuple[str, ...] = ()) -> set[str]:
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise RuntimeError(f"cyclic TypeScript config inheritance: {' -> '.join(stack + (name,))}")
        result = set(own_fields.get(name, set()))
        for parent in parents.get(name, ()):
            result.update(fields(parent, stack + (name,)))
        result.discard("properties")
        resolved[name] = result
        return result

    for name in own_fields:
        fields(name)

    # Type aliases used to express zero-field and intersection configs
    # idiomatically in TypeScript still represent the same Go config structs.
    resolved["CustomDataConnectorConfig"] = fields("DataConnectorConfig")
    resolved["CustomEndpointConfig"] = fields("EndpointConfig") | fields("FunctionConfig")
    resolved["ParallelCallSemanticsConfig"] = set()
    resolved["CallSemanticsGroup"] = {
        "functionCall",
        "taskPool",
        "priorityTaskPool",
        "parallelCall",
    }

    public_configs = {
        name
        for name in own_fields
        if name.endswith("Config")
    }
    public_configs.update(
        {
            "CustomDataConnectorConfig",
            "CustomEndpointConfig",
            "ParallelCallSemanticsConfig",
        }
    )
    return resolved, public_configs


def normalized_go_fields(name: str, fields: set[str]) -> set[str]:
    if name == "CallSemanticsGroup" and "function" in fields:
        return fields - {"function"} | {"functionCall"}
    return fields


def normalized_typescript_fields(
    name: str, fields: set[str], expected: set[str]
) -> set[str]:
    result = set(fields)
    # TypeScript discriminated unions are the idiomatic equivalent of Go's
    # GetType methods; the discriminator is not an additional YAML field in a
    # concrete Go config struct.
    if name.endswith("StreamConfig") or name.endswith("DataConnectorConfig"):
        result.discard("type")
    # StreamConfig exposes both source accessors just as the Go interface does;
    # each concrete config retains only the source shape valid for that node.
    for field in ("idSource", "idSources"):
        if field not in expected:
            result.discard(field)
    return result


def main() -> int:
    go = go_structs()
    typescript, typescript_public = typescript_structs()
    runtimes = {
        "cppservicelib": cpp_structs("cppservicelib"),
        "cppboostservicelib": cpp_structs("cppboostservicelib"),
        "tsservicelib": typescript,
    }
    errors: list[str] = []
    matrix: dict[str, object] = {}
    for name in sorted(go):
        expected = normalized_go_fields(name, go[name])
        entry: dict[str, object] = {"go_fields": sorted(go[name])}
        for runtime, structs in runtimes.items():
            actual = structs.get(name)
            if actual is not None and runtime == "tsservicelib":
                actual = normalized_typescript_fields(name, actual, expected)
            missing = sorted(expected - (actual or set()))
            extra = sorted((actual or set()) - expected)
            entry[runtime] = {
                "fields": sorted(actual or set()),
                "missing": missing,
                "extra": extra,
            }
            if actual is None:
                errors.append(f"{runtime}: missing {name}")
            elif missing or extra:
                errors.append(
                    f"{runtime}:{name}: missing={missing}, extra={extra}"
                )
        matrix[name] = entry

    for runtime, structs in runtimes.items():
        candidates = set(structs)
        if runtime == "tsservicelib":
            candidates = typescript_public - TYPESCRIPT_STRUCTURAL_CONFIGS
        unexpected = sorted(candidates - set(go))
        if unexpected:
            errors.append(f"{runtime}: unexpected typed structs {unexpected}")

    summary = {
        "status": "pass" if not errors else "fail",
        "typed_structs": len(go),
        "typed_fields": sum(len(fields) for fields in go.values()),
        "matrix": matrix,
        "errors": errors,
    }
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"Typed config schema conformance {summary['status']}: "
        f"{summary['typed_structs']} structs, {summary['typed_fields']} fields"
    )
    for error in errors:
        print(error, file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
