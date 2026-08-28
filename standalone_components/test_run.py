from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from standalone_components import run


class StandaloneComponentTest(unittest.TestCase):
    def test_docker_images_use_proxy_registry_only_when_enabled(self) -> None:
        with mock.patch.dict(run.os.environ, {}, clear=True):
            self.assertEqual(
                run.dependency_docker_image("library/node:24"),
                "docker.io/library/node:24",
            )
        with mock.patch.dict(
            run.os.environ,
            {
                "DEPENDENCY_PROXY_DIR": "/cache",
                "DEPENDENCY_PROXY_HOST": "proxy.example",
                "DEPENDENCY_PROXY_DOCKER_PORT": "19000",
            },
            clear=True,
        ):
            self.assertEqual(
                run.dependency_docker_image("library/node:24"),
                "proxy.example:19000/library/node:24",
            )

    def test_docker_process_environment_uses_container_proxy_host(self) -> None:
        with mock.patch.dict(
            run.os.environ,
            {
                "DEPENDENCY_PROXY_DIR": "/cache",
                "DEPENDENCY_PROXY_HOST": "localhost",
                "DEPENDENCY_PROXY_DOCKER_HOST": "host.docker.internal",
                "DEPENDENCY_APT_UBUNTU_PORTS_URL": (
                    "http://localhost:18081/repository/apt-ubuntu-ports"
                ),
                "DEPENDENCY_CONAN_REMOTE_URL": (
                    "http://localhost:18081/repository/conan-proxy"
                ),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": (
                    "url.http://localhost:18084/cgi-bin/git/github.com/.insteadOf"
                ),
                "GIT_CONFIG_VALUE_0": "https://github.com/",
            },
            clear=True,
        ):
            environment = run.docker_process_environment({"UNCHANGED": "yes"})

        self.assertEqual(environment["UNCHANGED"], "yes")
        self.assertEqual(
            environment["DEPENDENCY_APT_UBUNTU_PORTS_URL"],
            "http://host.docker.internal:18081/repository/apt-ubuntu-ports",
        )
        self.assertEqual(
            environment["DEPENDENCY_CONAN_REMOTE_URL"],
            "http://host.docker.internal:18081/repository/conan-proxy",
        )
        self.assertIn("host.docker.internal:18084", environment["GIT_CONFIG_KEY_0"])
        self.assertEqual(environment["PIP_TRUSTED_HOST"], "host.docker.internal")

    def test_declared_module_matrix_is_complete(self) -> None:
        self.assertEqual(set(run.DECLARED_MODULES), set(run.COMPONENTS))
        self.assertEqual(set(run.SERVICES) | set(run.MODULES), set(run.COMPONENTS))
        self.assertEqual(
            set(run.LANGUAGES),
            {"go", "cpp", "cppboost", "python", "rust", "typescript"},
        )

    def test_copy_source_removes_git_and_build_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / ".git").mkdir(parents=True)
            (source / "build").mkdir()
            (source / "src").mkdir()
            (source / "src" / "value.txt").write_text("value")
            target = root / "target"
            run.copy_source(source, target)

            self.assertTrue((target / "src" / "value.txt").is_file())
            self.assertFalse((target / ".git").exists())
            self.assertFalse((target / "build").exists())
            run.assert_plain_filesystem_tree(target)

    def test_plain_tree_rejects_nested_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "component" / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "Git metadata"):
                run.assert_plain_filesystem_tree(root)

    def test_go_workspace_contains_only_declared_local_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "goexample"
            for name in (*run.COMPONENTS, "unusedmodule"):
                physical_name = run.component_directory("go", name)
                (example / physical_name).mkdir(parents=True)
                (example / physical_name / "go.mod").write_text(
                    f"module github.com/gorundebug/{physical_name}\n\ngo 1.99.2\n"
                )
            (example / "go.work").write_text("go 1.99.2\n")
            (root / "servicelib").mkdir()
            (root / "servicelib" / "go.mod").write_text(
                "module github.com/gorundebug/servicelib\n\ngo 1.99.2\n"
            )
            target = root / "isolated"
            run.materialize_component(root, "go", "analyticsservice", target)

            workspace = (target / "go.work").read_text()
            self.assertIn("go 1.99.2", workspace)
            self.assertIn("./analyticsservice", workspace)
            self.assertIn("./model_go", workspace)
            self.assertIn("./servicelib", workspace)
            self.assertNotIn("unusedmodule", workspace)
            self.assertFalse((target / "inventory_service_api").exists())

    def test_unsupported_temporal_language_uses_generated_go_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            automation = root / "cppexample" / "automationservice"
            automation.mkdir(parents=True)
            (automation / "go.mod").write_text(
                "module example/automationservice\n\ngo 1.99.2\n"
            )

            self.assertEqual(
                run.implementation_language(root, "cpp", "automationservice"),
                "go",
            )

    def test_rust_workspace_patches_only_declared_local_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "rustexample"
            example.mkdir()
            (example / "Cargo.toml").write_text(
                '[workspace]\nresolver = "2"\nmembers = [\n'
                '    "analyticsservice",\n    "model_rust",\n'
                '    "inventory_service_api",\n]\n\n'
                '[patch."https://github.com/gorundebug/rustexample.git"]\n'
                'inventory-service-api = { path = "inventory_service_api" }\n'
                'example-model = { path = "model_rust" }\n'
                'order-service-api = { path = "order_service_api" }\n'
            )
            service = example / "analyticsservice"
            service.mkdir()
            (service / "Cargo.toml").write_text(
                '[package]\nname = "analyticsservice"\nversion = "0.1.0"\n'
                '[dependencies]\n'
                'servicelib-gorundebug = { git = "https://example", tag = "v1" }\n'
            )
            model = example / "model_rust"
            model.mkdir()
            (model / "Cargo.toml").write_text(
                '[package]\nname = "example-model"\nversion = "0.1.0"\n'
            )
            framework = root / "rustservicelib"
            framework.mkdir()
            (framework / "Cargo.toml").write_text(
                '[package]\nname = "servicelib-gorundebug"\nversion = "0.1.0"\n'
            )

            target = root / "isolated"
            run.materialize_component(root, "rust", "analyticsservice", target)
            workspace = (target / "Cargo.toml").read_text()

            self.assertIn('example-model = { path = "model_rust" }', workspace)
            self.assertNotIn("inventory-service-api", workspace)
            self.assertNotIn("order-service-api", workspace)

    def test_python_service_replaces_workspace_module_with_local_fetch_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "pyexample"
            service = example / "analyticsservice"
            service.mkdir(parents=True)
            (service / "pyproject.toml").write_text(
                '[project]\nname = "analytics-service"\nversion = "0.1.0"\n'
                'dependencies = ["pyservicelib-gorundebug", "model"]\n'
                '[tool.uv.sources]\n'
                'pyservicelib-gorundebug = '
                '{ git = "https://example", tag = "v1" }\n'
                'model = { workspace = true }\n'
            )
            model = example / "model_python"
            model.mkdir()
            (model / "pyproject.toml").write_text(
                '[project]\nname = "model"\nversion = "0.1.0"\n'
            )
            framework = root / "pyservicelib"
            framework.mkdir()
            (framework / "pyproject.toml").write_text(
                '[project]\nname = "pyservicelib-gorundebug"\nversion = "0.1.0"\n'
            )

            target = root / "isolated"
            run.materialize_component(root, "python", "analyticsservice", target)
            manifest = (target / "analyticsservice" / "pyproject.toml").read_text()

            self.assertIn(
                'model = { path = ".local-dependencies/model_python" }',
                manifest,
            )
            self.assertTrue(
                (target / "analyticsservice" / ".servicegen" /
                 "dependencies" / "model_python").is_dir()
            )

    def test_typescript_override_preserves_public_package_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "tsexample"
            (example / "package.json").parent.mkdir(parents=True)
            (example / "package.json").write_text('{"name":"root"}\n')
            wrapper = example / "dependency-download-env.generated.sh"
            wrapper.write_text("#!/bin/sh\nexec env \"$@\"\n")
            wrapper.chmod(0o755)
            service = example / "analyticsservice"
            service.mkdir()
            package = {
                "name": "@gorundebug/tsexample-analyticsservice",
                "dependencies": {
                    "@gorundebug/tsservicelib": "github:x/y#v1",
                    "@gorundebug/model": "git+https://example/model#v1",
                },
            }
            (service / "package.json").write_text(json.dumps(package))
            model = example / "model_ts"
            model.mkdir()
            (model / "package.json").write_text('{"name":"@gorundebug/model"}\n')
            framework = root / "tsservicelib"
            framework.mkdir()
            (framework / "package.json").write_text(
                '{"name":"@gorundebug/tsservicelib"}\n'
            )

            target = root / "isolated"
            run.materialize_component(root, "typescript", "analyticsservice", target)
            isolated = json.loads((target / "analyticsservice" / "package.json").read_text())
            self.assertEqual(
                isolated["dependencies"]["@gorundebug/tsservicelib"],
                "workspace:*",
            )
            self.assertEqual(
                isolated["dependencies"]["@gorundebug/model"],
                "workspace:*",
            )
            self.assertEqual(
                isolated["name"], "@gorundebug/tsexample-analyticsservice"
            )
            self.assertIn(
                "  '@swc/core': true",
                (target / "pnpm-workspace.yaml").read_text(),
            )
            self.assertIn(
                '  - "model_ts"',
                (target / "pnpm-workspace.yaml").read_text(),
            )

    def test_component_directory_suffixes_only_language_specific_modules(self) -> None:
        self.assertEqual(run.component_directory("go", "model"), "model_go")
        self.assertEqual(run.component_directory("cppboost", "model"), "model_cpp")
        self.assertEqual(run.component_directory("typescript", "model"), "model_ts")
        self.assertEqual(
            run.component_directory("rust", "inventory_service_api"),
            "inventory_service_api",
        )
        self.assertEqual(
            run.component_directory("python", "order_service_api"),
            "order_service_api",
        )

    def test_docker_run_proxy_arguments_apply_one_container_contract(self) -> None:
        with mock.patch.dict(
            run.os.environ,
            {
                "DEPENDENCY_PROXY_DIR": "/cache",
                "DEPENDENCY_PROXY_HOST": "localhost",
                "DEPENDENCY_PROXY_DOCKER_HOST": "host.docker.internal",
                "GOPROXY": "http://localhost:18081/repository/go-proxy/",
                "GOSUMDB": "off",
                "NPM_CONFIG_REGISTRY": (
                    "http://localhost:18081/repository/npm-proxy/"
                ),
            },
            clear=True,
        ), mock.patch.object(
            run.dependency_download_mirrors, "docker_environment", return_value={}
        ):
            arguments = run.docker_run_proxy_arguments()

        self.assertEqual(
            arguments[:2], ["--add-host", "host.docker.internal:host-gateway"]
        )
        self.assertIn(
            "GOPROXY=http://host.docker.internal:18081/repository/go-proxy/",
            arguments,
        )
        self.assertIn("GOSUMDB=off", arguments)
        self.assertIn(
            "COREPACK_NPM_REGISTRY="
            "http://host.docker.internal:18081/repository/npm-proxy/",
            arguments,
        )

    def test_diagnostic_summary_is_separate_from_authoritative_summary(self) -> None:
        self.assertNotEqual(run.SUMMARY, run.DIAGNOSTIC_SUMMARY)
        self.assertEqual(run.SUMMARY.name, "summary.json")
        self.assertEqual(run.DIAGNOSTIC_SUMMARY.name, "diagnostic-summary.json")

    def test_userver_context_is_only_set_for_an_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"USERVER_SOURCE_CONTEXT": "/stale/missing/userver"}

            run.configure_userver_source_context(environment, root)
            self.assertNotIn("USERVER_SOURCE_CONTEXT", environment)

            (root / "userver").mkdir()
            run.configure_userver_source_context(environment, root)
            self.assertEqual(
                environment["USERVER_SOURCE_CONTEXT"], str(root / "userver")
            )

    def test_python_service_uses_generated_dependency_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            component = target / "inventoryservice"
            contract = (
                component / ".servicegen" / "dependencies"
                / "inventory_service_api"
            )
            contract.mkdir(parents=True)
            (contract / "generate.generated.sh").write_text("#!/bin/sh\n")

            commands: list[list[str]] = []

            def record(command: list[str], *_args: object, **_kwargs: object) -> None:
                commands.append(command)

            with mock.patch.object(run, "run_command", side_effect=record):
                run.build_python(target, "inventoryservice")

            rendered = [" ".join(command) for command in commands]
            self.assertIn(
                "LOCAL_DEPENDENCIES_DIR="
                "/workspace/.servicegen/dependencies",
                rendered[0],
            )
            self.assertIn(
                "./scripts/fetch-dependencies.generated.sh", rendered[0]
            )
            self.assertLess(
                rendered[0].index("fetch-dependencies.generated.sh"),
                rendered[0].index("uv sync --all-extras"),
            )
            self.assertIn("uv run pytest tests", rendered[1])

if __name__ == "__main__":
    unittest.main()
