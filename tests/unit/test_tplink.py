"""TP-Link authentication, session reuse, failure isolation and encrypted settings."""
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import tplink
from hwmonitor.system import credentials


REAL_PROBE = tplink.Router._probe
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
        self.probe = patch.object(tplink.Router, "_probe", return_value=None)
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)
        # Most protocol tests skip the readiness wait; dedicated tests below
        # exercise the real 10-second two-probe handshake and login cooldown.
        ready = patch.object(tplink, "_READY_INTERVAL", 0)
        ready.start()
        self.addCleanup(ready.stop)

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

    def test_unknown_login_error_pauses_instead_of_retrying(self):
        with patch.object(self.router, "_post", return_value={"error_code": -1}) as post:
            self.assertIn("登录失败", self.read()["status"])
            self.assertIsNotNone(self.router.blocked)
            self.now.return_value = 10000
            self.assertTrue(self.read()["offline"])
            self.assertEqual(post.call_count, 1)

    def test_expired_session_reauthenticates_after_delay(self):
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "old"}, WAN, {"error_code": -1},
                {"error_code": -40401, "data": {"code": -40403}},
                {"error_code": 0, "stok": "new"}, WAN, {"error_code": -1}]) as post:
            self.assertFalse(self.read()["offline"])
            self.now.return_value = 104
            self.assertTrue(self.read()["offline"])
            self.now.return_value = 163
            self.assertTrue(self.read()["offline"])
            self.assertEqual(post.call_count, 4)
            self.now.return_value = 164
            self.assertFalse(self.read()["offline"])
            self.assertEqual(post.call_args.args[1], "/stok=new/ds")

    def test_disconnect_clears_values_and_backs_off(self):
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "ok"}, WAN, {"error_code": -1}, OSError("down"),
                {"error_code": 0, "stok": "new"}, WAN, {"error_code": -1}]) as post:
            self.read()
            self.now.return_value = 104
            result = self.read()
            self.assertTrue(result["offline"])
            self.assertIsNone(result["up"])
            self.assertIsNone(result["clients"])
            self.read()
            self.assertEqual(post.call_count, 4)
            self.now.return_value = 164
            self.assertFalse(self.read()["offline"])

    def test_reboot_errors_discard_old_session_and_reauthenticate(self):
        # Firmware may reject a pre-reboot stok with HTTP/non-JSON responses or
        # non-auth error codes. None of these should pin retries to the dead stok.
        for failure in (OSError("connection refused"), http.client.RemoteDisconnected(),
                        HTTPError("http://router/stok=old/ds", 404, "Not Found", {}, None),
                        ValueError("non-JSON response"), {"error_code": -1}, {"error_code": 0}):
            with self.subTest(failure=type(failure).__name__):
                self.router = tplink.Router()
                self.now.return_value = 100
                hosts = {"error_code": 0, "hosts_info": {"online_host": []}}
                with patch.object(self.router, "_post", side_effect=[
                        {"error_code": 0, "stok": "before-reboot"}, WAN, hosts,
                        failure, {"error_code": 0, "stok": "after-reboot"}, WAN, hosts]) as post:
                    self.assertFalse(self.read()["offline"])
                    self.now.return_value = 104
                    failed = self.read()
                    self.assertTrue(failed["offline"])
                    self.assertIsNone(failed["terminals"])
                    self.assertIsNone(self.router.session)
                    self.assertIsNone(self.router.blocked)
                    self.now.return_value = 163
                    self.read()
                    self.assertEqual(post.call_count, 4)  # No auth request during the 60 s wait.
                    self.now.return_value = 164
                    self.assertFalse(self.read()["offline"])
                    self.assertEqual(post.call_args_list[4].args[1], "/")
                    self.assertEqual(post.call_args_list[5].args[1], "/stok=after-reboot/ds")

    def test_repeated_outages_probe_without_ever_sending_password(self):
        with patch.object(self.router, "_probe", side_effect=OSError("still booting")) as probe, \
                patch.object(self.router, "_post") as post:
            for _ in range(6):
                now = self.now.return_value
                self.assertTrue(self.read()["offline"])
                self.assertEqual(self.router.next_at, now + 30)
                self.assertIsNone(self.router.blocked)
                self.now.return_value = now + 29
                self.read()
                self.now.return_value = now + 30
            self.assertEqual(probe.call_count, 6)
            post.assert_not_called()
        with patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "recovered"}, WAN, {"error_code": -1}]):
            self.assertFalse(self.read()["offline"])

    def test_readiness_requires_two_public_probes_ten_seconds_apart(self):
        with patch.object(tplink, "_READY_INTERVAL", 10), patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "ready"}, WAN, {"error_code": -1}]) as post:
            self.assertIn("等待 10 秒", self.read()["status"])
            self.now.return_value = 109
            self.read()
            self.assertEqual(self.probe_mock.call_count, 1)
            post.assert_not_called()
            self.now.return_value = 110
            self.assertFalse(self.read()["offline"])
            self.assertEqual(self.probe_mock.call_count, 2)
            self.assertEqual(post.call_count, 3)

    def test_failed_readiness_probe_restarts_stability_wait(self):
        with patch.object(tplink, "_READY_INTERVAL", 10), patch.object(self.router, "_probe", side_effect=[
                None, OSError("booting again"), None, None]), patch.object(self.router, "_post", side_effect=[
                {"error_code": 0, "stok": "ready"}, WAN, {"error_code": -1}]) as post:
            self.read()
            self.now.return_value = 110
            self.assertIn("暂不发送登录密码", self.read()["status"])
            self.now.return_value = 140
            self.assertIn("等待 10 秒", self.read()["status"])
            post.assert_not_called()
            self.now.return_value = 150
            self.assertFalse(self.read()["offline"])

    def test_readiness_probe_accepts_any_live_page_response(self):
        # Some firmware redirects / to /login.html, challenges with 401/403, or
        # answers 204; all mean the management server is up. Only hard failures
        # (4xx/5xx) and transport errors keep the readiness wait going.
        for status in (301, 302, 401, 403):
            with self.subTest(status=status):
                with patch.object(self.router.opener, "open",
                                  side_effect=HTTPError("http://router/", status, "no body", {}, None)):
                    REAL_PROBE(self.router, "http://router", 2)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b""  # 204 No Content
        with patch.object(self.router.opener, "open", return_value=response):
            REAL_PROBE(self.router, "http://router", 2)
        for status in (404, 500, 503):
            with self.subTest(status=status):
                with patch.object(self.router.opener, "open",
                                  side_effect=HTTPError("http://router/", status, "down", {}, None)):
                    with self.assertRaises(HTTPError):
                        REAL_PROBE(self.router, "http://router", 2)

    def test_corrected_address_is_not_blocked_by_old_login_cooldown(self):
        with patch.object(self.router, "_post", return_value={"error_code": -40401}) as post:
            self.assertIn("登录失败", self.read(url="http://192.168.0.1")["status"])
            self.now.return_value = 105
            # Re-encrypting the password for the same router keeps the cooldown.
            self.assertIn("冷却", self.read("re-encrypted", url="http://192.168.0.1")["status"])
            self.assertEqual(post.call_count, 1)
            # A corrected address is a different router, so the login may be tried.
            self.assertIn("登录失败", self.read(url="http://192.168.1.1")["status"])
            self.assertEqual(post.call_count, 2)

    def test_login_transport_or_protocol_failure_never_auto_retries(self):
        for error in (OSError("contains-private-password"), http.client.RemoteDisconnected(),
                      HTTPError("http://router/stok=private-token/ds", 403, "private-body", {}, None),
                      ValueError("private-response"), {"error_code": 0}, {"error_code": 0, "stok": ""}):
            with self.subTest(error=type(error).__name__):
                self.router = tplink.Router()
                self.now.return_value = 100
                with patch.object(self.router, "_post", side_effect=[error]) as post:
                    self.assertIn("登录结果无法确认", self.read()["status"])
                    for now in (200, 1000, 100000):
                        self.now.return_value = now
                        self.assertTrue(self.read()["offline"])
                    self.assertEqual(post.call_count, 1)

    def test_manual_retries_cannot_bypass_login_cooldown(self):
        with patch.object(self.router, "_post", return_value={"error_code": -40401}) as post:
            self.read()
            for now in (101, 120, 159):
                self.now.return_value = now
                self.assertIn("冷却", self.read(f"re-encrypted-at-{now}")["status"])
                self.assertEqual(post.call_count, 1)
            self.now.return_value = 160
            self.assertIn("登录失败", self.read("final-re-encryption")["status"])
            self.assertEqual(post.call_count, 2)

    def test_locks_reset_full_and_unknown_auth_reasons_never_relogin(self):
        for phase in ("wan", "hosts"):
            for reason in (-40402, -40404, -40405, -40406, -40408, -99999):
                for wrapped in (False, True):
                    with self.subTest(phase=phase, reason=reason, wrapped=wrapped):
                        if not wrapped and reason == -99999:
                            continue  # Generic query errors get one delayed recovery attempt.
                        self.router = tplink.Router()
                        self.now.return_value = 100
                        failure = {"error_code": -40401, "data": {"code": reason}} if wrapped else {"error_code": reason}
                        replies = [{"error_code": 0, "stok": "valid"}, WAN, {"error_code": -1}]
                        if phase == "hosts":
                            replies.append(WAN)
                        replies.append(failure)
                        with patch.object(self.router, "_post", side_effect=replies) as post:
                            self.assertFalse(self.read()["offline"])
                            self.now.return_value = 104
                            result = self.read()
                            self.assertIsNotNone(self.router.blocked)
                            self.assertIn(str(reason), result["status"])
                            self.now.return_value = 100000
                            self.assertTrue(self.read()["offline"])
                            self.assertEqual(post.call_count, len(replies))

    def test_new_sessions_that_cannot_query_wan_do_not_form_login_loop(self):
        for failure in ({"error_code": -40401, "data": {"code": -40403}},
                        {"error_code": -1}, {"error_code": 0}, OSError("down")):
            with self.subTest(failure=type(failure).__name__):
                self.router = tplink.Router()
                self.now.return_value = 100
                with patch.object(self.router, "_post", side_effect=[
                        {"error_code": 0, "stok": "unusable"}, failure]) as post:
                    self.assertIn("已暂停", self.read()["status"])
                    self.now.return_value = 100000
                    self.read()
                    self.assertEqual(post.call_count, 2)

    def test_diagnostic_logs_only_fixed_phase_and_numeric_codes(self):
        failure = {"error_code": -40401, "data": {"code": -40408, "password": "private-password"},
                   "stok": "private-token", "message": "private-body"}
        with patch.object(self.router, "_post", return_value=failure), self.assertLogs(tplink.log, "WARNING") as logs:
            self.read()
        text = " ".join(logs.output)
        self.assertIn("phase=login", text)
        self.assertIn("error_code=-40401 auth_code=-40408", text)
        self.assertNotIn("private", text)
        self.assertEqual(tplink._codes({"error_code": True, "data": {"code": "private-body"}}), (None, None))
        self.router = tplink.Router()
        error = HTTPError("http://router/stok=private-token/ds", 403, "private-password", {}, None)
        with patch.object(self.router, "_post", side_effect=error), self.assertLogs(tplink.log, "WARNING") as logs:
            self.read()
        self.assertIn("http_status=403", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    def test_disabling_monitoring_preserves_login_failure_latch(self):
        config = settings.validate({"routers": [dict(settings.DEFAULT_TPLINK,
            id="tp", name="网关", enabled=True, password_protected="cipher")]})
        worker = Collector(config)
        with patch.object(tplink.Router, "_post", return_value={"error_code": -40401}) as post:
            self.assertTrue(worker._read_routers(config)[0]["offline"])
            device = worker.routers["tp"]
            config["routers"][0]["enabled"] = False
            self.assertEqual(worker._read_routers(config), [])
            config["routers"][0]["enabled"] = True
            self.assertTrue(worker._read_routers(config)[0]["offline"])
            self.assertIs(worker.routers["tp"], device)
            self.assertEqual(post.call_count, 1)
            config["routers"] = []
            worker._read_routers(config)
            self.assertNotIn("tp", worker.routers)

    def test_real_http_reboot_uses_only_public_probes_until_ready(self):
        state = {"booting": False, "locked": False, "logins": 0, "requests": []}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                state["requests"].append(("GET", self.path, dict(self.headers)))
                self.reply(503 if state["booting"] else 200, b"<html>Management</html>")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["requests"].append(("POST", self.path, body))
                if state["booting"]:
                    self.reply(503, b"<html>Booting</html>")
                    return
                if "login" in body:
                    state["logins"] += 1
                    result = {"error_code": 0, "stok": f"session-{state['logins']}"}
                elif state["locked"]:
                    result = {"error_code": -40401, "data": {"code": -40408}}
                elif "network" in body:
                    result = WAN
                else:
                    result = {"error_code": 0, "hosts_info": {"online_host": []}}
                self.reply(200, json.dumps(result).encode())

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            with patch.object(tplink, "_READY_INTERVAL", 10), patch.object(self.router, "_probe",
                    side_effect=lambda base, timeout: REAL_PROBE(self.router, base, timeout)):
                self.assertTrue(self.read(url=url)["offline"])
                self.assertEqual(state["logins"], 0)
                self.now.return_value = 110
                self.assertFalse(self.read(url=url)["offline"])
                self.assertEqual(state["logins"], 1)
                state["booting"] = True
                for now in (120, 179, 180, 210):
                    self.now.return_value = now
                    self.assertTrue(self.read(url=url)["offline"])
                    self.assertEqual(state["logins"], 1)
                self.assertEqual(sum(method == "POST" for method, _, _ in state["requests"]), 4)
                state["booting"] = False
                self.now.return_value = 240
                self.read(url=url)
                self.assertEqual(state["logins"], 1)
                self.now.return_value = 250
                self.assertFalse(self.read(url=url)["offline"])
                self.assertEqual(state["logins"], 2)
                state["locked"] = True
                self.now.return_value = 252
                self.assertIn("已锁定", self.read(url=url)["status"])
                requests = len(state["requests"])
                self.now.return_value = 10000
                self.read(url=url)
                self.assertEqual(len(state["requests"]), requests)
                for method, path, data in state["requests"]:
                    if method == "GET":
                        self.assertEqual(path, "/")
                        self.assertNotIn("Cookie", data)
                        self.assertNotIn("Authorization", data)
                    elif "login" in data:
                        self.assertEqual(data["login"]["password"], tplink.encode_password("123456"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows 显示格式")
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

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows 显示格式")
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
