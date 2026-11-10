"""设置校验、原子保存与旧数据迁移。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hwmonitor import paths, settings


class SettingsTests(unittest.TestCase):
    def test_bad_structure_and_fields(self):
        for raw in ([], None, 7, "bad"):
            self.assertEqual(settings.validate(raw), settings.DEFAULTS)
        result = settings.validate({"alerts": [], "floating": None, "display": [1], "interval": float("nan")})
        self.assertEqual(result, settings.DEFAULTS)
        result = settings.validate({"alerts": {"cpu_temp": "broken", "gpu_temp": 500}, "nic_ids": [1, "nic"],
                                  "floating": {"x": "bad"}, "interval": -1})
        self.assertEqual(result["alerts"]["cpu_temp"], 85)
        self.assertEqual(result["alerts"]["gpu_temp"], 110)
        self.assertEqual(result["nic_ids"], ["nic"])
        self.assertEqual(result["interval"], .5)

    def test_window_geometry(self):
        for bad in (None, [], {"width": 300}, {"width": True, "height": 400, "x": 0, "y": 0},
                    {"width": "300", "height": 400, "x": 0, "y": 0}):
            self.assertIsNone(settings.validate({"window": bad})["window"])
        window = settings.validate({"window": {"width": 10, "height": 500.7, "x": -1920, "y": 40}})["window"]
        self.assertEqual(window, {"width": 100, "height": 500, "x": -1920, "y": 40})

    def test_empty_gpu_id_means_automatic(self):
        self.assertIsNone(settings.validate({"gpu_id": ""})["gpu_id"])
        self.assertEqual(settings.validate({"gpu_id": "/gpu-nvidia/0"})["gpu_id"], "/gpu-nvidia/0")

    def test_atomic_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            settings.save({"close_action": "tray"}, path)
            with patch("hwmonitor.settings.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    settings.save({"close_action": "exit"}, path)
            self.assertEqual(settings.load(path)["close_action"], "tray")
            self.assertEqual(len(list(Path(d).iterdir())), 1)

    def test_corrupt_json_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text('{"truncated":', encoding="utf-8")
            with self.assertLogs("hwmonitor.settings", "ERROR"):
                self.assertEqual(settings.load(path), settings.DEFAULTS)
            options = settings.validate({"display": {"cpu_temp": {"name": "处理器", "visible": False}}, "nic_ids": []})
            settings.save(options, path)
            self.assertEqual(settings.load(path), options)

    def test_legacy_data_moves_together_without_overwriting_new_data(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            data = base / "data"
            (base / "settings.json").write_text('{"close_action": "tray"}', encoding="utf-8")
            for name in ("history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm"):
                (base / name).write_bytes(name.encode())
            paths.migrate_legacy_data(base, data)
            self.assertEqual(settings.load(data / "settings.json")["close_action"], "tray")
            self.assertTrue((data / "history.sqlite3-wal").exists())
            self.assertFalse((base / "history.sqlite3").exists())
            (base / "settings.json").write_text('{"close_action": "exit"}', encoding="utf-8")
            (base / "history.sqlite3-wal").write_bytes(b"different-database")
            paths.migrate_legacy_data(base, data)
            self.assertEqual(settings.load(data / "settings.json")["close_action"], "tray")
            self.assertNotEqual((data / "history.sqlite3-wal").read_bytes(), b"different-database")
