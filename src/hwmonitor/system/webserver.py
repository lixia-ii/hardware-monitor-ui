"""传感器数据接口: 提供与 LibreHardwareMonitor Remote Web Server 兼容的 /data.json, 可选开放给局域网。"""
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading

log = logging.getLogger(__name__)
RULE = "HardwareMonitorUI Web"


class ExclusiveHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if sys.platform == "win32":
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def lan_addresses():
    """本机的局域网 IPv4 地址 (不发送数据), 用于显示访问地址。"""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # UDP connect 只选路由, 取默认出口网卡的地址
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return list(dict.fromkeys(ip for ip in found if not ip.startswith(("127.", "169.254."))))


_firewall_lock = threading.Lock()


def firewall(port=None, rule=RULE):
    """port 为 None 时删除放行规则, 否则只对本地子网放行本程序的该端口 (需要管理员)。"""
    if sys.platform != "win32":
        return True
    run = dict(capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with _firewall_lock:  # 数据服务和管理端的规则可能同时更新, netsh 串行执行
        try:
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"], **run)
            if port is not None:
                # 按程序放行, Windows 才不会再弹"是否允许访问网络"; remoteip=localsubnet 限定局域网
                subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}", "dir=in",
                                "action=allow", "protocol=TCP", f"localport={port}", f"program={sys.executable}",
                                "remoteip=localsubnet", "profile=any"], check=True, **run)
        except (OSError, subprocess.SubprocessError):
            log.exception("Cannot update firewall rule %s", rule)
            return False
    return True


_firewall_pending, _firewall_running, _firewall_lock_pending = {}, set(), threading.Lock()


def firewall_later(port=None, rule=RULE):
    """后台更新规则: 每条规则只有一个线程按顺序执行, 连续修改时只应用最后一次的状态。"""
    with _firewall_lock_pending:
        _firewall_pending[rule] = port
        if rule in _firewall_running:
            return
        _firewall_running.add(rule)
    def drain():
        while True:
            with _firewall_lock_pending:
                if rule not in _firewall_pending:
                    _firewall_running.discard(rule)
                    return
                wanted = _firewall_pending.pop(rule)
            firewall(wanted, rule)
    threading.Thread(target=drain, name="firewall", daemon=True).start()


class WebServer:
    def __init__(self, host=None):
        self.host = host
        self.lock = threading.Lock()
        self.body = None
        self.server = None
        self.config = (False, False, 0)
        self.error = None

    def publish(self, tree):
        """采集线程每次采样后调用; tree 为 None 表示传感器暂不可用。"""
        body = None if tree is None else json.dumps(tree, ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.body = body

    def _handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/data.json")
                    self.end_headers()
                    return
                if path != "/data.json":
                    self.send_error(404)
                    return
                with web.lock:
                    body = web.body
                if body is None:
                    self.send_error(503, "Sensors are initializing")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)
        return Handler

    def apply(self, enabled, lan, port):
        """按设置启动/重启/停止; 返回错误消息 (端口被占用等), 成功返回 None。"""
        wanted = (enabled, enabled and lan, port if enabled else 0)
        if wanted == self.config and (self.server is not None or self.error or not enabled):
            return self.error
        self.stop()
        self.config, self.error = wanted, None
        if enabled:
            try:
                server = ExclusiveHTTPServer((self.host or ("0.0.0.0" if lan else "127.0.0.1"), port), self._handler())
            except OSError as e:
                log.warning("Web server cannot listen on %s: %s", port, e)
                self.error = f"传感器数据接口无法使用端口 {port}（可能已被占用）"
                return self.error
            server.daemon_threads = True
            self.server = server
            threading.Thread(target=server.serve_forever, name="web-server", daemon=True).start()
        return None

    def stop(self):
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
