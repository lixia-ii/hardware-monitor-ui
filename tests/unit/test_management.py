"""LAN management service, settings boundaries and independent data service."""
import copy
import http.client
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

from hwmonitor import paths, settings
from hwmonitor.core import management
from hwmonitor.system import auth, management_server, webserver
from hwmonitor.system.webbridge import Bridge, WebError


def done(value):
    future = Future()
    future.set_result(value)
    return future


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.validate({})

    def update(self, changes):
        return management.update(self.config, {"revision": management.revision(self.config), "changes": changes})

    def test_default_and_local_roundtrip(self):
        self.assertEqual(self.config["web"]["port"], 8085)
        self.assertEqual(self.config["management"]["port"], 7076)
        self.assertFalse(self.config["management"]["enabled"])
        self.assertEqual(self.config["management"], {"enabled":False, "port":7076})
        legacy = settings.validate({"management":{"enabled":True,"port":7076,"password_hash":"old-hash","certfile":"old.pem","keyfile":"old.key"}})
        self.assertEqual(legacy["management"], {"enabled":True,"port":7076})
        self.assertNotIn("management", management.public_settings(legacy))

    def test_valid_patch_preserves_unrelated_fields(self):
        changed = self.update({"interval": 2, "alerts": {"cpu_temp": 90}, "floating": {"opacity": .7}})
        self.assertEqual(changed["interval"], 2)
        self.assertEqual(changed["alerts"]["cpu_temp"], 90)
        self.assertEqual(changed["floating"]["opacity"], .7)
        self.assertEqual(changed["web"], self.config["web"])
        self.assertEqual(self.config["interval"], 1)

    def test_stale_and_malformed_updates_rejected(self):
        with self.assertRaises(WebError) as result:
            management.update(self.config, {"revision": "old", "changes": {"interval": 2}})
        self.assertEqual(result.exception.status, 409)
        for changes in ({"management": {}}, {"web": {"port": 80}}, {"web": {"enabled": 1}}, {"web": {"host": "0.0.0.0"}}, {"interval": 0}, {"interval": True},
                        {"alerts": {"enabled": 1}}, {"floating": {"opacity": "0.7"}},
                        {"display": {"cpu_temp": {"visible": 0}}}, {"nic_ids": [None]},
                        {"gpu_id": 23}, {"alerts": {"cpu_temp": float("nan")}}):
            with self.subTest(changes=changes), self.assertRaises(WebError):
                self.update(changes)

    def test_all_device_kinds_and_secret_preservation(self):
        for kind, url in (("openwrt", "http://router/metrics"), ("hwmonitor", "http://pc"), ("tplink", "http://router")):
            device = dict(id="new", type=kind, url=url, name="设备", enabled=False, nic=None)
            if kind == "tplink":
                device.update(notify_clients=True)
            changed = self.update({"routers": [device]})
            self.assertEqual(len(changed["routers"]), 1)
            self.assertTrue(changed["routers"][0]["url"].startswith(url))
        self.config = settings.validate({"routers": [dict(settings.DEFAULT_TPLINK, id="router1", name="网关", password_protected="protected-secret")]})
        device = management.public_settings(self.config)["routers"][0]
        self.assertTrue(device["password_saved"])
        self.assertNotIn("protected-secret", json.dumps(device))
        self.assertEqual(self.update({"routers": [device]})["routers"][0]["password_protected"], "protected-secret")
        device.update(password="new-password", reconnect=True)
        with patch("hwmonitor.core.management.credentials.protect", return_value="new-protected") as protect:
            changed = self.update({"routers": [device]})
            protect.assert_called_once_with("new-password")
        self.assertEqual(changed["routers"][0]["password_protected"], "new-protected")
        self.assertNotIn("password", changed["routers"][0])

    def test_saved_password_never_follows_changed_address(self):
        self.config = settings.validate({"routers": [dict(settings.DEFAULT_TPLINK, id="router1", name="网关", url="http://192.168.0.1", password_protected="protected-secret")]})
        device = management.public_settings(self.config)["routers"][0]
        device["url"] = "http://203.0.113.9"
        with patch("hwmonitor.core.management.credentials.unprotect", side_effect=lambda p: p and "router-password") as unprotect:
            self.assertEqual(self.update({"routers": [device]})["routers"][0]["password_protected"], "")
            with self.assertRaises(WebError):
                self.update({"routers": [dict(device, reconnect=True)]})
            self.assertNotIn(("protected-secret",), [c.args for c in unprotect.call_args_list])

    def test_floating_position_is_desktop_only(self):
        before = management.revision(self.config)
        self.config["floating"]["x"] += 50
        self.assertEqual(management.revision(self.config), before)
        self.assertNotIn("x", management.public_settings(self.config)["floating"])
        with self.assertRaises(WebError):
            self.update({"floating": {"x": 0}})

    def test_history_query_snapshot(self):
        now = time.time()
        rows = [(now - 10, {"cpu_temp": 50.0, "gpu_id": "g", "gpu_name": "G"}), (now - 7200, {"cpu_temp": 90.0})]
        snapshot = {"rows": rows, "time": now, "gpu_id": "g", "net_selection": None}
        result = management.history_query(snapshot, {})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["peaks"]["cpu_temp"], 50.0)
        self.assertNotIn("rows", result)
        self.assertEqual(len(management.history_query(snapshot, {"hours": ["24"]}, export=True)["rows"]), 2)
        with self.assertRaises(WebError):
            management.history_query(snapshot, {"hours": ["2"]})

    def test_device_validation_and_cleanup(self):
        device = management.public_settings(self.config)["routers"][0]
        for bad in ({**device,"url":"file:///secret"}, {**device,"enabled":1}, {**device,"password_protected":"injected"}):
            with self.assertRaises(WebError):
                self.update({"routers": [bad]})
        with self.assertRaises(WebError):
            self.update({"routers": [device, device]})
        self.config["display"]["sensor:device:primary:cpu_temp"] = {"visible": False, "name": "", "order": 100}
        self.config["collapsed"] = ["router:primary", "case"]
        changed = self.update({"routers": []})
        self.assertEqual(changed["display"], {})
        self.assertEqual(changed["collapsed"], ["case"])


class DisplayBoundTests(unittest.TestCase):
    def test_display_total_is_bounded(self):
        config = settings.validate({})
        for batch in range(2):
            patch_ = {f"x{batch}-{i}": {"visible": True, "name": "", "order": 100} for i in range(1500)}
            payload = {"revision": management.revision(config), "changes": {"display": patch_}}
            if batch:
                with self.assertRaises(WebError):
                    management.update(config, payload)
            else:
                config = management.update(config, payload)
        with self.assertRaises(WebError):
            management.update(config, {"revision": management.revision(config),
                                       "changes": {"display": {"x" * 201: {"visible": True}}}})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = patch.object(paths, "DATA", Path(self.temp.name))
        data.start()
        self.addCleanup(data.stop)
        self.server = management_server.ManagementServer()
        with self.server.auth.lock:
            self.token = self.server.auth._issue("测试设备", "127.0.0.1")
        self.port = free_port()
        self.server.bridge = Mock()
        self.server.bridge.call.side_effect = lambda action, payload=None: (
            {"rows": [], "time": time.time(), "gpu_id": None, "net_selection": None} if action == "history"
            else {"data": None, "alerts": [], "hardware": done({"cpu": "x"}), "stats": {}} if action == "diagnostics"
            else {"ok": True, "value": float("nan")})
        self.assertIsNone(self.server.apply(True, self.port))

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, payload=None, headers=None, raw=None, login=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if login:
            request_headers.setdefault("Cookie", f"{auth.SESSION_COOKIE}={self.token}")
        body = json.dumps(payload).encode() if payload is not None else raw
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_lan_listener_and_routes_require_pairing(self):
        self.assertEqual(self.server.server.server_address[0], "0.0.0.0")
        routes = ("/api/state", "/api/alerts", "/api/settings", "/api/history", "/api/history.csv", "/api/hardware",
                  "/api/diagnostics", "/api/auth/devices")
        for path in routes:
            status, headers, _ = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("Set-Cookie", headers)
            self.assertEqual(self.request("GET", path, login=False)[0], 401, path)
            forged = {"Cookie": f"{auth.SESSION_COOKIE}=forged-token-value"}
            self.assertEqual(self.request("GET", path, headers=forged, login=False)[0], 401, path)
        self.assertEqual(self.request("POST", "/api/settings", {"changes":{}})[0], 200)
        for path in ("/api/settings", "/api/auth/approve", "/api/auth/revoke", "/api/auth/logout"):
            self.assertEqual(self.request("POST", path, {"changes":{}}, login=False)[0], 401, path)
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["pairing"], 0)
        for path in ("/", "/app.js", "/app.css", "/manifest.webmanifest", "/icon.svg", "/icon.png"):
            status, headers, body = self.request("GET", path, login=False)
            self.assertEqual(status, 200, path)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertGreater(len(body), 0)
        html = self.request("GET", "/", login=False)[2]
        self.assertIn(b'id="captcha-form"', html)
        self.assertIn(b'id="app" hidden', html)

    def test_pairing_flow_over_http(self):
        status, headers, body = self.request("GET", "/api/auth/status", login=False)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["authenticated"])
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        pair = cookie.split(";")[0]
        status, headers, image = self.request("GET", "/api/auth/captcha", headers={"Cookie": pair}, login=False)
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        answer = self.server.auth.captchas[auth.digest(pair.split("=", 1)[1])][0]
        self.assertEqual(self.request("POST", "/api/auth/request", {"captcha": answer},
                                      {"Cookie": pair, "Origin": "http://evil.test"}, login=False)[0], 403)
        with patch.object(auth.mailer, "load", return_value=dict(auth.mailer.DEFAULT)):
            status, _, body = self.request("POST", "/api/auth/request", {"captcha": answer}, {"Cookie": pair}, login=False)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["pending"]["mail"], "unconfigured")
        # Signed-in devices see the code; the requester never receives it.
        self.assertNotIn(self.server.auth.active()[0]["code"].encode(), body)
        listed = json.loads(self.request("GET", "/api/auth/devices")[2])
        self.assertEqual(len(listed["pending"]), 1)
        self.assertEqual(json.loads(self.request("GET", "/api/state")[2])["pairing"], 1)
        code = listed["pending"][0]["code"]
        self.assertEqual(self.request("POST", "/api/auth/verify", {"code": code}, login=False)[0], 400)  # other browser
        status, headers, _ = self.request("POST", "/api/auth/verify", {"code": code}, {"Cookie": pair}, login=False)
        self.assertEqual(status, 200)
        session = headers["Set-Cookie"].split(";")[0]
        self.assertTrue(session.startswith(auth.SESSION_COOKIE + "="))
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": session}, login=False)[0], 200)
        devices = json.loads(self.request("GET", "/api/auth/devices")[2])["devices"]
        self.assertEqual(len(devices), 2)
        self.assertNotIn(session.split("=", 1)[1], (Path(self.temp.name) / "management_devices.json").read_text(encoding="utf-8"))
        status, headers, _ = self.request("POST", "/api/auth/logout", {}, {"Cookie": session}, login=False)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": session}, login=False)[0], 401)

    def test_approval_and_revocation_over_http(self):
        pair = "p" * 32
        with self.server.auth.lock:
            self.server.auth.captchas[auth.digest(pair)] = ("23456", time.time() + 60)
        with patch.object(auth.mailer, "load", return_value=dict(auth.mailer.DEFAULT)):
            self.assertEqual(self.request("POST", "/api/auth/request", {"captcha": "23456"},
                                          {"Cookie": f"{auth.PAIR_COOKIE}={pair}"}, login=False)[0], 200)
        ident = self.server.auth.active()[0]["id"]
        self.assertEqual(self.request("POST", "/api/auth/approve", {"id": ident})[0], 200)
        self.assertEqual(self.request("POST", "/api/auth/approve", {"id": ident})[0], 404)
        status, headers, body = self.request("GET", "/api/auth/status", headers={"Cookie": f"{auth.PAIR_COOKIE}={pair}"}, login=False)
        self.assertTrue(json.loads(body)["authenticated"])
        session = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": session}, login=False)[0], 200)
        self.assertEqual(self.request("POST", "/api/auth/revoke", {"id": "others"})[0], 200)
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": session}, login=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_captcha_and_pairing_limits_ignore_forwarding_headers_and_cookie_rotation(self):
        with patch.object(auth.captcha, "png", return_value=b"png"):
            for index in range(auth.CAPTCHA_LIMIT * auth.SHARED_SOURCES):
                headers = {"Cookie": f"hm_pair=browser-cookie-{index:04d}", "X-Forwarded-For": f"192.0.2.{index}"}
                self.assertEqual(self.request("GET", "/api/auth/captcha", headers=headers, login=False)[0], 200)
            self.assertEqual(self.request("GET", "/api/auth/captcha", login=False)[0], 429)
        for index in range(auth.REQUEST_LIMIT * auth.SHARED_SOURCES):
            headers = {"Cookie": f"hm_pair=another-cookie-{index:04d}", "X-Forwarded-For": f"192.0.2.{index}"}
            self.assertEqual(self.request("POST", "/api/auth/request", {"captcha": "wrong"}, headers, login=False)[0], 400)
        self.assertEqual(self.request("POST", "/api/auth/request", {"captcha": "wrong"}, login=False)[0], 429)

    def test_failed_revoke_and_logout_can_be_retried_over_http(self):
        ident = self.server.auth.devices(None)[0]["id"]
        for endpoint, payload in (("revoke", {"id": ident}), ("logout", {})):
            with self.subTest(endpoint=endpoint):
                with patch.object(auth.os, "replace", side_effect=OSError), self.assertLogs(auth.log, level="ERROR"):
                    self.assertEqual(self.request("POST", "/api/auth/" + endpoint, payload)[0], 500)
                body = json.loads(self.request("GET", "/api/auth/devices")[2])
                self.assertEqual(body["devices"][0]["id"], ident)
        self.assertEqual(self.request("POST", "/api/auth/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertIsNone(auth.Auth().check(self.token, "127.0.0.1")[0])

    def test_unknown_routes_and_cross_site_writes(self):
        for path in ("/api/session", "/api/login", "/data.json"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/data.json", login=False)[0], 404)
        self.assertEqual(self.request("POST", "/api/login", {})[0], 404)
        self.assertEqual(self.request("POST", "/api/settings", {}, {"Origin":"http://evil.test"})[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", {}, {"Sec-Fetch-Site":"cross-site"})[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", {}, {"Origin":f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_rebindable_host_names_rejected(self):
        for host in ("evil.test:%d", "localhost.evil.test:%d", "127.0.0.1", "127.0.0.1:1"):
            host = host % self.port if "%d" in host else host
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/state", headers={"Host": host})[0], 403)
                self.assertEqual(self.request("POST", "/api/settings", {}, {"Host": host})[0], 403)
        for host in ("localhost:%d", "192.168.1.20:%d", "[::1]:%d"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/state", headers={"Host": host % self.port})[0], 200)

    def test_non_finite_numbers_serialized_as_null(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["value"])

    def test_write_limits_and_stop_restart(self):
        self.assertEqual(self.request("POST", "/api/settings", raw=b"[]")[0], 400)
        self.assertEqual(self.request("POST", "/api/settings", {}, {"Content-Length":"131073"})[0], 413)
        self.assertEqual(self.request("POST", "/api/settings", {}, {"Content-Type":"text/plain"})[0], 415)
        original = self.server.server
        self.assertIsNone(self.server.apply(True, self.port))
        self.assertIs(self.server.server, original)
        self.server.apply(False, self.port)
        self.assertIsNone(self.server.server)
        self.assertIsNone(self.server.apply(True, self.port))
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_failed_bind_is_retried(self):
        self.server.stop()
        with patch.object(management_server, "BoundedServer", side_effect=OSError):
            self.assertIsNotNone(self.server.apply(True, self.port))
        self.assertIsNone(self.server.apply(True, self.port))
        self.assertIsNotNone(self.server.server)

    def test_firewall_updates_apply_in_order(self):
        applied, started, release = [], threading.Event(), threading.Event()
        def slow(port, rule):
            started.set()
            release.wait(3)
            applied.append(port)
        with patch.object(webserver, "firewall", side_effect=slow):
            management_server.firewall_later(7076)
            started.wait(3)
            management_server.firewall_later(None)
            management_server.firewall_later(7077)
            release.set()
            for _ in range(100):
                if len(applied) == 2:
                    break
                time.sleep(.02)
        self.assertEqual(applied, [7076, 7077])

    def test_independent_legacy_service_and_firewall(self):
        legacy = webserver.WebServer()
        port = free_port()
        try:
            self.assertIsNone(legacy.apply(True, False, port))
            legacy.publish({"Children": []})
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/data.json")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            self.assertEqual(self.request("GET", "/data.json")[0], 404)
            self.server.stop()
            self.assertIsNotNone(legacy.server)
        finally:
            legacy.stop()
        with patch("hwmonitor.system.webserver.subprocess.run") as run:
            self.assertTrue(management_server.firewall(7076))
        if __import__("sys").platform != "win32":
            run.assert_not_called()
            return
        args = run.call_args_list[1].args[0]
        self.assertIn("remoteip=localsubnet", args)
        self.assertIn("localport=7076", args)
        self.assertIn("name=HardwareMonitorUI Management", args)
        self.assertNotEqual(management_server.RULE, webserver.RULE)


class BridgeTests(unittest.TestCase):
    def test_dispatch_from_owner_thread_and_cancelled_queue(self):
        bridge, result = Bridge(), []
        thread = threading.Thread(target=lambda: result.append(bridge.call("state", {"x":1})))
        thread.start()
        deadline = time.monotonic() + 2
        while bridge.pending.empty() and time.monotonic() < deadline:
            time.sleep(.001)
        dispatch = Mock(return_value={"ok":True})
        bridge.drain(dispatch)
        thread.join(timeout=2)
        self.assertEqual(result, [{"ok":True}])
        dispatch.assert_called_once_with("state", {"x":1})
        cancelled = Future()
        cancelled.cancel()
        bridge.pending.put((cancelled, "mutation", {}))
        bridge.drain(dispatch)
        self.assertEqual(dispatch.call_count, 1)
