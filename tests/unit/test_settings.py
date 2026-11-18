"""设置校验、原子保存与旧数据迁移。"""
from pathlib import Path
import sys
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

    def test_multiple_routers_migrate_and_roundtrip(self):
        legacy = {"enabled": True, "url": "http://old/metrics", "nic": "wan"}
        cfg = settings.validate({"router": legacy})
        self.assertEqual(cfg["routers"], [dict(legacy, id="primary", name="路由器 1")])
        cfg["routers"].append(dict(legacy, id="second", name="旁路由", url="http://second/metrics"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            settings.save(cfg, path)
            self.assertEqual(settings.load(path), cfg)
            cfg["routers"] = []
            settings.save(cfg, path)
            loaded = settings.load(path)
            self.assertEqual(loaded["routers"], [])
            self.assertFalse(loaded["router"]["enabled"])

    def test_router_list_validation(self):
        cfg = settings.validate({"routers": [None, {"id": "dup", "enabled": 1, "url": "ftp://bad"},
                                            {"id": "dup", "name": " ", "nic": 3},
                                            {"id": [], "name": " test ", "url": " https://b/metrics "}]})
        self.assertEqual(len({r["id"] for r in cfg["routers"]}), 3)
        self.assertFalse(cfg["routers"][0]["enabled"])
        self.assertEqual(cfg["routers"][0]["url"], settings.DEFAULT_ROUTER["url"])
        self.assertEqual(cfg["routers"][2]["name"], "test")
        self.assertEqual(cfg["routers"][2]["url"], "https://b/metrics")

    def test_router_display_preferences_migrate_by_identity(self):
        cfg = settings.validate({"display": {"router": {"name": "网关", "visible": False, "order": 2}}})
        self.assertNotIn("router", cfg["display"])
        self.assertEqual(cfg["display"]["router:primary"], {"name": "网关", "visible": False, "order": 2})
        cfg["routers"].append(dict(cfg["routers"][0], id="second", name="旁路由"))
        cfg["display"]["router:second"] = {"name": "旁路由显示", "visible": True, "order": 0}
        self.assertEqual(settings.validate(cfg), cfg)
        cfg["routers"].reverse()
        self.assertEqual(settings.validate(cfg)["display"], cfg["display"])
        cfg["display"] = {"router": {"name": "路由器", "visible": False}}
        migrated = settings.validate(cfg)
        self.assertEqual(settings.validate(migrated), migrated)
        self.assertTrue(all(not pref["visible"] for pref in migrated["display"].values()))

    def test_floating_options(self):
        result = settings.validate({"floating": {"enabled": True, "x": 10, "opacity": 0.1, "topmost": "no", "show_net": False}})
        self.assertEqual(result["floating"], dict(settings.DEFAULT_FLOATING, enabled=True, x=10, opacity=0.3, show_net=False))
        self.assertEqual(settings.validate({"floating": {"x": 1}})["floating"]["opacity"], 1.0)

    def test_main_window_topmost(self):
        self.assertFalse(settings.DEFAULTS["topmost"])
        self.assertTrue(settings.validate({"topmost": True})["topmost"])
        self.assertFalse(settings.validate({"topmost": 1})["topmost"])

    def test_appearance_font_and_scale(self):
        self.assertEqual(settings.DEFAULTS["appearance"], "dark")
        self.assertEqual(settings.DEFAULTS["font_size"], "normal")
        self.assertEqual(settings.DEFAULTS["ui_scale"], 100)
        cfg = settings.validate({"appearance": "system", "font_size": "large", "ui_scale": 150})
        self.assertEqual((cfg["appearance"], cfg["font_size"], cfg["ui_scale"]), ("system", "large", 150))
        cfg = settings.validate({"appearance": "blue", "font_size": "huge", "ui_scale": 110})
        self.assertEqual((cfg["appearance"], cfg["font_size"], cfg["ui_scale"]), ("dark", "normal", 100))
        self.assertEqual(settings.validate({"ui_scale": 100.0})["ui_scale"], 100)

    def test_theme_palette_and_font_factor(self):
        if sys.platform != "win32":
            self.skipTest("界面主题依赖 Tk")
        from hwmonitor.ui import theme
        self.assertEqual(set(theme.DARK), set(theme.LIGHT))
        mapping = theme.color_map(theme.DARK, theme.LIGHT)
        self.assertEqual(mapping["#16181d"], theme.LIGHT["BG"])
        self.assertEqual(mapping["#3b82f6"], theme.LIGHT["ON_BORDER"])  # 重复色值保留先出现的键
        previous = theme._font_factor
        try:
            theme.use_font("small")
            self.assertEqual(theme.font(10)[1], 9)
            theme.use_font("large")
            self.assertEqual(theme.font(8)[1], 9)
            self.assertEqual(theme.resolve_appearance("light"), "light")
            self.assertEqual(theme.resolve_appearance("nope"), "dark")
        finally:
            theme._font_factor = previous

    def test_window_geometry(self):
        for bad in (None, [], {"width": 300}, {"width": True, "height": 400, "x": 0, "y": 0},
                    {"width": "300", "height": 400, "x": 0, "y": 0}):
            self.assertIsNone(settings.validate({"window": bad})["window"])
        window = settings.validate({"window": {"width": 10, "height": 500.7, "x": -1920, "y": 40}})["window"]
        self.assertEqual(window, {"width": 100, "height": 500, "x": -1920, "y": 40, "zoomed": False})
        zoomed = settings.validate({"window": {"width": 400, "height": 300, "x": 10, "y": 20, "zoomed": True}})["window"]
        self.assertTrue(zoomed["zoomed"])
        self.assertFalse(settings.validate({"window": {"width": 400, "height": 300, "x": 0, "y": 0, "zoomed": 1}})["window"]["zoomed"])

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

    def test_legacy_router_skips_remote_devices(self):
        remote_pc = {"id": "pc", "type": "hwmonitor", "enabled": True, "url": "http://192.168.1.8:8085/data.json"}
        router = {"id": "r", "enabled": True, "url": "http://10.0.0.1:9100/metrics"}
        cfg = settings.validate({"routers": [remote_pc, router]})
        self.assertEqual(cfg["router"], {"enabled": True, "url": "http://10.0.0.1:9100/metrics", "nic": None})
        self.assertEqual(settings.validate({"routers": [remote_pc]})["router"], settings.DEFAULT_ROUTER)

    def test_legacy_router_collapse_key_migrates_to_device_id(self):
        cfg = settings.validate({"collapsed": ["router", "case", "router:primary", 3]})
        self.assertEqual(cfg["collapsed"], ["router:primary", "case"])

    def test_new_device_name_reuses_free_number(self):
        self.assertEqual(settings.new_device_name([]), "设备 1")
        self.assertEqual(settings.new_device_name([{"name": "设备 1"}, {"name": "设备 3"}]), "设备 2")

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
