from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependency_environment


class FrameworkEnvironmentTest(unittest.TestCase):
    def test_framework_script_supplies_buildkit_source_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory)
            script = framework / "scripts" / "dependency-proxy-env.sh"
            script.parent.mkdir()
            script.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ -n \"${DEPENDENCY_PROXY_DIR:-}\" ]]; then\n"
                "  export USERVER_SOURCE_CONTEXT=\"${DEPENDENCY_GIT_MIRROR_URL}/"
                "github.com/userver-framework/userver.git#pinned\"\n"
                "fi\n"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "DEPENDENCY_PROXY_DIR": "/cache",
                    "DEPENDENCY_GIT_MIRROR_URL": "http://mirror/cgi-bin/git",
                },
                clear=True,
            ):
                environment = dependency_environment.from_framework(framework)

        self.assertEqual(
            environment["USERVER_SOURCE_CONTEXT"],
            "http://mirror/cgi-bin/git/github.com/userver-framework/"
            "userver.git#pinned",
        )

    def test_framework_script_does_not_enable_proxy_implicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory)
            script = framework / "scripts" / "dependency-proxy-env.sh"
            script.parent.mkdir()
            script.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ -n \"${DEPENDENCY_PROXY_DIR:-}\" ]]; then\n"
                "  export USERVER_SOURCE_CONTEXT=unexpected\n"
                "fi\n"
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                environment = dependency_environment.from_framework(framework)

        self.assertNotIn("USERVER_SOURCE_CONTEXT", environment)


if __name__ == "__main__":
    unittest.main()
