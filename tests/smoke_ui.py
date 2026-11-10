"""Isolated GUI integration check. --preview keeps the fixture UI open for visual QA."""
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ["HARDWARE_MONITOR_DATA_DIR"] = str(ROOT / ".build" / "test-data")
sys.path.insert(0, str(ROOT / "app"))
from test_core import SensorTests
import config
import diagnostics
import monitor
import openwrt
import sensors
from tkinter import ttk

diagnostics.setup_logging()
failures = []
tree = SensorTests().tree()
tree["Children"].append({"HardwareId": "/lpc/0", "Text": "Board", "Children": [
    {"Text": "CPU Fan", "Value": "1200 RPM", "Type": "Fan"},
    {"Text": "Case Fan", "Value": "800 RPM", "Type": "Fan"}]})


def read(gpu_id=None, nic_ids=None):
    with patch("sensors._lhm") as lhm, patch("sensors._nvidia", return_value=[]):
        out = {"temps": [], "fans": [], "loads": [], "data": [], "extra": {}, "hw": [], "by_id": True}
        sensors._walk(tree, None, out)
        lhm.return_value = out
        return original_read(gpu_id, nic_ids)


def record_error(*args):
    failures.append(str(args))


original_read = sensors.read
with patch("collector.sensors.read", side_effect=read), patch("autostart.sync"), patch("autostart.is_enabled", return_value=False), patch("config.load", side_effect=lambda: config.validate({})):
    app = monitor.App()
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
                label = find(app.settings_pages[page], monitor.tk.Label, lambda w: w.cget("text") == title)
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
            assert url_entry.get() == config.DEFAULTS["router"]["url"] and url_row.desc.cget("fg") == monitor.RED, "Invalid URL accepted"
            row_titled("router", "显示路由器状态").switch.invoke()
            assert not app.settings["router"]["enabled"]
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
            app.show_floating()
            assert app.float_win.winfo_exists()
            app.hide_floating()
            assert app.float_win is None
            app.settings["display"] = {}
            app._apply_display()
            app.render(app.data)
            if "--preview" not in sys.argv:
                for window in app.root.winfo_children():
                    if isinstance(window, monitor.tk.Toplevel):
                        window.destroy()
                from tkinter import filedialog
                from unittest.mock import patch
                payload = {"error": None, "model": "Fixture PC", "os": "Windows", "details": []}
                with patch.object(filedialog, "asksaveasfilename", return_value=str(config.DATA / "report.json")), patch("dialogs.hwinfo.load", return_value=payload):
                    app.export_diagnostics()
                app.root.after(1200, finish)
        except Exception as e:
            failures.append(repr(e))
            app.quit()

    def finish():
        try:
            assert (config.DATA / "report.json").exists()
            assert app.collector.is_alive()
        except Exception as e:
            failures.append(repr(e))
        app.quit()

    app.root.after(1200, check_main)
    app.root.mainloop()

if failures:
    raise AssertionError("\n".join(failures))
print("GUI integration passed: main, charts (17,280 points), settings, customisation, floating window, export and clean exit")
