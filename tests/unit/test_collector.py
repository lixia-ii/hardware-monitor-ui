"""采集线程: 退避、历史写入失败与路由器隔离。"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import openwrt, remote


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

    def run_ticks(self, worker, ticks, **read):
        """运行 ticks 次采样; 两次之间稍停, 让线程池里的设备读取完成。返回每次送出的结果。"""
        results, waits = [], []
        def wait(_):
            results.append(worker.results.get_nowait())
            waits.append(1)
            if len(waits) >= ticks:
                worker.stop()
            else:
                time.sleep(.2)
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", **read), patch.object(worker.wake, "wait", side_effect=wait):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=5)
        return results

    def test_slow_router_never_delays_local_sampling(self):
        worker = Collector(settings.validate({"router": {"enabled": True}}))
        reading = dict(openwrt.empty_reading(), offline=False)
        started = threading.Event()
        running, overlaps = [], []
        def slow_router(*_):
            started.set()
            overlaps.append(len(running))
            running.append(1)
            time.sleep(.3)  # 比两次采样的间隔还长
            running.pop()
            return reading
        with patch.object(worker.router, "read", side_effect=slow_router) as read:
            first, second, third = self.run_ticks(worker, 3, side_effect=lambda *_: {"sensors_down": False})
        self.assertTrue(started.is_set())
        self.assertLess(first["collection_ms"], 100)
        self.assertEqual(first["routers"], [])  # 还没读到的设备暂不列出, 本机数据照常送出
        self.assertEqual(second["routers"], [])
        # 进行中的读取不重复提交。第三次采样提交的新读取可能在停止时被取消, 所以不断言调用次数。
        self.assertEqual(overlaps, [0] * read.call_count)
        self.assertEqual(third["routers"][0]["id"], "primary")

    def test_router_survives_sensor_failure(self):
        worker = Collector(settings.validate({"router": {"enabled": True}}))
        reading = dict(openwrt.empty_reading(), offline=False)
        with patch.object(worker.router, "read", return_value=reading), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            result = self.run_ticks(worker, 2, side_effect=RuntimeError("failure"))[-1]
        self.assertTrue(result["error"])
        self.assertEqual({k: result["router"][k] for k in reading}, reading)
        self.assertEqual(result["routers"][0]["id"], "primary")

    def test_poll_drops_results_of_replaced_device(self):
        cfg = settings.validate({"routers": [{"id": "a", "name": "旧名", "enabled": True}]})
        worker = Collector(cfg)
        with ThreadPoolExecutor(max_workers=1) as pool, patch("hwmonitor.sensors.openwrt.Router.read",
                                                              return_value=dict(openwrt.empty_reading(), offline=False)):
            worker.router_pool = pool
            worker._poll_routers(cfg)
            worker.device_reads["a"].result(timeout=2)
            cfg["routers"][0]["name"] = "新名"
            self.assertEqual([r["name"] for r in worker._poll_routers(cfg)], ["新名"])
            worker.device_reads["a"].result(timeout=2)
            cfg["routers"][0]["type"] = "hwmonitor"
            with patch("hwmonitor.sensors.remote.Device.read", side_effect=lambda *_: time.sleep(.2) or remote.empty_reading()):
                self.assertEqual(worker._poll_routers(cfg), [])  # 改类型后不显示旧类型的结果
                worker.device_reads["a"].result(timeout=2)
                self.assertEqual(worker._poll_routers(cfg)[0]["type"], "hwmonitor")

    def test_multiple_routers_keep_state_and_isolate_failures(self):
        cfg = settings.validate({"routers": [
            {"id": "a", "name": "主路由", "enabled": True, "url": "http://a/metrics"},
            {"id": "b", "name": "旁路由", "enabled": True, "url": "http://b/metrics"},
            {"id": "c", "enabled": False}]})
        worker = Collector(cfg)
        with patch("hwmonitor.core.collector.openwrt.Router") as factory:
            # Distinct instances must retain independent network counters and backoff.
            from unittest.mock import Mock
            a, b = Mock(), Mock()
            factory.side_effect = [a, b]
            a.read.side_effect = RuntimeError("offline")
            b.read.return_value = dict(openwrt.empty_reading(), offline=False, up=42)
            with self.assertLogs("hwmonitor.core.collector", "ERROR"):
                first = worker._read_routers(cfg)
                second = worker._read_routers(cfg)
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(first[0]["offline"])
            self.assertEqual(second[1]["up"], 42)
            self.assertEqual([r["id"] for r in first], ["a", "b"])
            b.read.assert_called_with("http://b/metrics", None)
            cfg["routers"] = [cfg["routers"][1]]
            worker._read_routers(cfg)
            self.assertEqual(set(worker.routers), {"b"})

    def test_router_pool_samples_concurrently(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        cfg = settings.validate({"routers": [{"id": key, "enabled": True} for key in ("a", "b")]})
        worker = Collector(cfg)
        barrier = threading.Barrier(2, timeout=2)
        def read(*args):
            barrier.wait()
            return dict(openwrt.empty_reading(), offline=False)
        with ThreadPoolExecutor(max_workers=2) as pool, patch("hwmonitor.sensors.openwrt.Router.read", side_effect=read):
            worker.router_pool = pool
            result = worker._read_routers(cfg)
        self.assertTrue(all(not r["offline"] for r in result))
