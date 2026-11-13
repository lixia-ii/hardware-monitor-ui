"""Long-lived hardware sampling thread and bounded router worker pool; no Tk calls.

设备 (路由器/远程电脑) 在线程池里各自读取, 采集循环只提交和取最近一次结果, 本机采样从不等网络。
"""
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

    def __init__(self, settings, source=None, web=None, backend=None):
        super().__init__(name="sensor-collector", daemon=True)
        self.source = source
        self.backend = backend
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
        self.device_reads = {}  # 设备 ID -> 进行中的读取 (Future), 每台设备同时只读一次
        self.device_lock = threading.Lock()
        self.device_latest = {}  # 设备 ID -> (读取对象, 最近一次结果); 由线程池回调写入

    def configure(self, settings):
        with self.lock:
            self.options = copy.deepcopy(settings)
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def reload_hardware(self):
        """重新打开硬件 (装好 PawnIO 驱动后), 下一次采样时生效。"""
        if self.backend is not None:
            self.backend.request_reopen()
        elif self.source is not None:
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

    def _sync_devices(self, options):
        """按配置增删、替换各设备的读取对象, 返回启用的设备配置。"""
        active = [cfg for cfg in options["routers"] if cfg["enabled"]]
        ids = {cfg["id"] for cfg in active}
        self.routers = {key: value for key, value in self.routers.items() if key in ids}
        self.device_types = {key: value for key, value in self.device_types.items() if key in ids}
        self.presence = {key: value for key, value in self.presence.items() if key in ids}
        self.device_reads = {key: value for key, value in self.device_reads.items() if key in ids}
        with self.device_lock:
            self.device_latest = {key: value for key, value in self.device_latest.items() if key in ids}
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
        return active

    def _read_one(self, cfg, device, presence):
        result = self._read_router(cfg, device)
        if presence is not None:
            for event in presence.update(cfg, result):
                self.device_events.put(event)
        return dict(result, id=cfg["id"], name=cfg["name"], type=cfg.get("type", "openwrt"))

    def _read_routers(self, options):
        """同步读取全部启用的设备并等待结果 (单次读取/测试用); 采集循环用不阻塞的 _poll_routers。"""
        active = self._sync_devices(options)
        def read_one(cfg):
            return self._read_one(cfg, self.routers[cfg["id"]], self.presence.get(cfg["id"]))
        return list(self.router_pool.map(read_one, active)) if self.router_pool else [read_one(cfg) for cfg in active]

    def _poll_routers(self, options):
        """不阻塞: 给空闲的设备提交一次后台读取, 返回各设备最近一次的结果。

        还没读到过的设备暂不列出 (卡片在有数据后出现); 上一次还没读完的设备不重复提交, 慢设备不拖累其他设备。
        """
        active = self._sync_devices(options)
        for cfg in active:
            ident = cfg["id"]
            pending = self.device_reads.get(ident)
            if pending is not None and not pending.done():
                continue
            device = self.routers[ident]
            future = self.router_pool.submit(self._read_one, cfg, device, self.presence.get(ident))
            self.device_reads[ident] = future
            future.add_done_callback(lambda f, ident=ident, device=device: self._store_device(ident, device, f))
        with self.device_lock:
            latest = dict(self.device_latest)
        # 只采用当前读取对象的结果: 改类型后旧对象晚到的结果不再显示; 名称以当前配置为准
        return [dict(latest[cfg["id"]][1], name=cfg["name"]) for cfg in active
                if cfg["id"] in latest and latest[cfg["id"]][0] is self.routers[cfg["id"]]]

    def _store_device(self, ident, device, future):
        if future.cancelled():
            return
        try:
            result = future.result()
        except Exception:  # _read_router 已兜底, 这里只防意外
            log.exception("Device sampling failed")
            return
        with self.device_lock:
            self.device_latest[ident] = (device, result)

    def run(self):
        store = None
        last_sample = -float("inf")
        failures = 0
        try:
            self.router_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="router")
            try:
                paths.DATA.mkdir(parents=True, exist_ok=True)
                store = HistoryStore(paths.DATA / "history.sqlite3")
                self.history_rows.put(("load", store.load()))
            except Exception:
                log.exception("History unavailable; continuing with memory history")
                self.storage_error = "历史保存不可用，当前仅在内存中保留；请检查目录权限或数据库。"
            pending = self.backend if self.backend is not None else self.source
            if pending is not None and not pending.ready:
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
                    if self.backend is not None:
                        snapshot = self.backend.sample(options["gpu_id"], options["nic_ids"])
                        data, tree = snapshot.data, snapshot.tree
                    else:
                        data = reading.read(options["gpu_id"], options["nic_ids"], self.source)
                        tree = self.source.last_tree if self.source is not None else None
                    failures = 0
                    data["selection"] = (options["gpu_id"], options["nic_ids"])
                    if self.web is not None:
                        self.web.publish(tree)
                    data["routers"] = self._poll_routers(options)
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
                    if self.web is not None:
                        self.web.publish(None)
                    log.exception("Sampling failed")
                    data = reading.empty_reading()
                    data.update(error="采集失败，请查看本地日志", sample_time=time.time(),
                                selection=(options["gpu_id"], options["nic_ids"]))
                    data["routers"] = self._poll_routers(options)
                    data["router"] = data["routers"][0] if data["routers"] else None
                    self._put(data)
                delay = options["interval"]
                if failures:
                    delay = max(delay, min(30, 2 ** min(failures, 5)))
                self.wake.wait(max(0, delay - (time.monotonic() - started)))
        finally:
            if self.router_pool:
                # 不等进行中的网络请求 (最长几秒), 先去关闭硬件; 请求结束后线程自行退出
                self.router_pool.shutdown(wait=False, cancel_futures=True)
            if store:
                store.close()
            if self.backend is not None:
                self.backend.close()
            elif self.source is not None:
                self.source.close()
