"""TP-Link Chinese stock firmware (TL-XDR3040): login and read-only /ds queries.

Protocol/field names follow this router's public jquery, class.js and uci.js.
WAN speeds are KiB/second: BasicNetState.htm's fc() renders raw values
as KB/s and divides by 1024 for MB/s. Host-table speeds use bytes/second;
the firmware's generic netSpeedTrans() is NOT the WAN conversion.
Normalize WAN rates to bytes/second for the rest of the application.
"""
import http.client
import json
import math
import re
import time
import urllib.request
from urllib.parse import quote, unquote

from hwmonitor.sensors.openwrt import empty_reading as router_empty
from hwmonitor.system import credentials

_KEY = "RDpbLfCPsJZ7fiv"
_DICTIONARY = ("yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW")
_AUTH = {-40401, -40402, -40403, -40404, -40408}


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
        self.failures = 0
        self.next_at = 0
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
        if not isinstance(result, dict) or not isinstance(result.get("error_code"), int):
            raise ValueError("Invalid response")
        return result

    def read(self, url, nic=None, timeout=2.0, *, password_protected=""):
        identity = (url, password_protected)
        if self.identity != identity:
            self.identity = identity
            self.session = self.blocked = None
            self.failures = self.next_at = self.clients_at = 0
            self.clients = None
            self.terminals = self.terminals_sample_time = None
            self.cached = empty_reading()
        if not password_protected:
            return empty_reading("请在设置中输入密码")
        if self.blocked:
            return empty_reading(self.blocked)
        now = time.monotonic()
        if now < self.next_at:
            return dict(self.cached)
        try:
            if not self.session:
                try:
                    password = credentials.unprotect(password_protected)
                except (OSError, ValueError, UnicodeError):
                    self.blocked = "请重新输入密码"
                    return empty_reading(self.blocked)
                result = self._post(url, "/", {"method": "do", "login": {"password": encode_password(password)}}, timeout)
                if result["error_code"] != 0:
                    self.blocked = "登录失败，请重新输入密码后连接"
                    return empty_reading(self.blocked)
                token = result.get("stok")
                if not isinstance(token, str) or not token or len(token) > 256:
                    raise ValueError("Missing session")
                self.session = quote(unquote(token), safe="")
            path = "/stok=" + self.session + "/ds"
            result = self._post(url, path, {"method": "get", "network": {"name": "wan_status"}}, timeout)
            if result["error_code"] in _AUTH:
                self.session = None
                self.clients_at = 0
                self.clients = self.terminals = self.terminals_sample_time = None
                self.cached = empty_reading("会话失效，正在重连")
                self.next_at = now + 5
                return dict(self.cached)
            if result["error_code"] != 0:
                raise ValueError("WAN query unavailable")
            r = parse_wan(result)
            if now >= self.clients_at:
                self.clients_at = now + 2
                self.clients = self.terminals = self.terminals_sample_time = None
                try:
                    hosts = self._post(url, path, {"method": "get", "hosts_info": {"table": "online_host"}}, timeout)
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
            self.failures = 0
            # The collector controls the refresh interval (0.5–30 s). Only
            # failures/session expiry delay reads here; healthy WAN data stays fresh.
            self.next_at = 0
            self.cached = r
        except (OSError, ValueError, http.client.HTTPException):
            # Never log HTTP exceptions containing the session URL or response bodies.
            self.failures += 1
            self.clients_at = 0
            self.clients = self.terminals = self.terminals_sample_time = None
            self.next_at = now + min(30, 2 ** min(self.failures, 5))
            self.cached = empty_reading("连接失败或固件接口不兼容")
        return dict(self.cached)
