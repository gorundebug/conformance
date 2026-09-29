from __future__ import annotations

import runpy
import sys
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
FRAMEWORKS = {"go", "cpp", "cppcoro", "python", "rust", "typescript"}
NATIVES = {"go-native", "cpp-native", "cppboost-native", "python-native", "rust-native", "typescript-native"}


def load(relative: str):
    path = HERE / relative
    with mock.patch.object(sys, "path", [str(path.parent), *sys.path]):
        return runpy.run_path(str(path))


class CoroRetirementTest(unittest.TestCase):
    def test_retired_framework_overlays_are_removed(self) -> None:
        for directory, retired, current in (
            ("scenarios", "compose.cppboost.yml", "compose.cppcoro.yml"),
            ("profiling/examples", "compose.cppboost.yml", "compose.cppcoro.yml"),
            ("benchmarks/examples", "compose.cpp-boost.yml", "compose.cpp-coro.yml"),
        ):
            with self.subTest(directory=directory):
                self.assertFalse((HERE / directory / retired).exists())
                self.assertTrue((HERE / directory / current).is_file())

    def test_live_language_matrices_keep_coro_and_userver(self) -> None:
        for path in ("metrics/run.py", "tracing/run.py", "kafka/run.py"):
            with self.subTest(path=path):
                runner = load(path)
                self.assertEqual({language.name for language in runner["LANGUAGES"]}, FRAMEWORKS)
                coro = next(language for language in runner["LANGUAGES"] if language.name == "cppcoro")
                with mock.patch.object(runner["cpp_source_cache"], "configure_environment") as configure:
                    env = runner["language_env"](coro)
                self.assertEqual(Path(env["SERVICELIB_SOURCE_CONTEXT"]).name, "cppcoroservicelib")
                self.assertEqual(env["USE_LOCAL_MODULES"], "1")
                self.assertEqual(configure.call_args.args[1].name, "cppcoroservicelib")

    def test_scenarios_keep_native_baselines(self) -> None:
        runner = load("scenarios/run.py")
        self.assertEqual({item.name for item in runner["IMPLEMENTATIONS"]}, FRAMEWORKS | NATIVES)
        self.assertEqual(runner["FRAMEWORK_IMPLEMENTATIONS"], FRAMEWORKS)

    def test_dashboards_and_kubernetes_keep_full_coro_checks(self) -> None:
        dashboards = load("dashboards/run.py")
        self.assertEqual(set(dashboards["LANGUAGES"]), FRAMEWORKS)
        self.assertEqual(set(dashboards["EXAMPLES"]), FRAMEWORKS)
        self.assertEqual(set(dashboards["CHECKS"]), FRAMEWORKS)
        self.assertEqual(
            {check.dashboard for check in dashboards["CHECKS"]["cppcoro"]},
            {"07_http_server", "08_http_client", "09_grpc_server", "10_grpc_client", "11_runtime", "12_kafka_client"},
        )
        kubernetes = load("kubernetes/run.py")
        self.assertEqual(set(kubernetes["EXAMPLES"]), FRAMEWORKS)
        self.assertEqual(
            Path(kubernetes["language_source_environment"]("cppcoro")["SERVICELIB_SOURCE_CONTEXT"]).name,
            "cppcoroservicelib",
        )

    def test_sanitizers_use_actual_coro_model_language(self) -> None:
        runner = load("sanitizers/run.py")
        self.assertEqual(set(runner["IMPLEMENTATIONS"]), FRAMEWORKS)
        self.assertEqual(runner["IMPLEMENTATION_LANGUAGES"]["cppcoro"], "cppCoro")
        self.assertEqual(runner["IMPLEMENTATION_LANGUAGES"]["cpp"], "cppUserver")
        self.assertEqual(runner["IMPLEMENTATION_SANITIZERS"]["cppcoro"], ("runtime", "asan", "tsan"))
        runner["validate_adapter_coverage"]()

    def test_aggregate_rejects_old_or_missing_coro_results(self) -> None:
        runner = load("aggregate.py")
        for suite in ("kafka", "tracing", "metrics", "dashboards", "logging", "kubernetes", "call-semantics"):
            with self.subTest(suite=suite):
                self.assertEqual(runner["LANGUAGE_SUITES"][suite], FRAMEWORKS)
                self.assertTrue(runner["passed"](suite, {"status": "pass", "languages": sorted(FRAMEWORKS)})[0])
                for invalid in (FRAMEWORKS - {"cppcoro"}, (FRAMEWORKS - {"cppcoro"}) | {"cppboost"}):
                    self.assertFalse(runner["passed"](suite, {"status": "pass", "languages": sorted(invalid)})[0])

    def test_performance_runners_and_graph_preparation_select_coro(self) -> None:
        expected_benchmark = {name.replace("cppboost-native", "cpp-boost-native").replace("cppcoro", "cpp-coro") for name in FRAMEWORKS | NATIVES}
        self.assertEqual(load("benchmarks/run.py")["LANGUAGES"], expected_benchmark)
        embedded = load("benchmarks/examples/run.py")
        self.assertEqual({item.name for item in embedded["LANGUAGES"]}, expected_benchmark)
        self.assertEqual(set(load("benchmarks/examples/call_semantics.py")["VARIANTS"]), {name.replace("cppcoro", "cpp-coro") for name in FRAMEWORKS})
        self.assertEqual(set(load("call_semantics/run.py")["VARIANTS"]), FRAMEWORKS)
        self.assertEqual(set(load("profiling/run.py")["ALL_LANGUAGES"]), FRAMEWORKS | NATIVES)
        embedded_profiling = load("profiling/examples/run.py")
        self.assertEqual({item.name for item in embedded_profiling["LANGUAGES"]}, FRAMEWORKS | NATIVES)

    def test_profile_comparison_uses_coro_not_retired_boost(self) -> None:
        runner = load("profiling/run.py")
        results = {}
        for name, rps in (("cppcoro", 50), ("cppboost-native", 100)):
            results[name] = {
                service: {
                    "load": {"requests_per_second": rps, "latency_ms": {"p50": 1, "p95": 2, "p99": 3}},
                    "allocation": {"allocations_per_request": 2, "allocated_bytes_per_request": 64},
                    "scheduler": {"request_count": 10, "totals": {
                        "runtime_ns": 100, "runqueue_wait_ns": 1, "timeslices": 2,
                        "voluntary_context_switches": 3, "involuntary_context_switches": 4,
                    }},
                }
                for service in runner["SERVICES"]
            }
        report = runner["compare_framework_native"](results)
        for service in runner["SERVICES"]:
            self.assertEqual(report[service]["framework_to_native_rps"], 0.5)
            self.assertEqual(set(report[service]["scheduler_per_request"]), {"cppcoro", "cppboost-native"})


if __name__ == "__main__":
    unittest.main()
