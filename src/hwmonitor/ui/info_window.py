"""硬件信息窗口: 型号、系统、运行时间与详细硬件清单 (后台读取 WMI)。"""
import ctypes
import threading
import tkinter as tk
from tkinter import ttk

from hwmonitor.sensors import hwinfo
from hwmonitor.ui.theme import BG, MUTED, set_icon


class InfoWindowMixin:
    def show_info(self):
        """打开硬件信息窗口 (已打开则置前)。信息在后台线程读取, 避免卡界面。"""
        if getattr(self, "info_win", None) and self.info_win.winfo_exists():
            self.info_win.lift()
            return
        w = self.info_win = tk.Toplevel(self.root)
        w.title("硬件信息")
        w.configure(bg=BG)
        w.attributes("-topmost", self.root.attributes("-topmost"))
        w.minsize(w.winfo_pixels("420p"), w.winfo_pixels("270p"))
        set_icon(w)
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
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            s = ctypes.windll.kernel32.GetTickCount64() // 1000
            vals["uptime"].config(text=f"{s // 86400}天{s % 86400 // 3600}小时{s % 3600 // 60}分钟{s % 60}秒")
            w.after(1000, uptime)
        uptime()
        self.center_on_main(w)

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
                ttk.Label(detail, text=label + ":", foreground=MUTED).grid(row=r, column=0, sticky="nw", padx=(0, 12))
                for line in lines or ["--"]:
                    ttk.Label(detail, text=line).grid(row=r, column=1, sticky="w")
                    r += 1
            self.center_on_main(w)  # 内容填入后窗口变大, 重新居中

        result = []  # 后台线程只写结果, 界面更新留在主线程 (tkinter 非线程安全)

        def work():
            try:
                result.append(hwinfo.load())
            except Exception as e:
                result.append({"error": f"读取失败: {e!r}"})
        threading.Thread(target=work, daemon=True).start()

        def wait():
            if not w.winfo_exists():
                return
            if result:
                fill(result[0])
            else:
                w.after(100, wait)
        wait()
