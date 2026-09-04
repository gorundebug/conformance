import json
import sys
import tempfile
import unittest
from argparse import Namespace
from os import environ
from pathlib import Path
from unittest.mock import patch

import run as benchmark


class LanguageLoggingTest(unittest.TestCase):
    def test_captures_complete_subprocess_output_per_profile_and_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            args = Namespace(graph_profile="current", result_prefix="")
            with patch.object(benchmark, "ARTIFACTS", artifacts):
                with benchmark.language_log(args, "go") as log:
                    benchmark.run(
                        [
                            sys.executable,
                            "-c",
                            "import sys; print('proxy-request'); print('build-error', file=sys.stderr)",
                        ],
                        cwd=artifacts,
                        env=environ.copy(),
                    )
                self.assertEqual(log, artifacts / "logs/current/go.log")
                contents = log.read_text()
                self.assertIn("proxy-request", contents)
                self.assertIn("build-error", contents)


class DependencyEnvironmentTest(unittest.TestCase):
    def test_preserves_complete_proxy_contract_for_builds(self) -> None:
        language = benchmark.LANGUAGES[0]
        args = Namespace(
            cores=2, duration="1s", graph_profile="current",
            loadgen_cores=2, vus=1,
        )
        contract = {
            "DEPENDENCY_PROXY_DIR": "/cache",
            "DEPENDENCY_CONAN_REMOTE_URL": "http://proxy/conan-group",
            "DEPENDENCY_GITHUB_RAW_URL": "http://proxy/github-raw",
            "GOPROXY": "http://proxy/go-proxy/",
            "NPM_CONFIG_REGISTRY": "http://proxy/npm-proxy/",
            "PIP_INDEX_URL": "http://proxy/pypi-proxy/simple",
            "CARGO_REGISTRIES_CRATES_IO_INDEX": "sparse+http://proxy/cargo-proxy/",
        }
        with patch.dict(environ, contract, clear=False):
            actual = benchmark.environment(args, language)
        for name, value in contract.items():
            self.assertEqual(actual[name], value)


class BenchmarkInputContractTest(unittest.TestCase):
    def test_native_baselines_use_the_shared_pinned_tooling_checkout(self) -> None:
        native = [
            language for language in benchmark.LANGUAGES
            if language.repository is not None
        ]
        self.assertEqual(len(native), 6)
        self.assertTrue(all(item.example.parent == benchmark.NATIVE_ROOT for item in native))
        self.assertTrue(
            all(
                item.example.parent == benchmark.ROOT
                for item in benchmark.LANGUAGES
                if item.repository is None
            )
        )


class PoolVerificationTest(unittest.TestCase):
    def language_with_graph(self, call_semantics: str) -> benchmark.Language:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        example = Path(temporary_directory.name)
        graph_directory = example / "orderservice" / "graph"
        graph_directory.mkdir(parents=True)
        (graph_directory / "orderservice.generated.yaml").write_text(
            "services:\n"
            "  orderService:\n"
            "    links:\n"
            f"      link1:\n        callSemantics: {call_semantics}\n"
        )
        return benchmark.Language("test", example, example / "compose.yml")

    def test_detects_priority_task_pool_link(self) -> None:
        language = self.language_with_graph("PriorityTaskPool")
        self.assertTrue(
            benchmark.service_uses_priority_task_pool(language, "orderservice")
        )

    def test_detects_ordinary_task_pool_link(self) -> None:
        language = self.language_with_graph("TaskPool")
        self.assertEqual(
            benchmark.service_pool_metrics(language, "orderservice"),
            ("task_pool_executors_target",),
        )

    def test_detects_both_pool_kinds(self) -> None:
        language = self.language_with_graph("TaskPool")
        graph = (
            language.example / "orderservice" / "graph"
            / "orderservice.generated.yaml"
        )
        graph.write_text(
            graph.read_text()
            + "      link2:\n        callSemantics: PriorityTaskPool\n"
        )
        self.assertEqual(
            benchmark.service_pool_metrics(language, "orderservice"),
            (
                "task_pool_executors_target",
                "priority_task_pool_executors_target",
            ),
        )

    def test_function_call_graph_does_not_require_pool_metric(self) -> None:
        language = self.language_with_graph("FunctionCall")
        self.assertFalse(
            benchmark.service_uses_priority_task_pool(language, "orderservice")
        )

    def test_missing_generated_graph_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            language = benchmark.Language(
                "test", Path(directory), Path(directory) / "compose.yml"
            )
            with self.assertRaisesRegex(RuntimeError, "failed to read"):
                benchmark.service_uses_priority_task_pool(
                    language, "orderservice"
                )

    def test_reads_effective_pool_sizes_from_status_graph(self) -> None:
        status = (
            "pools:\n"
            "  defaultPool:\n"
            "    executorsCount: 6\n"
            "    name: Default Pool\n"
            "  priorityPool:\n"
            "    executorsCount: 6\n"
            "    name: Priority Pool\n"
            "services:\n"
            "  orderService: {}\n"
        )
        self.assertEqual(
            benchmark.runtime_pool_executor_counts(status),
            [6, 6],
        )

    def test_ignores_nested_executor_counts_outside_pools(self) -> None:
        status = (
            "pools:\n"
            "  defaultPool:\n"
            "    executorsCount: 2\n"
            "services:\n"
            "  orderService:\n"
            "    executorsCount: 99\n"
        )
        self.assertEqual(benchmark.runtime_pool_executor_counts(status), [2])

    def test_reads_rust_indentationless_pool_sequence(self) -> None:
        status = (
            "pools:\n"
            "- name: Inventory Priority Workers\n"
            "  executorsCount: 4\n"
            "  queueCapacity: 0\n"
            "links:\n"
            "- from: 1\n"
            "  to: 2\n"
        )
        self.assertEqual(benchmark.runtime_pool_executor_counts(status), [4])


class NoopTelemetryVerificationTest(unittest.TestCase):
    @staticmethod
    def language() -> benchmark.Language:
        return benchmark.Language("rust", Path("/example"), Path("/overlay.yml"))

    @staticmethod
    def resolved_environment(**overrides: str) -> dict[str, object]:
        environment = {
            "SERVICELIB_NOOP_LOGS": "1",
            "SERVICELIB_NOOP_METRICS": "1",
            "SERVICELIB_NOOP_TRACING": "1",
        }
        environment.update(overrides)
        return {
            "services": {
                "inventoryservice": {"environment": dict(environment)},
                "orderservice": {"environment": dict(environment)},
            }
        }

    def test_accepts_noop_telemetry_for_both_services(self) -> None:
        completed = type(
            "Completed",
            (),
            {"stdout": json.dumps(self.resolved_environment())},
        )()
        with patch.object(benchmark, "run", return_value=completed):
            benchmark.verify_noop_telemetry_configuration(self.language(), {})

    def test_rejects_enabled_metrics(self) -> None:
        completed = type(
            "Completed",
            (),
            {
                "stdout": json.dumps(
                    self.resolved_environment(SERVICELIB_NOOP_METRICS="0")
                )
            },
        )()
        with patch.object(benchmark, "run", return_value=completed):
            with self.assertRaisesRegex(
                RuntimeError, "SERVICELIB_NOOP_METRICS must resolve to 1"
            ):
                benchmark.verify_noop_telemetry_configuration(self.language(), {})


class NativeExampleFetchTest(unittest.TestCase):
    def test_fetches_missing_native_example_at_pinned_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "native"
            language = benchmark.Language(
                "test-native",
                destination,
                root / "overlay.yml",
                verify_framework_pool=False,
                repository="https://example.test/native.git",
                revision="v1.2.3",
            )
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> None:
                commands.append(command)
                checkout = Path(command[-1])
                checkout.mkdir(parents=True)
                (checkout / "docker-compose.yml").write_text("services: {}\n")

            with patch.object(benchmark, "run", side_effect=fake_run):
                benchmark.ensure_example(language, {})

            self.assertTrue((destination / "docker-compose.yml").is_file())
            self.assertEqual(commands[0][0:4], [
                "git", "clone", "--branch", "v1.2.3",
            ])
            self.assertIn("--depth", commands[0])

    def test_existing_checkout_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "native"
            destination.mkdir()
            (destination / "docker-compose.yml").write_text("services: {}\n")
            language = benchmark.Language(
                "test-native",
                destination,
                destination / "overlay.yml",
                repository="https://example.test/native.git",
                revision="v1.2.3",
            )
            with patch.object(benchmark, "run") as mocked_run:
                benchmark.ensure_example(language, {})
            mocked_run.assert_not_called()

    def test_managed_checkout_moves_to_pinned_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "native"
            destination.mkdir()
            (destination / "docker-compose.yml").write_text("services: {}\n")
            language = benchmark.Language(
                "test-native",
                destination,
                destination / "overlay.yml",
                repository="https://example.test/native.git",
                revision="v1.2.3",
            )
            commands: list[list[str]] = []

            def fake_run(
                command: list[str], **_: object
            ) -> object:
                commands.append(command)
                stdout = ""
                if command[0:3] == ["git", "rev-parse", "HEAD"]:
                    stdout = "old\n"
                elif command[0:3] == ["git", "rev-list", "-n"]:
                    stdout = "new\n"
                return type("Completed", (), {"stdout": stdout})()

            with patch.object(benchmark, "run", side_effect=fake_run):
                benchmark.ensure_example(
                    language,
                    {"UPDATE_MANAGED_DEPENDENCIES": "1"},
                )

            self.assertIn(
                ["git", "checkout", "--detach", "v1.2.3"], commands
            )

    def test_incomplete_existing_directory_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "native"
            destination.mkdir()
            language = benchmark.Language(
                "test-native",
                destination,
                destination / "overlay.yml",
                repository="https://example.test/native.git",
                revision="v1.2.3",
            )
            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                benchmark.ensure_example(language, {})


class CleanCheckoutContextTest(unittest.TestCase):
    @staticmethod
    def args() -> Namespace:
        return Namespace(
            cores=2,
            loadgen_cores=8,
            duration="20s",
            vus=32,
        )

    def test_cpp_native_uses_remote_userver_when_local_checkout_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            language = next(
                item for item in benchmark.LANGUAGES if item.name == "cpp-native"
            )
            with (
                patch.object(benchmark, "ROOT", Path(directory)),
                patch.dict(environ, {}, clear=True),
            ):
                env = benchmark.environment(self.args(), language)

        self.assertEqual(
            env["USERVER_SOURCE_CONTEXT"], benchmark.USERVER_REMOTE_CONTEXT
        )

    def test_rust_uses_compose_default_when_local_runtime_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            language = next(
                item for item in benchmark.LANGUAGES if item.name == "rust"
            )
            with (
                patch.object(benchmark, "ROOT", Path(directory)),
                patch.dict(environ, {}, clear=True),
            ):
                env = benchmark.environment(self.args(), language)

        self.assertNotIn("RUSTSERVICELIB_SOURCE_CONTEXT", env)

    def test_typescript_uses_local_framework_source_context(self) -> None:
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "typescript"
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(benchmark, "ROOT", Path(directory)):
                env = benchmark.environment(self.args(), language)
        self.assertEqual(
            env["TSSERVICELIB_SOURCE_CONTEXT"],
            str(Path(directory) / "tsservicelib"),
        )

    def test_typescript_variants_are_registered(self) -> None:
        registered = {language.name for language in benchmark.LANGUAGES}
        self.assertIn("typescript", registered)
        self.assertIn("typescript-native", registered)

    def test_typescript_native_uses_an_explicit_managed_revision(self) -> None:
        language = next(
            item
            for item in benchmark.LANGUAGES
            if item.name == "typescript-native"
        )
        self.assertIsNotNone(language.revision)
        self.assertRegex(language.revision or "", r"^(?:main|v\d+\.\d+\.\d+)$")

    def test_cpp_boost_never_uses_example_as_dependency_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            versions = root / "cppboostservicelib" / "cmake"
            versions.mkdir(parents=True)
            (versions / "DependencyVersions.cmake").write_text(
                'set(CPPBOOSTSERVICELIB_GRPC_VERSION "v1.2.3")\n'
                'set(CPPBOOSTSERVICELIB_ASIO_GRPC_VERSION "v4.5.6")\n'
            )
            for language_name in ("cpp-boost", "cpp-boost-native"):
                language = next(
                    item for item in benchmark.LANGUAGES
                    if item.name == language_name
                )
                with (
                    patch.object(benchmark, "ROOT", root),
                    patch.dict(environ, {}, clear=True),
                ):
                    env = benchmark.environment(self.args(), language)

                self.assertEqual(
                    env["GRPC_SOURCE_CONTEXT"],
                    "https://github.com/grpc/grpc.git#v1.2.3",
                )
                self.assertEqual(
                    env["ASIO_GRPC_SOURCE_CONTEXT"],
                    "https://github.com/Tradias/asio-grpc.git#v4.5.6",
                )

    def test_cpp_boost_respects_explicit_dependency_sources(self) -> None:
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "cpp-boost"
        )
        explicit = {
            "GRPC_SOURCE_CONTEXT": "/cache/grpc-src",
            "ASIO_GRPC_SOURCE_CONTEXT": "/cache/asio-grpc-src",
        }
        with patch.dict(environ, explicit, clear=True):
            env = benchmark.environment(self.args(), language)

        self.assertEqual(env["GRPC_SOURCE_CONTEXT"], "/cache/grpc-src")
        self.assertEqual(
            env["ASIO_GRPC_SOURCE_CONTEXT"],
            "/cache/asio-grpc-src",
        )

    def test_typescript_framework_builds_runtime_images(self) -> None:
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "typescript"
        )
        with patch.object(benchmark, "run") as run:
            benchmark.build(language, {})
        run.assert_called_once_with(
            ["make", "docker-build", "RUNTIME_IMAGE=1"],
            cwd=language.example,
            env={},
            retry_network=True,
        )


class BoostWorkerConfigurationTest(unittest.TestCase):
    def test_framework_requires_workers_argument_for_both_services(self) -> None:
        language = next(item for item in benchmark.LANGUAGES if item.name == "cpp-boost")
        resolved = {
            "services": {
                service: {"command": ["binary", "--workers", "3"]}
                for service in ("inventoryservice", "orderservice")
            }
        }
        completed = type("Completed", (), {"stdout": __import__("json").dumps(resolved)})()
        with patch.object(benchmark, "run", return_value=completed):
            benchmark.verify_boost_worker_configuration(language, 3, {})

    def test_native_rejects_worker_count_different_from_cores(self) -> None:
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "cpp-boost-native"
        )
        resolved = {
            "services": {
                service: {"environment": {"NATIVE_WORKER_THREADS": "4"}}
                for service in ("inventoryservice", "orderservice")
            }
        }
        completed = type("Completed", (), {"stdout": __import__("json").dumps(resolved)})()
        with (
            patch.object(benchmark, "run", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "expected 2"),
        ):
            benchmark.verify_boost_worker_configuration(language, 2, {})


class CppComposeIsolationTest(unittest.TestCase):
    def test_runtime_overlays_do_not_repeat_image_entrypoints(self) -> None:
        examples = Path(__file__).resolve().parent
        for overlay in ("compose.cpp.yml", "compose.cpp-boost.yml"):
            with self.subTest(overlay=overlay):
                contents = (examples / overlay).read_text()
                self.assertNotIn("/usr/local/bin/example_", contents)

    def test_boost_runtime_uses_packaged_config_path(self) -> None:
        contents = (
            Path(__file__).resolve().parent / "compose.cpp-boost.yml"
        ).read_text()
        self.assertIn("/app/config/config.yaml", contents)
        self.assertNotIn("/app/inventoryservice/config", contents)
        self.assertNotIn("/app/orderservice/config", contents)

    def test_compose_command_includes_language_runtime_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            example = Path(directory)
            (example / "docker-compose.yml").write_text("services: {}\n")
            python_overlay = example / "docker-compose.python-runtime.generated.yml"
            cpp_overlay = example / "docker-compose.cpp-runtime.generated.yml"
            python_overlay.write_text("services: {}\n")
            cpp_overlay.write_text("services: {}\n")
            language = benchmark.Language(
                "mixed", example, example / "benchmark.yml"
            )

            command = benchmark.compose_command(language, "config")

        self.assertLess(
            command.index(str(cpp_overlay)),
            command.index(str(python_overlay)),
        )
        self.assertIn("config", command)

    def test_cpp_variants_require_their_own_build_image(self) -> None:
        for language_name, expected_prefix in (
            ("cpp", "cppexample"),
            ("cpp-boost", "cppboostexample"),
        ):
            with self.subTest(language=language_name):
                language = next(
                    item for item in benchmark.LANGUAGES
                    if item.name == language_name
                )
                resolved = {"services": {
                    service: {"image": f"{expected_prefix}-{service}:local"}
                    for service in ("inventoryservice", "orderservice")
                }}
                completed = type(
                    "Completed",
                    (),
                    {"stdout": __import__("json").dumps(resolved)},
                )()
                with patch.object(benchmark, "run", return_value=completed):
                    benchmark.verify_cpp_compose_isolation(language, {})

    def test_mixed_cpp_image_is_rejected_before_benchmark(self) -> None:
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "cpp-boost"
        )
        resolved = {
            "services": {
                service: {"image": "cppexample-cpp-build"}
                for service in ("inventoryservice", "orderservice")
            }
        }
        completed = type(
            "Completed", (), {"stdout": __import__("json").dumps(resolved)}
        )()
        with (
            patch.object(benchmark, "run", return_value=completed),
            self.assertRaisesRegex(RuntimeError, "refusing to mix"),
        ):
            benchmark.verify_cpp_compose_isolation(language, {})


class CppTelemetryBaselineTest(unittest.TestCase):
    def test_disables_http_and_grpc_client_request_middlewares(self) -> None:
        config = (
            "components_manager:\n"
            "  components:\n"
            "    server:\n"
            "      listener:\n"
            "        port: 9091\n"
            "    grpc-client-factory:\n"
            "      channel-args: {}\n"
        )

        prepared = benchmark._disable_userver_request_middlewares(
            config, "orderservice"
        )

        self.assertIn(
            "middleware-pipeline-builder: "
            "servicelib-disabled-server-middlewares",
            prepared,
        )
        self.assertIn("disable-all-pipeline-middlewares: true", prepared)

    def test_server_only_service_does_not_require_grpc_client_factory(self) -> None:
        config = (
            "components_manager:\n"
            "  components:\n"
            "    server:\n"
            "      listener:\n"
            "        port: 9092\n"
        )

        prepared = benchmark._disable_userver_request_middlewares(
            config, "inventoryservice"
        )

        self.assertIn(
            "servicelib-disabled-server-middlewares", prepared
        )
        self.assertNotIn("disable-all-pipeline-middlewares", prepared)

    def test_missing_server_component_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "server component"):
            benchmark._disable_userver_request_middlewares(
                "components_manager:\n  components: {}\n", "broken"
            )


class BenchmarkKafkaConfigurationTest(unittest.TestCase):
    def test_cpp_config_preserves_generated_kafka_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dependencies"
            artifacts = Path(directory) / "artifacts"
            for service in ("inventoryservice", "orderservice"):
                service_root = root / "cppexample" / service
                (service_root / "config").mkdir(parents=True)
                (service_root / "static_config.yaml").write_text(
                    "components_manager:\n"
                    "  task_processors:\n"
                    "    main-task-processor:\n      worker_threads: 2\n"
                    "    fs-task-processor:\n      worker_threads: 1\n"
                    "  components:\n"
                    "    server:\n"
                    "      listener:\n        port: 1\n"
                    + (
                        "    grpc-client-factory:\n"
                        if service == "orderservice" else ""
                    )
                    + (
                        ""
                        if service == "orderservice"
                        else "    grpc-server:\n      completion-queue-count: 1\n"
                    )
                )
                variables = (
                    "orderEventsPassword: secret\n"
                    "orderEventsUsername: user\n"
                    "orderEventsSaslMechanism: SCRAM-SHA-512\n"
                    "orderEventsSecurityProtocol: SASL_SSL\n"
                    if service == "orderservice" else ""
                )
                (service_root / "config" / "config_vars.integration.yaml").write_text(
                    variables
                )
            with (
                patch.object(benchmark, "ROOT", root),
                patch.object(benchmark, "ARTIFACTS", artifacts),
                patch.object(
                    benchmark,
                    "_disable_userver_request_middlewares",
                    lambda value, _service: value,
                ),
            ):
                benchmark.prepare_cpp_configs(2)

            prepared = (
                artifacts / "cpp-config" / "orderservice.config_vars.yaml"
            ).read_text()
            self.assertIn("orderEventsPassword: secret", prepared)
            self.assertIn("orderEventsUsername: user", prepared)
            self.assertIn("orderEventsSaslMechanism: SCRAM-SHA-512", prepared)
            self.assertIn("orderEventsSecurityProtocol: SASL_SSL", prepared)
            self.assertIn("orderProcessedEnabled: false", prepared)

    def test_boost_orderservice_override_disables_kafka(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dependencies"
            artifacts = Path(directory) / "artifacts"
            for service, pool in (
                ("inventoryservice", "inventoryPriorityWorkers"),
                ("orderservice", "defaultPool"),
            ):
                config = root / "cppboostexample" / service / "config"
                config.mkdir(parents=True)
                (config / "overrides.yaml").write_text(
                    "pools:\n"
                    f"  {pool}:\n"
                    "    executorsCount: 2\n"
                    "dataConnectors:\n"
                    "  inventoryServiceApi:\n"
                    "    connectionsCount: 1\n"
                    "endpoints:\n"
                    "  orderProcessed:\n"
                    "    enabled: true\n"
                )
            with (
                patch.object(benchmark, "ROOT", root),
                patch.object(benchmark, "ARTIFACTS", artifacts),
            ):
                benchmark.prepare_cppboost_configs(2, 2)

            prepared = (
                artifacts / "cppboost-config" / "orderservice.overrides.yaml"
            ).read_text()
            self.assertNotIn("enabled: true", prepared)
            self.assertEqual(
                prepared.count("orderProcessed:\n    enabled: false"), 1
            )

    def test_python_orderservice_override_disables_kafka_without_losing_streams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dependencies"
            artifacts = Path(directory) / "artifacts"
            for service in ("inventoryservice", "orderservice"):
                config = root / "pyexample" / service / "config"
                config.mkdir(parents=True)
                (config / "docker_overrides.yaml").write_text(
                    "streams:\n"
                    "  softDeadline:\n"
                    "    duration: 1000\n"
                )
                (config / "config_vars.yaml").write_text("")
            with (
                patch.object(benchmark, "ROOT", root),
                patch.object(benchmark, "ARTIFACTS", artifacts),
            ):
                benchmark.prepare_python_configs()

            prepared = (
                artifacts / "python-config" / "orderservice.overrides.yaml"
            ).read_text()
            self.assertEqual(prepared.count("streams:\n"), 1)
            self.assertIn("endpoints:\n  orderProcessed:\n    enabled: false", prepared)
            self.assertIn("softDeadline:\n    duration: 1000", prepared)

    def test_python_compose_uses_benchmark_specific_overrides(self) -> None:
        contents = (
            Path(__file__).resolve().parent / "compose.python.yml"
        ).read_text()
        self.assertIn("BENCHMARK_PYTHON_CONFIG_DIR", contents)
        self.assertNotIn("/workspace/config/docker_overrides.yaml", contents)


class ScenarioConfigurationTest(unittest.TestCase):
    def test_isolated_request_configuration_reaches_compose_environment(self) -> None:
        args = Namespace(
            cores=2,
            loadgen_cores=6,
            duration="20s",
            vus=256,
            method="GET",
            payload_mode="invalid-json",
            expected_status=404,
            scenario="http_transport_only",
            target="http://orderservice:9091/probe",
        )
        language = next(
            item for item in benchmark.LANGUAGES if item.name == "cpp-boost"
        )
        env = benchmark.environment(args, language)
        self.assertEqual(env["BENCHMARK_METHOD"], "GET")
        self.assertEqual(env["BENCHMARK_PAYLOAD_MODE"], "invalid-json")
        self.assertEqual(env["BENCHMARK_EXPECTED_STATUS"], "404")
        self.assertEqual(env["BENCHMARK_SCENARIO"], "http_transport_only")
        self.assertEqual(env["BENCHMARK_TARGET"], args.target)

    def test_result_prefix_keeps_isolated_artifacts_separate(self) -> None:
        self.assertEqual(
            benchmark.artifact_name(
                Namespace(result_prefix="http-transport"), "results.json"
            ),
            "http-transport.results.json",
        )

    def test_native_grpc_bypass_is_explicit_and_native_only(self) -> None:
        args = Namespace(
            cores=2,
            loadgen_cores=6,
            duration="20s",
            vus=256,
            native_diagnostic_bypass_grpc=True,
        )
        native = next(
            item for item in benchmark.LANGUAGES
            if item.name == "cpp-boost-native"
        )
        framework = next(
            item for item in benchmark.LANGUAGES if item.name == "cpp-boost"
        )
        self.assertEqual(
            benchmark.environment(args, native)["NATIVE_DIAGNOSTIC_BYPASS_GRPC"],
            "true",
        )
        self.assertNotIn(
            "NATIVE_DIAGNOSTIC_BYPASS_GRPC",
            benchmark.environment(args, framework),
        )


class ResultAggregationTest(unittest.TestCase):
    def test_selects_entire_run_with_highest_throughput(self) -> None:
        language = benchmark.Language(
            "test", Path("/tmp/test"), Path("/tmp/test/compose.yml")
        )
        args = Namespace(
            cores=2,
            loadgen_cores=6,
            vus=256,
            duration="20s",
        )
        slower = {
            "request_count": 800,
            "requests_per_second": 40.0,
            "error_rate": 0.0,
            "latency_ms": {"avg": 1.0, "p95": 2.0, "p99": 3.0},
        }
        faster = {
            "request_count": 1000,
            "requests_per_second": 50.0,
            "error_rate": 0.01,
            "latency_ms": {"avg": 10.0, "p95": 20.0, "p99": 30.0},
        }

        result = benchmark.aggregate(language, [slower, faster], args)

        self.assertEqual(result["best_run"], 2)
        self.assertEqual(result["requests_total"], 1000)
        self.assertEqual(result["requests_per_second"], 50.0)
        self.assertEqual(result["error_rate"], 0.01)
        self.assertEqual(result["latency_ms"], faster["latency_ms"])


if __name__ == "__main__":
    unittest.main()
