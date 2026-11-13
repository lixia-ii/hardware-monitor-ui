"""Linux fixtures and real HTTP integration, runnable on both platforms."""
from concurrent.futures import Future
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from hwmonitor import paths, settings
from hwmonitor.core import management
from hwmonitor.sensors import reading
from hwmonitor.sensors.linux import LinuxBackend
from hwmonitor.server import Controller
from hwmonitor.system import linux_credentials
from hwmonitor.system.webbridge import Bridge, WebError


class LinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 10
        self.counts = {"eth0": NS(bytes_sent=100, bytes_recv=200), "docker0": NS(bytes_sent=500, bytes_recv=500)}
        self.api = NS(cpu_percent=lambda **kw: 25, virtual_memory=lambda: NS(total=1000, available=400),
                      net_io_counters=lambda **kw: self.counts,
                      net_if_stats=lambda: {k: NS(isup=True) for k in self.counts})
        route = self.root / "proc/net/route"
        route.parent.mkdir(parents=True)
        route.write_text("Iface Destination Gateway Flags RefCnt Use Metric Mask\neth0 00000000 0 0003 0 0 100 0\n")
        self.backend = LinuxBackend(self.api, self.root / "sys", self.root / "proc", clock=lambda: self.now)

    def test_warmup_memory_and_network_resets(self):
        first = self.backend.sample().data
        self.assertIsNone(first["cpu_load"])
        self.assertIsNone(first["net_up"])
        self.assertEqual(first["mem_used"], 600)
        self.assertEqual(first["mem_load"], 60)
        self.now += 2
        self.counts["eth0"] = NS(bytes_sent=300, bytes_recv=800)
        second = self.backend.sample().data
        self.assertEqual((second["cpu_load"], second["net_up"], second["net_down"]), (25, 100, 300))
        self.assertEqual(second["network_basis"]["interfaces"], ["eth0"])
        self.now += 1
        self.counts["eth0"] = NS(bytes_sent=1, bytes_recv=2)
        self.assertIsNone(self.backend.sample().data["net_up"])
        self.now += 1
        ident = second["nics"][0][0]
        self.assertIsNone(self.backend.sample(nic_ids=[ident]).data["net_up"])
        self.counts.pop("eth0")
        self.assertIsNone(self.backend.sample(nic_ids=[ident]).data["net_up"])
        self.counts["eth0"] = NS(bytes_sent=50, bytes_recv=50)
        self.now += 1
        self.assertIsNone(self.backend.sample(nic_ids=[ident]).data["net_up"])

    def test_hwmon_identity_missing_and_real_zero(self):
        for index, chip in enumerate(("coretemp", "nvme", "nvme", "nct6798")):
            hw = self.root / f"sys/class/hwmon/hwmon{index}"
            hw.mkdir(parents=True)
            (hw / "name").write_text(chip)
            (hw / "temp1_input").write_text("53000")
            (hw / "temp2_input").write_text("bad")
        hw = self.root / "sys/class/hwmon/hwmon3"
        (hw / "fan1_input").write_text("0")
        (hw / "fan2_input").write_text("0")
        (hw / "fan2_label").write_text("CPU fan")
        snap = self.backend.sample()
        self.assertEqual(snap.data["cpu_temp"], 53)
        self.assertEqual(snap.data["cpu_fan"], 0)
        self.assertEqual(snap.data["case_fans"][0][1], 0)
        ids = snap.data["sensor_ids"]["extra_temps"]
        self.assertEqual(len(ids), len(set(ids)))
        parsed = reading.read(source=NS(sample=lambda: snap.tree), allow_nvidia=False)
        self.assertEqual(parsed["cpu_temp"], 53)
        self.assertEqual(parsed["mem_used"], 600)
        self.assertFalse(snap.data["error"])

    def test_hwmon_identity_ignores_class_device_index(self):
        ids = []
        for index in (1, 2):
            device = self.root / f"sys/devices/pci0000_00/nvme/nvme0/hwmon{index}"
            device.mkdir(parents=True)
            (device / "name").write_text("nvme")
            (device / "temp1_input").write_text("40000")
            link = self.root / "sys/class/hwmon/hwmon0"
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                link.symlink_to(device, target_is_directory=True)
            except OSError:
                self.skipTest("需要符号链接权限")
            ids.append(self.backend.sample().data["sensor_ids"]["extra_temps"])
            link.unlink()
        self.assertEqual(ids[0], ids[1])

    def test_optional_permissions_and_uuid_selection(self):
        with patch.object(self.backend, "_sensors", side_effect=PermissionError):
            self.assertEqual(self.backend.sample().data["mem_load"], 60)
        self.backend.enable_nvidia = True
        cards = [("same", 40, 0, 5, 1, 10, "uuid-1"), ("same", 70, None, 90, 3, 10, "uuid-2")]
        with patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
            self.assertEqual(self.backend.sample("nvidia:uuid-2").data["gpu_temp"], 70)
            self.assertIsNone(self.backend.sample("nvidia:missing").data["gpu_temp"])

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "Fernet 测试需要 cryptography（Linux 自动安装）")
    def test_credentials_and_missing_key(self):
        with patch.object(paths, "DATA", self.root):
            token = linux_credentials.protect("private-password")
            self.assertNotIn("private-password", token)
            self.assertEqual(linux_credentials.unprotect(token), "private-password")
            if sys.platform == "linux":
                self.assertEqual((self.root / "credentials.key").stat().st_mode & 0o777, 0o600)
            (self.root / "settings.json").write_text(json.dumps({"routers": [{"password_protected": token}]}))
            (self.root / "credentials.key").unlink()
            with self.assertRaises(ValueError):
                linux_credentials.unprotect(token)
            # Re-entering a password recovers: a new key is made, the old secret stays unreadable.
            fresh = linux_credentials.protect("new")
            self.assertEqual(linux_credentials.unprotect(fresh), "new")
            with self.assertRaises(ValueError):
                linux_credentials.unprotect(token)

    def test_controller_http_persistence_failure_conflict_and_shutdown(self):
        with patch.object(paths, "DATA", self.root):
            controller = Controller(settings.validate({"interval": .5}), self.backend, port=0, data_port=0)
            # Allocate each listener independently; real collisions are separately tested.
            controller.data_port = None
            failures = []
            def run():
                try:
                    controller.run()
                except Exception as error:
                    failures.append(error)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while controller.management.server is None and time.monotonic() < deadline:
                    time.sleep(.01)
                port = controller.management.server.server_address[1]
                self.assertEqual(controller.management.server.server_address[0], "127.0.0.1")
                def request(method, path, body=None):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    raw = response.read()
                    conn.close()
                    return response.status, raw
                current = json.loads(request("GET", "/api/settings")[1])
                self.assertFalse(current["capabilities"]["desktop"])
                update = {"revision": current["revision"], "changes": {"interval": 2}}
                with patch("hwmonitor.settings.save", side_effect=OSError):
                    self.assertEqual(request("POST", "/api/settings", update)[0], 500)
                self.assertEqual(controller.settings["interval"], .5)
                self.assertEqual(request("POST", "/api/settings", update)[0], 200)
                self.assertEqual(request("POST", "/api/settings", update)[0], 409)
                update = {"revision": management.revision(controller.settings), "changes": {"topmost": True}}
                self.assertEqual(request("POST", "/api/settings", update)[0], 400)
                self.assertEqual(settings.load()["interval"], 2)
                with patch("hwmonitor.sensors.hwinfo.load", return_value={"cpu": "test"}):
                    for route in ("/api/state", "/api/history", "/api/history.csv", "/api/diagnostics"):
                        status, raw = request("GET", route)
                        self.assertEqual(status, 200, raw)
                        self.assertNotIn(b"password_protected", raw)
                self.assertTrue(controller.history)
            finally:
                controller.stopping.set()
                worker.join(timeout=12)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failures, failures)
            self.assertFalse(controller.collector.is_alive())
            self.assertIsNone(controller.management.server)
            self.assertTrue((self.root / "history.sqlite3").exists())

    def test_collision_and_closed_bridge(self):
        one = Controller(settings.validate({}), self.backend, port=0)
        with patch.object(paths, "DATA", self.root):
            one.start()
            try:
                port = one.management.server.server_address[1]
                other = Controller(settings.validate({}), self.backend, port=port)
                with self.assertRaises(OSError), self.assertLogs("hwmonitor.system.management_server", "ERROR"):
                    other.start()
            finally:
                one.stop()
        bridge = Bridge()
        future = Future()
        bridge.pending.put((future, "update_settings", {}))
        bridge.close()
        with self.assertRaises(WebError):
            future.result()
        with self.assertRaises(WebError):
            bridge.call("state")

    def test_no_desktop_imports(self):
        code = "from hwmonitor import bootstrap; from hwmonitor import server; import sys; assert not any(x in sys.modules for x in ['tkinter', 'clr', 'hwmonitor.system.tray', 'hwmonitor.ui.main_window'])"
        subprocess.run([sys.executable, "-c", code], check=True, capture_output=True)


@unittest.skipUnless(sys.platform == "linux", "Linux 文件锁")
class LinuxLockTests(unittest.TestCase):
    def test_exclusive_lock_and_release(self):
        from hwmonitor.server import InstanceLock
        with tempfile.TemporaryDirectory() as directory, patch.object(paths, "DATA", Path(directory)), patch.dict("os.environ", {"RUNTIME_DIRECTORY": directory}):
            with InstanceLock():
                with self.assertRaises(OSError):
                    with InstanceLock():
                        pass
            with InstanceLock():
                pass
