"""Isolated GUI integration check. --preview keeps the fixture UI open for visual QA."""
import json
import os
import queue
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.environ["HARDWARE_MONITOR_DATA_DIR"] = str(ROOT / ".build" / "test-data")
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests.support import sample_tree
from hwmonitor import paths, settings
from hwmonitor.core import diagnostics
from hwmonitor.sensors import openwrt, reading, remote
from hwmonitor.ui import floating as floating_mod, main_window
from tkinter import ttk

diagnostics.setup_logging()
failures = []
tree = sample_tree()
tree["Children"].append({"HardwareId": "/lpc/0", "Text": "Board", "Children": [
    {"Text": "CPU Fan", "Value": "1200 RPM", "Type": "Fan"},
    {"Text": "Case Fan", "Value": "800 RPM", "Type": "Fan"}]})


def read(gpu_id=None, nic_ids=None, source=None):
    with patch("hwmonitor.sensors.reading._lhm") as lhm, patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
        out = {"temps": [], "fans": [], "loads": [], "data": [], "extra": {}, "hw": [], "by_id": True}
        reading._walk(tree, None, out)
        lhm.return_value = out
        return original_read(gpu_id, nic_ids)


def record_error(*args):
    failures.append(str(args))


original_read = reading.read
with socket.socket() as probe:  # 网页服务用空闲端口, 不与本机已有服务冲突
    probe.bind(("127.0.0.1", 0))
    WEB_PORT = probe.getsockname()[1]
with patch("hwmonitor.sensors.remote.Device.read", side_effect=lambda *args, **kwargs: remote.empty_reading()), patch("hwmonitor.core.collector.reading.read", side_effect=read), patch("hwmonitor.system.autostart.sync"), patch("hwmonitor.system.autostart.is_enabled", return_value=False), patch("hwmonitor.system.pawnio.status", return_value="ok"), patch("hwmonitor.settings.load", side_effect=lambda: settings.validate({"web": {"port": WEB_PORT}})):
    app = main_window.MainWindow()
    app.root.title("硬件监控 · 开发验证")
    app.root.report_callback_exception = record_error
    now = time.time()
    app.history.extend((now - (17280 - i) * 5, {"cpu_temp": 96 if i % 499 == 0 else 50 + i % 20,
                        "gpu_temp": 60 + i % 15, "cpu_load": i % 100, "gpu_load": i % 80,
                        "mem_load": 50, "gpu_mem_load": 20, "net_up": (i % 10) * 1024 ** 2,
                        "net_down": (i % 20) * 1024 ** 2,
                        "gpu_id": "/gpu-nvidia/0" if i < 8640 else "nvidia:UUID-FIXTURE", "gpu_name": "NVIDIA one",
                        "net_selection": None}) for i in range(17280))

    def check_main():
        # 采集线程在后台照常出数, 下面每次 root.update() 都可能让 tick 渲染一份真实读数 (没有路由器、没有错误),
        # 盖掉刚 render 的测试数据; 检查期间界面不收新结果 (采集线程换掉旧结果时也调 get_nowait, 照常放行)
        take = app.collector.results.get_nowait
        def ui_frozen():
            if threading.current_thread() is threading.main_thread():
                raise queue.Empty
            return take()
        frozen = patch.object(app.collector.results, "get_nowait", side_effect=ui_frozen)
        frozen.start()
        try:
            app.root.update_idletasks()
            assert app.data and app.data["cpu_temp"] == 70
            assert not app.data["error"], app.data["error"]
            app.show_history()
            history_window = app.history_win
            history_window.iconify()
            app.root.update()
            app.show_history()
            assert app.history_win is history_window and history_window.state() == "normal", "History window did not restore"
            app.show_settings()
            app.root.update()
            settings_window = app.settings_win
            def find(widget, kind, test=lambda w: True):
                for child in widget.winfo_children():
                    if isinstance(child, kind) and test(child):
                        return child
                    found = find(child, kind, test)
                    if found is not None:
                        return found
            def row_titled(page, title):
                label = find(app.settings_pages[page], main_window.tk.Label, lambda w: w.cget("text") == title)
                assert label is not None, f"Missing settings row: {title}"
                return label.master.master
            # Sidebar navigation shows exactly one page; Ctrl+Tab cycles pages.
            for key in app.settings_pages:
                app.settings_select(key)
                app.root.update()
                assert [k for k, page in app.settings_pages.items() if page.winfo_ismapped()] == [key]
            app.settings_select("general")
            settings_window.focus_force()
            app.root.update()
            settings_window.event_generate("<Control-Tab>")
            assert app._settings_page == "monitor"
            # Wheel over a closed dropdown scrolls the settings page without changing or saving its value.
            app.settings_select("general")
            app.root.update()
            close_combo = find(row_titled("general", "关闭主窗口时").control, ttk.Combobox)
            close_index, close_action = close_combo.current(), app.settings["close_action"]
            canvas = close_combo.master
            while not isinstance(canvas, main_window.tk.Canvas):
                canvas = canvas.master
            canvas.yview_moveto(0)
            settings_window.focus_force()
            app.root.update()
            close_combo.event_generate("<MouseWheel>", delta=-120)
            app.root.update()
            assert close_combo.current() == close_index and app.settings["close_action"] == close_action
            assert canvas.yview()[0] > 0, "Wheel over dropdown did not scroll settings"
            canvas.yview_moveto(0)
            close_combo.focus_force()
            app.root.update()
            for delta in (-120, 120):
                close_combo.event_generate("<MouseWheel>", delta=delta)
            assert close_combo.current() == close_index and app.settings["close_action"] == close_action
            close_combo.current(1)
            close_combo.event_generate("<<ComboboxSelected>>")
            assert app.settings["close_action"] == "tray", "Explicit dropdown selection stopped working"
            close_combo.current(close_index)
            close_combo.event_generate("<<ComboboxSelected>>")
            canvas.yview_moveto(0)
            # Dependent rows follow their master switch and save immediately.
            fan_switch = row_titled("alerts", "高温时风扇低转速提醒").switch
            fan_min = find(row_titled("alerts", "最低转速").control, ttk.Spinbox)
            assert fan_min.instate(["disabled"]), "Fan options enabled while fan alert is off"
            fan_switch.invoke()
            assert app.settings["alerts"]["fan_enabled"] and fan_min.instate(["!disabled"])
            fan_switch.invoke()
            assert not app.settings["alerts"]["fan_enabled"] and fan_min.instate(["disabled"])
            app.settings_select("alerts")
            app.root.update()
            cooldown = find(row_titled("alerts", "提醒间隔").control, ttk.Spinbox)
            cooldown.delete(0, "end")
            cooldown.insert(0, "99999")
            cooldown.event_generate("<FocusOut>")
            assert app.settings["alerts"]["cooldown"] == 7200 and cooldown.get() == "7200", "Out-of-range value not clamped"
            settings_window.focus_force()
            cooldown.focus_force()  # 按键事件发往焦点控件
            app.root.update()
            cooldown.delete(0, "end")
            cooldown.insert(0, "600")
            cooldown.event_generate("<space>")
            assert app.settings["alerts"]["cooldown"] == 600 and cooldown.get() == "600", "Space did not commit"
            # Clicking blank space moves focus off the input, so its <FocusOut> saves the value.
            cooldown.focus_force()
            app.root.update()
            cooldown.delete(0, "end")
            cooldown.insert(0, "900")
            blank = row_titled("alerts", "提醒间隔")
            blank.event_generate("<Button-1>", x=2, y=2)
            app.root.update()
            assert settings_window.focus_get() is not cooldown, "Click on blank space kept input focus"
            cooldown.event_generate("<FocusOut>")  # focus events need a foreground window; deliver it explicitly
            assert app.settings["alerts"]["cooldown"] == 900
            app.settings_select("router")
            app.root.update()
            url_row = row_titled("router", "指标地址")
            url_entry = find(url_row.control, ttk.Entry)
            assert url_entry.instate(["disabled"]), "Router options enabled while router is off"
            row_titled("router", "显示设备状态").switch.invoke()
            assert app.settings["router"]["enabled"] and url_entry.instate(["!disabled"])
            url_entry.delete(0, "end")
            url_entry.insert(0, "ftp://router")
            url_entry.event_generate("<FocusOut>")
            assert url_entry.get() == settings.DEFAULTS["router"]["url"] and url_row.desc.cget("fg") == main_window.RED, "Invalid URL accepted"
            row_titled("router", "显示设备状态").switch.invoke()
            assert not app.settings["router"]["enabled"]
            row_titled("router", "加入设备").control.winfo_children()[0].invoke()
            assert len(app.settings["routers"]) == 2
            def router_rows(title):
                def walk(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from walk(child)
                return [w for w in walk(app.settings_pages["router"])
                        if hasattr(w, "title") and hasattr(w.title, "cget") and w.title.cget("text") == title]
            names = router_rows("设备名称")
            def router_headers():
                stack, heads = [app.settings_pages["router"]], []
                while stack:  # 广度优先, 按页面顺序返回各设备的标题行
                    widget = stack.pop(0)
                    if hasattr(widget, "router_id"):
                        heads.append(widget)
                    stack.extend(widget.winfo_children())
                return heads
            first_head, second_head = router_headers()
            app.root.update_idletasks()
            first_head.event_generate("<Button-1>")
            app.root.update_idletasks()
            assert not first_head.router_body.winfo_manager()
            assert second_head.router_body.winfo_manager() == "pack", "Collapsing one router hid another"
            collapse_key = "router-settings:" + first_head.router_id
            # 配置帮助同样可折叠, 折叠状态单独保存, 不受设备增删影响。
            help_head = next(w for w in router_headers()[0].master.master.master.winfo_children()
                             if getattr(getattr(w, "label", None), "cget", None) and w.label.cget("text") == "配置帮助")
            help_head.toggle()
            assert "settings:router-help" in app.settings["collapsed"]
            assert all(not w.winfo_ismapped() for w in router_rows("接入远程电脑"))
            help_head.toggle()
            assert "settings:router-help" not in app.settings["collapsed"]
            assert collapse_key in json.loads((paths.DATA / "settings.json").read_text(encoding="utf-8"))["collapsed"], "Router fold state was not saved"
            second_name = find(names[1].control, ttk.Entry)
            second_name.delete(0, "end")
            second_name.insert(0, "旁路由")
            second_name.event_generate("<FocusOut>")
            assert app.settings["routers"][1]["name"] == "旁路由"
            router_rows("显示设备状态")[1].switch.invoke()
            second_url = find(router_rows("指标地址")[1].control, ttk.Entry)
            second_url.delete(0, "end")
            second_url.insert(0, "http://192.168.2.1:9100/metrics")
            second_url.event_generate("<FocusOut>")
            assert app.settings["routers"][1]["url"] == second_url.get()
            assert app.settings["routers"][0]["url"] == settings.DEFAULT_ROUTER["url"]
            # One mouse click opens a real confirmation; cancel/close preserve the device.
            delete_button = names[1].control.winfo_children()[1]
            def click_delete(answer):
                replies = []
                def respond():
                    dialogs = [w for w in settings_window.winfo_children()
                               if isinstance(w, main_window.tk.Toplevel) and w.title() == "删除设备"]
                    assert len(dialogs) == 1, "Delete confirmation did not open"
                    dialog = dialogs[0]
                    label = find(dialog, ttk.Label, lambda w: "旁路由" in w.cget("text"))
                    assert label is not None, "Confirmation omitted device name"
                    assert app.root.grab_current() is dialog
                    replies.append(answer)
                    if answer == "close":
                        dialog.destroy()
                    else:
                        find(dialog, ttk.Button, lambda w: w.cget("text") == answer).invoke()
                delete_button.event_generate("<Enter>")
                delete_button.event_generate("<ButtonPress-1>", x=2, y=2)
                app.root.update()
                app.root.after(50, respond)
                delete_button.event_generate("<ButtonRelease-1>", x=2, y=2)
                assert replies == [answer], "Delete required a second click"
                assert app.root.grab_current() is settings_window, "Confirmation lost settings modal grab"
            second_name.focus_force()
            app.root.update()
            click_delete("取消")
            assert len(app.settings["routers"]) == 2
            click_delete("close")
            assert len(app.settings["routers"]) == 2
            # Confirming deletes only the selected device.
            click_delete("删除")
            assert len(app.settings["routers"]) == 1
            assert not app.settings["routers"][0]["enabled"]
            names = router_rows("设备名称")
            first_head = router_headers()[0]
            assert not first_head.router_body.winfo_manager(), "Rebuilding router settings lost fold state"
            first_head.focus_force()
            app.root.update()
            first_head.event_generate("<Return>")
            assert first_head.router_body.winfo_manager() == "pack"
            assert collapse_key not in json.loads((paths.DATA / "settings.json").read_text(encoding="utf-8"))["collapsed"]
            # The same device manager adds either OpenWrt or a remote instance of this app.
            row_titled("router", "加入设备").control.winfo_children()[0].invoke()
            type_rows = router_rows("设备类型")
            assert len(type_rows) == len(app.settings["routers"]), "Device type remained outside device panels"
            assert all(row.master is router_rows("设备名称")[i].master for i, row in enumerate(type_rows))
            device_type = find(type_rows[1].control, ttk.Combobox)
            assert list(device_type.cget("values")) == ["OpenWrt", "远程电脑"]
            device_id = app.settings["routers"][-1]["id"]
            device_type.current(1)
            device_type.event_generate("<<ComboboxSelected>>")
            assert app.settings["routers"][-1]["id"] == device_id
            assert app.settings["routers"][-1]["type"] == "hwmonitor"
            assert app.settings["routers"][-1]["url"].endswith(":8085/data.json")
            assert len(router_rows("WAN 网卡")) == 1, "Remote computer incorrectly offered WAN selection"
            router_rows("显示设备状态")[1].switch.invoke()
            remote_url = find(router_rows("指标地址")[1].control, ttk.Entry)
            remote_url.delete(0, "end")
            remote_url.insert(0, "http://192.168.1.8:8085")
            remote_url.event_generate("<FocusOut>")
            assert remote_url.get() == "http://192.168.1.8:8085/data.json"
            # Switching only this device preserves its identity, enabled state and edited address.
            device_type = find(router_rows("设备类型")[1].control, ttk.Combobox)
            device_type.current(0)
            device_type.event_generate("<<ComboboxSelected>>")
            assert app.settings["routers"][1]["id"] == device_id
            assert app.settings["routers"][1]["enabled"]
            assert app.settings["routers"][1]["url"] == "http://192.168.1.8:8085/data.json"
            assert len(router_rows("WAN 网卡")) == 2
            assert app.settings["routers"][0].get("type", "openwrt") == "openwrt"
            with patch.object(app, "_confirm_delete_device", return_value=True) as confirmation:
                router_rows("设备名称")[1].control.winfo_children()[1].invoke()
                confirmation.assert_called_once()
            assert len(app.settings["routers"]) == 1

            app.settings_select("general")
            app.root.update()
            lan_switch = row_titled("general", "允许局域网访问").switch
            assert lan_switch.instate(["disabled"]) and app.web.server is None, "Web service should start disabled"
            row_titled("general", "启用网页服务").switch.invoke()
            assert app.settings["web"]["enabled"] and lan_switch.instate(["!disabled"]) and app.web.server is not None
            assert f"127.0.0.1:{WEB_PORT}/data.json" in row_titled("general", "访问地址").desc.cget("text")
            row_titled("general", "启用网页服务").switch.invoke()
            assert not app.settings["web"]["enabled"] and app.web.server is None
            topmost_switch = row_titled("general", "主窗口置顶").switch
            topmost_switch.invoke()
            assert app.settings["topmost"] and app.root.attributes("-topmost"), "Topmost switch not applied"
            assert settings_window.attributes("-topmost"), "Settings window fell behind the topmost main window"
            with patch.object(app.tray, "menu", return_value="topmost") as tray_menu:
                app.tray.events.append("menu")
                assert app._tray_events()
            assert tray_menu.call_args.kwargs == {"floating": False, "topmost": True}
            assert not app.settings["topmost"] and not app.root.attributes("-topmost")
            assert not app._topmost_var.get(), "Settings switch did not follow the tray menu"
            assert not settings_window.attributes("-topmost")
            assert not any(b.cget("text") in ("置顶", "悬浮窗") for b in app.btn_bar.winfo_children()
                           if isinstance(b, ttk.Button)), "Toolbar still has topmost/floating buttons"
            app.settings_select("general")
            app.show_customization()
            app.root.update_idletasks()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            custom = app.customization_win
            widgets = list(descendants(custom))
            custom.geometry("%dx%d" % custom.minsize())
            app.root.update()
            for button in (w for w in widgets if isinstance(w, ttk.Button)):
                assert button.winfo_ismapped() and button.winfo_rooty() + button.winfo_height() <= custom.winfo_rooty() + custom.winfo_height(), f"Action clipped: {button.cget('text')} at {button.winfo_rooty() + button.winfo_height()}, window bottom {custom.winfo_rooty() + custom.winfo_height()}"
            app._size_dialog(custom, "460p", "420p")
            tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
            entry = next(w for w in widgets if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox))
            assert set(tree.get_children()) == {"cpu_temp", "cpu_load", "mem_load", "cpu_fan"}
            entry.delete(0, "end")
            entry.insert(0, "处理器温度测试")
            tree.selection_set("cpu_load")
            tree.event_generate("<<TreeviewSelect>>")
            tree.selection_set("cpu_temp")
            tree.event_generate("<<TreeviewSelect>>")
            assert entry.get() == "处理器温度测试", "Draft name lost when selection changed"
            # Space toggles visibility and greys the row; "恢复默认" restores the current group.
            tree.focus_set()
            tree.event_generate("<space>")
            assert "hidden" in tree.item("cpu_temp", "tags") and tree.set("cpu_temp", "shown") == "已隐藏"
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "恢复默认").invoke()
            assert tree.item("cpu_temp", "tags") == "" and tree.set("cpu_temp", "name") == "CPU 温度", "Reset kept draft edits"
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "取消").invoke()
            assert not app.settings["display"], "Cancel applied draft settings"
            assert app.root.grab_current() is app.settings_win, "Parent modal grab not restored"
            app.show_customization()
            app.root.update_idletasks()
            # Stable identity also keeps a hidden sensor hidden after reordering.
            key = "sensor:storage:/nvme/0"
            app.settings["display"][key] = {"visible": False}
            app.render(app.data)
            assert app.extra_boxes["storage"]._keys == []
            changed = dict(app.data, extra_temps=[("storage", "Other", 35), *app.data["extra_temps"]],
                           sensor_ids={**app.data["sensor_ids"], "extra_temps": ["/nvme/other", "/nvme/0"]})
            app.render(changed)
            assert app.extra_boxes["storage"]._keys == [("/nvme/other", "Other")]
            app.settings["display"] = {}
            app.render(app.data)
            # Customization applies without losing the metric's value or pack geometry.
            app.settings["display"]["cpu_load"] = {"visible": False, "name": "负载", "order": 0}
            app.settings["display"]["storage"] = {"visible": True, "name": "磁盘温度", "order": 0}
            app._apply_display()
            app.render(app.data)
            assert not app.metric_blocks["cpu_load"][0].winfo_manager()
            assert app.extra_boxes["storage"].title_label.cget("text") == "磁盘温度"
            app.settings["display"]["cpu_load"]["visible"] = True
            app._apply_display()
            assert app.metric_blocks["cpu_load"][0].winfo_manager() == "pack"
            # Hiding a device temperature keeps the card labelled without implying a temperature.
            app.settings["display"]["cpu_temp"] = {"visible": False}
            app._apply_display()
            assert not app.metric_blocks["cpu_temp"][0].winfo_manager()
            assert app.metric_titles["cpu_temp"].cget("text") == "CPU"
            app.settings["display"]["cpu_temp"] = {"visible": True}
            app._apply_display()
            assert app.metric_titles["cpu_temp"].cget("text") == "CPU 温度"
            # Hiding every sensor of a group hides the card instead of reporting "未检测到".
            app.render(app.data)
            hidden = {f"sensor:case:{key}": {"visible": False} for key, _ in app.case_box._keys}
            assert hidden, "Fixture has no case fan rows"
            app.settings["display"].update(hidden)
            app._apply_display()
            app.render(app.data)
            assert not app.case_box.master.winfo_manager(), "Empty group card stayed visible"
            app.settings["display"] = {}
            # Router card appears only with router data and keeps working while offline.
            router = dict(openwrt.empty_reading(), offline=False, nic="eth0", nics=["eth0"], temp=39, cpu_load=12,
                          mem_used=4e8, mem_total=1.8e9, mem_load=22, load1=.39, up=2048, down_rate=3e6, uptime=465253)
            app.render(dict(app.data, router=router, routers=[dict(router, id="primary", name="主路由")]))
            assert app.router_box.master.winfo_manager() == "pack"
            assert app.router_vals["uptime"].cget("text") == "5天9小时14分"
            # The window size belongs to the user: content changes never resize it.
            # Cards scroll under a fixed toolbar; the overlay scrollbar never changes card width.
            app.root.update()
            width, minimum = app.root.minsize()
            device_row = app.metric_blocks["cpu_temp"][0].master.master
            device_height = device_row.winfo_height()
            assert minimum == app.root.winfo_pixels("200p")
            tall = app.root.winfo_pixels("900p")
            if tall <= app.root.winfo_screenheight() - 100:  # CI 运行器屏幕很小, 窗口会被系统限高, 这一段只在屏幕够高时检查
                app.root.geometry(f"{width}x{tall}")
                app.root.update()
                assert app.root.winfo_height() == tall, "Window height is capped"
                assert not app.main_scrollbar.winfo_ismapped(), "Scrollbar shown although content fits"
            card_width = app.router_box.master.winfo_width()
            for height in (minimum, minimum + app.root.winfo_pixels("80p")):
                app.root.geometry(f"{width}x{height}")
                app.root.update()
                assert app.main_scrollbar.winfo_ismapped(), "Expanded router should overflow a short window"
                assert app.router_box.master.winfo_width() == card_width, "Scrollbar changed card width"
                assert app.btn_bar.winfo_ismapped() and app.btn_bar.winfo_rooty() + app.btn_bar.winfo_height() <= app.root.winfo_rooty() + app.root.winfo_height(), "Toolbar not pinned inside the window"
                card_height = app.router_box.master.winfo_height()
                app.main_canvas.yview_moveto(0)
                app.values["cpu_temp"].event_generate("<MouseWheel>", delta=-120)
                app.root.update()
                assert app.main_canvas.yview()[0] > 0, "Wheel over a label did not scroll"
                app.router_box.toggle()
                app.root.update()
                assert not app.router_box.winfo_manager() and "router" in app.settings["collapsed"]
                assert app.router_box.master.winfo_height() < card_height
                assert app.root.winfo_height() == height, "Collapse resized the window"
                assert device_row.winfo_height() == device_height, "Collapse stretched device cards"
                assert app.root.minsize()[1] == minimum, "Expanded content changed minimum height"
                app.router_box.toggle()
                app.root.update()
                assert app.router_box.winfo_manager() == "pack" and "router" not in app.settings["collapsed"]
                assert app.root.winfo_height() == height
                assert device_row.winfo_height() == device_height
            # A collapsed group keeps its key reading in the header.
            app.case_box.toggle()
            app.render(app.data)
            assert app.case_box.summary.winfo_manager() == "pack" and app.case_box.summary.unit.cget("text") == "RPM"
            app.case_box.toggle()
            assert not app.case_box.summary.winfo_manager()
            # The user's window geometry is remembered.
            app._save_geometry()
            assert app.settings["window"]["height"] == app.root.winfo_height()
            app.render(dict(app.data, routers=[dict(openwrt.empty_reading(), id="primary", name="主路由")]))
            assert app.router_state.cget("text") == "未连接"
            app.render(dict(app.data, router=None, routers=[]))
            assert not app.router_group.winfo_manager()
            # Multiple routers render independent values and offline state; removed devices disappear.
            primary = dict(router, id="primary", name="主路由")
            second = dict(openwrt.empty_reading(), id="second", name="旁路由")
            app.render(dict(app.data, routers=[primary, second]))
            assert app.router_vals["temp"].cget("text") == "39 °C"
            first_box, first_widgets = app.router_widgets["primary"]
            second_box, second_widgets = app.router_widgets["second"]
            app.root.update_idletasks()
            assert first_box.master is not second_box.master
            assert first_box.master.winfo_manager() == second_box.master.winfo_manager() == "pack"
            assert second_widgets[0].cget("text") == "未连接"
            assert second_box.title_label.cget("text") == "旁路由"
            first_box.toggle()
            assert not first_box.winfo_manager() and second_box.winfo_manager() == "pack"
            second_box.toggle()
            assert not second_box.winfo_manager()
            assert "router" in app.settings["collapsed"] and "router:second" in app.settings["collapsed"]
            first_box.toggle()
            assert first_box.winfo_manager() == "pack" and not second_box.winfo_manager()
            # Reordering and removing the first device keep the second device's fold state.
            app.render(dict(app.data, routers=[second, primary]))
            assert [w for w in app.main_content.pack_slaves() if w in (second_box.master, first_box.master)] == [second_box.master, first_box.master]
            assert not second_box.winfo_manager()
            app.render(dict(app.data, routers=[second]))
            assert set(app.router_widgets) == {"second"}
            assert app.router_box is second_box and not second_box.winfo_manager()
            assert app.router_state.cget("text") == "未连接"
            app.render(dict(app.data, routers=[]))
            assert not app.router_widgets
            app.render(dict(app.data, routers=[second]))
            assert not app.router_box.winfo_manager(), "Recreating a router card lost fold state"
            app.router_box.toggle()
            app.render(dict(app.data, routers=[]))
            # OpenWrt and remote software have independent cards and sensor values.
            pc = dict(remote.empty_reading(), id="pc", name="书房电脑", type="hwmonitor", offline=False,
                      cpu_temp=55, cpu_load=25, mem_load=50, mem_used=8 * 1024 ** 3, mem_total=16 * 1024 ** 3,
                      net_up=1024, net_down=2048)
            app.render(dict(app.data, routers=[primary, pc]))
            pc_box, pc_widgets = app.router_widgets["pc"]
            assert app.router_widgets["primary"][0].master.winfo_manager() == "pack"
            assert pc_box.master.winfo_manager() == "pack"
            assert not pc_widgets[0].winfo_manager(), "Connected computer displayed redundant status"
            assert pc_box.cpu_usage.cget("text") == "C 25%"
            assert pc_box.gpu_usage.cget("text") == "G --%"
            assert app.router_widgets["primary"][0].cpu_usage.cget("text") == "C 12%"
            assert pc_box._vals["cpu_temp"].cget("text") == "55 °C"
            assert pc_box._vals["gpu_temp"].cget("text") == "--", "Remote computer used local GPU data"
            assert pc_box._vals["mem_load"].cget("text") == "8.0/16.0 GB  50 %"
            # 展开时标题行只留名称, 读数 (含网速) 都在下方; 收起后才显示徽标和网速摘要。
            for widget in (pc_box.cpu_usage, pc_box.gpu_usage, pc_box.header_rates, pc_widgets[0]):
                assert not widget.winfo_manager(), "Expanded device header still shows readings"
            assert pc_box._vals["net"].cget("text") == "↓ 2.0 KB/s  ↑ 1.0 KB/s"
            primary_box = app.router_widgets["primary"][0]
            assert app.router_widgets["primary"][1][2]["nic"].cget("text") == "eth0"
            pc_box.toggle()
            assert not pc_box.winfo_manager() and "router:pc" in app.settings["collapsed"]
            assert pc_box.cpu_usage.winfo_manager() == "grid" and pc_box.header_rates.winfo_manager() == "grid"
            if primary_box.winfo_manager():
                primary_box.toggle()  # 两张卡片都收起, 才能比较标题行各列是否对齐
            # Device headers share aligned metric/rate columns on a single row.
            # Offline badges and 100% values must remain readable at both supported widths.
            geometry = app.root.geometry()
            narrow_pc = dict(pc, extra_temps=[("storage", "SSD", 36)])
            app.render(dict(app.data, routers=[primary, narrow_pc]))
            for card_width in (app.root.minsize()[0], app.root.winfo_pixels("400p")):
                app.root.geometry(f"{card_width}x{app.root.winfo_height()}")
                app.root.update()
                for cpu in (0, 100):
                    app.render(dict(app.data, routers=[primary, dict(narrow_pc, cpu_load=cpu, gpu_load=cpu)]))
                    app.root.update_idletasks()
                    assert pc_box.cpu_usage.cget("text") == f"C {cpu}%"
                    assert pc_box.gpu_usage.cget("text") == f"G {cpu}%"
                assert not pc_box.summary.winfo_manager(), "Collapsed remote card showed temperature"
                assert pc_box.master.pack_slaves() == [pc_box.head], "Collapsed remote card showed its body"
                def check_header(box):
                    labels = [w for w in descendants(box.head) if isinstance(w, main_window.tk.Label)
                              and w.winfo_ismapped() and w is not box.title_label]
                    for label in labels:
                        assert label.winfo_width() >= label.winfo_reqwidth(), f"Device header clipped {label.cget('text')}"
                        assert label.winfo_rootx() >= box.head.winfo_rootx()
                        assert label.winfo_rootx() + label.winfo_width() <= box.head.winfo_rootx() + box.head.winfo_width()
                check_header(pc_box)
                # 标题行始终单行: 名称、徽标和网速垂直居中在同一行, 名称至少保留几个字的宽度。
                head_mid = pc_box.head.winfo_rooty() + pc_box.head.winfo_height() // 2
                for widget in (pc_box.title_label, pc_box.cpu_usage, pc_box.header_rates):
                    assert abs(widget.winfo_rooty() + widget.winfo_height() // 2 - head_mid) <= 2, "Device header wrapped"
                assert pc_box.title_label.winfo_width() >= app.root.winfo_pixels("36p"), "Device name squeezed out"
                cpu_left = pc_box.cpu_usage.winfo_rootx()
                cpu_right = cpu_left + pc_box.cpu_usage.winfo_width()
                assert cpu_right <= pc_box.gpu_usage.winfo_rootx()
                assert pc_box.gpu_usage.winfo_rootx() + pc_box.gpu_usage.winfo_width() <= pc_box.header_rates.winfo_rootx()
                router_box, router_widgets = app.router_widgets["primary"]
                badge = router_widgets[0]
                assert badge.cget("text") == "eth0"
                check_header(router_box)
                assert router_box.cpu_usage.winfo_rootx() == cpu_left
                assert router_box.header_rates.winfo_rootx() == pc_box.header_rates.winfo_rootx()
                offline = dict(remote.empty_reading(), id="pc", name="办公室电脑", type="hwmonitor")
                app.render(dict(app.data, routers=[primary, offline]))
                app.root.update_idletasks()
                check_header(pc_box)
                assert pc_widgets[0].winfo_manager() == "grid"
                # Nested badges and speed labels still toggle the complete card.
                # 合成点击在 Windows 上按屏幕位置投递: 关掉上面重新打开的"自定义显示",
                # 临时释放设置窗口的模态抓取并把主窗口提到最前, 验证后再恢复。
                # 每次按下都补上松开, 否则 Tk 认为按键仍按着, 后续点击会送到同一控件。
                existing_custom = getattr(app, "customization_win", None)
                if existing_custom and existing_custom.winfo_exists():
                    existing_custom.destroy()
                grab = app.root.grab_current()
                if grab:
                    grab.grab_release()
                app.root.lift()
                app.root.update()
                for expanded in (True, False):
                    pc_box.cpu_usage.event_generate("<Button-1>")
                    pc_box.cpu_usage.event_generate("<ButtonRelease-1>")
                    assert bool(pc_box.winfo_manager()) == expanded
                if grab:
                    grab.lift()
                    grab.grab_set()
            app.root.geometry(geometry)
            offline_pc = dict(remote.empty_reading(), id="pc", name="书房电脑", type="hwmonitor")
            app.render(dict(app.data, routers=[primary, offline_pc]))
            assert pc_widgets[0].cget("text") == "未连接"
            assert pc_box.cpu_usage.cget("text") == "C --%"
            assert pc_box.gpu_usage.cget("text") == "G --%"
            assert pc_widgets[0].winfo_manager() == "grid"
            assert pc_box._vals["cpu_temp"].cget("text") == "--", "Offline computer retained stale readings"
            app.render(dict(app.data, routers=[primary, pc]))
            assert pc_box._vals["cpu_temp"].cget("text") == "55 °C" and not pc_box.winfo_manager()
            pc_box.toggle()
            app.settings["display"]["router:pc"] = {"visible": False, "name": "书房", "order": 0}
            app.render(dict(app.data, routers=[primary, pc]))
            assert not pc_box.master.winfo_manager() and pc_box.title_label.cget("text") == "书房"
            app.settings["display"] = {}
            app.render(dict(app.data, routers=[]))
            # Customisation lists each configured router, including disabled devices.
            original_configs, original_data = app.settings["routers"], app.data
            app.settings["routers"] = [dict(settings.DEFAULT_ROUTER, id="primary", name="主路由"),
                                       dict(settings.DEFAULT_ROUTER, id="second", name="旁路由")]
            app.data = dict(original_data, routers=[primary, second])
            existing_custom = getattr(app, "customization_win", None)
            if existing_custom and existing_custom.winfo_exists():
                existing_custom.destroy()
            def open_router_customisation():
                app.show_customization()
                app.root.update_idletasks()
                widgets = list(descendants(app.customization_win))
                selector = next(w for w in widgets if isinstance(w, ttk.Combobox))
                selector.set("可选分组")
                selector.event_generate("<<ComboboxSelected>>")
                return widgets, next(w for w in widgets if isinstance(w, ttk.Treeview))
            widgets, custom_tree = open_router_customisation()
            assert "router" not in custom_tree.get_children()
            assert {"router:primary", "router:second"} <= set(custom_tree.get_children())
            custom_tree.selection_set("router:second")
            custom_tree.event_generate("<<TreeviewSelect>>")
            custom_entry = next(w for w in widgets if isinstance(w, ttk.Entry) and not isinstance(w, ttk.Combobox))
            custom_entry.delete(0, "end")
            custom_entry.insert(0, "旁路由监控")
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "上移").invoke()
            custom_tree.selection_set("router:primary")
            custom_tree.event_generate("<<TreeviewSelect>>")
            next(w for w in widgets if isinstance(w, ttk.Checkbutton) and w.cget("text") == "显示").invoke()
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "保存").invoke()
            assert not app.router_widgets["primary"][0].master.winfo_manager()
            assert app.router_widgets["second"][0].master.winfo_manager() == "pack"
            assert app.router_widgets["second"][0].title_label.cget("text") == "旁路由监控"
            app.settings["display"]["router:primary"]["visible"] = True
            app.render(app.data)
            cards = [app.router_widgets[key][0].master for key in ("second", "primary")]
            assert [w for w in app.main_content.pack_slaves() if w in cards] == cards
            widgets, custom_tree = open_router_customisation()
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "恢复默认").invoke()
            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget("text") == "保存").invoke()
            assert app.router_widgets["second"][0].title_label.cget("text") == "旁路由"
            assert all(box.master.winfo_manager() == "pack" for box, _ in app.router_widgets.values())
            app.settings["routers"], app.data = original_configs, original_data
            app.settings["display"] = {}
            app.render(app.data)
            app.settings["display"] = {key: {"visible": False} for key in app.groups}
            app._apply_display()
            app.render(app.data)
            app.root.update()
            assert not app.main_scrollbar.winfo_ismapped(), "Scrollbar stayed visible when content fits"
            assert app.main_canvas.yview() == (0.0, 1.0), "View did not reset after content shrank"
            app.values["cpu_temp"].event_generate("<MouseWheel>", delta=-120)
            app.root.update()
            assert app.main_canvas.yview() == (0.0, 1.0), "Fitting content scrolled into empty space"
            app.settings["display"] = {}
            app._apply_display()
            app.render(app.data)
            app.render(dict(app.data, error=main_window.lhm.NO_PYTHONNET))
            app.root.update()
            assert app.action_btn.winfo_manager() and app._action == app.install_deps
            app.render(app.data)
            app.root.update()
            assert bool(app.action_btn.winfo_manager()) == app.pawnio_missing
            with patch.object(app.tray, "menu", return_value="floating"):
                app.tray.events.append("menu")
                app._tray_events()
            fw = app.float_win
            assert fw is not None, "Tray menu did not open the floating window"
            assert fw.winfo_exists()
            app.root.update()
            assert fw.rows["cpu"].value.unit_label.cget("text") == "°C"
            fw._vars = {"show_net": main_window.tk.BooleanVar(value=False)}
            fw._toggle("show_net")
            assert not fw.rows["net"].frame.winfo_manager()
            fw._settle(fw.work[0] + 3, fw.work[1] + 100, save=False)
            assert fw.pos[0] == fw.work[0] and fw.edge in ("left", None)
            if fw.edge:
                fw.conceal()
                while fw.sliding:
                    app.root.update()
                assert fw.hidden and fw.winfo_width() == floating_mod.STRIP
                fw.reveal()
                while fw.sliding:
                    app.root.update()
                assert not fw.hidden and fw.winfo_rootx() == fw.pos[0]
            with patch.object(app.tray, "menu", return_value="floating") as tray_menu:
                app.tray.events.append("menu")
                app._tray_events()
            assert tray_menu.call_args.kwargs["floating"] and app.float_win is None, "Tray menu did not close the floating window"
            app.settings["display"] = {}
            app._apply_display()
            app.render(app.data)
            if "--preview" not in sys.argv:
                for window in app.root.winfo_children():
                    if isinstance(window, main_window.tk.Toplevel):
                        window.destroy()
                from tkinter import filedialog
                payload = {"error": None, "model": "Fixture PC", "os": "Windows", "details": []}
                with patch.object(filedialog, "asksaveasfilename", return_value=str(paths.DATA / "report.json")), patch("hwmonitor.ui.settings_dialog.hwinfo.load", return_value=payload):
                    app.export_diagnostics()
                app.root.after(1200, finish)
        except Exception as e:
            failures.append(traceback.format_exc())
            app.quit()
        finally:
            frozen.stop()

    def finish():
        try:
            assert (paths.DATA / "report.json").exists()
            assert app.collector.is_alive()
        except Exception as e:
            failures.append(traceback.format_exc())
        app.quit()

    app.root.after(1200, check_main)
    app.root.mainloop()

if failures:
    print("\n".join(failures), file=sys.stderr)  # setup_logging 把 excepthook 改成写日志文件, CI 里看不到回溯, 这里直接打到控制台
    raise AssertionError("\n".join(failures))
print("GUI integration passed: main, charts (17,280 points), settings, customisation, floating window, export and clean exit")
