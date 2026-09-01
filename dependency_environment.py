"""Load one generated dependency environment at direct runner boundaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def from_framework(framework: Path) -> dict[str, str]:
    """Return the framework-owned environment for a direct subprocess.

    Generated Make targets already load this contract themselves.  Conformance
    runners that invoke Docker directly must do the same so proxy and direct
    modes have exactly one source of dependency URLs and pinned Git contexts.
    """
    script = framework / "scripts" / "dependency-proxy-env.sh"
    if not script.is_file():
        raise RuntimeError(f"dependency environment is missing: {script}")
    process = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'source "$1"; env -0',
            "framework-dependency-environment",
            str(script),
        ],
        check=True,
        capture_output=True,
        env=os.environ,
    )
    environment: dict[str, str] = {}
    for entry in process.stdout.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        name, value = entry.split(b"=", 1)
        environment[name.decode()] = value.decode()
    return environment
