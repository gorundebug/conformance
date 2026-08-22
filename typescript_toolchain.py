"""Deterministic host-side TypeScript toolchain commands for conformance."""

from __future__ import annotations

import os
from pathlib import Path


CONFORMANCE_DIR = Path(__file__).resolve().parent
PNPM_STORE = CONFORMANCE_DIR / ".dependencies" / ".pnpm-store" / "v11"


def environment() -> dict[str, str]:
    """Disable interactive pnpm repair prompts in unattended gates."""
    return {**os.environ, "CI": "true"}


def install_command() -> list[str]:
    return [
        "corepack",
        "pnpm",
        "install",
        "--frozen-lockfile",
        "--store-dir",
        str(PNPM_STORE),
    ]


def tsc_command(tsconfig: str, *, force: bool = False) -> list[str]:
    command = [
        "node",
        "node_modules/typescript/bin/tsc",
        "--build",
        tsconfig,
    ]
    if force:
        command.append("--force")
    return command


def copy_runtime_assets_command() -> list[str]:
    """Copy non-TypeScript runtime assets required by compiled modules."""
    return ["node", "scripts/copy-status-assets.mjs", "dist"]
