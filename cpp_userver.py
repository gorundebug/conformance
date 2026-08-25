"""Shared Conan/CMake configuration for canonical userver checks."""

from __future__ import annotations


def configure_script(*, extra_args: str = "") -> str:
    suffix = f" {extra_args.strip()}" if extra_args.strip() else ""
    return (
        "./scripts/conan-install.sh Debug /workspace/build-conan/debug && "
        "conan_toolchain=$(find /workspace/build-conan/debug -type f "
        "-name conan_toolchain.cmake -print -quit) && "
        "test -n \"$conan_toolchain\" && "
        "cmake --fresh --preset docker "
        "-DCMAKE_TOOLCHAIN_FILE=\"$conan_toolchain\""
        f"{suffix}"
    )
