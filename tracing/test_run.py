from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "tracing_run", Path(__file__).with_name("run.py")
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CppOtlpConfigTests(unittest.TestCase):
    def test_cpp_runtime_uses_the_workspace_binary_it_just_built(self) -> None:
        language = next(
            language for language in MODULE.LANGUAGES if language.name == "cpp"
        )
        command = MODULE.compose_command(language, "up")

        self.assertIn(
            str(language.example / "docker-compose.integration.generated.yml"),
            command,
        )
        self.assertFalse(
            any("cpp-runtime.generated.yml" in argument for argument in command)
        )

    def test_otlp_only_service_gains_required_default_grpc_client_config(self) -> None:
        rendered = MODULE.inject_cpp_otlp_config(
            "components_manager:\n"
            "  components:\n"
            "    servicelib-runtime:\n"
            "      config-path: config/config.yaml\n"
            "  default_task_processor: main-task-processor\n",
            source=Path("analyticsservice/static_config.yaml"),
            otlp_variable="analyticsServiceOtlpEndpoint",
            service="analyticsservice",
        )

        self.assertIn("    grpc-otlp-factory:\n", rendered)
        self.assertIn("    grpc-client-common:\n", rendered)
        self.assertIn("    grpc-blocking-task-processor:\n", rendered)

    def test_existing_default_grpc_client_config_is_preserved(self) -> None:
        rendered = MODULE.inject_cpp_otlp_config(
            "components_manager:\n"
            "  components:\n"
            "    grpc-blocking-task-processor:\n"
            "      worker_threads: 1\n"
            "    grpc-client-common:\n"
            "      blocking-task-processor: grpc-blocking-task-processor\n"
            "    servicelib-runtime:\n"
            "      config-path: config/config.yaml\n"
            "  default_task_processor: main-task-processor\n",
            source=Path("orderservice/static_config.yaml"),
            otlp_variable="orderServiceOtlpEndpoint",
            service="orderservice",
        )

        self.assertEqual(rendered.count("    grpc-client-common:\n"), 1)
        self.assertEqual(rendered.count("    grpc-otlp-factory:\n"), 1)


class TraceEventNormalizationTests(unittest.TestCase):
    def test_grpc_input_allows_async_result_events_to_race_with_acceptance(self) -> None:
        go_order = [
            "begin_request",
            "consume_message",
            "eof",
            "send",
            "result_consumed",
            "result_received",
        ]
        rust_order = [
            "begin_request",
            "send",
            "result_consumed",
            "consume_message",
            "eof",
            "result_received",
        ]

        self.assertEqual(
            MODULE._normalize_span_events("grpc.input", go_order),
            MODULE._normalize_span_events("grpc.input", rust_order),
        )

    def test_grpc_input_still_rejects_causal_event_reversal(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError, "send before result_consumed"
        ):
            MODULE._normalize_span_events(
                "grpc.input",
                ["begin_request", "result_consumed", "send"],
            )

    def test_other_span_event_order_is_not_rewritten(self) -> None:
        events = ["handle_response", "grpc_call"]
        self.assertIs(
            MODULE._normalize_span_events("grpc.output", events), events
        )


if __name__ == "__main__":
    unittest.main()
