from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("coro_signature_gate", Path(__file__).parent / "signatures" / "run.py")
assert SPEC and SPEC.loader
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


class CoroSignatureGateTests(unittest.TestCase):
    def execute(self, canonical, coro, policy=None, *, missing_coro=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for repository, source in (("cppservicelib", canonical), ("cppcoroservicelib", coro)):
                if repository == "cppcoroservicelib" and missing_coro:
                    continue
                headers = root / repository / "include" / "servicelib"
                headers.mkdir(parents=True)
                (headers / "consumer.hpp").write_text(source)
            policy_file = root / "policy.json"
            policy_file.write_text(json.dumps(policy or {"signature_deviations": {}}))
            output = root / "report.json"
            argv = ["run.py", "--root", str(root), "--policy", str(policy_file), "--output", str(output)]
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                code = GATE.main()
            return code, json.loads(output.read_text()) if output.exists() else None

    def test_same_declarations_pass(self):
        code, report = self.execute("void consume(int value);", "void consume(int message);")
        self.assertEqual(code, 0)
        self.assertEqual(report["shared_headers"], 1)

    def test_awaitable_is_not_silently_erased(self):
        code, report = self.execute("void consume(int value);", "boost::asio::awaitable<void> consume(int value);")
        self.assertEqual(code, 1)
        self.assertIn("deviation-mismatch:consumer.hpp", report["errors"])

    def test_reviewed_pair_is_exact_and_preserves_argument_types(self):
        policy = {"signature_deviations": {"consumer.hpp": {
            "reason": "Explicit coroutine return boundary, same argument type.",
            "canonical_only": ["callable||void consume ( int )"],
            "coro_only": ["callable||boost :: asio :: awaitable < void > consume ( int )"],
        }}}
        code, _ = self.execute("void consume(int value);", "boost::asio::awaitable<void> consume(int value);", policy)
        self.assertEqual(code, 0)
        code, report = self.execute("void consume(int value);", "boost::asio::awaitable<void> consume(long value);", policy)
        self.assertEqual(code, 1)
        self.assertIn("deviation-mismatch:consumer.hpp", report["errors"])

    def test_stale_header_allowance_fails(self):
        code, report = self.execute("void consume(int value);", "void consume(int value);", {"signature_deviations": {"deleted.hpp": {}}})
        self.assertEqual(code, 1)
        self.assertIn("stale-policy-header:deleted.hpp", report["errors"])

    def test_missing_runtime_is_not_an_empty_success(self):
        code, _ = self.execute("void consume(int value);", "", missing_coro=True)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
