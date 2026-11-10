"""采集线程: 退避、历史写入失败与路由器隔离。"""
import sqlite3
import time
import unittest
from unittest.mock import patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import openwrt


class CollectorTests(unittest.TestCase):
    def test_backoff_reset_and_single_thread(self):
        worker = Collector(settings.validate({"history_enabled": False}))
        delays = []
        broken = [True]
        def read(*args):
            if broken[0]:
                raise RuntimeError("failure")
            return {"sensors_down": False}
        def wait(delay):
            delays.append(round(delay))
            if len(delays) == 3:
                broken[0] = False
            if len(delays) == 4:
                worker.stop()
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", side_effect=read), patch.object(worker.wake, "wait", side_effect=wait), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(delays, [2, 4, 8, 1])
        self.assertFalse(worker.results.empty())

    def test_storage_failure_keeps_collecting(self):
        worker = Collector(settings.validate({}))
        with patch("hwmonitor.core.collector.HistoryStore", side_effect=sqlite3.DatabaseError("corrupt")), patch("hwmonitor.core.collector.reading.read", return_value={"sensors_down": False}), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            worker.start()
            worker.join(timeout=3)
        self.assertIsNotNone(worker.storage_error)
        self.assertFalse(worker.results.empty())

    def test_unexpected_error_clears_stale_readings(self):
        worker = Collector(settings.validate({}))
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", side_effect=RuntimeError("failure")), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        data = worker.results.get_nowait()
        self.assertIsNone(data["cpu_temp"])
        self.assertTrue(data["error"])

    def test_router_survives_sensor_failure_and_counts_in_collection_time(self):
        worker = Collector(settings.validate({"router": {"enabled": True}}))
        reading = dict(openwrt.empty_reading(), offline=False)
        def slow_router(*_):
            time.sleep(.05)
            return reading
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", return_value={"sensors_down": False}), patch.object(worker.router, "read", side_effect=slow_router), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        self.assertGreaterEqual(worker.results.get_nowait()["collection_ms"], 40)
        worker = Collector(settings.validate({"router": {"enabled": True}}))
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", side_effect=RuntimeError("failure")), patch.object(worker.router, "read", return_value=reading), patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        self.assertIs(worker.results.get_nowait()["router"], reading)
