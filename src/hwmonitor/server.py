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
from hwmonitor.system.management_server import ManagementServer
from hwmonitor.system.webbridge import Bridge, WebError
from hwmonitor.system.webserver import WebServer

log = logging.getLogger(__name__)


class Controller(ManagementActions):
    def __init__(self, options, backend, host="127.0.0.1", port=7076, data_port=None):
        self.settings = options
        self.state = RuntimeState()
        self.history, self.recent_alerts = self.state.history, self.state.recent_alerts
        self.data, self.sensor_names = None, {}
        self.web = WebServer(host=host)
        self.management = ManagementServer(host=host)
        self.web_bridge = self.management.bridge = Bridge()
        self.collector = Collector(options, web=self.web, backend=backend)
        self.port, self.data_port = port, data_port
        self.stopping = threading.Event()

    def _runtime_info(self):
        return {"platform": "linux", "mode": "headless", "capabilities": {"desktop": False}}

    def _validate_capabilities(self, payload):
        patch = payload.get("changes")
        if isinstance(patch, dict) and set(patch) & {"floating", "topmost", "close_action"}:
            raise WebError(400, "服务器模式不支持桌面、托盘和悬浮窗设置")

    def _web_flush_editors(self):
        pass

    def _commit_candidate(self, candidate):
        self.settings = candidate
        self.collector.configure(candidate)

    def start(self):
        if self.port == self.data_port:
            raise ValueError("管理服务与数据服务端口必须不同")
        try:
            error = self.management.apply(True, self.port)
            if error:
                raise OSError(error)
            if self.data_port is not None:
                error = self.web.apply(True, False, self.data_port)
                if error:
                    raise OSError(error)
            self.collector.start()
            log.info("硬件监控启动成功 [HWMONITOR_READY]；管理地址：http://%s:%s/", self.management.host, self.port)
            if self.management.host == "0.0.0.0":
                log.info("浏览器请使用 http://服务器IP:%s/ 访问", self.port)
        except Exception:
            self.stop()
            raise

    def step(self):
        self.web_bridge.drain(self._web_dispatch)
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


def main(args):
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
            with tempfile.TemporaryFile(dir=paths.DATA) as probe:
                probe.write(b"check")
            from hwmonitor.sensors.linux import LinuxBackend
            controller = Controller(options, LinuxBackend(enable_nvidia=args.nvidia), args.host, args.port, args.data_port)
            def stop(signum, frame):
                controller.stopping.set()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            controller.run()
        return 0
    except (OSError, ValueError, ImportError) as error:
        print(f"服务器启动失败：{error}", file=sys.stderr)
        return 1
