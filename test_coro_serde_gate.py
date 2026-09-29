"""Coro replaces the old runtime without weakening cross-language serde checks."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch


DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parent


class CoroSerdeGateTest(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {"DEPENDENCIES_DIR": str(ROOT)}):
            self.gate = runpy.run_path(str(DIRECTORY / "serde/run.py"))

    def test_coro_source_contract_preserves_shared_serde(self) -> None:
        self.assertEqual(self.gate["CORO"], ROOT / "cppcoroservicelib")
        result = self.gate["verify_sources"]()
        self.assertTrue(result["canonical_coro_headers_byte_identical"])
        self.assertEqual(result["required_case_markers_per_cpp_runtime"], 10)
        self.assertEqual(len(result["headers"]), 2)

    def test_wire_comparison_still_rejects_any_difference(self) -> None:
        compare = self.gate["compare_wire_fixtures"]
        reference = {"integer": "01000000", "string": "02abcd"}
        compare(reference, dict(reference), "coro-cpp")
        for changed in ({"integer": "02000000", "string": "02abcd"},
                        {"integer": "01000000"}, {**reference, "extra": "00"}):
            with self.subTest(candidate=changed):
                with self.assertRaisesRegex(RuntimeError, "wire fixtures differ"):
                    compare(reference, changed, "coro-cpp")

    def test_custom_serde_requires_all_four_type_erasure_checks(self) -> None:
        values = '\n'.join(f'{name}={{"ok":true}}' for name in
                           ("order_item", "order_item_result", "order", "order_state"))
        run = self.gate["subprocess"].run
        for count in (None, 3, 4):
            output = values + (f"\ngenerated_type_erasure_checks={count}\n" if count is not None else "\n")
            with self.subTest(count=count), patch.object(
                self.gate["subprocess"], "run",
                return_value=SimpleNamespace(returncode=0, stdout=output),
            ):
                if count == 4:
                    values_result, evidence = self.gate["json_probe"]("coro", ["probe"], ROOT)
                    self.assertEqual(len(values_result), 4)
                    self.assertEqual(evidence["generated_type_erasure_checks"], 4)
                else:
                    with self.assertRaises(RuntimeError):
                        self.gate["json_probe"]("coro", ["probe"], ROOT)


if __name__ == "__main__":
    unittest.main()
