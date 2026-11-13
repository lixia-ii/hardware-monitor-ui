"""No initial flood, cached-sample confirmation, flapping and recovery behavior."""
import queue
import unittest
from unittest.mock import Mock, patch

from hwmonitor import settings
from hwmonitor.core.device_presence import DevicePresence
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import tplink


CONFIG = {"id": "tp", "name": "主路由", "url": "http://router", "password_protected": "cipher", "notify_clients": True}


def sample(now, *names):
    return dict(tplink.empty_reading(), offline=False, terminals=[{"id": name, "name": name} for name in names],
                terminals_sample_time=now)


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.detector = DevicePresence()

    def update(self, now, *names, config=CONFIG):
        return self.detector.update(config, sample(now, *names))

    def test_initial_baseline_online_offline_and_name_changes(self):
        self.assertEqual(self.update(0, "A"), [])
        self.assertEqual(self.update(2, "A", "B"), [])
        # Reusing a cached table must not advance confirmation time.
        self.assertEqual(self.update(2, "A", "B"), [])
        event = self.update(6, "A", "B")
        self.assertEqual([(e["name"], e["online"]) for e in event], [("B", True)])
        self.assertEqual(self.update(8, "B"), [])
        event = self.update(12, "B")
        self.assertEqual([(e["name"], e["online"]) for e in event], [("A", False)])
        self.assertEqual(self.update(14, "B"), [])

    def test_flapping_does_not_notify(self):
        self.update(0, "A")
        self.update(2)
        self.assertEqual(self.update(4, "A"), [])
        self.update(6, "A", "B")
        self.assertEqual(self.update(8, "A"), [])
        self.assertEqual(self.update(12, "A"), [])

    def test_connection_failure_resets_baseline_silently(self):
        self.update(0, "A", "B")
        self.assertEqual(self.detector.update(CONFIG, tplink.empty_reading()), [])
        self.assertEqual(self.update(10, "C"), [])

    def test_host_table_failure_keeps_baseline(self):
        self.update(0, "A", "B")
        self.assertEqual(self.detector.update(CONFIG, dict(tplink.empty_reading(), offline=False)), [])
        self.assertEqual(self.update(10, "A", "B", "C"), [])
        event = self.update(14, "A", "B", "C")
        self.assertEqual([(e["name"], e["online"]) for e in event], [("C", True)])

    def test_disabled_or_changed_credentials_reset_baseline(self):
        self.update(0, "A")
        self.assertEqual(self.update(5, "B", config=dict(CONFIG, notify_clients=False)), [])
        self.assertEqual(self.update(10, "B"), [])
        self.assertEqual(self.update(20, "C", config=dict(CONFIG, password_protected="changed")), [])

    def test_successful_empty_table_can_confirm_offline(self):
        self.update(0, "A")
        self.update(2)
        self.assertFalse(self.update(6)[0]["online"])

    def test_collector_queues_events_separately_from_replaceable_samples(self):
        cfg = settings.validate({"routers": [dict(CONFIG, enabled=True, type="tplink")]})
        worker = Collector(cfg)
        with patch.object(tplink.Router, "read", side_effect=[sample(0, "A"), sample(2, "A", "B"), sample(6, "A", "B")]):
            for _ in range(3):
                worker._put({})
                worker._read_routers(cfg)
        self.assertEqual(worker.device_events.get_nowait()["name"], "B")
        with self.assertRaises(queue.Empty):
            worker.device_events.get_nowait()
        cfg["routers"] = []
        worker._read_routers(cfg)
        self.assertFalse(worker.presence)

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows 显示格式")
    def test_ui_delivers_one_grouped_notification_and_honors_current_switch(self):
        from hwmonitor.ui.main_window import MainWindow
        app = MainWindow.__new__(MainWindow)
        app.settings = {"routers": [dict(CONFIG, enabled=True)]}
        app.collector = Mock(device_events=queue.SimpleQueue())
        app.tray = Mock()
        app.recent_alerts = []
        app._flash = Mock()
        for name, online in (("A", True), ("B", False)):
            app.collector.device_events.put(dict(router_id="tp", name=name, online=online))
        app._device_notifications()
        app.tray.notify.assert_called_once()
        message = app.tray.notify.call_args.args[1]
        self.assertIn("上线：A", message)
        self.assertIn("下线：B", message)
        app.settings["routers"][0]["notify_clients"] = False
        app.collector.device_events.put(dict(router_id="tp", name="C", online=True))
        app._device_notifications()
        self.assertEqual(app.tray.notify.call_count, 1)
