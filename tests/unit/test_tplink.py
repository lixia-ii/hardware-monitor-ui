"""TP-Link authentication, session reuse, failure isolation and encrypted settings."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import tplink
from hwmonitor.system import credentials


WAN = {"error_code": 0, "network": {"wan_status": {
    "up_speed": "1", "down_speed": "2", "ipaddr": "192.0.2.1", "link_status": "1"}}}


class TPLinkTests(unittest.TestCase):
    def setUp(self):
        self.router = tplink.Router()
        self.decrypt = patch.object(credentials, "unprotect", return_value="123456")
        self.decrypt.start()
        self.addCleanup(self.decrypt.stop)
        self.clock = patch.object(tplink.time, "monotonic", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def read(self, protected="encrypted", url="http://router"):
        return self.router.read(url, password_protected=protected)

    def test_login_reuse_and_wan_follows_collector_cadence(self):
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "a%2Fb"}, WAN,
                {"error_code": 0, "hosts_info": {"online_host": [
                    {"host_info_14": {"online": "1"}}, {"host_info_15": {"online": "1"}},
                    {"host_info_16": {"online": "1"}}, {"host_info_17": {"online": "1"}},
                    {"host_info_18": {"online": "1"}}]}},
                {"error_code": 0, "network": {"wan_status": {"up_speed": "3", "down_speed": "4"}}},
                WAN, WAN, {"error_code": 0, "hosts_info": {"online_host": []}}]) as post:
            first = self.read()
            self.assertEqual((first["up"], first["down_rate"], first["clients"]), (1024, 2048, 5))
            self.assertFalse(first["offline"])
            self.assertIsNone(first["temp"])
            self.assertEqual(post.call_args_list[1].args[1], "/stok=a%2Fb/ds")
            # Confirmed against the physical TL-XDR3040: online_host is a table,
            # and querying it with name returns -40101.
            self.assertEqual(post.call_args_list[2].args[2], {"method": "get", "hosts_info": {"table": "online_host"}})
            # Even at the minimum global interval, new WAN data must not wait
            # for the old 3-second cache. Keep the host table on its own cadence.
            self.now.return_value = 100.5
            second = self.read()
            self.assertEqual((second["up"], second["down_rate"], second["clients"]), (3072, 4096, 5))
            self.assertEqual(post.call_count, 4)
            self.now.return_value = 101
            self.read()
            self.assertEqual(post.call_count, 5)
            self.now.return_value = 102
            self.read()
            self.assertEqual(post.call_count, 7)
            self.assertEqual(post.call_args.args[2]["method"], "get")

    def test_missing_password_never_sends_login(self):
        with patch.object(self.router, "_post") as post:
            self.assertIn("密码", self.read("")["status"])
            post.assert_not_called()

    def test_password_encoding_matches_router_javascript(self):
        # Golden outputs evaluated from the router's public class.js (2026-11-11).
        for plain, expected in [("123456", "0KcgeXhc9TefbwK"), (" test space 密码 ", "4rdQo2ra8Ur76Vg"),
                                ("密码😀", "1O7LQbhc9TefbwK")]:
            self.assertEqual(tplink.encode_password(plain), expected)

    def test_bad_password_latches_until_manual_retry(self):
        with patch.object(self.router, "_post", return_value={"error_code": -40401}) as post:
            self.assertIn("登录失败", self.read()["status"])
            self.now.return_value = 10000
            self.read()
            self.assertEqual(post.call_count, 1)
            self.read("newly-encrypted")
            self.assertEqual(post.call_count, 2)

    def test_expired_session_reauthenticates_after_delay(self):
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "old"}, {"error_code": -40403},
                {"error_code": 0, "stok": "new"}, WAN, {"error_code": -1}]) as post:
            self.assertTrue(self.read()["offline"])
            self.now.return_value = 106
            result = self.read()
            self.assertFalse(result["offline"])
            self.assertEqual(post.call_args.args[1], "/stok=new/ds")

    def test_disconnect_clears_values_and_backs_off(self):
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "ok"}, WAN, {"error_code": -1}, OSError("down"), WAN, {"error_code": -1}]) as post:
            self.read()
            self.now.return_value = 104
            result = self.read()
            self.assertTrue(result["offline"])
            self.assertIsNone(result["up"])
            self.assertIsNone(result["clients"])
            self.read()
            self.assertEqual(post.call_count, 4)
            self.now.return_value = 107
            self.assertFalse(self.read()["offline"])

    def test_optional_hosts_do_not_break_wan(self):
        with patch.object(self.router, "_post", side_effect=[{"error_code": 0, "stok": "ok"}, WAN, OSError("timeout")]):
            result = self.read()
            self.assertFalse(result["offline"])
            self.assertIsNone(result["clients"])

    def test_invalid_wan_does_not_count_as_success(self):
        for payload in ({"error_code": 0}, {"error_code": 0, "network": []},
                        {"error_code": 0, "network": {"wan_status": {}}}):
            with self.subTest(payload=payload):
                self.router = tplink.Router()
                with patch.object(self.router, "_post", side_effect=[{"error_code": 0, "stok": "ok"}, payload]):
                    self.assertTrue(self.read()["offline"])

    def test_metrics_reject_nonfinite_negative_and_bool(self):
        data = tplink.parse_wan({"network": {"wan_status": {"up_speed": "NaN", "down_speed": -1}}})
        self.assertIsNone(data["up"])
        self.assertIsNone(data["down_rate"])
        self.assertIsNone(tplink._number(True))

    def test_wan_rates_use_firmware_kib_per_second_not_host_bytes(self):
        # BasicNetState.htm fc(153) -> "153 KB/s"; the app consumes bytes/s.
        # The host-table formatter ac() divides by 1024, but does not apply here.
        data = tplink.parse_wan({"network": {"wan_status": {"up_speed": 153, "down_speed": "339"}}})
        self.assertEqual(data["up"], 153 * 1024)
        self.assertEqual(data["down_rate"], 339 * 1024)
        from hwmonitor.ui.display import fmt_rate
        self.assertEqual(fmt_rate(data["up"])[:2], ("153", "KB/s"))
        self.assertEqual(fmt_rate(data["down_rate"])[:2], ("339", "KB/s"))
        data = tplink.parse_wan({"network": {"wan_status": {"up_speed": "1024", "down_speed": 0}}})
        self.assertEqual(fmt_rate(data["up"])[:2], ("1.0", "MB/s"))
        self.assertEqual(data["down_rate"], 0)

    def test_http_request_contains_only_encoded_password(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"error_code":0,"stok":"secret-session"}'
        with patch.object(self.router.opener, "open", return_value=response) as fetch:
            self.router._post("http://router", "/", {"method": "do", "login": {"password": tplink.encode_password("123456")}}, 2)
            request = fetch.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertNotIn(b"123456", request.data)
            self.assertEqual(json.loads(request.data)["method"], "do")

    def test_wan_status_and_online_duration(self):
        for state in range(5):
            data = tplink.parse_wan({"network": {"wan_status": {"link_status": state, "up_time": "39114"}}})
            self.assertEqual(data["wan_connected"], state == 1)
            self.assertEqual(data["wan_uptime"], 39114 if state == 1 else None)
        from hwmonitor.ui.display import fmt_duration
        self.assertEqual(fmt_duration(39114), "10小时51分54秒")
        self.assertEqual(fmt_duration(86461), "1天0小时1分1秒")
        self.assertEqual(fmt_duration(None), "--")

    def test_terminal_ranking_host_speeds_are_bytes_not_kib(self):
        payload = {"hosts_info": {"online_host": [
            {"host_info_1": {"mac": "AA-BB-CC-DD-EE-01", "hostname": "电脑", "ip": "192.0.2.1", "up_speed": "1024", "down_speed": "2048"}},
            {"host_info_2": {"mac": "aa:bb:cc:dd:ee:02", "hostname": "手机", "ip": "192.0.2.2", "up_speed": "8192", "down_speed": "0"}},
            {"host_info_3": {"mac": "AA-BB-CC-DD-EE-03", "hostname": "", "ip": "192.0.2.3", "up_speed": "NaN", "down_speed": "-1"}}]}}
        rows = tplink.parse_terminals(payload)
        self.assertEqual([row["name"] for row in rows], ["手机", "电脑", "192.0.2.3"])
        self.assertEqual(rows[1]["up"], 1024)
        self.assertEqual(rows[0]["id"], "AA:BB:CC:DD:EE:02")
        self.assertIsNone(rows[2]["up"])
        self.assertEqual(tplink.parse_terminals({"hosts_info": {"online_host": []}}), [])
        self.assertIsNone(tplink.parse_terminals({"error_code": 0}))
        self.assertIsNone(tplink.parse_terminals({"hosts_info": {"online_host": [{"broken": {"hostname": "no identity"}}]}}))

    def test_host_failure_clears_ranking_without_hiding_wan(self):
        hosts = {"error_code": 0, "hosts_info": {"online_host": [
            {"host_info_1": {"mac": "AA-BB-CC-DD-EE-01", "hostname": "电脑", "up_speed": "1024"}}]}}
        with patch.object(self.router, "_post", side_effect=[{"error_code": 0, "stok": "ok"}, WAN, hosts, WAN, OSError("down")]):
            first = self.read()
            self.assertEqual(len(first["terminals"]), 1)
            self.assertEqual(first["terminals_sample_time"], 100)
            self.now.return_value = 102
            second = self.read()
            self.assertFalse(second["offline"])
            self.assertIsNone(second["terminals"])
            self.assertIsNone(second["terminals_sample_time"])

    def test_terminal_names_decode_like_firmware_without_double_decoding(self):
        cases = [("%E5%AE%A2%E5%8E%85%20%E6%89%8B%E6%9C%BA", "客厅 手机"),
                 ("desktop%2Dpc", "desktop-pc"), ("A+B%20C", "A+B C"),
                 ("already 中文", "already 中文"), ("literal%2520name", "literal%20name"),
                 ("100% ready", "100% ready"), ("bad%E4%ZZ", "bad%E4%ZZ"),
                 ("bad%FF", "bad%FF"), ("%E4%B8%AD" * 90, "中" * 80)]
        for encoded, expected in cases:
            with self.subTest(encoded=encoded):
                rows = tplink.parse_terminals({"hosts_info": {"online_host": [
                    {"host_1": {"mac": "AA-BB-CC-DD-EE-01", "hostname": encoded}}]}})
                self.assertEqual(rows[0]["name"], expected)

    def test_notification_preference_defaults_and_roundtrips(self):
        self.assertTrue(settings.validate({"routers": [{"type": "tplink"}]})["routers"][0]["notify_clients"])
        self.assertFalse(settings.validate({"routers": [{"type": "tplink", "notify_clients": False}]})["routers"][0]["notify_clients"])

    def test_settings_normalize_origin_and_drop_plaintext(self):
        cfg = settings.validate({"routers": [{"id": "tp", "type": "tplink", "enabled": True,
            "url": "http://192.168.0.1/pc/Content.htm?x=1", "password": "never-store", "password_protected": "cipher"}]})
        self.assertEqual(cfg["routers"][0]["url"], "http://192.168.0.1")
        self.assertEqual(cfg["routers"][0]["password_protected"], "cipher")
        self.assertNotIn("password", cfg["routers"][0])
        self.assertEqual(cfg["router"], settings.DEFAULT_ROUTER)
        self.assertIsNone(settings.device_url("http://user:pass@router", "tplink"))

    def test_collector_routes_credentials_without_including_them_in_results(self):
        cfg = settings.validate({"routers": [{"id": "tp", "type": "tplink", "enabled": True, "password_protected": "cipher"}]})
        worker = Collector(cfg)
        with patch.object(tplink.Router, "read", return_value=tplink.empty_reading()) as read:
            result = worker._read_routers(cfg)
            read.assert_called_once_with("http://192.168.0.1", password_protected="cipher")
            self.assertNotIn("cipher", json.dumps(result))


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
class CredentialTests(unittest.TestCase):
    def test_dpapi_and_settings_roundtrip(self):
        password = " test space 密码 "
        protected = credentials.protect(password)
        self.assertNotIn(password, protected)
        self.assertEqual(credentials.unprotect(protected), password)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings.save({"routers": [{"id": "tp", "type": "tplink", "password_protected": protected}]}, path)
            self.assertNotIn(password, path.read_text(encoding="utf-8"))
            self.assertEqual(credentials.unprotect(settings.load(path)["routers"][0]["password_protected"]), password)

    def test_invalid_encrypted_password(self):
        with self.assertRaises((ValueError, OSError)):
            credentials.unprotect("broken")
