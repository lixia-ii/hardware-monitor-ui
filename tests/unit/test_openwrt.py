"""OpenWrt 路由器指标解析与离线退避。"""
import unittest
from unittest.mock import Mock, patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import openwrt
from tests.support import FIXTURES


class RouterTests(unittest.TestCase):
    METRICS = (FIXTURES / "openwrt_metrics.txt").read_text(encoding="utf-8")

    @staticmethod
    def advance(text, cpu_busy, cpu_idle, rx, tx):
        """把 cpu0 的 user/idle 与 eth0 的收发字节往前推, 模拟下一次采集。"""
        text = text.replace('mode="user"} 5399.81', f'mode="user"}} {5399.81 + cpu_busy}')
        text = text.replace('mode="idle"} 455321.58', f'mode="idle"}} {455321.58 + cpu_idle}')
        text = text.replace('receive_bytes_total{device="eth0"} 36865966813', f'receive_bytes_total{{device="eth0"}} {36865966813 + rx}')
        return text.replace('transmit_bytes_total{device="eth0"} 4254765171', f'transmit_bytes_total{{device="eth0"}} {4254765171 + tx}')

    def read(self, router, texts, times, nic=None, url="http://router/metrics"):
        results = []
        for text, now in zip(texts, times):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = text.encode()
            with patch("hwmonitor.sensors.openwrt.urllib.request.urlopen", return_value=response), patch("hwmonitor.sensors.openwrt.time.monotonic", return_value=now):
                results.append(router.read(url, nic))
        return results

    def test_gauges_and_automatic_wan(self):
        metrics = openwrt.parse(self.METRICS)
        self.assertNotIn("node_nat_traffic", metrics)
        self.assertEqual(openwrt.pick_wan(openwrt._counters(metrics)), "eth0")
        first, = self.read(openwrt.Router(), [self.METRICS], [100])
        self.assertFalse(first["offline"])
        self.assertEqual((first["host"], first["temp"], first["load1"], first["nic"]), ("OpenWrt", 39, .39, "eth0"))
        self.assertAlmostEqual(first["mem_load"], (1880727552 - 1451405312) / 1880727552 * 100)
        self.assertEqual(first["uptime"], 1794225271 - 1793760018)
        self.assertIsNone(first["cpu_load"])  # 计数器需要两次采样
        self.assertIsNone(first["up"])
        self.assertIn("utun", first["nics"])

    def test_counter_rates_reset_and_manual_nic(self):
        router = openwrt.Router()
        later = self.advance(self.METRICS, 1, 3, 2 * 1024 ** 2, 1024 ** 2)
        _, second = self.read(router, [self.METRICS, later], [100, 102])
        self.assertAlmostEqual(second["cpu_load"], 25)
        self.assertAlmostEqual(second["down_rate"], 1024 ** 2)
        self.assertAlmostEqual(second["up"], 512 * 1024)
        reset, = self.read(router, [self.advance(self.METRICS, 0, 0, -1000, 0)], [104])
        self.assertIsNone(reset["down_rate"])
        manual, = self.read(router, [self.METRICS], [106], nic="utun")
        self.assertEqual(manual["nic"], "utun")
        missing, = self.read(router, [self.METRICS], [108], nic="pppoe-wan")
        self.assertIsNone(missing["nic"])
        self.assertIsNone(missing["up"])

    def test_offline_backs_off_until_url_changes(self):
        router = openwrt.Router()
        with patch("hwmonitor.sensors.openwrt.urllib.request.urlopen", side_effect=OSError("timeout")) as urlopen, patch("hwmonitor.sensors.openwrt.time.monotonic", return_value=100):
            self.assertTrue(router.read("http://a/metrics")["offline"])
            self.assertTrue(router.read("http://a/metrics")["offline"])
            self.assertEqual(urlopen.call_count, 1)
            router.read("http://b/metrics")
            self.assertEqual(urlopen.call_count, 2)

    def test_settings(self):
        self.assertFalse(settings.validate({})["router"]["enabled"])
        result = settings.validate({"router": {"enabled": True, "url": " https://10.0.0.1:9100/metrics ", "nic": ""}})
        self.assertEqual(result["router"], {"enabled": True, "url": "https://10.0.0.1:9100/metrics", "nic": None})
        result = settings.validate({"router": {"enabled": 1, "url": "file:///etc/passwd", "nic": "eth1"}})
        self.assertEqual(result["router"], {**settings.DEFAULTS["router"], "nic": "eth1"})

    def test_collector_isolates_router_errors(self):
        worker = Collector(settings.validate({"router": {"enabled": True}}))
        with patch.object(worker.router, "read", side_effect=RuntimeError("bug")), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            self.assertTrue(worker._read_router(worker.options["router"])["offline"])
        self.assertIsNone(Collector(settings.validate({}))._read_router(settings.DEFAULTS["router"]))
