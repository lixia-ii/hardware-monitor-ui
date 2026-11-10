"""网页服务与局域网防火墙规则。"""
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from hwmonitor.system import webserver


class WebServerTests(unittest.TestCase):
    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def get(self, port, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_serves_published_tree_and_stops(self):
        server, port = webserver.WebServer(), self.free_port()
        try:
            self.assertIsNone(server.apply(True, False, port))
            self.assertEqual(server.server.server_address[0], "127.0.0.1")
            self.assertEqual(self.get(port, "/data.json")[0], 503)
            server.publish({"Text": "Sensor", "Children": [{"Text": "温度"}]})
            status, body = self.get(port, "/data.json")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["Children"][0]["Text"], "温度")
            self.assertEqual(self.get(port, "/other")[0], 404)
            self.assertEqual(self.get(port, "/")[0], 200)  # 重定向到 /data.json
            self.assertIsNone(server.apply(True, False, port))  # 设置未变不重启
        finally:
            server.apply(False, False, port)
        self.assertIsNone(server.server)
        with self.assertRaises(OSError):
            self.get(port, "/data.json")

    def test_port_in_use_reports_error(self):
        import socket
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            server = webserver.WebServer()
            with self.assertLogs("hwmonitor.system.webserver", "WARNING"):
                error = server.apply(True, False, port)
        self.assertIn(str(port), error)
        self.assertIsNone(server.server)

    def test_firewall_rule_limited_to_local_subnet(self):
        with patch("hwmonitor.system.webserver.subprocess.run") as run:
            self.assertTrue(webserver.firewall(8085))
            self.assertTrue(webserver.firewall(None))
        add = run.call_args_list[1].args[0]
        self.assertIn("remoteip=localsubnet", add)
        self.assertIn("localport=8085", add)
        self.assertEqual(run.call_count, 3)  # 删除 + 添加, 再次删除
