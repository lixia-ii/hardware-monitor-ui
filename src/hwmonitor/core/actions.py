"""Serialized management actions shared by desktop and headless runtimes."""
import copy
from concurrent.futures import Future
import logging
import threading
import time

from hwmonitor import __version__, settings
from hwmonitor.core import management
from hwmonitor.core.metadata import METRIC_NAMES, GROUP_NAMES
from hwmonitor.sensors import hwinfo
from hwmonitor.system.webbridge import WebError


class ManagementActions:
    def _runtime_info(self):
        return {"platform": "win32", "mode": "desktop", "capabilities": {"desktop": True}}

    def _validate_capabilities(self, payload):
        pass

    def _management_port(self):
        cfg = self.settings["management"]
        return cfg["port"] if cfg["enabled"] else None

    def _web_settings(self):
        return {"settings": management.public_settings(self.settings), "revision": management.revision(self.settings),
                "names": {**METRIC_NAMES, **{k: v for k, v in GROUP_NAMES.items() if k != "router"},
                          **{"router:" + d["id"]: d["name"] for d in self.settings["routers"]}, **self.sensor_names},
                "gpus": (self.data or {}).get("gpus", []), "nics": (self.data or {}).get("nics", []),
                "web_error": self.web.error, **self._runtime_info()}

    def _hardware_future(self, refresh=False):
        """One shared background hardware inventory; refresh starts a new one only after the last finished."""
        future = getattr(self, "_web_hardware", None)
        if future is None or (refresh and future.done()):
            future = self._web_hardware = Future()
            def load():
                try:
                    result = hwinfo.load()
                except Exception:
                    logging.getLogger(__name__).exception("Hardware info failed")
                    result = {"error": True}
                future.set_result({"error": "硬件信息读取失败，请查看电脑端日志"} if result.get("error") else result)
            threading.Thread(target=load, name="web-hardware-info", daemon=True).start()
        return future

    def _web_dispatch(self, action, payload):
        if action == "settings":
            return self._web_settings()
        if action == "state":
            # Every client polls once a second; copy each sample once and hash the settings only after a
            # save (both desktop and web saves reset the cache; the 2 s expiry covers unsaved in-memory edits).
            # The HTTP workers only serialize the shared copy. update() still checks a fresh revision.
            cached = getattr(self, "_state_data", None)
            if cached is None or cached[0] is not self.data:
                cached = self._state_data = (self.data, copy.deepcopy(self.data))
            now = time.monotonic()
            revision = getattr(self, "_state_revision", None)
            if revision is None or now - revision[0] > 2:
                revision = self._state_revision = (now, management.revision(self.settings))
            return {"version": __version__, "time": time.time(), "data": cached[1],
                    "revision": revision[1], "alerts_revision": str(self.runtime.alert_revision),
                    "storage_error": self.runtime.storage_error(self.collector),
                    **self._runtime_info()}
        if action == "alerts":
            # The revision of exactly this page lets the browser skip refetching it.
            return {**management.alerts_query(self.recent_alerts, payload or {}),
                    "revision": str(self.runtime.alert_revision)}
        if action == "update_settings":
            self._web_flush_editors()
            self._validate_capabilities(payload)
            candidate = management.update(self.settings, payload)
            web = candidate["web"]
            if web["enabled"] and web["port"] == self._management_port():
                raise WebError(400, "传感器数据接口端口必须与网页管理端口不同")
            # Persist before publishing the new state; a failed write changes nothing.
            try:
                settings.save(candidate)
            except (OSError, ValueError):
                raise WebError(500, "设置保存失败，请检查电脑数据目录的写入权限") from None
            self._commit_candidate(candidate)
            self._state_revision = None
            return self._web_settings()
        if action == "history":
            # Snapshot only; management.history_query reduces it on the HTTP worker.
            return {"rows": list(self.history), "time": time.time(), "gpu_id": (self.data or {}).get("gpu_id"),
                    "net_selection": (self.data or {}).get("net_selection")}
        if action == "hardware":
            future = self._hardware_future((payload or {}).get("refresh") == ["1"])
            return copy.deepcopy(future.result()) if future.done() else {"loading": True}
        if action == "diagnostics":
            # The HTTP worker waits on the shared load; the desktop thread never blocks on it.
            return {"data": copy.deepcopy(self.data), "alerts": list(self.recent_alerts), "hardware": self._hardware_future(),
                    "stats": {"last_read_ms": (self.data or {}).get("collection_ms"), "history_samples": len(self.history)}}
        raise WebError(404, "接口不存在")
