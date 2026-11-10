"""局域网内另一份本软件的数据接入、隔离和退避。"""
import http.client
import json
import socket
import unittest
from unittest.mock import Mock, patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import openwrt, remote
from hwmonitor.system.webserver import WebServer
from tests.support import sample_tree


class RemoteTests(unittest.TestCase):
    def response(self, payload):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def test_remote_tree_never_uses_local_nvidia(self):
        tree = sample_tree()
        tree["Children"] = [tree["Children"][0], *tree["Children"][2:]]
        with patch("hwmonitor.sensors.remote.urllib.request.urlopen", return_value=self.response(tree)), patch("hwmonitor.sensors.reading.nvidia.card_for") as local_gpu:
            result = remote.Device().read("http://pc/data.json")
        self.assertFalse(result["offline"])
        self.assertEqual(result["cpu_load"], 25)
        self.assertEqual(result["net_up"], 1024 ** 2)
        self.assertIsNone(result["gpu_temp"])
        local_gpu.assert_not_called()

    def test_truncated_http_response_backs_off(self):
        response = self.response(sample_tree())
        response.read.side_effect = http.client.IncompleteRead(b"{")
        for module, client in ((remote, remote.Device()), (openwrt, openwrt.Router())):
            with self.subTest(module=module.__name__), patch(module.__name__ + ".time.monotonic", return_value=100), patch(module.__name__ + ".urllib.request.urlopen", return_value=response) as fetch:
                self.assertTrue(client.read("http://a/x")["offline"])
                self.assertTrue(client.read("http://a/x")["offline"])
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(client.failures, 1)

    def test_invalid_response_backoff_and_address_change(self):
        client = remote.Device()
        with patch("hwmonitor.sensors.remote.time.monotonic", return_value=100), patch("hwmonitor.sensors.remote.urllib.request.urlopen", return_value=self.response({"hello": "world"})) as fetch:
            self.assertTrue(client.read("http://a/data.json")["offline"])
            self.assertTrue(client.read("http://a/data.json")["offline"])
            self.assertEqual(fetch.call_count, 1)
            fetch.return_value = self.response(sample_tree())
            self.assertFalse(client.read("http://b/data.json")["offline"])
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(client.failures, 0)

    def test_offline_clears_previous_values_and_recovers(self):
        client = remote.Device()
        with patch("hwmonitor.sensors.remote.urllib.request.urlopen", side_effect=[self.response(sample_tree()), OSError("down"), self.response(sample_tree())]), patch("hwmonitor.sensors.remote.time.monotonic", side_effect=[100, 101, 104]):
            self.assertFalse(client.read("http://a/data.json")["offline"])
            offline = client.read("http://a/data.json")
            self.assertTrue(offline["offline"])
            self.assertIsNone(offline["cpu_temp"])
            self.assertFalse(client.read("http://a/data.json")["offline"])

    def test_mixed_devices_are_dispatched_and_isolated(self):
        cfg = settings.validate({"routers": [{"id": "router", "enabled": True},
                                             {"id": "pc", "enabled": True, "type": "hwmonitor"}]})
        worker = Collector(cfg)
        with patch("hwmonitor.sensors.openwrt.Router.read", return_value=dict(openwrt.empty_reading(), offline=False)), patch("hwmonitor.sensors.remote.Device.read", side_effect=RuntimeError("bug")), self.assertLogs("hwmonitor.core.collector", "ERROR"):
            result = worker._read_routers(cfg)
        self.assertFalse(result[0]["offline"])
        self.assertTrue(result[1]["offline"])
        self.assertIsNone(result[1]["gpu_temp"])
        self.assertEqual(result[1]["type"], "hwmonitor")

    def test_own_webserver_end_to_end(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = WebServer()
        try:
            self.assertIsNone(server.apply(True, False, port))
            url = f"http://127.0.0.1:{port}/data.json"
            self.assertTrue(remote.Device().read(url)["offline"])  # 对方仍在初始化。
            server.publish(sample_tree())
            data = remote.Device().read(url)
            self.assertFalse(data["offline"])
            self.assertEqual(data["gpu_temp"], 65)
            self.assertEqual(data["gpu_mem_total"], 8192 * 1024 ** 2)
            self.assertEqual(data["extra_temps"][0][2], 40)
        finally:
            server.stop()

    def test_device_settings_and_url_roundtrip(self):
        cfg = settings.validate({"routers": [{"id": "pc", "name": "书房", "enabled": True,
                                             "type": "hwmonitor", "url": " http://192.168.1.8:8085 "}]})
        self.assertEqual(cfg["routers"][0]["url"], "http://192.168.1.8:8085/data.json")
        self.assertEqual(settings.validate(cfg), cfg)
        self.assertIsNone(settings.device_url("http://:bad"))
        self.assertIsNone(settings.device_url("file:///etc/passwd"))
        self.assertEqual(settings.validate({"routers": [{"type": "unknown"}]})["routers"], [])
