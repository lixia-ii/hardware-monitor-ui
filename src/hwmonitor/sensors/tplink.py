"""TP-Link Chinese stock firmware (TL-XDR3040): login and read-only /ds queries.

Protocol/field names follow this router's public jquery, class.js and uci.js.
WAN speeds are KiB/second: BasicNetState.htm's fc() renders raw values
as KB/s and divides by 1024 for MB/s. Host-table speeds use bytes/second;
the firmware's generic netSpeedTrans() is NOT the WAN conversion.
Normalize WAN rates to bytes/second for the rest of the application.
"""
import http.client
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, unquote

from hwmonitor.sensors.openwrt import empty_reading as router_empty
from hwmonitor.system import credentials

_KEY = "RDpbLfCPsJZ7fiv"
_DICTIONARY = ("yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW")
# Firmware error.js: EUNAUTH can wrap the actual reason in data.code.
_AUTH = set(range(-40408, -40400))
_SESSION_EXPIRED = {-40401, -40403, -40407}
_RECONNECT_DELAY = 60
_LOGIN_INTERVAL = 60
_READY_INTERVAL = 10
_PROBE_INTERVAL = 30
log = logging.getLogger(__name__)


def _codes(result):
    """Only bounded integer codes are safe to display/log; never copy response text."""
    def code(value):
        return value if type(value) is int and -(2 ** 31) <= value < 2 ** 31 else None
    data = result.get("data")
    return code(result.get("error_code")), code(data.get("code")) if isinstance(data, dict) else None


def encode_password(password):
    # JS charCodeAt uses UTF-16 code units, including surrogate pairs.
    raw = password.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    return "".join(_DICTIONARY[((units[i] if i < len(units) else 187) ^
                                (ord(_KEY[i]) if i < len(_KEY) else 187)) % len(_DICTIONARY)]
                   for i in range(max(len(units), len(_KEY))))


def empty_reading(status="未连接"):
    return dict(router_empty(), status=status, wan_ip=None, wan_status=None, wan_connected=None,
                wan_uptime=None, clients=None, terminals=None, terminals_sample_time=None)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def _wan_rate(value):
    rate = _number(value)
    return rate * 1024 if rate is not None else None


def parse_wan(payload):
    network = payload.get("network")
    wan = network.get("wan_status") if isinstance(network, dict) else None
    if not isinstance(wan, dict) or not wan:
        raise ValueError("Missing WAN status")
    r = empty_reading("")
    r.update(offline=False, host="TP-Link", nic="WAN", nics=["WAN"],
             up=_wan_rate(wan.get("up_speed")), down_rate=_wan_rate(wan.get("down_speed")),
             wan_ip=wan.get("ipaddr") if isinstance(wan.get("ipaddr"), str) else None)
    # Confirmed in the router's language/cn/str.js: DOWN=0, UP=1,
    # LINKING_UP=2, LINKING_DOWN=3, DOWN_EXPT=4.
    state = wan.get("link_status")
    r["wan_status"] = str(state)[:40] if isinstance(state, (str, int)) else None
    if r["wan_status"] in ("0", "1", "2", "3", "4"):
        r["wan_connected"] = r["wan_status"] == "1"
    if r["wan_connected"]:
        r["wan_uptime"] = _number(wan.get("up_time"))
    return r


def _decode_text(value):
    """Match the firmware's JSON string reviver: decodeURIComponent once.

    Keep literal plus signs and preserve the original if any escape/UTF-8 is
    malformed, just as slpParseJSON's try/catch does in the router's jquery.
    """
    if not isinstance(value, str):
        return ""
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return value
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        return value


def parse_terminals(payload):
    """Only retain fields needed for ranking/presence; host speeds are already B/s."""
    info = payload.get("hosts_info")
    table = info.get("online_host") if isinstance(info, dict) else None
    if not isinstance(table, list):
        return None
    terminals = {}
    for row in table:
        if not isinstance(row, dict) or not row:
            return None
        for host in row.values():
            if not isinstance(host, dict):
                return None
            mac = _decode_text(host.get("mac", "")).replace("-", ":").upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                return None  # Incomplete identity data must not imply devices went offline.
            def text(key, limit):
                value = _decode_text(host.get(key))
                return "".join(c for c in value if c.isprintable()).strip()[:limit]
            ip = text("ip", 64)
            name = text("hostname", 80)
            terminals[mac] = {"id": mac, "name": (name if name != "-" else "") or ip or mac,
                              "ip": ip, "up": _number(host.get("up_speed")),
                              "down_rate": _number(host.get("down_speed"))}
    return sorted(terminals.values(), key=lambda t: (-(t["up"] or 0) - (t["down_rate"] or 0), t["id"]))


def _clients(payload):
    info = payload.get("hosts_info", {})
    if not isinstance(info, dict):
        return None
    if isinstance(info.get("host_info"), dict):
        info = info["host_info"]
    if isinstance(info.get("online_host"), list):
        return len(info["online_host"])
    count = _number(info.get("online_host"))
    if count is not None:
        return int(count)
    hosts = info.get("host_info")
    if isinstance(hosts, list) and all(isinstance(host, dict) and "online" in host for host in hosts):
        return sum(1 for host in hosts if str(host["online"]) == "1")
    return None


class Router:
    def __init__(self):
        self.identity = None
        self.session = None
        self.blocked = None
        self.next_at = 0
        # Do not reset the login cooldown when the password is re-encrypted for
        # a manual retry. Repeated clicks must not send repeated credentials.
        self.login_after = 0
        self.ready_since = None
        self.session_verified = False
        self.cached = empty_reading()
        self.clients_at = 0
        self.clients = None
        self.terminals = None
        self.terminals_sample_time = None
        # Do not send LAN credentials through a configured system HTTP proxy or redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _post(self, base, path, body, timeout):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json; charset=UTF-8",
                                                  "Referer": base + "/"})
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Response too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or _codes(result)[0] is None:
            raise ValueError("Invalid response")
        return result

    def _probe(self, base, timeout):
        """Check the public page without a password, stok, cookies or auth requests.

        Redirects (some firmware sends GET / to /login.html), authentication
        challenges and empty 2xx bodies all prove the management server is up.
        Redirects are never followed, so credentials cannot be forwarded to
        another host.
        """
        request = urllib.request.Request(base + "/", method="GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                response.read(1)
        except urllib.error.HTTPError as error:
            ready = error.code in (401, 403) or 300 <= error.code < 400
            error.close()
            if not ready:
                raise

    def _offline(self, status):
        self.clients_at = 0
        self.clients = self.terminals = self.terminals_sample_time = None
        self.cached = empty_reading(status)
        return dict(self.cached)

    def _pause(self, status):
        self.session = None
        self.session_verified = False
        self.ready_since = None
        self.blocked = status
        return self._offline(status)

    def _reconnect(self, now):
        self.session = None
        self.session_verified = False
        self.ready_since = None
        self.next_at = now + _RECONNECT_DELAY
        return self._offline(f"连接中断或会话失效，等待 {_RECONNECT_DELAY} 秒后检查管理页面再重连")

    @staticmethod
    def _log_exception(phase, error):
        # HTTPError/JSONDecodeError can contain a stok URL or entire response.
        # Never log the exception message, traceback, password or response body.
        status = getattr(error, "code", None)
        status = status if type(status) is int and 100 <= status <= 599 else None
        log.warning("TP-Link request failed phase=%s exception=%s http_status=%s",
                    phase, type(error).__name__, status)

    def _check_result(self, result, phase, now):
        outer, inner = _codes(result)
        if outer == 0:
            return None
        log.warning("TP-Link request rejected phase=%s error_code=%s auth_code=%s", phase, outer, inner)
        suffix = f"（错误码 {outer}" + (f" / {inner}" if inner is not None else "") + "）"
        reason = inner if outer == -40401 and inner is not None else outer
        if outer in (-40404, -40408) or reason in (-40404, -40408):
            return self._pause("路由器已锁定管理登录，已暂停；请先在路由器端解除限制" + suffix)
        if reason == -40405:
            return self._pause("路由器提示状态重置，已暂停；请检查初始化状态和管理密码" + suffix)
        if reason == -40406:
            return self._pause("路由器管理登录数量已满，已暂停；请关闭其他管理会话后手动重连" + suffix)
        if phase == "login":
            # A failure of any kind may count toward the router's lockout limit.
            return self._pause("登录失败，已暂停自动登录；请检查路由器后手动重连" + suffix)
        if outer in _AUTH and reason not in _SESSION_EXPIRED:
            return self._pause("路由器认证状态异常，已暂停自动登录；请检查管理页面" + suffix)
        if not self.session_verified:
            return self._pause("新会话未能读取状态，已暂停自动登录；请检查管理页面后手动重连" + suffix)
        return self._reconnect(now)

    def read(self, url, nic=None, timeout=2.0, *, password_protected=""):
        identity = (url, password_protected)
        if self.identity != identity:
            # A corrected address belongs to a different router, so its login
            # cooldown must not carry over. Re-encrypting the password for the
            # same address must keep the cooldown to prevent repeated logins.
            if self.identity is None or self.identity[0] != url:
                self.login_after = 0
            self.identity = identity
            self.session = self.blocked = None
            self.session_verified = False
            self.ready_since = None
            self.next_at = 0
            self._offline("正在检查路由器管理页面")
        if not password_protected:
            return empty_reading("请在设置中输入密码")
        if self.blocked:
            return dict(self.cached)
        now = time.monotonic()
        if now < self.next_at:
            return dict(self.cached)
        if not self.session:
            if now < self.login_after:
                self.next_at = self.login_after
                return self._offline("登录冷却中，避免触发路由器防护；稍后继续检查管理页面")
            # Two public GETs at least _READY_INTERVAL apart, after a reconnect wait.
            # While booting/unreachable, only GETs repeat -- never password logins.
            try:
                self._probe(url, timeout)
            except (OSError, ValueError, http.client.HTTPException) as error:
                self._log_exception("probe", error)
                self.ready_since = None
                self.next_at = now + _PROBE_INTERVAL
                return self._offline(f"管理页面未就绪，{_PROBE_INTERVAL} 秒后检查；暂不发送登录密码")
            if self.ready_since is None:
                self.ready_since = now
            if now - self.ready_since < _READY_INTERVAL:
                self.next_at = self.ready_since + _READY_INTERVAL
                return self._offline(f"管理页面已响应，等待 {_READY_INTERVAL} 秒确认稳定后尝试一次登录")
        phase = "login" if not self.session else "wan"
        try:
            if not self.session:
                try:
                    password = credentials.unprotect(password_protected)
                    if not password:
                        raise ValueError("Empty password")
                except (OSError, ValueError, UnicodeError):
                    return self._pause("密码无法读取，已暂停；请重新输入密码")
                # Set before sending: a lost response may still have reached the router.
                self.login_after = time.monotonic() + _LOGIN_INTERVAL
                result = self._post(url, "/", {"method": "do", "login": {"password": encode_password(password)}}, timeout)
                failure = self._check_result(result, phase, now)
                if failure is not None:
                    return failure
                token = result.get("stok")
                if not isinstance(token, str) or not token or len(token) > 256:
                    raise ValueError("Missing session")
                self.session = quote(unquote(token), safe="")
            phase = "wan"
            path = "/stok=" + self.session + "/ds"
            result = self._post(url, path, {"method": "get", "network": {"name": "wan_status"}}, timeout)
            failure = self._check_result(result, phase, now)
            if failure is not None:
                return failure
            r = parse_wan(result)
            if now >= self.clients_at:
                self.clients_at = now + 2
                self.clients = self.terminals = self.terminals_sample_time = None
                try:
                    hosts = self._post(url, path, {"method": "get", "hosts_info": {"table": "online_host"}}, timeout)
                    # Optional table failures are harmless, but auth/lockout signals
                    # must stop requests instead of being ignored until the next tick.
                    if hosts["error_code"] in _AUTH:
                        failure = self._check_result(hosts, "hosts", now)
                        if failure is not None:
                            return failure
                    if hosts["error_code"] == 0:
                        self.clients = _clients(hosts)
                        self.terminals = parse_terminals(hosts)
                        if self.terminals is not None:
                            self.terminals_sample_time = now
                except (OSError, ValueError, http.client.HTTPException):
                    pass  # Optional host table must not hide working WAN metrics.
            r["clients"] = self.clients
            r["terminals"] = self.terminals
            r["terminals_sample_time"] = self.terminals_sample_time
            self.session_verified = True
            self.next_at = 0
            self.cached = r
        except (OSError, ValueError, http.client.HTTPException) as error:
            self._log_exception(phase, error)
            if phase == "login":
                # Never retry a password after an ambiguous timeout/HTML/HTTP reply.
                return self._pause("登录结果无法确认，已暂停自动登录；请检查管理页面后手动重连")
            if not self.session_verified:
                return self._pause("新会话状态异常，已暂停自动登录；请检查管理页面后手动重连")
            return self._reconnect(now)
        return dict(self.cached)
