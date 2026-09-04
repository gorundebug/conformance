from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependency_environment


class DependencyEnvironmentTest(unittest.TestCase):
    def test_dependency_command_retries_without_changing_environment(self) -> None:
        expected_environment = {"GOPROXY": "http://proxy.invalid/go"}
        failed = mock.Mock()
        failed.stdout = iter(["failed to connect to proxy\n"])
        failed.wait.return_value = 1
        completed = mock.Mock()
        completed.stdout = iter(["installed\n"])
        completed.wait.return_value = 0
        with (
            mock.patch(
                "dependency_environment.subprocess.Popen",
                side_effect=[failed, completed],
            ) as popen,
            mock.patch("dependency_environment.time.sleep") as sleep,
        ):
            result = dependency_environment.run_dependency_command(
                ["go", "install"],
                cwd=Path("/tmp"),
                env=expected_environment,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs["env"], expected_environment)
        sleep.assert_called_once_with(2.0)

    def test_dependency_command_does_not_retry_build_failure(self) -> None:
        failed = mock.Mock()
        failed.stdout = iter(["ld: undefined reference to symbol\n"])
        failed.wait.return_value = 1
        with (
            mock.patch(
                "dependency_environment.subprocess.Popen",
                return_value=failed,
            ) as popen,
            mock.patch("dependency_environment.time.sleep") as sleep,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                dependency_environment.run_dependency_command(
                    ["make", "build"],
                    cwd=Path("/tmp"),
                    env={},
                )

        popen.assert_called_once()
        sleep.assert_not_called()

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

    def test_direct_docker_run_receives_container_reachable_proxy_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory)
            scripts = framework / "scripts"
            scripts.mkdir()
            (scripts / "dependency-proxy-env.sh").write_text(
                'export DEPENDENCY_PROXY_DIR="/cache"\n'
                'export DEPENDENCY_GITHUB_RAW_URL="http://localhost:18081/repository/github-raw"\n'
                'export PIP_INDEX_URL="http://localhost:18081/repository/pypi-proxy/simple"\n'
                'export DEPENDENCY_CONAN_CREDENTIAL_FILE="/host/secret"\n'
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                arguments = dependency_environment.docker_arguments(framework)
            self.assertEqual(arguments[:2], [
                "--add-host", "host.docker.internal:host-gateway",
            ])
            rendered = " ".join(arguments)
            self.assertIn(
                "DEPENDENCY_GITHUB_RAW_URL=http://host.docker.internal:18081/repository/github-raw",
                rendered,
            )
            self.assertIn(
                "PIP_INDEX_URL=http://host.docker.internal:18081/repository/pypi-proxy/simple",
                rendered,
            )
            self.assertNotIn("DEPENDENCY_CONAN_CREDENTIAL_FILE", rendered)

    def test_generated_project_is_the_direct_runner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            scripts = project / "scripts"
            scripts.mkdir()
            script = scripts / "docker-dependency-proxy.generated.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "test \"$1\" = --print-environment\n"
                "export NPM_CONFIG_REGISTRY=mirror/npm\n"
                "env -0\n"
            )
            script.chmod(0o755)
            with mock.patch.dict(os.environ, {"BASE_VALUE": "kept"}, clear=True):
                environment = dependency_environment.from_project(project)
            self.assertEqual(environment["BASE_VALUE"], "kept")
            self.assertEqual(environment["NPM_CONFIG_REGISTRY"], "mirror/npm")

    def test_container_contract_is_translated_for_host_dependency_commands(self) -> None:
        environment = {
            "DEPENDENCY_PROXY_DIR": "/cache",
            "GOPROXY": "http://host.docker.internal:18081/repository/go-proxy/",
            "PIP_TRUSTED_HOST": "host.docker.internal",
            "GIT_CONFIG_KEY_0": (
                "url.http://host.docker.internal:18084/cgi-bin/git/github.com/.insteadOf"
            ),
            "UNRELATED": "kept",
        }

        host_environment = dependency_environment.for_host(environment)

        self.assertEqual(
            host_environment["GOPROXY"],
            "http://localhost:18081/repository/go-proxy/",
        )
        self.assertEqual(host_environment["PIP_TRUSTED_HOST"], "localhost")
        self.assertEqual(
            host_environment["GIT_CONFIG_KEY_0"],
            "url.http://localhost:18084/cgi-bin/git/github.com/.insteadOf",
        )
        self.assertEqual(host_environment["UNRELATED"], "kept")


if __name__ == "__main__":
    unittest.main()
