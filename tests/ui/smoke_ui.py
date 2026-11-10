"""Isolated GUI integration check. --preview keeps the fixture UI open for visual QA."""
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
from hwmonitor.sensors import openwrt, reading
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
with patch("hwmonitor.core.collector.reading.read", side_effect=read), patch("hwmonitor.system.autostart.sync"), patch("hwmonitor.system.autostart.is_enabled", return_value=False), patch("hwmonitor.system.pawnio.status", return_value="ok"), patch("hwmonitor.settings.load", side_effect=lambda: settings.validate({"web": {"port": WEB_PORT}})):
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
            settings_window.event_generate("<Control-Tab>")
            assert app._settings_page == "monitor"
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
            row_titled("router", "显示路由器状态").switch.invoke()
            assert app.settings["router"]["enabled"] and url_entry.instate(["!disabled"])
            url_entry.delete(0, "end")
            url_entry.insert(0, "ftp://router")
            url_entry.event_generate("<FocusOut>")
            assert url_entry.get() == settings.DEFAULTS["router"]["url"] and url_row.desc.cget("fg") == main_window.RED, "Invalid URL accepted"
            row_titled("router", "显示路由器状态").switch.invoke()
            assert not app.settings["router"]["enabled"]
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
            app.render(dict(app.data, router=router))
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
            assert app.case_box.summary.winfo_manager() == "pack" and app.case_box.summary.cget("text").endswith("RPM")
            app.case_box.toggle()
            assert not app.case_box.summary.winfo_manager()
            # The user's window geometry is remembered.
            app._save_geometry()
            assert app.settings["window"]["height"] == app.root.winfo_height()
            app.render(dict(app.data, router=openwrt.empty_reading()))
            assert app.router_state.cget("text") == "未连接"
            app.render(dict(app.data, router=None))
            assert not app.router_box.master.winfo_manager()
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
