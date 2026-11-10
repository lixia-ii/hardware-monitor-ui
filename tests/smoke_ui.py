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
