#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import os
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CONFORMANCE_DIR = Path(__file__).resolve().parent


class DependencyRootTest(unittest.TestCase):
    def test_profile_workspace_removes_stale_generated_files_before_snapshot(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "profile_workspace.py"))
        archive = Path("/tmp/generated-example.zip")
        self.assertEqual(
            globals_["merge_generated_command"](archive),
            [
                "bash",
                "scripts/merge.generated.sh",
                "--remove-stale",
                str(archive),
            ],
        )

    def test_generation_workspace_resolves_modules_and_inherits_profile(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "generation/run.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_module = root / "real-module"
            real_module.mkdir()
            linked_module = root / "linked-module"
            linked_module.symlink_to(real_module, target_is_directory=True)
            go_work = root / "go.work"

            globals_["write_local_go_work"](go_work, (linked_module,))

            self.assertIn(str(real_module.resolve()), go_work.read_text())
            self.assertNotIn(str(linked_module), go_work.read_text())

        with mock.patch.dict(
            os.environ, {"CONFORMANCE_EXAMPLE_PROFILE": "current"}
        ):
            self.assertEqual(globals_["active_example_profile"](), "current")

        generation_environment = {
            "GOWORK": "/tmp/conformance.go.work",
            "SERVICEGEN_EXAMPLE_PROFILE": "current",
        }
        preflight_environment = globals_["generator_preflight_environment"](
            generation_environment
        )
        self.assertNotIn("SERVICEGEN_EXAMPLE_PROFILE", preflight_environment)
        self.assertEqual(
            preflight_environment["GOWORK"], "/tmp/conformance.go.work"
        )
        self.assertEqual(
            generation_environment["SERVICEGEN_EXAMPLE_PROFILE"], "current"
        )

    def test_generation_parity_excludes_local_environment_file(self) -> None:
        generation = (CONFORMANCE_DIR / "generation/run.py").read_text()
        self.assertIn('publishable.discard(".env")', generation)

    def test_quickstart_keeps_performance_natives_outside_profile_workspace(self) -> None:
        quickstart = (CONFORMANCE_DIR / "quickstart.sh").read_text()
        native_export = (
            'export PERFORMANCE_NATIVE_DEPENDENCIES_DIR='
            '"$DEPENDENCIES_DIR/performance-native"'
        )
        profile_switch = 'DEPENDENCIES_DIR="$PROFILE_WORKSPACE"'
        self.assertIn(native_export, quickstart)
        self.assertIn(profile_switch, quickstart)
        self.assertLess(quickstart.index(native_export), quickstart.index(profile_switch))

    def test_benchmark_entrypoints_share_the_canonical_defaults(self) -> None:
        makefile = (CONFORMANCE_DIR / "benchmarks/examples/Makefile").read_text()
        wrapper = (CONFORMANCE_DIR / "benchmarks/run.py").read_text()
        for setting in (
            "CORES ?= 2",
            "LOADGEN_CORES ?= 6",
            "VUS ?= 256",
            "DURATION ?= 20s",
            "WARMUP ?= 5s",
            "RUNS ?= 3",
        ):
            self.assertIn(setting, makefile)
        for argument in (
            'parser.add_argument("--cores", type=int, default=2)',
            'parser.add_argument("--loadgen-cores", type=int, default=6)',
            'parser.add_argument("--vus", type=int, default=256)',
            'parser.add_argument("--duration", default="20s")',
            'parser.add_argument("--warmup", default="5s")',
            'parser.add_argument("--runs", type=int, default=3)',
        ):
            self.assertIn(argument, wrapper)

    def test_scenario_grpc_probe_is_built_once_and_reused(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "scenarios/run.py"))
        implementation = globals_["IMPLEMENTATIONS"][0]
        compose_command = globals_["command"](implementation, "run", "grpc-probe")
        overlay = str(CONFORMANCE_DIR / "scenarios/compose.grpc-probe.yml")
        self.assertIn(overlay, compose_command)

        build_commands: list[list[str]] = []
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            binary = artifacts / "grpc-probe"

            def fake_run(command: list[str], **_: object) -> mock.Mock:
                build_commands.append(command)
                binary.touch()
                return mock.Mock(returncode=0)

            prepare = globals_["prepare_grpc_probe"]
            prepare.__globals__["ARTIFACTS"] = artifacts
            prepare.__globals__["GRPC_PROBE_BINARY"] = binary
            with mock.patch.object(globals_["subprocess"], "run", side_effect=fake_run):
                prepare()

        self.assertEqual(len(build_commands), 1)
        build = build_commands[0]
        self.assertEqual(build[:3], ["docker", "run", "--rm"])
        self.assertIn(f"{globals_['ROOT'] / 'goexample'}:/repo/goexample:ro", build)
        self.assertIn(f"{CONFORMANCE_DIR}:/repo/conformance:ro", build)
        self.assertIn("servicelib-conformance-scenario-grpc-probe-build-cache:/go-cache", build)
        self.assertIn("servicelib-conformance-scenario-grpc-probe-module-cache:/go/pkg/mod", build)
        self.assertEqual(build[-7:], ["go", "build", "-trimpath", "-buildvcs=false", "-o", "/out/grpc-probe", "."])

        overlay_text = (CONFORMANCE_DIR / "scenarios/compose.grpc-probe.yml").read_text()
        self.assertIn("entrypoint: [/scenario-artifacts/grpc-probe]", overlay_text)
        self.assertNotIn("go run", overlay_text)
        for compose_file in (CONFORMANCE_DIR / "scenarios").glob("compose.*.yml"):
            if compose_file.name == "compose.grpc-probe.yml":
                continue
            with self.subTest(compose=compose_file.name):
                self.assertNotIn("grpc-probe:", compose_file.read_text())

    runners = (
        ("config/run.py", "DEFAULT_ROOT", "DEFAULT_ARTIFACT"),
        ("config/runtime.py", "DEFAULT_ROOT", "DEFAULT_ARTIFACT"),
        ("config/runtime_go.py", "ROOT", "OUTPUT"),
        ("config/schema.py", "ROOT", "ARTIFACT"),
        ("call_semantics/run.py", "ROOT", "ARTIFACTS"),
        ("benchmarks/run.py", "ROOT", "ARTIFACTS"),
        ("dependencies/run.py", "ROOT", "ARTIFACT_DIR"),
        ("dashboards/run.py", "ROOT", "ARTIFACT"),
        ("generation/run.py", "ROOT", "ARTIFACTS"),
        ("kubernetes/run.py", "ROOT", "SUMMARY"),
        ("logging/run.py", "ROOT", "ARTIFACT"),
        ("metrics/run.py", "ROOT", "ARTIFACTS"),
        ("operators/run.py", "ROOT", "ARTIFACT"),
        ("pools/run.py", "ROOT", "ARTIFACT"),
        ("profiling/run.py", "ROOT", "ARTIFACTS"),
        ("scenarios/run.py", "ROOT", "ARTIFACTS"),
        ("serde/run.py", "ROOT", "ARTIFACT"),
        ("signatures/run.py", "ROOT", "OUTPUT"),
        ("standalone_components/run.py", "DEFAULT_ROOT", "SUMMARY"),
        ("structure/run.py", "DEFAULT_ROOT", "DEFAULT_ARTIFACT"),
        ("tracing/run.py", "ROOT", "ARTIFACTS"),
        ("transports/run.py", "ROOT", "ARTIFACT"),
    )

    def test_every_runner_honors_external_dependency_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dependency_root = Path(directory).resolve()
            with mock.patch.dict(
                os.environ,
                {"CONFORMANCE_DEPENDENCIES_DIR": str(dependency_root)},
            ):
                for relative, root_name, artifact_name in self.runners:
                    with self.subTest(runner=relative):
                        globals_ = runpy.run_path(str(CONFORMANCE_DIR / relative))
                        self.assertEqual(globals_[root_name], dependency_root)
                        artifact = Path(globals_[artifact_name]).resolve()
                        self.assertTrue(
                            artifact.is_relative_to(CONFORMANCE_DIR / ".artifacts"),
                            f"{relative} writes outside conformance: {artifact}",
                        )

    def test_make_uses_managed_dependencies_for_direct_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dependencies").mkdir()
            makefile = root / "Makefile"
            makefile.write_text(
                (CONFORMANCE_DIR / "Makefile").read_text()
                + "\nprint-root:\n\t@printf '%s' \"$$CONFORMANCE_DEPENDENCIES_DIR\"\n"
            )
            clean_environment = dict(os.environ)
            clean_environment.pop("CONFORMANCE_DEPENDENCIES_DIR", None)
            result = subprocess.run(
                ["make", "--no-print-directory", "print-root"],
                cwd=root,
                env=clean_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout, str((root / ".dependencies").resolve()))

            external = root / "external"
            result = subprocess.run(
                ["make", "--no-print-directory", "print-root"],
                cwd=root,
                env={**os.environ, "CONFORMANCE_DEPENDENCIES_DIR": str(external)},
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout, str(external))

    def test_dependency_binary_inspection_uses_versioned_build_volume(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "dependencies/run.py"))
        commands: list[list[str]] = []

        def command(args: list[str], **_: object) -> str:
            commands.append(args)
            return "libjemalloc.so.2 => /lib/libjemalloc.so.2\n"

        linked_dependencies = globals_["linked_dependencies"]
        linked_dependencies.__globals__["command"] = command
        with (
            mock.patch.dict(
                os.environ,
                {"SERVICEGEN_CPPBOOST_BUILD_VOLUME": "expected-build-volume"},
                clear=False,
            ),
            mock.patch.object(
                globals_["cpp_source_cache"],
                "build_volume_name",
                return_value="expected-build-volume",
            ),
        ):
            linked_dependencies(skip_build=True)

        framework_commands = commands[:2]
        self.assertEqual(len(framework_commands), 2)
        for args in framework_commands:
            self.assertEqual(args[:3], ["docker", "run", "--rm"])
            self.assertNotIn("compose", args)
            self.assertIn(
                "expected-build-volume:/workspace/build:ro",
                args,
            )

    def test_aggregate_prints_complete_terminal_summary(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        suites = globals_["SUITES"]
        matrix = {
            name: {"status": "pass", "detail": "pass"}
            for name in suites
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            globals_["print_summary"](matrix, [])
        text = output.getvalue()

        for name in suites:
            self.assertIn(f"PASS  {name}", text)
        self.assertIn(f"Result: PASS — {len(suites)}/{len(suites)} suites passed", text)
        self.assertIn("Full report: .artifacts/summary.json", text)

    def test_suite_runner_replaces_stale_success_with_current_failure(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "run_suite.py"))
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            summary = artifacts / "profiling" / "summary.json"
            summary.parent.mkdir(parents=True)
            summary.write_text('{"status":"pass"}\n')
            globals_["main"].__globals__["ARTIFACTS"] = artifacts

            with (
                mock.patch.object(
                    sys,
                    "argv",
                    ["run_suite.py", "profiling", "false"],
                ),
                mock.patch.object(
                    globals_["subprocess"],
                    "run",
                    return_value=mock.Mock(returncode=7),
                ),
                contextlib.redirect_stderr(io.StringIO()),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(globals_["main"](), 7)

            result = json.loads(summary.read_text())
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["command"], ["false"])
            self.assertIn("code 7", result["error"])
            self.assertEqual(result["schemaVersion"], "1.0")
            self.assertEqual(result["operation"], "verify")
            self.assertEqual(result["summary"], {"errors": 1, "warnings": 0})
            self.assertEqual(
                result["diagnostics"],
                [{
                    "code": "SG_VERIFICATION_COMMAND_FAILED",
                    "severity": "error",
                    "stage": "verification",
                    "message": "Conformance suite 'profiling' failed",
                    "object": {"kind": "conformanceSuite", "name": "profiling"},
                    "details": {"exitCode": 7},
                }],
            )

    def test_aggregate_preserves_suite_diagnostics(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            globals_["main"].__globals__["ROOT"] = artifacts.parent
            globals_["main"].__globals__["ARTIFACTS"] = artifacts
            globals_["main"].__globals__["OUTPUT"] = artifacts / "summary.json"
            for suite in globals_["SUITES"]:
                summary = artifacts / suite / "summary.json"
                summary.parent.mkdir(parents=True, exist_ok=True)
                value: dict[str, object] = {"status": "pass"}
                expected = globals_["LANGUAGE_SUITES"].get(suite)
                if expected is not None:
                    value["languages"] = sorted(expected)
                summary.write_text(json.dumps(value) + "\n")
            failed = artifacts / "profiling" / "summary.json"
            failed.write_text(json.dumps({
                "status": "fail",
                "diagnostics": [{
                    "code": "SG_VERIFICATION_COMMAND_FAILED",
                    "severity": "error",
                    "stage": "verification",
                    "message": "Conformance suite 'profiling' failed",
                }],
            }) + "\n")

            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(RuntimeError),
            ):
                globals_["main"]()

            result = json.loads((artifacts / "summary.json").read_text())
            profiling = result["matrix"]["profiling"]
            self.assertEqual(
                profiling["detail"],
                "[SG_VERIFICATION_COMMAND_FAILED] Conformance suite 'profiling' failed",
            )
            self.assertEqual(profiling["diagnostics"][0]["stage"], "verification")

    def test_resume_selects_only_failed_or_missing_leaf_suites(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "resume.py"))
        aggregate_globals = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            for suite in aggregate_globals["SUITES"]:
                summary = artifacts / suite / "summary.json"
                summary.parent.mkdir(parents=True, exist_ok=True)
                value: dict[str, object] = {"status": "pass"}
                expected_languages = aggregate_globals["LANGUAGE_SUITES"].get(suite)
                if expected_languages is not None:
                    value["languages"] = sorted(expected_languages)
                summary.write_text(json.dumps(value) + "\n")

            (artifacts / "config-schema" / "summary.json").unlink()
            (artifacts / "config-runtime" / "summary.json").write_text(
                '{"status":"fail"}\n'
            )
            (artifacts / "dashboards" / "summary.json").write_text("invalid\n")

            self.assertEqual(
                globals_["pending_targets"](artifacts),
                ["config-schema", "config-runtime-core", "dashboards-core"],
            )

    def test_resume_uses_full_language_matrix_validation(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "resume.py"))
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            summary = artifacts / "serde" / "summary.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(
                json.dumps({"status": "pass", "languages": ["go"]}) + "\n"
            )
            self.assertFalse(globals_["suite_passed"]("serde", artifacts))

    def test_aggregate_requires_the_full_serde_language_matrix(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        self.assertEqual(
            globals_["LANGUAGE_SUITES"]["serde"],
            {"go", "canonical-cpp", "cppboost", "python", "rust", "typescript"},
        )

    def test_aggregate_requires_the_full_transport_language_matrix(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        self.assertEqual(
            globals_["LANGUAGE_SUITES"]["transports"],
            {"go", "canonical-cpp", "cppboost", "python", "rust", "typescript"},
        )

    def test_aggregate_rejects_a_partial_scenario_matrix(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))
        passed = globals_["passed"]

        ok, detail = passed("scenarios", {
            "status": "passed",
            "implementations": {"typescript-native": {"status": "passed"}},
        })

        self.assertFalse(ok)
        self.assertIn("language matrix differs", detail)

    def test_scenario_matrix_covers_every_framework_and_native_runtime(self) -> None:
        scenarios = runpy.run_path(str(CONFORMANCE_DIR / "scenarios/run.py"))
        actual = {value.name for value in scenarios["IMPLEMENTATIONS"]}
        aggregate = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))

        self.assertEqual(
            actual,
            {
                "go", "go-native",
                "cpp", "cpp-native", "cppboost", "cppboost-native",
                "python", "python-native", "rust", "rust-native",
                "typescript", "typescript-native",
            },
        )
        self.assertEqual(actual, aggregate["LANGUAGE_SUITES"]["scenarios"])

    def test_pooled_call_semantics_matrix_covers_every_framework(self) -> None:
        pooled = runpy.run_path(str(CONFORMANCE_DIR / "call_semantics/run.py"))
        aggregate = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))

        self.assertEqual(
            set(pooled["VARIANTS"]),
            {"go", "cpp", "cppboost", "python", "rust", "typescript"},
        )
        self.assertEqual(
            set(pooled["VARIANTS"]),
            aggregate["LANGUAGE_SUITES"]["call-semantics"],
        )
        self.assertEqual(
            pooled["EXPECTED_GRAPH_MARKERS"],
            (
                "callSemantics: TaskPool",
                "callSemantics: PriorityTaskPool",
                "callSemantics: ParallelCall",
                "poolName: Default Pool",
                "poolName: Inventory Priority Workers",
            ),
        )

    def test_standalone_component_matrix_covers_every_framework(self) -> None:
        standalone = runpy.run_path(
            str(CONFORMANCE_DIR / "standalone_components/run.py")
        )
        aggregate = runpy.run_path(str(CONFORMANCE_DIR / "aggregate.py"))

        self.assertEqual(
            set(standalone["LANGUAGES"]),
            aggregate["LANGUAGE_SUITES"]["standalone-components"],
        )
        self.assertEqual(
            set(standalone["SERVICES"]),
            {"analyticsservice", "inventoryservice", "orderservice"},
        )

    def test_quickstart_supports_full_current_profile(self) -> None:
        quickstart = (CONFORMANCE_DIR / "quickstart.sh").read_text()
        self.assertIn('--profile)', quickstart)
        self.assertIn('CONFORMANCE_EXAMPLE_PROFILE="$EXAMPLE_PROFILE"', quickstart)
        self.assertIn('profile_workspace.py', quickstart)
        self.assertIn('CONFORMANCE_DEPENDENCIES_DIR="$DEPENDENCIES_DIR"', quickstart)

    def test_profile_switch_discards_only_incompatible_cpp_build_trees(self) -> None:
        quickstart = (CONFORMANCE_DIR / "quickstart.sh").read_text()

        self.assertIn("cppexample_cpp-cmake-build", quickstart)
        self.assertIn('docker volume rm "$build_volume"', quickstart)
        self.assertNotIn("cpp-ccache-*", quickstart)
        self.assertNotIn("conformance-source-cache-*", quickstart)

    def test_current_profile_workspace_requires_all_call_semantics(self) -> None:
        profile = runpy.run_path(str(CONFORMANCE_DIR / "profile_workspace.py"))
        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory)
            graph = example / "graph"
            graph.mkdir()
            (graph / "example.generated.yaml").write_text(
                "callSemantics: TaskPool\n"
                "callSemantics: PriorityTaskPool\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
                "callSemantics: ParallelCall\n"
            )
            self.assertEqual(
                profile["verify_current_graph"](example),
                {
                    "task_pool_links": 1,
                    "priority_task_pool_links": 1,
                    "parallel_call_links": 3,
                },
            )

    def test_call_semantics_runner_honors_the_selected_profile(self) -> None:
        runner = (CONFORMANCE_DIR / "call_semantics" / "run.py").read_text()
        self.assertIn(
            'active_profile in {"function-call", "current"}', runner
        )
        self.assertIn(
            'verify_graph(ROOT / VARIANTS[language], active_profile)', runner
        )
        self.assertIn(
            '"1" if profile == "current" else "0"', runner
        )

    def test_pooled_scenario_metric_parser_requires_named_activity(self) -> None:
        scenarios = runpy.run_path(str(CONFORMANCE_DIR / "scenarios/run.py"))
        counter_total = scenarios["counter_total"]
        metrics = (
            '# TYPE priority_task_pool_tasks_total counter\n'
            'priority_task_pool_tasks_total{name="Default Pool",service="Order Service"} 7\n'
            'priority_task_pool_tasks_total{name="Other",service="Order Service"} 11\n'
        )
        self.assertEqual(
            counter_total(
                metrics, "priority_task_pool_tasks_total", "Default Pool"
            ),
            7,
        )
        with self.assertRaisesRegex(RuntimeError, "missing"):
            counter_total(
                metrics,
                "priority_task_pool_tasks_total",
                "Inventory Priority Workers",
            )

    def test_consolidated_tooling_fetches_every_native_dependency(self) -> None:
        native_repositories = {
            "gonativeexample", "cppnativeexample", "cppboostnativeexample",
            "pynativeexample", "rustnativeexample", "tsnativeexample",
        }
        conformance_quickstart = (CONFORMANCE_DIR / "quickstart.sh").read_text()
        for repository in native_repositories:
            self.assertRegex(
                conformance_quickstart,
                rf"(?m)^REPOS=\([^\n]*\b{repository}\b",
            )

        for obsolete_repository in ("benchmarks", "profiling"):
            self.assertNotRegex(
                conformance_quickstart,
                rf"(?m)^REPOS=\([^\n]*\b{obsolete_repository}\b",
            )
        profiling_runner = (
            CONFORMANCE_DIR / "profiling" / "examples" / "run.py"
        ).read_text()
        benchmark_runner = (
            CONFORMANCE_DIR / "benchmarks" / "examples" / "run.py"
        ).read_text()
        for repository in native_repositories:
            with self.subTest(repository=repository):
                self.assertIn(repository, profiling_runner)
                self.assertIn(repository, benchmark_runner)

        profiling_gate = runpy.run_path(
            str(CONFORMANCE_DIR / "profiling" / "run.py")
        )
        self.assertEqual(
            profiling_gate["PROFILING_ROOT"],
            CONFORMANCE_DIR / "profiling",
        )
        makefile = (CONFORMANCE_DIR / "Makefile").read_text()
        self.assertIn("benchmarks benchmark:", makefile)
        self.assertIn("profiling-all:", makefile)

    def test_runtime_graph_normalization_uses_semantic_node_identity(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "metrics/run.py"))
        normalize = globals_["normalize_runtime_graphs"]

        def graph(first_id: int, second_id: int, *, reverse: bool) -> str:
            nodes = [
                {"id": first_id, "label": "Input(INPUT)\n[Service]", "x": 1},
                {"id": second_id, "label": "Map(MAP)\n[Service]", "x": 2},
            ]
            edges = [
                {
                    "from": first_id,
                    "to": second_id,
                    "label": "Value\ncalls: 1",
                    "arrows": "to",
                }
            ]
            if reverse:
                nodes.reverse()
            return json.dumps({"nodes": nodes, "edges": edges})

        left = normalize({"service": graph(1, 2, reverse=False)})
        right = normalize({"service": graph(41, 99, reverse=True)})

        self.assertEqual(left, right)
        edge = left["service"]["edges"][0]
        self.assertEqual(edge["from"], "Input(INPUT)\n[Service]")
        self.assertEqual(edge["to"], "Map(MAP)\n[Service]")
        self.assertEqual(edge["type"], "Value")
        self.assertEqual(edge["calls"], 1)

    def test_runtime_graph_normalization_rejects_dangling_edges(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "metrics/run.py"))
        normalize = globals_["normalize_runtime_graphs"]
        raw = json.dumps(
            {
                "nodes": [{"id": 1, "label": "Input(INPUT)\n[Service]"}],
                "edges": [{"from": 1, "to": 2, "label": "Value"}],
            }
        )

        with self.assertRaisesRegex(RuntimeError, "references a missing node"):
            normalize({"service": raw})

    def test_generation_uses_project_scoped_temporary_volumes(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "generation/run.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "docker-compose.yml",
                "docker-compose.cmake.generated.yml",
                "docker-compose.integration.generated.yml",
            ):
                build_name = (
                    "${SERVICEGEN_CPPBOOST_BUILD_VOLUME:-"
                    "cppboostexample_cpp-cmake-build-v0.2.7}"
                )
                (root / name).write_text(
                    "volumes:\n"
                    "  cpp-cmake-build:\n"
                    f"    name: {build_name}\n"
                    "  cpp-ccache:\n"
                    "    name: cppboostexample_cpp-ccache\n"
                    "  retained:\n"
                    "    name: unrelated-volume\n"
                )
            globals_["isolate_compose_volumes"](root)
            for name in (
                "docker-compose.yml",
                "docker-compose.cmake.generated.yml",
                "docker-compose.integration.generated.yml",
            ):
                text = (root / name).read_text()
                self.assertNotIn("cppboostexample_cpp-", text)
                self.assertIn("name: unrelated-volume", text)

    def test_generation_preserves_existing_user_files_and_ignores_local_state(
        self,
    ) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "generation/run.py"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "business.cpp").write_text("before")
            (root / ".servicegen").mkdir()
            (root / ".servicegen" / "merge.log").write_text("first")
            before = globals_["manifest"](root, generated=False)

            (root / ".servicegen" / "merge.log").write_text("second")
            (root / ".env").write_text("NEW_SETTING=value\n")
            after = globals_["manifest"](root, generated=False)
            changes, additions = globals_["classify_user_changes"](before, after)

            self.assertEqual(changes, [])
            self.assertEqual(additions, [".env"])
            self.assertNotIn(".servicegen/merge.log", before)
            self.assertNotIn(".servicegen/merge.log", after)

            (root / "business.cpp").write_text("after")
            changed_after = globals_["manifest"](root, generated=False)
            changes, _ = globals_["classify_user_changes"](before, changed_after)
            self.assertEqual(changes, ["business.cpp"])

            (root / "package.json").write_text("before")
            overwritten = {"package.json"}
            user_owned = globals_["manifest"](
                root, generated=False, overwritten=overwritten
            )
            generator_owned = globals_["manifest"](
                root, generated=True, overwritten=overwritten
            )
            self.assertNotIn("package.json", user_owned)
            self.assertIn("package.json", generator_owned)

    def test_transport_profiles_share_versioned_dependency_sources(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "transports/run.py"))
        source_cache = globals_["boost_source_cache_build_dir"]()
        source_arguments = globals_["boost_source_cache_cmake_args"]()
        generator_environment = globals_["boost_generator_environment"]()
        grpc = globals_["boost_command"]("build/grpc-test", False, False)[-1]
        kafka = globals_["boost_kafka_command"](
            "build/kafka-test", True, False
        )[-1]

        self.assertRegex(
            source_cache,
            r"^conformance-source-cache-[A-Za-z0-9_.-]+-[0-9a-f]{12}$",
        )
        self.assertIn("/servicegen-cpp-source-cache/grpc-src", grpc)
        self.assertIn("/servicegen-cpp-source-cache/librdkafka-src", kafka)
        self.assertIn(
            "/servicegen-cpp-source-cache/opentelemetry-cpp-src",
            source_arguments,
        )
        self.assertIn(
            "/servicegen-cpp-source-cache/opentelemetry-cpp-build/"
            "opentelemetry-proto-prefix/src/opentelemetry-proto",
            source_arguments,
        )
        self.assertIn(source_arguments, grpc)
        self.assertIn(source_arguments, kafka)
        self.assertNotIn("FETCHCONTENT_BASE_DIR", grpc)
        self.assertNotIn("FETCHCONTENT_BASE_DIR", kafka)
        self.assertEqual(
            generator_environment["SERVICEGEN_CPPBOOST_SOURCE_CACHE_DIR"],
            str(globals_["cpp_source_cache"].source_dir(globals_["BOOST"])),
        )
        self.assertIn(
            globals_["cpp_source_cache"].source_mount(globals_["BOOST"]),
            globals_["boost_command"]("build/grpc-test", False, False),
        )

    def test_generation_uses_the_transport_versioned_source_cache(self) -> None:
        transports = runpy.run_path(
            str(CONFORMANCE_DIR / "transports/run.py")
        )
        generation = runpy.run_path(
            str(CONFORMANCE_DIR / "generation/run.py")
        )
        self.assertEqual(
            generation["boost_source_cache_build_dir"](),
            transports["boost_source_cache_build_dir"](),
        )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            scripts = project / "scripts"
            scripts.mkdir()
            compose_command = (
                "docker compose -f docker-compose.cmake.generated.yml"
            )
            configure_command = (
                'cmake --preset "$SERVICEGEN_CPP_CMAKE_PRESET"'
            )
            for name in ("build.generated.sh", "test.generated.sh"):
                (scripts / name).write_text(
                    f"{compose_command} run cpp-build\n{configure_command}\n"
                )
            integration = scripts / "integration-test.generated.sh"
            integration.write_text(
                f"{compose_command} run cpp-build\n"
                "cleanup() {\n"
                "  docker compose -f docker-compose.integration.generated.yml "
                "down --timeout 30\n"
                "}\n"
            )
            source_cache = project / "shared-sources"
            source_cache.mkdir()

            generation["attach_boost_source_cache"](project, source_cache)

            override = (
                project / "docker-compose.source-cache.generated.yml"
            ).read_text()
            self.assertIn(
                f"{source_cache}:/servicegen-cpp-source-cache:ro",
                override,
            )
            self.assertIn(
                '"SERVICEGEN_CPPBOOST_SOURCE_CACHE": "1"',
                override,
            )
            for name in ("build.generated.sh", "test.generated.sh"):
                text = (scripts / name).read_text()
                self.assertIn(
                    "-f docker-compose.source-cache.generated.yml", text
                )
                self.assertIn(
                    "-C /workspace/conformance-source-cache.generated.cmake",
                    text,
                )
            self.assertIn(
                "-f docker-compose.source-cache.generated.yml",
                integration.read_text(),
            )

    def test_early_boost_suites_use_the_shared_source_cache(self) -> None:
        pools = runpy.run_path(str(CONFORMANCE_DIR / "pools/run.py"))
        serde = runpy.run_path(str(CONFORMANCE_DIR / "serde/run.py"))
        expected = "/servicegen-cpp-source-cache/googletest-src"

        self.assertIn(expected, pools["boost_framework_build_script"]())
        self.assertIn(expected, serde["boost_serde_script"](False))
        self.assertNotIn("FETCHCONTENT_SOURCE_DIR", serde["boost_serde_script"](True))
        self.assertIn(
            serde["cpp_source_cache"].source_mount(serde["BOOST"]),
            serde["boost_source_mount_args"](),
        )
        self.assertIn(
            "cpp_source_cache.ensure(BOOST)",
            (CONFORMANCE_DIR / "serde/run.py").read_text(),
        )

    def test_serde_wire_matrix_covers_every_runtime(self) -> None:
        serde = runpy.run_path(str(CONFORMANCE_DIR / "serde/run.py"))
        source = (CONFORMANCE_DIR / "serde/run.py").read_text()
        for language in (
            "go", "canonical-cpp", "boost-cpp", "python", "rust", "typescript",
        ):
            with self.subTest(language=language):
                self.assertIn(f'"{language}"', source)
        self.assertTrue((CONFORMANCE_DIR / "serde/python_probe.py").is_file())
        self.assertTrue((CONFORMANCE_DIR / "serde/typescript_probe.mjs").is_file())
        self.assertTrue((serde["RUST"] / "examples/serde_wire_probe.rs").is_file())
        self.assertIn("compare_wire_fixtures(go_fixtures", source)

    def test_source_cache_population_retries_without_changing_cache(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "cpp_source_cache.py"))
        dependency_root = Path(
            os.environ.get(
                "CONFORMANCE_DEPENDENCIES_DIR",
                str(CONFORMANCE_DIR.parent),
            )
        )
        framework = dependency_root / "cppboostservicelib"
        command = globals_["prepare_command"](framework)
        script = command[-1]

        self.assertIn("max_attempts=6", script)
        self.assertIn("attempt $attempt failed; retrying", script)
        self.assertIn(
            "-DCPPBOOSTSERVICELIB_GITHUB_ARCHIVE_BASE=",
            script,
        )
        self.assertIn(globals_["build_dir"](framework), script)
        self.assertTrue(any(
            value.startswith(str(globals_["cache_dir"](framework)) + ":")
            for value in command
        ))

    def test_source_cache_uses_docker_proxy_host_on_macos_and_linux(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "cpp_source_cache.py"))
        dependency_root = Path(
            os.environ.get(
                "CONFORMANCE_DEPENDENCIES_DIR",
                str(CONFORMANCE_DIR.parent),
            )
        )
        framework = dependency_root / "cppboostservicelib"
        old_raw = os.environ.get("SERVICEGEN_GITHUB_RAW_URL")
        old_host = os.environ.get("SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST")
        try:
            os.environ["SERVICEGEN_GITHUB_RAW_URL"] = (
                "http://localhost:18081/repository/github-raw"
            )
            os.environ["SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST"] = (
                "host.docker.internal"
            )
            command = globals_["prepare_command"](framework)
        finally:
            if old_raw is None:
                os.environ.pop("SERVICEGEN_GITHUB_RAW_URL", None)
            else:
                os.environ["SERVICEGEN_GITHUB_RAW_URL"] = old_raw
            if old_host is None:
                os.environ.pop(
                    "SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST", None
                )
            else:
                os.environ["SERVICEGEN_DEPENDENCY_PROXY_DOCKER_HOST"] = old_host

        self.assertIn(
            "SERVICEGEN_GITHUB_RAW_URL=http://host.docker.internal:18081/"
            "repository/github-raw",
            command,
        )
        self.assertIn("host.docker.internal:host-gateway", command)

    def test_native_scenarios_receive_the_shared_grpc_source_contexts(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "scenarios/run.py"))
        grpc_source = Path("/cache/grpc-src")
        asio_grpc_source = Path("/cache/asio-grpc-src")
        globals_["environment"].__globals__["NATIVE_SOURCE_CONTEXTS"] = (
            grpc_source,
            asio_grpc_source,
        )
        native = next(
            value for value in globals_["IMPLEMENTATIONS"]
            if value.name == "cppboost-native"
        )

        environment = globals_["environment"](native)

        self.assertEqual(
            environment["SERVICEGEN_GRPC_SOURCE_CONTEXT"], str(grpc_source)
        )
        self.assertEqual(
            environment["SERVICEGEN_ASIO_GRPC_SOURCE_CONTEXT"],
            str(asio_grpc_source),
        )

    def test_scenario_cleanup_runs_when_readiness_fails(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "scenarios/run.py"))
        evaluate = globals_["evaluate"]
        implementation = globals_["Implementation"](
            "go", CONFORMANCE_DIR, CONFORMANCE_DIR / "scenarios/compose.go.yml"
        )
        function_globals = evaluate.__globals__
        function_globals["run"] = mock.Mock()
        function_globals["wait_ready"] = mock.Mock(
            side_effect=RuntimeError("not ready")
        )
        function_globals["command"] = mock.Mock(return_value=["compose", "down"])
        function_globals["environment"] = mock.Mock(return_value={})

        with mock.patch.object(subprocess, "run") as subprocess_run:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                evaluate(implementation)

        subprocess_run.assert_called_once()
        self.assertEqual(subprocess_run.call_args.args[0], ["compose", "down"])

    def test_logging_python_test_uses_clean_machine_docker_image(self) -> None:
        globals_ = runpy.run_path(str(CONFORMANCE_DIR / "logging/run.py"))
        build_command, build_env = globals_["python_image_build"]()
        test_command = globals_["python_test_command"]()

        self.assertIn("PYSERVICELIB_SOURCE_CONTEXT", build_env)
        self.assertEqual(build_command[-2:], ["build", "inventoryservice"])
        self.assertIn("example-python:latest", test_command)
        self.assertIn("/workspace/.venv/bin/python", test_command)
        self.assertIn("PYTHONPATH=/workspace/.pyservicelib/src", test_command)
        self.assertNotIn(str(globals_["PYTHON"] / ".venv"), test_command)


if __name__ == "__main__":
    unittest.main()
