"""硬件监控: 实时显示 CPU/GPU 温度与风扇转速。"""
import ctypes
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

import sensors

INTERVAL_MS = 1000


def temp_color(t):
    if t is None:
        return "gray"
    return "#2e9e44" if t < 60 else "#d99a00" if t < 80 else "#d12f2f"


class App:
    def __init__(self):
        self.root = r = tk.Tk()
        r.title("硬件监控")
        r.geometry("360x380")
        r.minsize(320, 340)
        try:
            r.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
        except tk.TclError:
            pass

        main = ttk.Frame(r, padding=12)
        main.pack(fill="both", expand=True)

        self.values = {}
        self._section(main, "温度", [("cpu_temp", "CPU 温度"), ("gpu_temp", "显卡温度")])
        self._section(main, "风扇", [("cpu_fan", "CPU 风扇"), ("gpu_fan", "显卡风扇")])

        self.case_box = ttk.LabelFrame(main, text="机箱 / 主板风扇", padding=8)
        self.case_box.pack(fill="x", pady=(8, 0))

        self.status = ttk.Label(main, text="", foreground="#d12f2f", wraplength=320)
        self.status.pack(fill="x", pady=(8, 0))

        # 仅当 LibreHardwareMonitor 由 start.bat 拉起时, 关闭本窗口才一并关闭它
        self.own_libre = "--own-libre" in sys.argv
        r.protocol("WM_DELETE_WINDOW", self.close)

        self.data = None
        self.busy = False
        self.tick()

    def close(self):
        if self.own_libre:
            subprocess.run(["taskkill", "/F", "/IM", "LibreHardwareMonitor.exe"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.root.destroy()

    def _section(self, parent, title, items):
        box = ttk.LabelFrame(parent, text=title, padding=8)
        box.pack(fill="x", pady=(0, 8))
        for key, label in items:
            f = ttk.Frame(box)
            f.pack(fill="x", pady=2)
            ttk.Label(f, text=label, font=("Microsoft YaHei UI", 11)).pack(side="left")
            v = tk.Label(f, text="--", font=("Consolas", 16, "bold"))
            v.pack(side="right")
            self.values[key] = v

    def _poll(self):
        self.data = sensors.read()
        self.busy = False

    def tick(self):
        if not self.busy:  # 后台线程读取, 避免卡界面
            self.busy = True
            threading.Thread(target=self._poll, daemon=True).start()
        if self.data:
            self.render(self.data)
        self.root.after(INTERVAL_MS, self.tick)

    def render(self, d):
        for k in ("cpu_temp", "gpu_temp"):
            t = d[k]
            self.values[k].config(text="--" if t is None else f"{t:.0f} °C", fg=temp_color(t))
        self.values["cpu_fan"].config(text="--" if d["cpu_fan"] is None else f"{d['cpu_fan']:.0f} RPM")
        if d["gpu_fan"] is not None:
            self.values["gpu_fan"].config(text=f"{d['gpu_fan']:.0f} RPM")
        elif d["gpu_fan_pct"] is not None:
            self.values["gpu_fan"].config(text=f"{d['gpu_fan_pct']:.0f} %")
        else:
            self.values["gpu_fan"].config(text="--")

        for w in self.case_box.winfo_children():
            w.destroy()
        if d["case_fans"]:
            for name, rpm in d["case_fans"]:
                f = ttk.Frame(self.case_box)
                f.pack(fill="x", pady=1)
                ttk.Label(f, text=name).pack(side="left")
                ttk.Label(f, text=f"{rpm:.0f} RPM", font=("Consolas", 11)).pack(side="right")
        else:
            ttk.Label(self.case_box, text="未检测到(主板未提供风扇传感器)", foreground="gray").pack(anchor="w")
        self.status.config(text=d["error"] or "")


if __name__ == "__main__":
    # 独立的任务栏标识, 否则任务栏会归到 pythonw 并显示 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hwmonitor.fan.app")
    App().root.mainloop()
