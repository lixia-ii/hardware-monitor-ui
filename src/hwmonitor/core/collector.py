"""Long-lived hardware sampling thread and bounded router worker pool; no Tk calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import logging
import queue
import threading
import time

from hwmonitor import paths
from hwmonitor.core.history import HistoryStore, sample
from hwmonitor.core.device_presence import DevicePresence
from hwmonitor.sensors import openwrt, reading, remote, tplink

log = logging.getLogger(__name__)


class Collector(threading.Thread):
    """source: lhm.Sensors, 只在本线程里打开/刷新/关闭; web: webserver.WebServer, 每次采样后发布。"""

    def __init__(self, settings, source=None, web=None):
        super().__init__(name="sensor-collector", daemon=True)
        self.source = source
        self.web = web
        self.lock = threading.Lock()
        self.options = copy.deepcopy(settings)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.results = queue.Queue(maxsize=1)
        self.history_rows = queue.SimpleQueue()
        self.storage_error = None
        self.router = openwrt.Router()
        self.routers = {"primary": self.router}
        self.device_types = {"primary": "openwrt"}
        self.router_pool = None
        self.device_events = queue.SimpleQueue()
        self.presence = {}

    def configure(self, settings):
        with self.lock:
            self.options = copy.deepcopy(settings)
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def reload_hardware(self):
        """重新打开硬件 (装好 PawnIO 驱动后), 下一次采样时生效。"""
        if self.source is not None:
            self.source.request_reopen()
        self.wake.set()

    def _put(self, data):
        try:
            self.results.get_nowait()
        except queue.Empty:
            pass
        self.results.put_nowait(data)

    def _read_router(self, options, router=None):
        """路由器独立于传感器: 离线或解析出错只影响路由器卡片, 也不参与退避计数。"""
        if not options["enabled"]:
            return None
        try:
            if options.get("type") == "tplink":
                return router.read(options["url"], password_protected=options.get("password_protected", ""))
            return (router or self.router).read(options["url"], options["nic"])
        except Exception:
            log.exception("Device sampling failed")
            return (tplink.empty_reading() if options.get("type") == "tplink" else
                    remote.empty_reading() if options.get("type") == "hwmonitor" else openwrt.empty_reading())

    def _read_routers(self, options):
        active = [cfg for cfg in options["routers"] if cfg["enabled"]]
        ids = {cfg["id"] for cfg in active}
        self.routers = {key: value for key, value in self.routers.items() if key in ids}
        self.device_types = {key: value for key, value in self.device_types.items() if key in ids}
        self.presence = {key: value for key, value in self.presence.items() if key in ids}
        for cfg in active:
            kind = cfg.get("type", "openwrt")
            if cfg["id"] not in self.routers or self.device_types.get(cfg["id"]) != kind:
                self.routers[cfg["id"]] = (tplink.Router() if kind == "tplink" else remote.Device() if kind == "hwmonitor" else
                                            self.router if cfg["id"] == "primary" else openwrt.Router())
            self.device_types[cfg["id"]] = kind
            if kind == "tplink":
                self.presence.setdefault(cfg["id"], DevicePresence())
            else:
                self.presence.pop(cfg["id"], None)

        def read_one(cfg):
            result = self._read_router(cfg, self.routers[cfg["id"]])
            if cfg["id"] in self.presence:
                for event in self.presence[cfg["id"]].update(cfg, result):
                    self.device_events.put(event)
            return dict(result, id=cfg["id"], name=cfg["name"], type=cfg.get("type", "openwrt"))

        return list(self.router_pool.map(read_one, active)) if self.router_pool else [read_one(cfg) for cfg in active]

    def run(self):
        store = None
        last_sample = -float("inf")
        failures = 0
        first = True
        try:
            self.router_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="router")
            try:
                paths.DATA.mkdir(parents=True, exist_ok=True)
                store = HistoryStore(paths.DATA / "history.sqlite3")
                self.history_rows.put(("load", store.load()))
            except Exception:
                log.exception("History unavailable; continuing with memory history")
                self.storage_error = "历史保存不可用，当前仅在内存中保留；请检查目录权限或数据库。"
            if self.source is not None and not self.source.ready:
                # 首次打开硬件要几秒, 先让界面显示提示
                self._put(dict(reading.empty_reading(), error="正在初始化硬件传感器…", initializing=True,
                               sample_time=time.time(),
                               selection=(self.options["gpu_id"], self.options["nic_ids"]), router=None))
            while not self.stopping.is_set():
                self.wake.clear()
                with self.lock:
                    options = copy.deepcopy(self.options)
                started = time.monotonic()
                try:
                    data = reading.read(options["gpu_id"], options["nic_ids"], self.source)
                    failures = 0
                    data["selection"] = (options["gpu_id"], options["nic_ids"])
                    if self.web is not None:
                        self.web.publish(self.source.last_tree if self.source is not None else None)
                    if first:
                        # 首份本机数据先送出, 启动界面不必等路由器/远程设备 (外网地址可能要等到超时)
                        first = False
                        self._put(dict(data, routers=[], router=None, sample_time=time.time()))
                    data["routers"] = self._read_routers(options)
                    data["router"] = data["routers"][0] if data["routers"] else None
                    data["collection_ms"] = round((time.monotonic() - started) * 1000, 1)
                    data["sample_time"] = time.time()
                    if started - last_sample >= 5:
                        last_sample = started
                        row = (data["sample_time"], sample(data))
                        self.history_rows.put(("append", row))
                        if store and options["history_enabled"]:
                            try:
                                store.append(*row)
                            except Exception:
                                log.exception("History write failed")
                                self.storage_error = "历史保存失败，当前数据仍保留在内存中。"
                                store.close()
                                store = None
                    self._put(data)
                except Exception:
                    failures += 1
                    log.exception("Sampling failed")
                    data = reading.empty_reading()
                    data.update(error="采集失败，请查看本地日志", sample_time=time.time(),
                                selection=(options["gpu_id"], options["nic_ids"]))
                    data["routers"] = self._read_routers(options)
                    data["router"] = data["routers"][0] if data["routers"] else None
                    self._put(data)
                delay = options["interval"]
                if failures:
                    delay = max(delay, min(30, 2 ** min(failures, 5)))
                self.wake.wait(max(0, delay - (time.monotonic() - started)))
        finally:
            if self.router_pool:
                self.router_pool.shutdown(wait=True)
            if store:
                store.close()
            if self.source is not None:
                self.source.close()
