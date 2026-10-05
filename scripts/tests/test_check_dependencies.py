"""Run with: python -m unittest discover -s scripts/tests."""

import contextlib
import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "check_dependencies.py"
SPEC = importlib.util.spec_from_file_location("check_dependencies", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class AdvisoryChecksTests(unittest.TestCase):
    def test_withdrawn_python_advisory_does_not_block_release(self):
        response = {"vulnerabilities": [
            {"id": "ACTIVE", "fixed_in": ["2.0"]},
            {"id": "WITHDRAWN", "withdrawn": "2026-01-01"},
        ]}
        with patch.object(CHECK, "request_json", return_value=response):
            result = CHECK.check_python(("example", "1.0"))
        self.assertEqual([advisory["id"] for advisory in result], ["ACTIVE"])

    def test_missing_advisory_response_is_not_treated_as_clean(self):
        with patch.object(CHECK, "request_json", return_value={}):
            with self.assertRaises(ValueError):
                CHECK.check_python(("example", "1.0"))

    def test_unavailable_registry_returns_incomplete_and_nonzero(self):
        output = io.StringIO()
        with patch.object(CHECK, "request_json", side_effect=urllib.error.URLError("private")):
            with contextlib.redirect_stdout(output):
                code = CHECK.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["unavailable_checks"]["PyPI"], 0)
        self.assertEqual(result["unavailable_checks"]["npm"], 1)
        self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
