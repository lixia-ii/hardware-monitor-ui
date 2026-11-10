"""源码运行时补装 Python 依赖。"""
import subprocess
import unittest
from unittest.mock import patch

from hwmonitor import paths
from hwmonitor.system import deps


class InstallTests(unittest.TestCase):
    def test_runs_editable_install_in_program_dir(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(deps.install())
        args = run.call_args.args[0]
        self.assertEqual(args[1:4], ["-m", "pip", "install"])
        self.assertEqual(args[-2:], ["-e", str(paths.BASE)])
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_failure_reported(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "boom")), \
                self.assertLogs("hwmonitor.system.deps", "ERROR"):
            self.assertFalse(deps.install())
        with patch("subprocess.run", side_effect=OSError("no python")), self.assertLogs("hwmonitor.system.deps"):
            self.assertFalse(deps.install())


if __name__ == "__main__":
    unittest.main()
