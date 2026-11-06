import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import alerts
import config
import diagnostics
import history
import sensors
from collector import Collector


class SettingsTests(unittest.TestCase):
    def test_bad_structure_and_fields(self):
        for raw in ([], None, 7, "bad"):
            self.assertEqual(config.validate(raw), config.DEFAULTS)
        result = config.validate({"alerts": [], "floating": None, "display": [1], "interval": float("nan")})
        self.assertEqual(result, config.DEFAULTS)
        result = config.validate({"alerts": {"cpu_temp": "broken", "gpu_temp": 500}, "nic_ids": [1, "nic"],
                                  "floating": {"x": "bad"}, "interval": -1})
        self.assertEqual(result["alerts"]["cpu_temp"], 85)
        self.assertEqual(result["alerts"]["gpu_temp"], 110)
        self.assertEqual(result["nic_ids"], ["nic"])
        self.assertEqual(result["interval"], .5)

    def test_empty_gpu_id_means_automatic(self):
        self.assertIsNone(config.validate({"gpu_id": ""})["gpu_id"])
        self.assertEqual(config.validate({"gpu_id": "/gpu-nvidia/0"})["gpu_id"], "/gpu-nvidia/0")

    def test_atomic_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            config.save({"close_action": "tray"}, path)
            with patch("config.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    config.save({"close_action": "exit"}, path)
            self.assertEqual(config.load(path)["close_action"], "tray")
            self.assertEqual(len(list(Path(d).iterdir())), 1)

    def test_corrupt_json_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text('{"truncated":', encoding="utf-8")
            with self.assertLogs("config", "ERROR"):
                self.assertEqual(config.load(path), config.DEFAULTS)
            options = config.validate({"display": {"cpu_temp": {"name": "处理器", "visible": False}}, "nic_ids": []})
            config.save(options, path)
            self.assertEqual(config.load(path), options)

    def test_legacy_data_moves_together_without_overwriting_new_data(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            data = base / "data"
            (base / "settings.json").write_text('{"close_action": "tray"}', encoding="utf-8")
            for name in ("history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm"):
                (base / name).write_bytes(name.encode())
            config.migrate_legacy_data(base, data)
            self.assertEqual(config.load(data / "settings.json")["close_action"], "tray")
            self.assertTrue((data / "history.sqlite3-wal").exists())
            self.assertFalse((base / "history.sqlite3").exists())
            (base / "settings.json").write_text('{"close_action": "exit"}', encoding="utf-8")
            (base / "history.sqlite3-wal").write_bytes(b"different-database")
            config.migrate_legacy_data(base, data)
            self.assertEqual(config.load(data / "settings.json")["close_action"], "tray")
            self.assertNotEqual((data / "history.sqlite3-wal").read_bytes(), b"different-database")


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = alerts.AlertEngine()
        self.cfg = copy.deepcopy(config.DEFAULT_ALERTS)
        self.data = {"cpu_temp": 86, "gpu_temp": None, "cpu_fan": 1000, "gpu_id": "g1"}

    def check(self, at, **values):
        self.data.update(values)
        return self.engine.check(self.data, self.cfg, at)

    def test_sustained_only(self):
        self.assertFalse(self.check(0))
        self.assertFalse(self.check(9))
        self.assertEqual(len(self.check(10)), 1)

    def test_threshold_jitter_does_not_bypass_cooldown(self):
        self.check(0)
        self.assertTrue(self.check(10))
        self.assertFalse(self.check(11, cpu_temp=84))
        self.assertFalse(self.check(12, cpu_temp=86))
        self.assertFalse(self.check(13, cpu_temp=78))
        self.assertFalse(self.check(14, cpu_temp=86))
        self.assertFalse(self.check(24))
        for t in range(44, 305, 20):
            self.check(t)
        self.assertTrue(self.check(310))

    def test_missing_or_long_gap_breaks_duration(self):
        self.check(0)
        self.check(9, cpu_temp=None)
        self.assertFalse(self.check(10, cpu_temp=90))
        self.assertFalse(self.check(19))
        self.assertTrue(self.check(20))
        self.engine = alerts.AlertEngine()
        self.check(0)
        self.assertFalse(self.check(100))
        self.assertTrue(self.check(110))

    def test_fan_missing_and_low_temperature_stop_are_normal(self):
        self.cfg.update(enabled=False, fan_enabled=True)
        for t in (0, 30, 60):
            self.assertFalse(self.check(t, cpu_temp=50, cpu_fan=0, gpu_temp=50, gpu_fan=0))
        self.assertFalse(self.check(61, cpu_temp=90, cpu_fan=None, gpu_temp=90, gpu_fan=None))
        self.assertFalse(self.check(100))

    def test_hot_low_fan_sustained_and_recovery(self):
        self.cfg.update(enabled=False, fan_enabled=True)
        self.assertFalse(self.check(0, cpu_fan=0))
        self.assertFalse(self.check(29))
        self.assertTrue(self.check(30))
        self.assertFalse(self.check(31, cpu_fan=1000))
        self.assertFalse(self.check(32, cpu_fan=0))
        self.assertFalse(self.check(62))

    def test_gpu_switch_does_not_inherit_sustained_state(self):
        self.data["cpu_temp"] = None
        self.assertFalse(self.check(0, gpu_temp=90))
        self.assertFalse(self.check(9))
        self.assertFalse(self.check(10, gpu_id="g2"))
        self.assertTrue(self.check(20))
        self.assertFalse(self.check(21, gpu_id="g1"))


class HistoryTests(unittest.TestCase):
    def test_unique_nvidia_provider_ids_share_one_history_choice(self):
        groups = history.gpu_groups([
            ("/gpu-nvidia/0", "NVIDIA GeForce GTX 1060 6GB"),
            ("nvidia:GPU-A", "nvidia  geforce GTX 1060 6GB"),
            ("/gpu-nvidia/0", "NVIDIA GeForce GTX 1060 6GB"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups["nvidia:GPU-A"][1], {"/gpu-nvidia/0", "nvidia:GPU-A"})

    def test_same_model_cards_and_ambiguous_provider_matches_stay_separate(self):
        identities = ("/gpu-nvidia/0", "/gpu-nvidia/1", "nvidia:A", "nvidia:B")
        for ids in (identities[:2], identities[2:], identities[:3], identities):
            with self.subTest(ids=ids):
                groups = history.gpu_groups([(identity, "NVIDIA RTX") for identity in ids])
                self.assertEqual(len(groups), len(ids))
                self.assertTrue(all(members == {identity} for identity, (_, members) in groups.items()))

    def test_different_models_or_unknown_providers_never_merge_by_name(self):
        for devices in (
            [("/gpu-nvidia/0", "RTX 4060"), ("nvidia:A", "RTX 4060 Ti")],
            [("/gpu-amd/0", "GPU"), ("nvidia:A", "GPU")],
            [("a", None), ("b", None)],
        ):
            self.assertEqual(len(history.gpu_groups(devices)), 2)

    def test_provider_aliases_keep_both_history_parts_and_filter_other_cards(self):
        ids = frozenset(("/gpu-nvidia/0", "nvidia:A"))
        rows = [(t, dict(gpu_id=identity, gpu_temp=temp, gpu_load=20, gpu_mem_load=30,
                         cpu_temp=60, net_up=100, net_down=200, net_selection="nic1"))
                for t, identity, temp in ((0, "/gpu-nvidia/0", 50), (5, "nvidia:A", 90), (10, "nvidia:B", 100))]
        self.assertEqual(history.segments(rows, "gpu_temp", 0, 10, 100, gpu_ids=ids), [[(0, 50), (5, 90)]])
        filtered = [history.filter_sample(values, ids, "nic1") for _, values in rows]
        self.assertEqual([values["gpu_temp"] for values in filtered], [50, 90, None])
        self.assertEqual([values["cpu_temp"] for values in filtered], [60, 60, 60])
        self.assertEqual([values["gpu_load"] for values in filtered], [20, 20, None])
        self.assertEqual([values["gpu_mem_load"] for values in filtered], [30, 30, None])
        self.assertIsNone(history.filter_sample(rows[0][1], ids, "nic2")["net_up"])
        self.assertEqual(rows[2][1]["gpu_temp"], 100)  # Raw history is retained unchanged.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "aliases.csv"
            history.export_csv(path, [(t, values) for (t, _), values in zip(rows, filtered)])
            import csv
            with path.open(encoding="utf-8-sig", newline="") as f:
                exported = list(csv.DictReader(f))
            self.assertEqual([row["gpu_temp"] for row in exported], ["50", "90", ""])

    def test_row_limit_with_duplicate_and_out_of_order_timestamps(self):
        with tempfile.TemporaryDirectory() as d, patch("history.MAX_ROWS", 3):
            store = history.HistoryStore(Path(d) / "history.db")
            try:
                now = time.time()
                for t in (now, now, now - 5, now + 5, now - 90000):
                    store.append(t, {"cpu_temp": 50})
                self.assertEqual([t for t, _ in store.load()], [now, now, now + 5])
                self.assertEqual(store.row_count, 3)
            finally:
                store.close()

    def test_reduction_preserves_order_extrema_and_gaps_in_same_bucket(self):
        rows = [(i, {"cpu_temp": v}) for i, v in enumerate((50, 90, 10, 50, None, 60, 20, 80, 60))]
        self.assertEqual(history.segments(rows, "cpu_temp", 0, 20, 1),
                         [[(0, 50), (1, 90), (2, 10), (3, 50)], [(5, 60), (6, 20), (7, 80), (8, 60)]])

    def test_restart_and_24_hour_retention(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.db"
            store = history.HistoryStore(path)
            now = time.time()
            store.append(now - 90000, {"gpu_id": "old"})
            store.append(now, {"gpu_id": "a", "gpu_temp": 95})
            store.close()
            store = history.HistoryStore(path)
            try:
                self.assertEqual(store.load(), [(now, {"gpu_id": "a", "gpu_temp": 95})])
            finally:
                store.close()

    def test_reduction_preserves_spike_and_device_boundary(self):
        rows = [(t, {"gpu_temp": 100 if t == 7 else 50, "gpu_id": "a" if t < 15 else "b"}) for t in range(30)]
        runs = history.segments(rows, "gpu_temp", 0, 30, 1, "a")
        points = [p for run in runs for p in run]
        self.assertIn((7, 100), points)
        self.assertTrue(all(t < 15 for t, _ in points))
        self.assertLessEqual(len(points), 4)

    def test_missing_and_sleep_leave_gaps(self):
        rows = [(0, {"cpu_temp": 50}), (5, {"cpu_temp": None}), (10, {"cpu_temp": 60}), (100, {"cpu_temp": 90})]
        self.assertEqual(len(history.segments(rows, "cpu_temp", 0, 100, 100)), 3)

    def test_csv_is_excel_readable_and_preserves_device(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.csv"
            history.export_csv(path, [(time.time(), {"gpu_id": "a", "gpu_name": "显卡", "gpu_temp": 88})])
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("显卡", path.read_text(encoding="utf-8-sig"))

    def test_invalid_saved_values_become_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store = history.HistoryStore(Path(d) / "history.db")
            try:
                store.append(time.time(), {"gpu_id": [], "cpu_temp": "bad"})
                self.assertIsNone(store.load()[0][1]["cpu_temp"])
                self.assertIsNone(store.load()[0][1]["gpu_id"])
            finally:
                store.close()


def leaf(text, value, typ):
    return {"Text": text, "Value": value, "Type": typ, "Children": []}


def hardware(hwid, name, children):
    return {"HardwareId": hwid, "Text": name, "Children": children}


class SensorTests(unittest.TestCase):
    def parse_tree(self, tree):
        out = {"temps": [], "fans": [], "loads": [], "data": [], "extra": {}, "hw": [], "by_id": True}
        sensors._walk(tree, None, out)
        return out

    def test_uuid_selection_survives_lhm_reconnection_without_cross_fill(self):
        tree = self.tree()
        tree["Children"].append(hardware("/gpu-nvidia/1", "NVIDIA two", [leaf("GPU Core", "80 °C", "Temperature")]))
        cards = [("NVIDIA one", 65, 20, 50, 1024, 8192, "A"), ("NVIDIA two", 80, 70, 10, 6144, 8192, "B")]
        with patch("sensors._lhm", return_value=self.parse_tree(tree)), patch("sensors._nvidia", return_value=cards):
            data = sensors.read("nvidia:B")
        self.assertEqual(data["gpu_id"], "nvidia:B")
        self.assertEqual(data["gpu_temp"], 80)
        self.assertEqual(data["gpu_load"], 10)
        self.assertEqual(data["gpu_mem_load"], 75)
        self.assertIsNone(data["gpu_fan"])

    def test_identical_gpu_names_use_uuid_provider_only(self):
        tree = self.tree()
        tree["Children"][1]["Text"] = "RTX"
        tree["Children"].append(hardware("/gpu-nvidia/1", "RTX", [leaf("GPU Core", "75 °C", "Temperature")]))
        cards = [("RTX", 65, 20, 50, 1024, 8192, "A"), ("RTX", 80, 70, 10, 6144, 8192, "B")]
        with patch("sensors._lhm", return_value=self.parse_tree(tree)), patch("sensors._nvidia", return_value=cards):
            data = sensors.read("nvidia:B")
        self.assertEqual((data["gpu_id"], data["gpu_temp"], data["gpu_mem_load"]), ("nvidia:B", 80, 75))
        self.assertIsNone(data["gpu_fan"])

    def test_pinned_missing_gpu_does_not_select_other_card(self):
        cards = [("NVIDIA one", 65, 20, 50, 1024, 8192, "A")]
        for connection in (dict(return_value=self.parse_tree(self.tree())), dict(side_effect=OSError("offline"))):
            with patch("sensors._lhm", **connection), patch("sensors._nvidia", return_value=cards), patch("sensors.lhm_running", return_value=False):
                data = sensors.read("/gpu-intel/absent")
            self.assertIsNone(data["gpu_temp"])
            self.assertIsNone(data["gpu_id"])

    def test_dynamic_sensor_identity_survives_other_device_disconnect(self):
        tree = self.tree()
        tree["Children"].append(hardware("/nvme/1", "SSD", [leaf("Composite Temperature", "55 °C", "Temperature")]))
        for i in range(2):
            tree["Children"].append(hardware(f"/lpc/{i}", "Board", [leaf("Case Fan", "800 RPM", "Fan")]))
        with patch("sensors._lhm", return_value=self.parse_tree(tree)), patch("sensors._nvidia", return_value=[]):
            before = sensors.read()
        tree["Children"] = [n for n in tree["Children"] if n["HardwareId"] not in ("/nvme/0", "/lpc/0")]
        with patch("sensors._lhm", return_value=self.parse_tree(tree)), patch("sensors._nvidia", return_value=[]):
            after = sensors.read()
        self.assertEqual(before["sensor_ids"]["extra_temps"][1], after["sensor_ids"]["extra_temps"][0])
        self.assertEqual(before["sensor_ids"]["case_fans"][1], after["sensor_ids"]["case_fans"][0])
        self.assertNotEqual(*before["sensor_ids"]["case_fans"])

    def tree(self):
        return {"Children": [hardware("/amdcpu/0", "Ryzen", [leaf("Core (Tctl)", "100 °C", "Temperature"),
                    leaf("Core (Tdie)", "158 °F", "Temperature"), leaf("CPU Total", "25 %", "Load")]),
                hardware("/gpu-nvidia/0", "NVIDIA one", [leaf("GPU Core", "65 °C", "Temperature"),
                    leaf("GPU Core", "50 %", "Load"), leaf("GPU Fan", "0 RPM", "Fan"),
                    leaf("GPU Memory Used", "1024 MB", "SmallData"), leaf("GPU Memory Total", "8192 MB", "SmallData")]),
                hardware("/nic/1", "Ethernet", [leaf("Upload Speed", "1 MB/s", "Throughput"), leaf("Download Speed", "2 MB/s", "Throughput")]),
                hardware("/nic/2", "Hyper-V Virtual", [leaf("Upload Speed", "10 MB/s", "Throughput")]),
                hardware("/nvme/0", "SSD", [leaf("Composite Temperature", "40 °C", "Temperature"), leaf("Critical Temperature", "90 °C", "Temperature")])]}

    def parsed(self):
        class Response:
            def __enter__(inner):
                return inner
            def __exit__(inner, *args):
                pass
            def read(inner):
                return json.dumps(self.tree()).encode()
        return patch("sensors.urllib.request.urlopen", return_value=Response())

    def test_fahrenheit_tdie_thresholds_memory_and_network(self):
        with self.parsed(), patch("sensors._nvidia", return_value=[]):
            data = sensors.read()
        self.assertEqual(data["cpu_temp"], 70)
        self.assertEqual(data["gpu_mem_load"], 12.5)
        self.assertEqual(data["gpu_fan"], 0)
        self.assertEqual(data["net_up"], 1024 ** 2)
        self.assertEqual(data["extra_temps"], [("storage", "SSD", 40)])
        self.assertEqual(len(data["nics"]), 2)

    def test_explicit_virtual_nic_selection_and_empty_selection(self):
        with self.parsed(), patch("sensors._nvidia", return_value=[]):
            self.assertEqual(sensors.read(nic_ids=["/nic/2"])["net_up"], 10 * 1024 ** 2)
            self.assertIsNone(sensors.read(nic_ids=[])["net_up"])

    def test_disconnect_with_nvidia_uuid_fallback(self):
        cards = [("GPU", 70, 0, 20, 1024, 8192, "UUID-A"), ("GPU", 80, 10, 50, 2048, 8192, "UUID-B")]
        with patch("sensors._lhm", side_effect=OSError("offline")), patch("sensors.lhm_running", return_value=False), patch("sensors._nvidia", return_value=cards):
            data = sensors.read("nvidia:UUID-B")
        self.assertTrue(data["lhm_down"])
        self.assertEqual(data["gpu_temp"], 80)
        self.assertEqual(data["gpu_id"], "nvidia:UUID-B")
        self.assertEqual(len(data["gpus"]), 2)

    def test_process_query_is_cached(self):
        with patch("sensors._process_cache", (0, False)), patch("sensors.time.monotonic", return_value=100), patch("sensors.subprocess.run", return_value=Mock(stdout="LibreHardwareMonitor.exe")) as run:
            self.assertTrue(sensors.lhm_running())
            self.assertTrue(sensors.lhm_running())
            self.assertEqual(run.call_count, 1)
            sensors.lhm_running(force=True)
            self.assertEqual(run.call_count, 2)

    def test_same_model_nvidia_cards_do_not_cross_fill(self):
        with patch("sensors._nvidia", return_value=[("RTX", 60), ("RTX", 80)]):
            self.assertIsNone(sensors._nvidia_for("NVIDIA RTX"))
            self.assertIsNone(sensors._nvidia_for(None, "/gpu-nvidia/1"))


class CollectorTests(unittest.TestCase):
    def test_backoff_reset_and_single_thread(self):
        worker = Collector(config.validate({"history_enabled": False}))
        delays = []
        data = {"lhm_down": True}
        def wait(delay):
            delays.append(round(delay))
            if len(delays) == 3:
                data["lhm_down"] = False
            if len(delays) == 4:
                worker.stop()
        with patch("collector.HistoryStore") as store, patch("collector.sensors.read", side_effect=lambda *args: dict(data)), patch.object(worker.wake, "wait", side_effect=wait):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(delays, [2, 4, 8, 1])
        self.assertFalse(worker.results.empty())

    def test_storage_failure_keeps_collecting(self):
        worker = Collector(config.validate({}))
        with patch("collector.HistoryStore", side_effect=sqlite3.DatabaseError("corrupt")), patch("collector.sensors.read", return_value={"lhm_down": False}), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()), self.assertLogs("collector", "ERROR"):
            worker.start()
            worker.join(timeout=3)
        self.assertIsNotNone(worker.storage_error)
        self.assertFalse(worker.results.empty())

    def test_unexpected_error_clears_stale_readings(self):
        worker = Collector(config.validate({}))
        with patch("collector.HistoryStore") as store, patch("collector.sensors.read", side_effect=RuntimeError("failure")), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()), self.assertLogs("collector", "ERROR"):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        data = worker.results.get_nowait()
        self.assertIsNone(data["cpu_temp"])
        self.assertTrue(data["error"])


class DiagnosticTests(unittest.TestCase):
    def test_no_environment_or_private_exception_path(self):
        data = diagnostics.report({"error": "C:/Users/private/file", "cpu_temp": 70}, {"error": "C:/private"}, [], {})
        output = json.dumps(data)
        self.assertNotIn("private", output)
        self.assertNotIn("USERNAME", output)
        self.assertEqual(data["readings"]["cpu_temp"], 70)


@unittest.skipUnless(sys.platform == "win32", "Windows process handles")
class ProcessTests(unittest.TestCase):
    def test_no_handle_never_closes_external_process(self):
        import lifecycle
        process = lifecycle.LibreProcess.__new__(lifecycle.LibreProcess)
        process.handle = None
        process.kernel, process.user = Mock(), Mock()
        process.stop()
        self.assertEqual(process.kernel.mock_calls, [])
        self.assertEqual(process.user.mock_calls, [])

    def test_owned_handle_gets_single_bounded_wait_and_termination(self):
        import lifecycle
        process = lifecycle.LibreProcess.__new__(lifecycle.LibreProcess)
        process.handle = 123
        process.kernel, process.user = Mock(), Mock()
        process.kernel.WaitForSingleObject.side_effect = [258, 258]
        process.kernel.GetProcessId.return_value = 42
        process.stop()
        process.kernel.WaitForSingleObject.assert_any_call(123, 4000)
        process.kernel.TerminateProcess.assert_called_once_with(123, 0)
        process.kernel.CloseHandle.assert_called_once_with(123)
        self.assertIsNone(process.handle)

    def test_only_owned_pid_receives_close_message(self):
        import lifecycle
        process = lifecycle.LibreProcess.__new__(lifecycle.LibreProcess)
        process.handle = 123
        process.kernel, process.user = Mock(), Mock()
        process.kernel.WaitForSingleObject.side_effect = [258, 0]
        process.kernel.GetProcessId.return_value = 42
        process.user.GetWindowThreadProcessId.side_effect = lambda hwnd, ptr: setattr(ptr._obj, "value", 42 if hwnd == 1 else 999)
        process.user.EnumWindows.side_effect = lambda callback, arg: (callback(1, arg), callback(2, arg))
        process.stop()
        process.user.PostMessageW.assert_called_once_with(1, 0x10, 0, 0)
        process.kernel.TerminateProcess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
