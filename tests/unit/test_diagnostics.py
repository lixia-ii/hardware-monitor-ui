"""诊断报告不泄露环境变量与私有路径。"""
import json
import unittest

from hwmonitor.core import diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_no_environment_or_private_exception_path(self):
        data = diagnostics.report({"error": "C:/Users/private/file", "cpu_temp": 70}, {"error": "C:/private"}, [], {})
        output = json.dumps(data)
        self.assertNotIn("private", output)
        self.assertNotIn("USERNAME", output)
        self.assertEqual(data["readings"]["cpu_temp"], 70)
