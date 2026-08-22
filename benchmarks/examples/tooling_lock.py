from __future__ import annotations

import atexit
import os
from pathlib import Path


LOCK_ENV = "SERVICELIB_TOOLING_LOCK_HELD"


def acquire() -> None:
    if os.environ.get(LOCK_ENV) == "1":
        return
    lock = Path(os.environ.get("TMPDIR", "/tmp")) / "servicelib-tooling.lock"

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    for _ in range(2):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            try:
                owner = int((lock / "pid").read_text().strip())
            except (OSError, ValueError):
                owner = 0
            if owner and alive(owner):
                raise RuntimeError(
                    "another ServiceLib benchmark/profiling/conformance run "
                    f"is active (pid {owner}); run these tools sequentially"
                )
            try:
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass
    else:
        raise RuntimeError(
            f"ServiceLib tooling lock is busy: {lock}; "
            "run benchmark, profiling and conformance sequentially"
        )

    (lock / "pid").write_text(f"{os.getpid()}\n")
    os.environ[LOCK_ENV] = "1"

    def release() -> None:
        try:
            (lock / "pid").unlink(missing_ok=True)
            lock.rmdir()
        except OSError:
            pass

    atexit.register(release)
