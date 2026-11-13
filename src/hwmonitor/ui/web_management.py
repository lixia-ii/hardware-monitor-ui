"""Management RPCs run on the desktop thread; network workers never touch Tk."""
import sys
import tkinter as tk

from hwmonitor.system import management_server
from hwmonitor.system.webbridge import WebError
from hwmonitor.core.actions import ManagementActions
from hwmonitor.ui.settings_dialog import INPUTS


class WebManagementMixin(ManagementActions):
    def _apply_management(self):
        cfg = self.settings["management"]
        web = self.settings["web"]
        if cfg["enabled"] and web["enabled"] and cfg["port"] == web["port"]:
            self.management.stop()
            error = "网页管理端口必须与传感器数据接口端口不同"
        else:
            error = self.management.apply(cfg["enabled"], cfg["port"])
        if error:
            self._flash(error)
        port = cfg["port"] if self.management.server else None
        if port != self._management_firewall and "--self-test" not in sys.argv:
            management_server.firewall_later(port)
        self._management_firewall = port
        return error

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
            # Password fields only save on 保存并连接; rebuilding the window would drop the typed text.
            pending = [window]
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if widget.winfo_class() in ("TEntry", "Entry") and widget.cget("show") and widget.get():
                    raise WebError(409, "电脑端有尚未保存的设备密码，请在电脑上点击「保存并连接」或清空后再试")

    def _commit_candidate(self, candidate):
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
        if previous["web"] != candidate["web"]:
            self._apply_web()
        self._apply_display()
        if self.data:
            self.render(self.data)
        # Rebuild the settings window so its callbacks cannot write stale
        # settings; pending input was committed by _web_flush_editors.
        window = getattr(self, "settings_win", None)
        if window and window.winfo_exists():
            page = getattr(self, "_settings_page", "general")
            geometry = window.geometry()
            window.destroy()
            self.settings_win = None
            self.show_settings(page)
            # Keep the user's size and position instead of re-centering the rebuilt window.
            self.settings_win.geometry(geometry)
