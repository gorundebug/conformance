from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from standalone_components import run


class StandaloneComponentTest(unittest.TestCase):
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
                (example / name).mkdir(parents=True)
                (example / name / "go.mod").write_text(
                    f"module github.com/gorundebug/{name}\n\ngo 1.25.4\n"
                )
            (example / "go.work").write_text("go 1.25.4\n")
            (root / "servicelib").mkdir()
            (root / "servicelib" / "go.mod").write_text(
                "module github.com/gorundebug/servicelib\n\ngo 1.25.4\n"
            )
            target = root / "isolated"
            run.materialize_component(root, "go", "analyticsservice", target)

            workspace = (target / "go.work").read_text()
            self.assertIn("./analyticsservice", workspace)
            self.assertIn("./model", workspace)
            self.assertIn("./servicelib", workspace)
            self.assertNotIn("unusedmodule", workspace)
            self.assertFalse((target / "inventory_service_api").exists())

    def test_typescript_override_preserves_public_package_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "tsexample"
            (example / "package.json").parent.mkdir(parents=True)
            (example / "package.json").write_text('{"name":"root"}\n')
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
            (service / "package.standalone.generated.json").write_text(json.dumps(package))
            model = example / "model"
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
                "SERVICEGEN_LOCAL_DEPENDENCIES_DIR="
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
