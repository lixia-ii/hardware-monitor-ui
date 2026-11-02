"""硬件监控: 实时显示 CPU/GPU 温度与风扇转速。"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import hwinfo
import sensors

INTERVAL = 1.0  # 两次读取的最短间隔 (秒)
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


# 深色配色
BG = "#16181d"
CARD = "#20232a"
BORDER = "#2c3039"
TRACK = "#2f333d"
FG = "#e6e8ee"
MUTED = "#8a90a0"
RED = "#f87171"
PIN = "#1e3a5f"  # 置顶按钮开启时的底色
CHIP_HOVER = "#3a3f4b"
ACCENT = "#2563eb"  # 主操作按钮
ERR_BG = "#2a1d21"  # 错误提示条
ERR_BORDER = "#4a2a2f"
FONT = "Microsoft YaHei UI"
NUM = "Segoe UI"  # 数字等宽, 跳动时不抖


def set_icon(win):
    try:
        win.iconbitmap(ICON)
    except tk.TclError:
        pass
    try:  # Windows 10/11 深色标题栏, 与界面配色一致
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
    except Exception:
        pass


def short_gpu(name):
    """显卡名去掉厂商前缀等, 放进小标签: "NVIDIA GeForce RTX 4060" -> "RTX 4060", "Intel(R) UHD Graphics 770" -> "UHD 770"。"""
    s = re.sub(r"\((?:R|TM)\)|\b(?:NVIDIA|AMD|Intel|GeForce|Graphics)\b|\bRadeon\s+(?=RX\b)", " ", name, flags=re.I)
    return " ".join(s.split()) or name


def temp_color(t):
    if t is None:
        return MUTED
    return "#4ade80" if t < 60 else "#fbbf24" if t < 80 else RED


class App:
    def __init__(self):
        self.root = r = tk.Tk()
        r.title("硬件监控")
        r.configure(bg=BG)
        r.minsize(r.winfo_pixels("300p"), 0)  # 尺寸用点 (p) 而非像素, 高 DPI 下按比例放大
        self._style()
        set_icon(r)

        main = tk.Frame(r, bg=BG, padx="10p", pady="10p")
        main.pack(fill="both", expand=True)

        self.values = {}
        self.bars = {}
        self._devices(main)

        self.case_box = self._card(main, "机箱风扇")

        # 水冷泵: 只有检测到时才显示, 插在机箱风扇下面
        self.pump_box = self._card(main, "水冷泵")

        # 硬盘 / 内存 / 网卡各一项, 可点击标题收起或展开
        self.extra_boxes = {k: self._card(main, f"{label}温度", collapsible=True)
                            for k, label in sensors.EXTRA_LABELS.items()}

        self.footer = tk.Frame(main, bg=BG)
        self.footer.pack(fill="x", pady=("8p", 0))
        # 错误提示条: 有消息才显示, 在按钮上方; 未连接 LibreHardwareMonitor 时右侧带"启动"按钮
        self.notice = tk.Frame(self.footer, bg=ERR_BG, padx="8p", pady="5p",
                               highlightthickness=1, highlightbackground=ERR_BORDER)
        self.notice.columnconfigure(0, weight=1)
        self.status = tk.Label(self.notice, text="", bg=ERR_BG, fg=RED, font=(FONT, 9),
                               wraplength="120p", justify="left", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.launch_btn = ttk.Button(self.notice, text="启动", style="Accent.TButton", width=7,  # 定宽, 切换文字时不跳
                                     command=self.launch_libre, cursor="hand2")
        self.notice.bind("<Configure>", self._wrap_status)
        self.status_msg = ("", 0.0)  # 启动失败等临时消息, 在到期前盖过读取到的错误

        self.btn_bar = tk.Frame(self.footer, bg=BG)
        self.btn_bar.pack(fill="x")
        self.info_btn = ttk.Button(self.btn_bar, text="硬件信息", command=self.show_info, cursor="hand2")
        self.info_btn.pack(side="right")
        # 窗口置顶开关 (原生标题栏放不了按钮, 放在底栏), 开启时高亮
        self.pin_btn = ttk.Button(self.btn_bar, text="置顶", command=self.toggle_topmost, cursor="hand2")
        self.pin_btn.pack(side="right", padx=(0, "6p"))
        # 可选分组: 没有数据就不显示。按此顺序排在底栏之前
        self.optional = [b.master for b in (self.case_box, self.pump_box, *self.extra_boxes.values())]
        self.shown = ()

        # 仅当 LibreHardwareMonitor 由 start.bat 拉起时, 关闭本窗口才一并关闭它
        self.own_libre = "--own-libre" in sys.argv
        r.protocol("WM_DELETE_WINDOW", self.close)

        self.data = None
        self.launch_until = 0.0
        self.gpu_sel = None  # 用户选的显卡 HardwareId, None 表示自动 (优先独显)
        self.gpu_cur = None  # 当前显示的显卡 HardwareId
        self.fresh = False   # 后台线程读到新数据后置 True, 由主线程渲染
        self.busy = False
        self.last_start = 0.0
        self.centered = False
        self.center()
        self.tick()

    def _style(self):
        """ttk 控件 (按钮、硬件信息窗口) 也换成深色, 与主界面一致。"""
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, font=(FONT, 9))
        s.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
        s.configure("TButton", background=CARD, foreground=FG, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, focuscolor=CARD, padding=("10p", "3p"), width=0)  # 宽度随文字, 不用主题默认的最小宽度
        hover = [("pressed", BORDER), ("active", "#2a2e37")]
        s.map("TButton", background=hover, lightcolor=hover, darkcolor=hover)
        # 置顶开启时的按钮样式
        s.configure("On.TButton", background=PIN, foreground="#93c5fd", bordercolor="#3b82f6",
                    lightcolor=PIN, darkcolor=PIN, focuscolor=PIN)
        hover = [("pressed", "#1a3354"), ("active", "#24466f")]
        s.map("On.TButton", background=hover, lightcolor=hover, darkcolor=hover)
        # 主操作按钮 (提示条里的"启动")
        s.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT, anchor="center")
        hover = [("disabled", CARD), ("pressed", "#1d4ed8"), ("active", "#3b82f6")]
        s.map("Accent.TButton", background=hover, lightcolor=hover, darkcolor=hover,
              bordercolor=[("disabled", BORDER)], foreground=[("disabled", MUTED)])

    def toggle_topmost(self):
        on = not self.root.attributes("-topmost")
        self.root.attributes("-topmost", on)
        self.pin_btn.config(style="On.TButton" if on else "TButton")
        if getattr(self, "info_win", None) and self.info_win.winfo_exists():  # 硬件信息窗口跟随, 免得被压在下面
            self.info_win.attributes("-topmost", on)

    @staticmethod
    def _libre_exe():
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "LibreHardwareMonitor", "LibreHardwareMonitor.exe")

    def launch_libre(self):
        """尝试启动 LibreHardwareMonitor (最小化)。它需要管理员权限, 本程序未提权时会弹出 UAC 确认。"""
        exe = self._libre_exe()
        if not os.path.exists(exe):
            self._flash(f"找不到 {exe}")
            return
        # 7 = SW_SHOWMINNOACTIVE; 返回值 <= 32 表示失败 (如用户在 UAC 里点了取消)
        ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, os.path.dirname(exe), 7) > 32
        if not ok:
            self._flash("启动 LibreHardwareMonitor 失败或已取消")
            return
        self.own_libre = True  # 与 start.bat 一致: 由本程序拉起的, 关闭本窗口时一并关闭
        self.launch_until = time.monotonic() + 6  # 进程出现前的缓冲期, 避免重复点击
        self._update_launch_btn(False)

    def _update_launch_btn(self, running):
        """进程已在运行 (还在启动, 网页服务没起来) 或刚点过启动时置灰。"""
        busy = running or time.monotonic() < self.launch_until
        self.launch_btn.state(["disabled" if busy else "!disabled"])
        self._set(self.launch_btn, "启动中…" if busy else "启动")

    def _flash(self, msg):
        """临时消息显示 5 秒, 否则下一次渲染会立刻被读取到的错误覆盖。"""
        self.status_msg = (msg, time.monotonic() + 5)
        self._set(self.status, msg)

    def _notice(self, error, launch, running):
        """提示条: 有消息才显示; launch 时右侧显示启动按钮。"""
        msg, until = self.status_msg
        msg = msg if time.monotonic() < until else error or ""
        self._set(self.status, msg)
        if bool(msg) != bool(self.notice.winfo_manager()):
            if msg:
                self.notice.pack(fill="x", pady=(0, "8p"), before=self.btn_bar)
            else:
                self.notice.pack_forget()
        if launch != bool(self.launch_btn.winfo_manager()):
            if launch:
                self.launch_btn.grid(row=0, column=1, padx=("8p", 0))
            else:
                self.launch_btn.grid_remove()
            self._wrap_status()
        if launch:
            self._update_launch_btn(running)

    def _wrap_status(self, _=None):
        """消息按提示条的实际宽度换行 (扣掉按钮), 不把窗口撑宽。"""
        width = self.notice.winfo_width() - 2 * self.notice.winfo_pixels("8p") - 2
        if self.launch_btn.winfo_manager():
            width -= self.launch_btn.winfo_reqwidth() + self.notice.winfo_pixels("8p")
        if width > 50:
            self.status.config(wraplength=width)

    def center_on_main(self, w):
        """把 w 居中到主窗口中央 (主窗口在哪, 就在哪弹出)。"""
        w.update_idletasks()
        mw, mh = max(w.winfo_reqwidth(), w.minsize()[0]), max(w.winfo_reqheight(), w.minsize()[1])
        x = self.root.winfo_x() + (self.root.winfo_width() - mw) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - mh) // 2
        w.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def center(self):
        """窗口居中到屏幕。"""
        r = self.root
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        x = (r.winfo_screenwidth() - w) // 2
        y = (r.winfo_screenheight() - h) // 2
        r.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def close(self):
        if self.own_libre:
            self.root.withdraw()  # 等待 LibreHardwareMonitor 退出最多 3 秒, 先隐藏窗口免得看起来卡死
            self._stop_libre()
        self.root.destroy()

    @staticmethod
    def _stop_libre():
        """先正常关闭, 让 LibreHardwareMonitor 自己做退出清理 (如把风扇控制交还主板); 超时才强杀。"""
        flags = subprocess.CREATE_NO_WINDOW
        subprocess.run(["taskkill", "/IM", "LibreHardwareMonitor.exe"], capture_output=True, creationflags=flags)
        for _ in range(15):
            time.sleep(0.2)
            alive = subprocess.run(["tasklist", "/fi", "imagename eq LibreHardwareMonitor.exe"],
                                   capture_output=True, text=True, creationflags=flags).stdout
            if "LibreHardwareMonitor.exe" not in alive:
                return
        subprocess.run(["taskkill", "/F", "/IM", "LibreHardwareMonitor.exe"],
                       capture_output=True, creationflags=flags)

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

    def _devices(self, parent):
        """CPU / 显卡各一张卡片: 大字温度, 下面是利用率 (带进度条) 和风扇。"""
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        for i, (dev, name, accent) in enumerate((("cpu", "CPU", "#60a5fa"), ("gpu", "显卡", "#a78bfa"))):
            row.columnconfigure(i, weight=1, uniform="dev")
            card = tk.Frame(row, bg=CARD, padx="10p", pady="8p", highlightthickness=1, highlightbackground=BORDER)
            # 两侧各留一半间距, 两张卡片才一样宽 (padx 也算在 uniform 列宽里)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, "3p") if i == 0 else ("3p", 0))
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x")
            tk.Label(head, text=name, bg=CARD, fg=MUTED, font=(FONT, 10, "bold")).pack(side="left")
            if dev == "gpu":  # 多显卡时才显示的切换标签, 在卡片右上角, 点击切到下一块
                # 不用 Menubutton: Windows 原生下拉菜单不认配色, 在深色界面里是一块白
                chip = self.gpu_chip = tk.Label(head, bg=TRACK, fg=FG, font=(FONT, 9), padx="5p", cursor="hand2")
                chip.bind("<Button-1>", self._next_gpu)
                chip.bind("<Enter>", lambda _: chip.config(bg=CHIP_HOVER))
                chip.bind("<Leave>", lambda _: chip.config(bg=TRACK))
                self.gpu_list = []
            v = tk.Label(card, text="--", bg=CARD, fg=MUTED, font=(NUM, 24, "bold"))
            v.pack(anchor="w")
            self.values[f"{dev}_temp"] = v
            for kind, title in (("load", "利用率"), ("fan", "风扇")):
                f = tk.Frame(card, bg=CARD)
                f.pack(fill="x", pady=("4p", 0))
                tk.Label(f, text=title, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
                self.values[f"{dev}_{kind}"] = self._value(f)
                if kind == "load":
                    track = tk.Frame(card, bg=TRACK, height="3p")
                    track.pack(fill="x", pady=("3p", 0))
                    bar = tk.Frame(track, bg=accent)
                    bar.rel = 0
                    self.bars[dev] = bar

    def _next_gpu(self, _=None):
        ids = [h for h, _ in self.gpu_list]
        if len(ids) < 2:
            return
        cur = self.gpu_sel if self.gpu_sel in ids else self.gpu_cur
        self.gpu_sel = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self._show_gpu(self.gpu_sel)  # 标签先切过去, 数值等下一次读取
        self.last_start = 0.0  # 立即按新选择重读, 不等下一个周期

    def _show_gpu(self, hwid):
        self.gpu_cur = hwid
        name = next((n for h, n in self.gpu_list if h == hwid), "")
        self._set(self.gpu_chip, f"{short_gpu(name)}  ⇄")

    def _update_gpu(self, d):
        """多块显卡时显示切换标签, 只有一块 (或没有) 就隐藏。"""
        self.gpu_list = d["gpus"]
        if len(self.gpu_list) > 1:
            self._show_gpu(d["gpu_id"])
            if not self.gpu_chip.winfo_manager():
                self.gpu_chip.pack(side="right")
        elif self.gpu_chip.winfo_manager():
            self.gpu_chip.pack_forget()

    @staticmethod
    def _card(parent, title, collapsible=False):
        """分组卡片, 返回放内容的 body 框架 (body.master 是卡片本身)。collapsible: 点击标题收起/展开。"""
        card = tk.Frame(parent, bg=CARD, padx="10p", pady="7p", highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(side="left")
        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", pady=("4p", 0))
        if collapsible:
            arrow = tk.Label(head, text="▾", bg=CARD, fg=MUTED, font=(FONT, 12))
            arrow.pack(side="right")

            def toggle(_=None):
                if body.winfo_manager():
                    body.pack_forget()
                    arrow.config(text="▸")
                else:
                    body.pack(fill="x", pady=("4p", 0))
                    arrow.config(text="▾")
            for w in (head, *head.winfo_children()):
                w.config(cursor="hand2")
                w.bind("<Button-1>", toggle)
        return body

    @staticmethod
    def _value(parent):
        """右对齐的数值标签。"""
        v = tk.Label(parent, text="--", bg=CARD, fg=FG, font=(NUM, 10, "bold"))
        v.pack(side="right")
        return v

    def _bar(self, dev, pct):
        bar = self.bars[dev]
        rel = 0 if pct is None else max(0.0, min(pct, 100.0)) / 100
        if bar.rel != rel:
            bar.rel = rel
            if rel:
                bar.place(x=0, y=0, relheight=1, relwidth=rel)
            else:  # relwidth=0 仍会画出 1 像素, 直接隐藏
                bar.place_forget()

    def _poll(self):
        try:
            sel = self.gpu_sel
            self.data = sensors.read(sel)
            self.fresh = sel == self.gpu_sel  # 读取期间切换了显卡: 这份是旧显卡的, 丢掉, 免得标签跳回去
        finally:  # 无论如何都要复位, 否则界面会永远停在最后一次读数
            self.busy = False

    def tick(self):
        now = time.monotonic()
        if not self.busy and now - self.last_start >= INTERVAL:  # 后台线程读取, 避免卡界面
            self.busy = True
            self.last_start = now
            threading.Thread(target=self._poll, daemon=True).start()
        if self.fresh:  # 读完立即渲染, 不必等下一个周期
            self.fresh = False
            self.render(self.data)
            if not self.centered:  # 首次渲染后分组数量确定, 窗口大小变了, 再居中一次
                self.centered = True
                self.center()
        self.root.after(200, self.tick)

    @staticmethod
    def _set(label, text, fg=None):
        """只在内容变化时才更新控件, 避免整页重绘闪烁。"""
        if label.cget("text") != text:
            label.config(text=text)
        if fg is not None and str(label.cget("fg")) != fg:
            label.config(fg=fg)

    def _rows(self, box, rows, empty_text, make_value):
        """动态行: 仅当行集合变化时才重建, 否则只更新数值。rows: [(key, 名称, 数值文本, 颜色)]
        key 须包含名称: 只用序号的话, 数量不变但换了风扇时名称会对不上。"""
        keys = [r[0] for r in rows]
        if getattr(box, "_keys", None) != keys:
            for w in box.winfo_children():
                w.destroy()
            box._keys, box._vals = keys, {}
            if not rows:
                tk.Label(box, text=empty_text, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
            for key, name, _, _ in rows:
                f = tk.Frame(box, bg=CARD)
                f.pack(fill="x", pady="1p")
                tk.Label(f, text=name, bg=CARD, fg=FG, font=(FONT, 10)).pack(side="left")
                box._vals[key] = make_value(f)
        for key, _, text, color in rows:
            self._set(box._vals[key], text, color)

    def render(self, d):
        self._update_gpu(d)
        for k in ("cpu_temp", "gpu_temp"):
            t = d[k]
            self._set(self.values[k], "--" if t is None else f"{t:.0f} °C", temp_color(t))
        for k in ("cpu_load", "gpu_load"):
            u = d[k]
            self._set(self.values[k], "--" if u is None else f"{u:.0f} %")
            self._bar(k[:3], u)
        self._set(self.values["cpu_fan"], "--" if d["cpu_fan"] is None else f"{d['cpu_fan']:.0f} RPM")
        if d["gpu_fan"] is not None:
            gpu_fan = f"{d['gpu_fan']:.0f} RPM"
        elif d["gpu_fan_pct"] is not None:
            gpu_fan = f"{d['gpu_fan_pct']:.0f} %"
        else:
            gpu_fan = "--"
        self._set(self.values["gpu_fan"], gpu_fan)

        self._rows(self.case_box,
                   [((i, n), n, f"{rpm:.0f} RPM", FG if rpm else MUTED)
                    for i, (n, rpm) in enumerate(d["case_fans"])],
                   "未检测到风扇", self._value)
        for kind, box in self.extra_boxes.items():
            self._rows(box,
                       [((i, n), n, f"{t:.0f} °C", temp_color(t))
                        for i, (k, n, t) in enumerate(d["extra_temps"]) if k == kind],
                       "未检测到", self._value)
        pumps = d.get("pumps", [])
        if pumps:
            self._rows(self.pump_box,
                       [((i, n), n, f"{rpm:.0f} RPM", None) for i, (n, rpm) in enumerate(pumps)],
                       "", self._value)
        has = [bool(d["case_fans"]), bool(pumps)] + [any(e[0] == k for e in d["extra_temps"]) for k in self.extra_boxes]
        self._layout(has)
        self._notice(d["error"], d["lhm_down"], d["lhm_running"])

    def _layout(self, has):
        """可选分组有数据才显示; 显示集合变化时按原顺序重新排列。"""
        shown = tuple(has)
        if shown == self.shown:
            return
        self.shown = shown
        for w in self.optional:
            w.pack_forget()
        for w, ok in zip(self.optional, has):
            if ok:
                w.pack(fill="x", pady=("6p", 0), before=self.footer)


if __name__ == "__main__":
    # 独立的任务栏标识, 否则任务栏会归到 pythonw 并显示 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hwmonitor.fan.app")
    try:  # 声明 DPI 感知, 否则缩放 125%/150% 的屏幕上会被系统拉伸而发虚
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().root.mainloop()
