from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependency_environment


class DependencyEnvironmentTest(unittest.TestCase):
    def test_framework_script_is_the_single_direct_runner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory)
            scripts = framework / "scripts"
            scripts.mkdir()
            script = scripts / "dependency-proxy-env.sh"
            script.write_text('export USERVER_SOURCE_CONTEXT="mirror/context"\n')
            with mock.patch.dict(os.environ, {"BASE_VALUE": "kept"}, clear=True):
                environment = dependency_environment.from_framework(framework)
            self.assertEqual(environment["BASE_VALUE"], "kept")
            self.assertEqual(
                environment["USERVER_SOURCE_CONTEXT"], "mirror/context"
            )

    def test_missing_framework_contract_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "dependency environment"):
                dependency_environment.from_framework(Path(directory))


if __name__ == "__main__":
    unittest.main()
