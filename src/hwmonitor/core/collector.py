"""One long-lived sampling thread; no Tk calls and no per-tick processes/threads."""
import copy
import logging
import queue
import threading
import time

from hwmonitor import paths
from hwmonitor.core.history import HistoryStore, sample
from hwmonitor.sensors import openwrt, reading

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

    def _read_router(self, options):
        """路由器独立于传感器: 离线或解析出错只影响路由器卡片, 也不参与退避计数。"""
        if not options["enabled"]:
            return None
        try:
            return self.router.read(options["url"], options["nic"])
        except Exception:
            log.exception("Router sampling failed")
            return openwrt.empty_reading()

    def run(self):
        store = None
        last_sample = -float("inf")
        failures = 0
        try:
            try:
                paths.DATA.mkdir(parents=True, exist_ok=True)
                store = HistoryStore(paths.DATA / "history.sqlite3")
                self.history_rows.put(("load", store.load()))
            except Exception:
                log.exception("History unavailable; continuing with memory history")
                self.storage_error = "历史保存不可用，当前仅在内存中保留；请检查目录权限或数据库。"
            if self.source is not None and not self.source.ready:
                # 首次打开硬件要几秒, 先让界面显示提示
                self._put(dict(reading.empty_reading(), error="正在初始化硬件传感器…", sample_time=time.time(),
                               selection=(self.options["gpu_id"], self.options["nic_ids"]), router=None))
            while not self.stopping.is_set():
                self.wake.clear()
                with self.lock:
                    options = copy.deepcopy(self.options)
                started = time.monotonic()
                try:
                    data = reading.read(options["gpu_id"], options["nic_ids"], self.source)
                    failures = 0
                    if self.web is not None:
                        self.web.publish(self.source.last_tree if self.source is not None else None)
                    data["router"] = self._read_router(options["router"])
                    data["collection_ms"] = round((time.monotonic() - started) * 1000, 1)
                    data["sample_time"] = time.time()
                    data["selection"] = (options["gpu_id"], options["nic_ids"])
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
                                selection=(options["gpu_id"], options["nic_ids"]),
                                router=self._read_router(options["router"]))
                    self._put(data)
                delay = options["interval"]
                if failures:
                    delay = max(delay, min(30, 2 ** min(failures, 5)))
                self.wake.wait(max(0, delay - (time.monotonic() - started)))
        finally:
            if store:
                store.close()
            if self.source is not None:
                self.source.close()
