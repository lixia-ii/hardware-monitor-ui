"""PawnIO 驱动检测与安装。"""
from pathlib import Path
import unittest
import sys

if sys.platform != "win32":
    raise unittest.SkipTest("Windows 桌面专属测试")
from unittest.mock import patch

from hwmonitor import paths
from hwmonitor.system import pawnio


class PawnIoTests(unittest.TestCase):
    def test_status_by_version(self):
        for found, expected in ((None, "missing"), ((1, 9, 0, 0), "outdated"), ((2, 1, 0, 0), "ok")):
            with patch("hwmonitor.system.pawnio.version", return_value=found):
                self.assertEqual(pawnio.status(), expected)

    def test_bundled_installer_and_library_present(self):
        self.assertTrue(pawnio.SETUP.exists())
        self.assertTrue((paths.LHM_DIR / "LibreHardwareMonitorLib.dll").exists())

    def test_install_runs_setup_and_rechecks(self):
        with patch("hwmonitor.system.pawnio.subprocess.run") as run, patch("hwmonitor.system.pawnio.status", return_value="ok"):
            self.assertTrue(pawnio.install())
        self.assertEqual(run.call_args.args[0][1:], ["-install"])
        with self.assertRaises(OSError):
            pawnio.install(Path("missing.exe"))
