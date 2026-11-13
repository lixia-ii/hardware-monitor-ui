"""Independent LAN management service; never shares the legacy data port."""
import io
import ipaddress
import json
import logging
import math
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from hwmonitor.core import diagnostics, history, management
from hwmonitor.system import auth, mailer, webserver
from hwmonitor.system.webbridge import WebError

log = logging.getLogger(__name__)
RULE = "HardwareMonitorUI Management"
ASSETS = Path(__file__).with_name("web")


def finite(value):
    """Strict JSON has no NaN/Infinity; sensors and exporters may still report them."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def allowed_host(host, port):
    """Only IP literals or localhost: a DNS name could be rebound to this listener."""
    try:
        parts = urlsplit("//" + (host or ""))
        name, given = parts.hostname, parts.port
    except ValueError:
        return False
    if not name or given != port:
        return False
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class BoundedServer(webserver.ExclusiveHTTPServer):
    """Bound simultaneous clients and idle sockets."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(10)
        return request, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        except OSError:
            request.close()
        finally:
            self.slots.release()


class ManagementServer:
    def __init__(self, host="0.0.0.0"):
        self.host = host
        self.server = None
        self.config = (False, 0)
        self.error = None
        self.bridge = None
        self.auth = auth.Auth()

    def _handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HardwareMonitor"
            sys_version = ""

            def _send(self, status, body, content_type="application/json; charset=utf-8", headers=None):
                if not isinstance(body, bytes):
                    body = json.dumps(finite(body), ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                for key, value in [*(headers or {}).items(), *self.cookies]:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _check_host(self):
                if not allowed_host(self.headers.get("Host"), self.server.server_address[1]):
                    raise WebError(403, "请使用电脑的 IP 地址访问管理端")

            def _same_origin(self):
                self._check_host()
                origin = self.headers.get("Origin")
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise WebError(403, "不允许跨站请求")
                if origin:
                    parts = urlsplit(origin)
                    if parts.scheme not in ("http", "https") or parts.netloc != self.headers.get("Host"):
                        raise WebError(403, "不允许跨站请求")

            def _cookie(self, name):
                return auth.cookies(self.headers.get("Cookie")).get(name)

            def _pair(self):
                """The browser's pairing id; a new one is set on this response when missing."""
                value = self._cookie(auth.PAIR_COOKIE)
                if not value or not re.fullmatch(r"[\w-]{16,64}", value):
                    value = secrets.token_urlsafe(24)
                    self.cookies.append(("Set-Cookie", auth.set_cookie(auth.PAIR_COOKIE, value, auth.PAIR_AGE)))
                return value

            def _login(self, token):
                self.cookies.append(("Set-Cookie", auth.set_cookie(auth.SESSION_COOKIE, token, auth.SESSION_AGE)))

            def _device(self, required=True):
                token = self._cookie(auth.SESSION_COOKIE)
                device, renew = web.auth.check(token, self.client_address[0])
                if device is None and required:
                    raise WebError(401, "请先登录网页管理端")
                if renew:
                    self._login(token)
                return device

            def _call(self, action, payload=None):
                if web.bridge is None:
                    raise WebError(503, "管理服务尚未就绪")
                return web.bridge.call(action, payload)

            def _run(self, method):
                self.cookies = []
                try:
                    method()
                except WebError as error:
                    self._send(error.status, {"error": str(error)})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                except Exception:
                    log.exception("Management request failed")
                    self._send(500, {"error": "请求失败，请查看电脑端日志"})

            def do_GET(self):
                self._run(self._get)

            def _get(self):
                self._check_host()
                parsed = urlsplit(self.path)
                path = parsed.path
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/app.css": ("app.css", "text/css; charset=utf-8"),
                         "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                         "/icon.svg": ("icon.svg", "image/svg+xml"),
                         "/icon.png": ("icon.png", "image/png")}
                if path in files:
                    name, content_type = files[path]
                    self._send(200, (ASSETS / name).read_bytes(), content_type)
                    return
                if path == "/api/auth/status":
                    pair = self._pair()
                    if self._device(required=False):
                        self._send(200, {"authenticated": True})
                        return
                    status, token = web.auth.status(pair)
                    if token:
                        self._login(token)
                    self._send(200, {"authenticated": bool(token), **status})
                    return
                if path == "/api/auth/captcha":
                    self._send(200, web.auth.new_captcha(self._pair()).encode("utf-8"), "image/svg+xml")
                    return
                if not path.startswith("/api/"):
                    raise WebError(404, "页面不存在")
                device = self._device()
                if path == "/api/state":
                    self._send(200, {**self._call("state"), "pairing": len(web.auth.active())})
                elif path == "/api/settings":
                    self._send(200, self._call("settings"))
                elif path == "/api/auth/devices":
                    mail = mailer.load()
                    self._send(200, {"devices": web.auth.devices(device["hash"]), "pending": web.auth.active(),
                                     "mail": {"configured": mailer.configured(mail), "to": mailer.mask(mail["mail_to"])}})
                elif path == "/api/diagnostics":
                    snapshot = self._call("diagnostics")
                    try:
                        hardware = snapshot["hardware"].result(timeout=60)
                    except TimeoutError:
                        raise WebError(503, "硬件信息读取超时，请稍后再导出") from None
                    self._send(200, diagnostics.report(snapshot["data"], hardware, snapshot["alerts"], snapshot["stats"]))
                elif path == "/api/hardware":
                    self._send(200, self._call("hardware", parse_qs(parsed.query)))
                elif path in ("/api/history", "/api/history.csv"):
                    # The desktop thread only takes a snapshot; the reduction runs here.
                    export = path.endswith(".csv")
                    result = management.history_query(self._call("history"), parse_qs(parsed.query), export)
                    if export:
                        stream = io.StringIO(newline="")
                        history.write_csv(stream, result["rows"])
                        self._send(200, stream.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                                   {"Content-Disposition": 'attachment; filename="hardware-history.csv"'})
                    else:
                        self._send(200, result)
                else:
                    raise WebError(404, "页面不存在")

            def do_POST(self):
                self._run(self._post)

            def _post(self):
                path = urlsplit(self.path).path
                if self.headers.get_content_type() != "application/json":
                    raise WebError(415, "请使用 JSON 请求")
                try:
                    size = int(self.headers.get("Content-Length", "-1"))
                    if self.headers.get("Transfer-Encoding") or not 0 <= size <= 131072:
                        raise WebError(413, "请求过大或长度无效")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise WebError(400, "JSON 格式无效") from None
                self._same_origin()
                if path == "/api/auth/request":
                    self._send(200, web.auth.request(self._pair(), payload.get("captcha"), self.client_address[0],
                                                     self.headers.get("User-Agent")))
                    return
                if path == "/api/auth/verify":
                    self._login(web.auth.verify(self._pair(), payload.get("code")))
                    self._send(200, {"authenticated": True})
                    return
                if not path.startswith("/api/"):
                    raise WebError(404, "接口不存在")
                device = self._device()
                ident = payload.get("id")
                if path == "/api/settings":
                    self._send(200, self._call("update_settings", payload))
                elif path in ("/api/auth/approve", "/api/auth/reject"):
                    web.auth.decide(ident, path.endswith("approve"))
                    self._send(200, {"pending": web.auth.active()})
                elif path == "/api/auth/revoke":
                    if not isinstance(ident, str):
                        raise WebError(400, "设备编号无效")
                    web.auth.revoke(ident, device["hash"])
                    self._send(200, {"devices": web.auth.devices(device["hash"])})
                elif path == "/api/auth/logout":
                    web.auth.logout(self._cookie(auth.SESSION_COOKIE))
                    self.cookies.append(("Set-Cookie", auth.set_cookie(auth.SESSION_COOKIE, "", 0)))
                    self._send(200, {"authenticated": False})
                else:
                    raise WebError(404, "接口不存在")

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)
        return Handler

    def apply(self, enabled, port):
        """Start/stop the independent HTTP listener; LAN access is always enabled."""
        wanted = (enabled, port if enabled else 0)
        # A failed bind is retried on the next call: the port may have been released since.
        if wanted == self.config and (self.server is not None or not enabled):
            return self.error
        self.stop()
        self.config, self.error = wanted, None
        if enabled:
            try:
                server = BoundedServer((self.host, port), self._handler())
            except OSError:
                log.exception("Management server cannot listen on %s", port)
                self.error = f"管理端无法使用端口 {port}（可能已被占用）"
                return self.error
            self.server = server
            threading.Thread(target=server.serve_forever, name="management-server", daemon=True).start()
        return None

    def stop(self):
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()


def firewall(port=None):
    """Separate local-subnet rule for the management listener; never edits the LAN data rule."""
    return webserver.firewall(port, rule=RULE)


def firewall_later(port=None):
    webserver.firewall_later(port, rule=RULE)
