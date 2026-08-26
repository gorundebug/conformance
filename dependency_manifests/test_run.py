from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run import (
    IGNORED_PARTS,
    RUST_PROJECTS,
    module_directories,
    pnpm_importer_specifiers,
    python_requirement,
)


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

    def test_rust_example_uses_the_local_framework_for_lock_validation(self) -> None:
        options = RUST_PROJECTS["rustexample"]
        self.assertIn("--config", options)
        self.assertTrue(any("../rustservicelib" in option for option in options))

    def test_python_requirement_normalizes_name_and_specifier(self) -> None:
        self.assertEqual(
            python_requirement("Example_Package[grpc] >= 1.2 ; python_version > '3.12'"),
            ("example-package", ">=1.2"),
        )

    def test_pnpm_importer_parser_reads_exact_specifiers(self) -> None:
        self.assertEqual(
            pnpm_importer_specifiers("""lockfileVersion: '9.0'
importers:

  .:
    dependencies:
      '@scope/runtime':
        specifier: 1.2.3
        version: 1.2.3
  service:
    devDependencies:
      local:
        specifier: workspace:*
        version: link:../local
packages:
"""),
            {".": {"@scope/runtime": "1.2.3"}, "service": {"local": "workspace:*"}},
        )


if __name__ == "__main__":
    unittest.main()
