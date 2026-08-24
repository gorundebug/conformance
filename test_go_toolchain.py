from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import go_toolchain


class GoToolchainTest(unittest.TestCase):
    def test_reads_workspace_version_and_renders_it_without_a_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "go.work"
            module = root / "service"
            module.mkdir()
            workspace.write_text("go 1.99.2\n")

            version = go_toolchain.workspace_version(workspace)

            self.assertEqual(version, "1.99.2")
            self.assertEqual(
                go_toolchain.render_workspace(version, [module]),
                f"go 1.99.2\n\nuse (\n\t{module.resolve()}\n)\n",
            )

    def test_rejects_workspace_without_go_directive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "go.work"
            workspace.write_text("use ./service\n")
            with self.assertRaisesRegex(RuntimeError, "no valid go directive"):
                go_toolchain.workspace_version(workspace)


if __name__ == "__main__":
    unittest.main()
