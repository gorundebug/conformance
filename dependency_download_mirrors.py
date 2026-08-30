#!/usr/bin/env python3
"""Load the generated special-downloader catalog without duplicating routes."""

from __future__ import annotations

import os
import re
from pathlib import Path
from string import Template


CONFORMANCE = Path(__file__).resolve().parent
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)=(.+)$")


def catalog_path() -> Path:
    dependencies = Path(
        os.environ.get("DEPENDENCIES_DIR", CONFORMANCE / ".dependencies")
    )
    return dependencies / "servicegen" / "dependency-download-mirrors.generated.env"


def environment(path: Path | None = None) -> dict[str, str]:
    catalog = path or catalog_path()
    if not catalog.is_file():
        return {}
    result: dict[str, str] = {}
    for raw_line in catalog.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise RuntimeError(f"invalid dependency mirror assignment in {catalog}: {line}")
        name, value = match.groups()
        result[name] = os.environ.get(name, Template(value).safe_substitute(os.environ))
    return result


def docker_environment(path: Path | None = None) -> dict[str, str]:
    result = environment(path)
    host = os.environ.get("DEPENDENCY_PROXY_HOST", "localhost")
    docker_host = os.environ.get(
        "DEPENDENCY_PROXY_DOCKER_HOST", "host.docker.internal"
    )
    return {
        name: value.replace(f"://{host}:", f"://{docker_host}:")
        for name, value in result.items()
    }
