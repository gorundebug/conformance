from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run import IGNORED_PARTS, module_directories


class DependencyManifestTests(unittest.TestCase):
    def test_module_directories_ignore_generated_build_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "service/go.mod", "api/go.mod", "dist/stale/go.mod",
                "build/probe/go.mod", "node_modules/tool/go.mod",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("module example.invalid/module\n")
            self.assertEqual(module_directories(root), [root / "api", root / "service"])
            self.assertIn("dist", IGNORED_PARTS)


if __name__ == "__main__":
    unittest.main()
