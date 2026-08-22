#!/usr/bin/env python3
"""Expose the shared conformance profile-workspace implementation."""

from __future__ import annotations

import runpy
from pathlib import Path


_IMPLEMENTATION = Path(__file__).resolve().parents[1] / "profile_workspace.py"
_NAMESPACE = runpy.run_path(str(_IMPLEMENTATION))

# Keep the profiler's former import/runpy surface without maintaining a second
# copy of profile generation semantics.
globals().update({
    name: value
    for name, value in _NAMESPACE.items()
    if not name.startswith("__")
})


if __name__ == "__main__":
    raise SystemExit(_NAMESPACE["main"]())
