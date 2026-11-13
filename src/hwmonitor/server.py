"""Headless runtime with a single serialized control loop and bounded shutdown."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import tempfile

from hwmonitor import paths, settings
from hwmonitor.core.actions import ManagementActions
from hwmonitor.core.collector import Collector
from hwmonitor.core.runtime import RuntimeState
from hwmonitor.system import auth
from hwmonitor.system.management_server import ManagementServer
from hwmonitor.system.webbridge import Bridge, WebError
from hwmonitor.system.webserver import WebServer

log = logging.getLogger(__name__)


class Controller(ManagementActions):
    def __init__(self, options, backend, host="127.0.0.1", port=7076):
        self.settings = options
        self.runtime = self.state = RuntimeState()
        self.history, self.recent_alerts = self.state.history, self.state.recent_alerts
        self.data, self.sensor_names = None, {}
        self.web = WebServer()
        self.management = ManagementServer(host=host)
        self.web_bridge = self.management.bridge = Bridge()
        self.collector = Collector(options, web=self.web, backend=backend)
        self.port = port
        self.stopping = threading.Event()

    def _runtime_info(self):
        return {"platform": "linux", "mode": "headless", "capabilities": {"desktop": False}}

    def _validate_capabilities(self, payload):
        patch = payload.get("changes")
        if isinstance(patch, dict) and set(patch) & {"floating", "topmost", "close_action"}:
            raise WebError(400, "服务器模式不支持桌面、托盘和悬浮窗设置")

    def _management_port(self):
        server = self.management.server
        return server.server_address[1] if server else self.port

    def _apply_web(self):
        """Same switch as the desktop; a busy port is reported, never fatal to the service."""
        web = self.settings["web"]
        if web["enabled"] and web["port"] == self._management_port():
            self.web.stop()
            # Forget the old listener config so switching back to it starts again.
            self.web.config, self.web.error = (False, False, 0), "传感器数据接口端口必须与网页管理端口不同"
        else:
            # Without LAN access stay on loopback; with it, follow the management listener's address.
            self.web.host = self.management.host if web["lan"] else "127.0.0.1"
            self.web.apply(web["enabled"], web["lan"], web["port"])
        if self.web.error:
            log.warning("%s", self.web.error)
        elif web["enabled"]:
            log.info("传感器数据接口：http://%s:%s/data.json", self.web.host, web["port"])

    def _web_flush_editors(self):
        pass

    def _commit_candidate(self, candidate):
        previous, self.settings = self.settings, candidate
        self.collector.configure(candidate)
        if previous["web"] != candidate["web"]:
            self._apply_web()

    def start(self):
        try:
            self.state.load_alerts(paths.DATA / "alerts.sqlite3")
            error = self.management.apply(True, self.port)
            if error:
                raise OSError(error)
            self._apply_web()
            self.collector.start()
            log.info("硬件监控启动成功 [HWMONITOR_READY]；管理地址：http://%s:%s/", self.management.host, self.port)
            if self.management.host == "0.0.0.0":
                log.info("浏览器请使用 http://服务器IP:%s/ 访问", self.port)
        except Exception:
            self.stop()
            raise

    def step(self):
        self.web_bridge.drain(self._web_dispatch)
        for kind, info in self.management.auth.drain():
            # Headless: the log is the local place to read a pairing code when mail is unavailable.
            log.warning("%s", auth.describe(kind, info))
        data = self.state.latest(self.collector, self.settings)
        if data:
            self.data = data
            for group, section in (("case_fans", "case"), ("pumps", "pump"), ("extra_temps", None)):
                for index, item in enumerate(data.get(group, [])):
                    identities = data.get("sensor_ids", {}).get(group, [])
                    if index < len(identities):
                        category = section or ("network_temp" if item[0] == "network" else item[0])
                        self.sensor_names[f"sensor:{category}:{identities[index]}"] = item[-2]
            for message in self.state.alerts(data, self.settings["alerts"]):
                log.warning("%s", message)
        configs = {c["id"]: c for c in self.settings["routers"]}
        while not self.collector.device_events.empty():
            event = self.collector.device_events.get()
            cfg = configs.get(event["router_id"], {})
            if cfg.get("enabled") and cfg.get("notify_clients", True):
                self.state.record(cfg["name"] + " · " + event["name"] + (" 上线" if event["online"] else " 下线"))

    def run(self):
        self.start()
        try:
            while not self.stopping.is_set():
                self.step()
                self.stopping.wait(.05)
        finally:
            self.stop()

    def stop(self):
        self.stopping.set()
        self.web_bridge.close()
        self.collector.stop()
        self.management.stop()
        self.web.stop()
        self.state.close_alerts()
        if self.collector.ident is not None:
            self.collector.join(timeout=8)
            if self.collector.is_alive():
                log.error("Collector did not stop within 8 seconds")


class InstanceLock:
    def __enter__(self):
        import fcntl
        directory = Path(os.environ.get("RUNTIME_DIRECTORY", paths.DATA / "run"))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Also lock the data directory: a manual process must not share service SQLite state.
        self.streams = []
        try:
            for path in (paths.DATA / ".instance.lock", directory / "hwmonitor.lock"):
                stream = path.open("a+")
                self.streams.append(stream)
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            for stream in self.streams:
                stream.close()
            raise OSError("同一运行目录已有监控实例") from None
        return self

    def __exit__(self, *args):
        for stream in self.streams:
            stream.close()


def typed(text):
    """Apply Backspace/Delete a terminal passed through as characters (^H, ^?) and drop other control characters."""
    out = []
    for char in text:
        if char in "\x08\x7f":
            if out:
                out.pop()
        elif char.isprintable():
            out.append(char)
    return "".join(out).strip()


def mail_setup():
    """Interactive mail configuration; the running service reads it on the next pairing request."""
    from getpass import getpass
    from hwmonitor.system import credentials, mailer
    try:
        # Line editing for input(): without it a terminal sending ^H for Backspace echoes "^H",
        # and arrow keys / pasted text are not handled.
        import readline  # noqa: F401
    except ImportError:
        pass
    os.umask(0o077)
    paths.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = mailer.load()
    print("配置网页管理端配对码邮件，直接回车保留方括号中的当前值。")
    for key, title in (("smtp_host", "SMTP 服务器（如 smtp.qq.com）"), ("smtp_port", "端口（465 为 SSL）"),
                       ("smtp_user", "账号"), ("mail_from", "发件地址"), ("mail_from_name", "发件人名称"),
                       ("mail_to", "收件地址")):
        while True:
            value = typed(input(f"{title} [{config[key]}]: "))
            if not value:
                break
            if key == "smtp_port":
                if value.isdigit() and 1 <= int(value) <= 65535:
                    config[key] = int(value)
                    break
                print("端口应为 1–65535 的整数")
            elif key in ("mail_from", "mail_to", "smtp_user") and "@" not in value:
                print("请输入有效的邮箱地址")
            else:
                config[key] = value
                break
    secret = typed(getpass("授权码（输入时不显示，" + ("回车保留原值" if config["smtp_pass_protected"] else "邮箱设置中生成") + "）: "))
    if secret:
        config["smtp_pass_protected"] = credentials.protect(secret)
    mailer.save(config)
    print(f"已保存到 {mailer.path()}")
    if not mailer.configured(config):
        print("配置尚不完整，配对码只会显示在日志和已登录设备的网页中。")
        return 1
    if input("发送测试邮件？[Y/n]: ").strip().lower() in ("", "y", "yes"):
        try:
            mailer.send(config, "[硬件监控] 测试邮件", "<p>邮件设置正确，网页管理端的配对码会发送到这个邮箱。</p>")
        except Exception as error:
            print(f"发送失败（{type(error).__name__}）：请检查服务器、端口、账号和授权码。", file=sys.stderr)
            return 1
        print(f"测试邮件已发送到 {config['mail_to']}")
    return 0


def main(args):
    if args.mail_setup:
        try:
            return mail_setup()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，未保存修改。", file=sys.stderr)
            return 1
        except (OSError, ValueError) as error:
            print(f"邮件设置保存失败：{error}", file=sys.stderr)
            return 1
    if args.check:
        result = {"platform": sys.platform, "python": sys.version.split()[0], "mode": "headless",
                  "dependencies": {name: importlib.util.find_spec(name) is not None for name in ("psutil", "cryptography")}}
        if sys.platform == "linux" and all(result["dependencies"].values()):
            from hwmonitor.sensors.linux import LinuxBackend
            backend = LinuxBackend(enable_nvidia=args.nvidia)
            try:
                snapshot = backend.sample()
                result["capabilities"] = snapshot.data["capabilities"]
                result["network"] = snapshot.data.get("network_basis")
            finally:
                backend.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if sys.platform == "linux" and all(result["dependencies"].values()) else 1
    if sys.platform != "linux":
        print("后台服务模式目前仅支持 Linux", file=sys.stderr)
        return 1
    from hwmonitor.core import diagnostics
    try:
        os.umask(0o077)
        paths.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(paths.DATA, 0o700)
        with InstanceLock():
            diagnostics.setup_logging()
            logging.getLogger().addHandler(logging.StreamHandler())
            # Fail startup before opening listeners if configuration cannot be written.
            config = paths.DATA / "settings.json"
            # Same fallback as the desktop: a corrupt file is logged and replaced by defaults, not a restart loop.
            # Keep the damaged copy, the next save would otherwise overwrite it.
            try:
                json.loads(config.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except ValueError:
                config.replace(config.with_name("settings.json.corrupt"))
            options = settings.load(config)
            if args.data_port is not None:
                # Legacy command line: move it into the setting the management page controls.
                options["web"] = {**options["web"], "enabled": True, "lan": True, "port": args.data_port}
                settings.save(options)
                log.warning("--data-port 已写入设置，之后请在网页管理端「设置 → 常规」修改传感器数据接口")
            with tempfile.TemporaryFile(dir=paths.DATA) as probe:
                probe.write(b"check")
            from hwmonitor.sensors.linux import LinuxBackend
            controller = Controller(options, LinuxBackend(enable_nvidia=args.nvidia), args.host, args.port)
            def stop(signum, frame):
                controller.stopping.set()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            controller.run()
        return 0
    except (OSError, ValueError, ImportError) as error:
        print(f"服务器启动失败：{error}", file=sys.stderr)
        return 1
