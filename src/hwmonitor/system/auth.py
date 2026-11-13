"""Management login: captcha -> emailed pairing code (also shown locally) -> long-lived device token.

Thread-safe; HTTP workers call it directly. Desktop and headless loops drain `events` for
local display. Only SHA-256 hashes of device tokens are stored.
"""
from collections import deque
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import html
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import socket
import tempfile
import threading
import time

from hwmonitor import paths
from hwmonitor.system import captcha, mailer
from hwmonitor.system.webbridge import WebError

log = logging.getLogger(__name__)
SESSION_COOKIE, PAIR_COOKIE = "hm_session", "hm_pair"
SESSION_AGE = 180 * 86400  # Sliding: refreshed while the device keeps using the page.
PAIR_AGE = 86400
REFRESH = 6 * 3600  # Persist last use (and renew the cookie) at most this often.
CODE_TTL, CAPTCHA_TTL, MAX_ATTEMPTS = 600, 120, 5
# Every request may send a mail: bound them all, whoever asks.
MAIL_INTERVAL, MAIL_LIMITS = 60, ((3600, 5), (86400, 20))
MAX_DEVICES, MAX_PENDING, MAX_CAPTCHAS = 20, 8, 256


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cookies(header):
    try:
        jar = SimpleCookie(header or "")
    except CookieError:
        return {}
    return {key: morsel.value for key, morsel in jar.items()}


def set_cookie(name, value, age):
    return f"{name}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict"


def device_name(agent):
    agent = agent or ""
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                                          ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux"))
                   if key in agent), "未知设备")
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
                                           ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
                    if key in agent), "")
    # A home-screen web app on iOS reports no Safari token.
    if not browser and system in ("iPhone", "iPad") and "AppleWebKit" in agent:
        browser = "主屏幕应用"
    return f"{system} · {browser}" if browser else system


def describe(kind, info):
    """Local (desktop notice / server log) text for an auth event."""
    source = f"{info['name']} · {info['ip']}"
    if kind == "pairing":
        mail = {"sending": "正在发送邮件", "sent": "已发往邮箱", "failed": "邮件发送失败"}.get(info["mail"], "未配置邮件")
        return f"网页管理端配对码 {info['code']}（{source}，{mail}，10 分钟内有效）"
    if kind == "mail_failed":
        return f"配对码邮件发送失败，请检查邮件设置；配对码 {info['code']}（{source}）"
    return f"新设备已登录网页管理端：{source}"


class Auth:
    def __init__(self, load_mail=mailer.load, send_mail=mailer.send, clock=time.time):
        self.lock = threading.Lock()
        self.load_mail, self.send_mail, self.clock = load_mail, send_mail, clock
        self.events = queue.Queue()
        self.captchas, self.pending, self.sent = {}, {}, deque()
        self._devices = None

    def drain(self):
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    # Devices -----------------------------------------------------------------
    @staticmethod
    def path():
        return Path(paths.DATA) / "management_devices.json"

    def _all(self):
        if self._devices is None:
            self._devices = {}
            try:
                raw = json.loads(self.path().read_text(encoding="utf-8"))
                for item in raw.get("devices", []):
                    if (isinstance(item, dict) and isinstance(item.get("hash"), str) and isinstance(item.get("id"), str)
                            and all(isinstance(item.get(k), (int, float)) for k in ("created", "last_seen"))):
                        self._devices[item["hash"]] = {"id": item["id"], "hash": item["hash"],
                                                       "name": str(item.get("name", ""))[:60], "ip": str(item.get("ip", ""))[:64],
                                                       "created": item["created"], "last_seen": item["last_seen"],
                                                       "saved": item["last_seen"]}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError):
                log.exception("Cannot load management devices; all devices must pair again")
        now = self.clock()
        for key in [k for k, d in self._devices.items() if now - d["last_seen"] > SESSION_AGE]:
            del self._devices[key]
        return self._devices

    def _persist(self):
        target = self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        devices = [{k: d[k] for k in ("id", "hash", "name", "ip", "created", "last_seen")} for d in self._all().values()]
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as f:
                temp = f.name
                json.dump({"devices": devices}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
        except OSError:
            log.exception("Cannot save management devices")
            raise WebError(500, "登录信息保存失败，请检查电脑数据目录的写入权限") from None
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
        for d in self._all().values():
            d["saved"] = d["last_seen"]

    def _issue(self, name, ip):
        """Caller holds the lock. Returns the raw token, which is never stored."""
        devices = self._all()
        token = secrets.token_urlsafe(32)
        now = self.clock()
        while len(devices) >= MAX_DEVICES:
            del devices[min(devices, key=lambda k: devices[k]["last_seen"])]
        devices[digest(token)] = {"id": secrets.token_hex(6), "hash": digest(token), "name": name, "ip": ip,
                                  "created": now, "last_seen": now, "saved": now}
        try:
            self._persist()
        except WebError:
            del devices[digest(token)]  # Never hand out a token that would not survive a restart.
            raise
        self.events.put(("paired", {"name": name, "ip": ip}))
        return token

    def check(self, token, ip):
        """(device, renew cookie) for a valid token, else (None, False)."""
        if not token:
            return None, False
        with self.lock:
            device = self._all().get(digest(token))
            if device is None:
                return None, False
            now = self.clock()
            device["last_seen"], device["ip"] = now, ip
            renew = now - device["saved"] > REFRESH
            if renew:
                try:
                    self._persist()
                except WebError:
                    renew = False  # Still valid in memory; next request retries.
            return dict(device), renew

    def devices(self, current):
        with self.lock:
            items = sorted(self._all().values(), key=lambda d: -d["last_seen"])
            return [{"id": d["id"], "name": d["name"], "ip": d["ip"], "created": d["created"],
                     "last_seen": d["last_seen"], "current": d["hash"] == current} for d in items]

    def revoke(self, ident, current):
        """ident is a device id or "others" (every device except the current one)."""
        with self.lock:
            devices = self._all()
            keys = [k for k, d in devices.items() if (k != current if ident == "others" else d["id"] == ident)]
            if not keys and ident != "others":
                raise WebError(404, "设备不存在或已移除")
            for key in keys:
                del devices[key]
            self._persist()
            return len(keys)

    def logout(self, token):
        with self.lock:
            if self._all().pop(digest(token), None) is not None:
                self._persist()

    # Pairing -----------------------------------------------------------------
    def _prune(self, now):
        for key in [k for k, (_, expires) in self.captchas.items() if expires < now]:
            del self.captchas[key]
        for key in [k for k, p in self.pending.items() if p["expires"] < now]:
            del self.pending[key]
        while self.sent and now - self.sent[0] > MAIL_LIMITS[-1][0]:
            self.sent.popleft()

    def new_captcha(self, pair):
        with self.lock:
            now = self.clock()
            self._prune(now)
            while len(self.captchas) >= MAX_CAPTCHAS:
                del self.captchas[min(self.captchas, key=lambda k: self.captchas[k][1])]
            text = captcha.answer()
            self.captchas[digest(pair)] = (text, now + CAPTCHA_TTL)
        return captcha.svg(text)

    def _wait(self, now):
        """Seconds until another request is allowed (0 when allowed)."""
        wait = MAIL_INTERVAL - (now - self.sent[-1]) if self.sent else 0
        for window, limit in MAIL_LIMITS:
            recent = [t for t in self.sent if now - t < window]
            if len(recent) >= limit:
                wait = max(wait, window - (now - recent[0]))
        return max(0, int(wait + .999))

    def request(self, pair, answer, ip, agent):
        key = digest(pair)
        with self.lock:
            now = self.clock()
            self._prune(now)
            expected = self.captchas.pop(key, None)  # One attempt per image.
            if expected is None:
                raise WebError(400, "验证码已过期，请点击图片刷新")
            if not isinstance(answer, str) or not hmac.compare_digest(answer.strip().upper().encode(), expected[0].encode()):
                raise WebError(400, "验证码错误，已换一张")
            wait = self._wait(now)
            if wait:
                raise WebError(429, f"请求过于频繁，请 {wait} 秒后再试" if wait < 120 else f"请求过于频繁，请约 {wait // 60} 分钟后再试")
            for old in [k for k, p in self.pending.items() if p["pair"] == key]:
                del self.pending[old]
            if len(self.pending) >= MAX_PENDING:
                del self.pending[min(self.pending, key=lambda k: self.pending[k]["created"])]
            config = self.load_mail()
            request = {"id": secrets.token_hex(8), "pair": key, "code": f"{secrets.randbelow(10 ** 6):06d}",
                       "created": now, "expires": now + CODE_TTL, "ip": ip, "name": device_name(agent),
                       "attempts": 0, "approved": False, "mail": "sending" if mailer.configured(config) else "unconfigured"}
            self.pending[request["id"]] = request
            self.sent.append(now)
            result, event = self._status(request), self.public(request)
        self.events.put(("pairing", event))
        if result["pending"]["mail"] == "sending":
            threading.Thread(target=self._mail, args=(config, request), name="pairing-mail", daemon=True).start()
        return result

    def _mail(self, config, request):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(request["created"]))
        host = socket.gethostname()
        body = f"""<div style="max-width:560px;margin:0 auto;font-family:'PingFang SC','Microsoft YaHei',sans-serif;color:#333;">
  <p style="margin:0 0 10px;">有设备正在申请登录 <b>{html.escape(host)}</b> 的硬件监控网页管理端。</p>
  <p style="margin:0 0 6px;">配对码（10 分钟内有效，只能使用一次）：</p>
  <p style="margin:0 0 16px;font-size:32px;letter-spacing:8px;font-weight:bold;color:#1e7e34;">{request["code"]}</p>
  <p style="margin:0 0 4px;color:#666;">来源：{html.escape(request["ip"])} · {html.escape(request["name"])}</p>
  <p style="margin:0 0 14px;color:#666;">时间：{when}</p>
  <p style="margin:0;color:#c0392b;">如果不是你本人操作，请忽略此邮件，不要把配对码告诉他人。</p>
</div>"""
        try:
            self.send_mail(config, f"[硬件监控] 网页管理端配对码 {request['code']}", body)
            result = "sent"
        except Exception as error:
            log.warning("Pairing mail failed: %s", type(error).__name__)
            result = "failed"
        with self.lock:
            request["mail"] = result
        if result == "failed":
            self.events.put(("mail_failed", self.public(request)))

    @staticmethod
    def public(request):
        return {k: request[k] for k in ("id", "code", "created", "expires", "ip", "name", "mail")}

    @staticmethod
    def _status(request):
        return {"pending": {"expires": request["expires"], "mail": request["mail"]}}

    def status(self, pair):
        """Requester side. Returns (status, token); token is set once an approved request is collected."""
        key = digest(pair)
        with self.lock:
            self._prune(self.clock())
            request = next((p for p in self.pending.values() if p["pair"] == key), None)
            if request is None:
                return {"pending": None}, None
            if request["approved"]:
                token = self._issue(request["name"], request["ip"])
                del self.pending[request["id"]]
                return {"pending": None}, token
            return self._status(request), None

    def verify(self, pair, code):
        key = digest(pair)
        with self.lock:
            self._prune(self.clock())
            request = next((p for p in self.pending.values() if p["pair"] == key), None)
            if request is None:
                raise WebError(400, "配对码已失效，请重新获取")
            if not isinstance(code, str) or not hmac.compare_digest(code.strip().encode(), request["code"].encode()):
                request["attempts"] += 1
                if request["attempts"] >= MAX_ATTEMPTS:
                    del self.pending[request["id"]]
                    raise WebError(400, "配对码错误次数过多，请重新获取")
                raise WebError(400, f"配对码错误，还可尝试 {MAX_ATTEMPTS - request['attempts']} 次")
            token = self._issue(request["name"], request["ip"])
            del self.pending[request["id"]]
            return token

    def active(self):
        """Pending requests waiting for a code, for local display and signed-in devices."""
        with self.lock:
            self._prune(self.clock())
            return [self.public(p) for p in sorted(self.pending.values(), key=lambda p: p["created"]) if not p["approved"]]

    def decide(self, ident, approve):
        with self.lock:
            self._prune(self.clock())
            request = self.pending.get(ident)
            if request is None or request["approved"]:
                raise WebError(404, "配对请求已过期或已处理")
            if approve:
                request["approved"] = True
            else:
                del self.pending[ident]
