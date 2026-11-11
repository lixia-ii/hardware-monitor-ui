"""Management RPCs run on the desktop thread; network workers never touch Tk."""
import copy
import threading
import time
import sys
import tkinter as tk

from hwmonitor import __version__, settings
from hwmonitor.core import diagnostics, management
from hwmonitor.sensors import hwinfo
from hwmonitor.system import management_server
from hwmonitor.system.webbridge import WebError
from hwmonitor.ui.display import METRIC_NAMES, GROUP_NAMES
from hwmonitor.ui.settings_dialog import INPUTS


class WebManagementMixin:
    def _apply_management(self):
        cfg = self.settings["management"]
        if cfg["enabled"] and cfg["port"] == self.settings["web"]["port"]:
            self.management.stop()
            error = "手机管理端口必须与内网数据服务端口不同"
        else:
            error = self.management.apply(cfg["enabled"], cfg["port"])
        if error:
            self._flash(error)
        port = cfg["port"] if self.management.server else None
        if port != self._management_firewall and "--self-test" not in sys.argv:
            threading.Thread(target=management_server.firewall, args=(port,), daemon=True).start()
        self._management_firewall = port
        return error

    def _web_settings(self):
        return {"settings": management.public_settings(self.settings), "revision": management.revision(self.settings),
                "names": {**METRIC_NAMES, **{k: v for k, v in GROUP_NAMES.items() if k != "router"},
                          **{"router:" + d["id"]: d["name"] for d in self.settings["routers"]}, **self.sensor_names},
                "gpus": (self.data or {}).get("gpus", []), "nics": (self.data or {}).get("nics", [])}

    def _web_flush_editors(self):
        """Desktop edits win: commit a field still being typed in (it saves on
        <FocusOut>, which then turns this web save into a revision conflict) and
        refuse while the customization dialog holds unsaved preferences."""
        custom = getattr(self, "customization_win", None)
        if custom and custom.winfo_exists():
            raise WebError(409, "电脑端正在编辑「自定义显示」，请在电脑上保存或关闭该窗口后再试")
        window = getattr(self, "settings_win", None)
        if window and window.winfo_exists():
            try:
                focused = window.focus_get()
            except (KeyError, tk.TclError):  # 下拉框弹出列表等内部窗口
                focused = None
            if focused is not None and focused.winfo_toplevel() is window and focused.winfo_class() in INPUTS:
                focused.event_generate("<FocusOut>")

    def _web_dispatch(self, action, payload):
        if action == "settings":
            return self._web_settings()
        if action == "state":
            return {"version": __version__, "time": time.time(), "data": copy.deepcopy(self.data),
                    "revision": management.revision(self.settings), "alerts": list(self.recent_alerts),
                    "storage_error": self.collector.storage_error}
        if action == "update_settings":
            self._web_flush_editors()
            candidate = management.update(self.settings, payload)
            # Persist before publishing the new state; a failed write changes nothing.
            try:
                settings.save(candidate)
            except (OSError, ValueError):
                raise WebError(500, "设置保存失败，请检查电脑数据目录的写入权限") from None
            previous = self.settings
            # FloatingWindow keeps a reference to this dictionary.
            floating = previous["floating"]
            before = dict(floating)
            floating.update(candidate["floating"])
            candidate["floating"] = floating
            removed = {d["id"] for d in previous["routers"]} - {d["id"] for d in candidate["routers"]}
            if removed:  # Same cleanup as the desktop device editor.
                prefixes = tuple("sensor:device:" + ident + ":" for ident in removed)
                for key in [key for key in self.sensor_names if key.startswith(prefixes)]:
                    del self.sensor_names[key]
            self.settings = candidate
            self.collector.configure(candidate)
            self.gpu_sel = candidate["gpu_id"]
            if previous["topmost"] != candidate["topmost"]:
                self.set_topmost(candidate["topmost"])
            if floating["enabled"]:
                if self.float_win is not None and self.float_win.winfo_exists():
                    self.float_win.apply_options(before)  # 不调用 present(): 贴边隐藏的悬浮窗保持隐藏
                else:
                    self.show_floating()
            elif self.float_win is not None:
                self.hide_floating()
            self._apply_display()
            if self.data:
                self.render(self.data)
            # Rebuild the settings window so its callbacks cannot write stale
            # settings; pending input was committed by _web_flush_editors.
            window = getattr(self, "settings_win", None)
            if window and window.winfo_exists():
                page = getattr(self, "_settings_page", "general")
                window.destroy()
                self.settings_win = None
                self.show_settings(page)
            return self._web_settings()
        if action == "history":
            # Snapshot only; management.history_query reduces it on the HTTP worker.
            return {"rows": list(self.history), "time": time.time(), "gpu_id": (self.data or {}).get("gpu_id"),
                    "nic_ids": copy.deepcopy(self.settings["nic_ids"])}
        if action in ("hardware", "diagnostics"):
            current = getattr(self, "_web_hardware", None)
            refresh = action == "hardware" and (payload or {}).get("refresh") == ["1"]
            if current is None or (refresh and not current.get("loading")):
                self._web_hardware = {"loading": True}
                def load():
                    result = hwinfo.load()
                    self._web_hardware = {"error": "硬件信息读取失败，请查看电脑端日志"} if result.get("error") else result
                threading.Thread(target=load, name="web-hardware-info", daemon=True).start()
            if action == "hardware":
                return copy.deepcopy(self._web_hardware)
            return diagnostics.report(copy.deepcopy(self.data), self._web_hardware, list(self.recent_alerts),
                                      {"last_read_ms": (self.data or {}).get("collection_ms"), "history_samples": len(self.history)})
        raise WebError(404, "接口不存在")
