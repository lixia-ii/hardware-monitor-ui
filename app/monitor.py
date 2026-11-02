"""硬件监控: 实时显示 CPU/GPU 温度与风扇转速。"""
import ctypes
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

import hwinfo
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
        r.minsize(360, 340)
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

        self.extra_box = ttk.LabelFrame(main, text="硬盘 / 内存 / 网卡温度", padding=8)
        self.extra_box.pack(fill="x", pady=(8, 0))

        ttk.Button(main, text="硬件信息", command=self.show_info).pack(anchor="e", pady=(8, 0))

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

    def show_info(self):
        """打开硬件信息窗口 (已打开则置前)。信息在后台线程读取, 避免卡界面。"""
        if getattr(self, "info_win", None) and self.info_win.winfo_exists():
            self.info_win.lift()
            return
        w = self.info_win = tk.Toplevel(self.root)
        w.title("硬件信息")
        w.minsize(560, 360)
        try:
            w.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
        except tk.TclError:
            pass
        body = ttk.Frame(w, padding=12)
        body.pack(fill="both", expand=True)

        cards = ttk.Frame(body)
        cards.pack(fill="x")
        vals = {}
        for i, (key, title) in enumerate((("model", "型号信息"), ("os", "系统信息"), ("uptime", "运行时间"))):
            cards.columnconfigure(i, weight=1, uniform="card")
            box = ttk.LabelFrame(cards, text=title, padding=10)
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            vals[key] = ttk.Label(box, text="读取中...", font=("Microsoft YaHei UI", 10),
                                  wraplength=160, justify="center")
            vals[key].pack(expand=True)

        detail = ttk.LabelFrame(body, text="详细信息", padding=10)
        detail.pack(fill="both", expand=True, pady=(10, 0))

        def uptime():
            if not w.winfo_exists():
                return
            s = ctypes.windll.kernel32.GetTickCount64() // 1000
            vals["uptime"].config(text=f"{s // 86400}天{s % 86400 // 3600}小时{s % 3600 // 60}分钟{s % 60}秒")
            w.after(1000, uptime)
        uptime()

        def fill(info):
            if not w.winfo_exists():
                return
            if info["error"]:
                vals["model"].config(text=info["error"])
                return
            vals["model"].config(text=info["model"])
            vals["os"].config(text=info["os"])
            r = 0
            for label, lines in info["details"]:
                ttk.Label(detail, text=label + ":").grid(row=r, column=0, sticky="nw", padx=(0, 12))
                for line in lines or ["--"]:
                    ttk.Label(detail, text=line).grid(row=r, column=1, sticky="w")
                    r += 1

        result = []  # 后台线程只写结果, 界面更新留在主线程 (tkinter 非线程安全)
        threading.Thread(target=lambda: result.append(hwinfo.load()), daemon=True).start()

        def wait():
            if not w.winfo_exists():
                return
            if result:
                fill(result[0])
            else:
                w.after(100, wait)
        wait()

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

    @staticmethod
    def _set(label, text, fg=None):
        """只在内容变化时才更新控件, 避免整页重绘闪烁。"""
        if label.cget("text") != text:
            label.config(text=text)
        if fg is not None and str(label.cget("fg")) != fg:
            label.config(fg=fg)

    def _rows(self, box, rows, empty_text, make_value):
        """动态行: 仅当行集合变化时才重建, 否则只更新数值。rows: [(key, 名称, 数值文本, 颜色)]"""
        keys = [r[0] for r in rows]
        if getattr(box, "_keys", None) != keys:
            for w in box.winfo_children():
                w.destroy()
            box._keys, box._vals = keys, {}
            if not rows:
                ttk.Label(box, text=empty_text, foreground="gray").pack(anchor="w")
            for key, name, _, _ in rows:
                f = ttk.Frame(box)
                f.pack(fill="x", pady=1)
                ttk.Label(f, text=name).pack(side="left")
                box._vals[key] = make_value(f)
        for key, _, text, color in rows:
            self._set(box._vals[key], text, color)

    def render(self, d):
        for k in ("cpu_temp", "gpu_temp"):
            t = d[k]
            self._set(self.values[k], "--" if t is None else f"{t:.0f} °C", temp_color(t))
        self._set(self.values["cpu_fan"], "--" if d["cpu_fan"] is None else f"{d['cpu_fan']:.0f} RPM")
        if d["gpu_fan"] is not None:
            gpu_fan = f"{d['gpu_fan']:.0f} RPM"
        elif d["gpu_fan_pct"] is not None:
            gpu_fan = f"{d['gpu_fan_pct']:.0f} %"
        else:
            gpu_fan = "--"
        self._set(self.values["gpu_fan"], gpu_fan)

        self._rows(self.case_box,
                   [(n, n, f"{rpm:.0f} RPM", None) for n, rpm in d["case_fans"]],
                   "未检测到(主板未提供风扇传感器)",
                   lambda f: self._pack_right(tk.Label(f, font=("Consolas", 11))))
        self._rows(self.extra_box,
                   [((k, n), f"[{sensors.EXTRA_LABELS[k]}] {n}", f"{t:.0f} °C", temp_color(t))
                    for k, n, t in d["extra_temps"]],
                   "未检测到(无硬盘/内存/网卡温度传感器)",
                   lambda f: self._pack_right(tk.Label(f, font=("Consolas", 11, "bold"))))
        self._set(self.status, d["error"] or "")

    @staticmethod
    def _pack_right(w):
        w.pack(side="right")
        return w


if __name__ == "__main__":
    # 独立的任务栏标识, 否则任务栏会归到 pythonw 并显示 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hwmonitor.fan.app")
    App().root.mainloop()
