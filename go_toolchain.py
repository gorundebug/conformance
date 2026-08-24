"""Single Go toolchain-version reader for conformance-owned workspaces."""

from __future__ import annotations

import re
from pathlib import Path


GO_DIRECTIVE = re.compile(r"(?m)^go\s+(\d+\.\d+(?:\.\d+)?)\s*$")


def workspace_version(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"Go workspace is missing: {path}")
    match = GO_DIRECTIVE.search(path.read_text())
    if match is None:
        raise RuntimeError(f"Go workspace has no valid go directive: {path}")
    return match.group(1)


def example_version(root: Path) -> str:
    """Read the version generated from the canonical DSL; never duplicate it."""
    return workspace_version(root / "goexample" / "go.work")


def docker_image(root: Path) -> str:
    """Return the Go toolchain image matching the canonical generated workspace."""
    return f"golang:{example_version(root)}-bookworm"


def render_workspace(version: str, modules: list[Path] | tuple[Path, ...]) -> str:
    body = "".join(f"\t{module.resolve()}\n" for module in modules)
    return f"go {version}\n\nuse (\n{body})\n"
