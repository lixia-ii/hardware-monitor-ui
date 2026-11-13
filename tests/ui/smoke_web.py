"""Isolated end-to-end desktop/HTTP management test, using simulated sensors."""
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.environ["HARDWARE_MONITOR_DATA_DIR"] = str(ROOT / ".build" / "test-data" / "web-smoke")
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
(Path(os.environ["HARDWARE_MONITOR_DATA_DIR"]) / "management_devices.json").unlink(missing_ok=True)
sys.argv.append("--self-test")  # No hardware, driver prompts, firewall or startup changes.

from hwmonitor import settings
from hwmonitor.core import history, management
from hwmonitor.system import auth
from hwmonitor.system.webbridge import WebError
from hwmonitor.sensors import reading
from hwmonitor.ui.main_window import MainWindow
from tests.support import sample_tree


def port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


management_port, data_port = port(), port()
config = settings.validate({"management": {"enabled": True, "port": management_port},
                            "web": {"enabled": True, "port": data_port}})
fixture = reading.read(source=type("Source", (), {"sample": lambda self: sample_tree()})(), allow_nvidia=False)
failures, done = [], threading.Event()
session = {}


def request(method, path, body=None, extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", management_port, timeout=10)
    headers = {"Content-Type": "application/json", **session, **(extra or {})}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    conn.close()
    return result


def check_http(app):
    try:
        assert app.management.server.server_address[0] == "0.0.0.0"
        assert request("GET", "/api/state")[0] == 401
        assert request("GET", "/")[0] == 200
        with app.management.auth.lock:
            session["Cookie"] = f"{auth.SESSION_COOKIE}={app.management.auth._issue('smoke', '127.0.0.1')}"
        current = json.loads(request("GET", "/api/settings")[2])
        status, _, body = request("POST", "/api/settings", {"revision":current["revision"], "changes":{
            "interval":2, "topmost":True, "alerts":{"cpu_temp":91}, "floating":{"enabled":True,"opacity":.7},
            "display":{"cpu_temp":{"name":"手机设置的温度","visible":True,"order":1}},
            "routers":[{"id":"web-device","name":"网页加入的设备","type":"hwmonitor","url":"http://127.0.0.1:1","enabled":False,"nic":None}]
        }})
        assert status == 200, body.decode()
        updated = json.loads(body)
        assert updated["settings"]["alerts"]["cpu_temp"] == 91
        assert "management" not in updated["settings"]
        assert request("POST", "/api/settings", {"revision":current["revision"],"changes":{"interval":3}})[0] == 409
        # The floating window is open now: changes apply to it in place.
        status, _, body = request("POST", "/api/settings", {"revision":updated["revision"], "changes":{
            "floating":{"opacity":.5,"show_mem":False}}})
        assert status == 200, body.decode()
        deadline = time.monotonic() + 5
        while json.loads(request("GET", "/api/hardware")[2]).get("loading") and time.monotonic() < deadline:
            time.sleep(.05)
        assert request("GET", "/api/hardware?refresh=1")[0] == 200
        assert request("GET", "/api/state", None, {"Host": f"rebind.test:{management_port}"})[0] == 403
        history_data = json.loads(request("GET", "/api/history?hours=24")[2])
        assert history_data["count"] >= 1
        assert "手机设置的温度" not in json.dumps(history_data)  # metrics retain stable keys
        status, _, csv = request("GET", "/api/history.csv?hours=24")
        assert status == 200 and csv.startswith(b"\xef\xbb\xbf") and b"cpu_temp" in csv
        assert request("GET", "/api/history?hours=2")[0] == 400
        assert request("GET", "/api/state")[0] == 200
        first = json.loads(request("GET", "/api/alerts?page=1")[2])
        last = json.loads(request("GET", "/api/alerts?page=3")[2])
        assert first["total"] >= 53 and len(first["items"]) == 20
        assert last["page"] == 3 and last["items"]
        assert request("GET", "/api/alerts?page_size=101")[0] == 400
        diag = json.loads(request("GET", "/api/diagnostics")[2])
        assert "password_hash" not in json.dumps(diag)
        assert request("GET", "/api/session")[0] == 404
    except Exception:
        failures.append(traceback.format_exc())
    finally:
        done.set()


with patch("hwmonitor.settings.load", return_value=config), patch("hwmonitor.core.collector.reading.read", side_effect=lambda *args: dict(fixture)), patch("hwmonitor.system.autostart.sync"), patch("hwmonitor.system.autostart.is_enabled", return_value=False), patch("hwmonitor.core.actions.hwinfo.load", return_value={"model":"Fixture", "details":[], "error":None}) as hardware_load:
    app = MainWindow()
    for index in range(53):
        app.runtime.record(f"分页验证告警 {index}")
    app.root.withdraw()
    app.root.report_callback_exception = lambda *args: failures.append(str(args))
    app.history.append((time.time(), history.sample(fixture)))
    deadline = time.monotonic() + 30
    def finish():
        if not done.is_set() and time.monotonic() < deadline:
            app.root.after(100, finish)
            return
        try:
            assert done.is_set(), "HTTP worker timed out"
            app.alerts_btn.invoke()
            alert_window = app.alerts_win
            assert "分页验证告警" in alert_window.text.get("1.0", "end")
            app.show_alerts()
            assert app.alerts_win is alert_window
            alert_window.destroy()
            app.show_alerts()
            assert app.alerts_win is not alert_window
            app.alerts_win.destroy()
            # From the modal settings dialog the log takes the grab, so it can be closed.
            app.show_settings()
            app.show_alerts()
            assert app.root.grab_current() is app.alerts_win
            app.alerts_win.event_generate("<Escape>")
            app.root.update()
            assert not app.alerts_win.winfo_exists()
            assert app.root.grab_current() is app.settings_win
            app.settings_win.destroy()
            assert app.settings["interval"] == 2
            assert app.root.attributes("-topmost")
            assert app.float_win is not None
            assert app.settings["floating"]["opacity"] == .5
            assert hardware_load.call_count == 2  # refresh=1 reads again
            assert app.float_win.options is app.settings["floating"]
            assert not app.float_win.rows["mem"].frame.winfo_manager()
            # An open customization dialog holds unsaved preferences: web saves wait.
            app.show_customization()
            try:
                app._web_dispatch("update_settings", {"revision": management.revision(app.settings), "changes": {"interval": 3}})
                raise AssertionError("web save accepted while customization dialog open")
            except WebError as error:
                assert error.status == 409
            app.customization_win.destroy()
            assert app.settings["interval"] == 2
            assert app.metric_titles["cpu_temp"].cget("text") == "手机设置的温度"
            assert app.collector.options["interval"] == 2
            assert app.web.server is not None and app.management.server is not None
            assert app.settings["web"]["port"] == data_port
            # The data endpoint is switched from the web page like the desktop setting.
            def web(changes):
                return app._web_dispatch("update_settings", {"revision": management.revision(app.settings), "changes": {"web": changes}})
            try:
                web({"port": management_port})
                raise AssertionError("data port equal to the management port accepted")
            except WebError as error:
                assert error.status == 400
            assert web({"enabled": False})["settings"]["web"]["enabled"] is False
            assert app.web.server is None
            assert web({"enabled": True})["web_error"] is None
            assert app.web.server is not None
            # A pairing request is shown on the main window until it is used or expires.
            pair = "smoke-pair-0000000000"
            with app.management.auth.lock:
                app.management.auth.captchas[auth.digest(pair)] = ("23456", time.time() + 60)
            with patch("hwmonitor.system.auth.mailer.load", return_value=dict(auth.mailer.DEFAULT)):
                app.management.auth.request(pair, "23456", "192.168.1.9", "iPhone AppleWebKit")
            app.tick()
            app.status_msg = ("", 0.0)  # Temporary messages (here: the smoke device signing in) take precedence.
            app._notice(None)
            code = app.management.auth.active()[0]["code"]
            assert code in app.status.cget("text"), app.status.cget("text")
            app.management.auth.verify(pair, code)
            app.tick()
            assert code not in app.status.cget("text")
            assert len(app.management.auth.devices(None)) == 2
            app.show_settings("access")
            assert app._settings_page == "access"
            labels = []
            pending = [app.settings_pages["access"]]
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if widget.winfo_class() == "Label":
                    labels.append(widget.cget("text"))
            for text in ("SMTP 服务器", "授权码", "收件地址", "smoke", "iPhone · 主屏幕应用", "全部移除"):
                assert any(text in label for label in labels), text
            app.settings_win.destroy()
            app.show_settings()
            texts = []
            def walk(widget):
                try:
                    texts.append(str(widget.cget("text")))
                except Exception:
                    pass
                for child in widget.winfo_children():
                    walk(child)
            walk(app.settings_win)
            assert all(label not in texts for label in ("与内网数据服务分开", "直接开放公网", "管理密码", "HTTPS 证书", "HTTPS 私钥"))
        except Exception:
            failures.append(traceback.format_exc())
        app.quit()
    app.root.after(200, lambda: threading.Thread(target=check_http, args=(app,), daemon=True).start())
    app.root.after(400, finish)
    app.root.mainloop()

if failures:
    raise AssertionError("\n".join(failures))
print("Management integration passed: pairing required, pairing code on the main window, HTTP settings -> desktop/collector/floating, history/CSV, conflicts, diagnostics, separate ports and removed rows")
