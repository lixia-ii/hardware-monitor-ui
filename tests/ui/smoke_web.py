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
sys.argv.append("--self-test")  # No hardware, driver prompts, firewall or startup changes.

from hwmonitor import settings
from hwmonitor.core import history, management
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


def request(method, path, body=None, extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", management_port, timeout=10)
    headers = {"Content-Type": "application/json", **(extra or {})}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    conn.close()
    return result


def check_http(app):
    try:
        assert app.management.server.server_address[0] == "0.0.0.0"
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
        diag = json.loads(request("GET", "/api/diagnostics")[2])
        assert "password_hash" not in json.dumps(diag)
        assert request("GET", "/api/session")[0] == 404
    except Exception:
        failures.append(traceback.format_exc())
    finally:
        done.set()


with patch("hwmonitor.settings.load", return_value=config), patch("hwmonitor.core.collector.reading.read", side_effect=lambda *args: dict(fixture)), patch("hwmonitor.system.autostart.sync"), patch("hwmonitor.system.autostart.is_enabled", return_value=False), patch("hwmonitor.core.actions.hwinfo.load", return_value={"model":"Fixture", "details":[], "error":None}) as hardware_load:
    app = MainWindow()
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
print("Management integration passed: LAN access without login, HTTP settings -> desktop/collector/floating, history/CSV, conflicts, diagnostics, separate ports and removed rows")
