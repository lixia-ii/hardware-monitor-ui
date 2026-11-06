"""硬件监控: 实时显示 CPU/GPU 温度、利用率、内存/显存占用、风扇转速与网速。"""
import ctypes
import json
import logging
import queue
import os
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

import config
import diagnostics
from alerts import AlertEngine
from collector import Collector
from dialogs import Dialogs
from lifecycle import LibreProcess, elevate_ui

import autostart
import hwinfo
import sensors
import tray

ICON = str(config.RESOURCE / "app.ico")
HISTORY_MAX = 17280
DEFAULT_ALERTS = config.DEFAULT_ALERTS

# 深色配色
BG = "#16181d"
CARD = "#20232a"
BORDER = "#2c3039"
TRACK = "#2f333d"
FG = "#e6e8ee"
MUTED = "#8a90a0"
SUB = "#b4b9c5"  # 行名称: 比数值暗、比分组标题亮, 拉开层次
RED = "#f87171"
PIN = "#1e3a5f"  # 置顶按钮开启时的底色
CHIP_HOVER = "#3a3f4b"
ACCENT = "#2563eb"  # 主操作按钮
ERR_BG = "#2a1d21"  # 错误提示条
ERR_BORDER = "#4a2a2f"
FONT = "Microsoft YaHei UI"
NUM = "Segoe UI"  # 数字等宽, 跳动时不抖


def grayscale_text():
    """Tk 建字体固定用 DEFAULT_QUALITY, 跟随系统的 ClearType: 深色底上浅色字两侧会有红蓝彩边, 看着像重影。
    改写 tk86t.dll 导入表里的 CreateFontIndirectW, 把字体质量换成灰度抗锯齿 (只影响本程序)。须在 tk.Tk() 之前调用。"""
    from ctypes import wintypes as wt
    k32, gdi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("gdi32")
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k32.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    real = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(("CreateFontIndirectW", gdi))
    base = k32.GetModuleHandleW("tk86t.dll")
    if not base:
        return

    def u32(off):
        return ctypes.c_uint32.from_address(base + off).value

    nt = u32(0x3C)
    imports = u32(nt + 24 + (112 if ctypes.sizeof(ctypes.c_void_p) == 8 else 96) + 8)  # 数据目录 [1]: 导入表
    ptr = ctypes.sizeof(ctypes.c_void_p)
    while imports and u32(imports + 12):
        if ctypes.string_at(base + u32(imports + 12)).lower() == b"gdi32.dll":
            names, slots = u32(imports), u32(imports + 16)
            for i in range(10000):
                entry = ctypes.c_size_t.from_address(base + names + i * ptr).value
                if not entry:
                    break
                if entry >> (ptr * 8 - 1) or ctypes.string_at(base + entry + 2) != b"CreateFontIndirectW":
                    continue  # 按序号导入的或别的函数

                @ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
                def hook(logfont):
                    lf = ctypes.create_string_buffer(ctypes.string_at(logfont, 92), 92)  # sizeof(LOGFONTW)
                    if lf[26] == b"\x00":  # lfQuality: DEFAULT_QUALITY -> ANTIALIASED_QUALITY
                        lf[26] = b"\x04"
                    return real(lf)
                grayscale_text.hook = hook  # 保持引用, 否则回调被回收
                slot, old = base + slots + i * ptr, wt.DWORD()
                if k32.VirtualProtect(slot, ptr, 0x04, ctypes.byref(old)):  # PAGE_READWRITE
                    ctypes.c_void_p.from_address(slot).value = ctypes.cast(hook, ctypes.c_void_p).value
                    k32.VirtualProtect(slot, ptr, old.value, ctypes.byref(old))
                return
        imports += 20


def set_icon(win):
    try:
        win.iconbitmap(ICON)
    except tk.TclError:
        pass
    try:  # Windows 10/11 深色标题栏, 与界面配色一致
        from ctypes import wintypes as wt
        win.update_idletasks()
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user.GetAncestor.restype = wt.HWND
        hwnd = user.GetAncestor(win.winfo_id(), 2)  # GA_ROOT: this dialog's native wrapper, not its owner.
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
        # Windows 11's explicit caption colors also cover nested/active dialogs
        # when the OS uses a light theme. Older systems ignore these attributes.
        for attribute, color in ((35, CARD), (36, FG)):
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            value = wt.DWORD(red | green << 8 | blue << 16)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def short_gpu(name):
    """显卡名去掉厂商前缀等, 放进小标签: "NVIDIA GeForce RTX 4060" -> "RTX 4060", "Intel(R) UHD Graphics 770" -> "UHD 770"。"""
    s = re.sub(r"\((?:R|TM)\)|\b(?:NVIDIA|AMD|Intel|GeForce|Graphics)\b|\bRadeon\s+(?=RX\b)", " ", name, flags=re.I)
    return " ".join(s.split()) or name


def fmt_rate(b):
    """字节/秒 -> (数字, 单位, 单位颜色), 如 ("12.5", "MB/s", FG)。数到 1000 进一级, 数字最多三位。
    单位按量级变色, 扫一眼就知道大概多快: B/KB 灰 (基本空闲), MB 白, 50 MB/s 以上青, GB 橙。"""
    if b is None:
        return "--", "", MUTED
    for unit in ("B/s", "KB/s", "MB/s"):
        if b < 1000:
            num = f"{b:.1f}" if b < 10 and unit != "B/s" else f"{b:.0f}"
            color = FG if unit == "MB/s" else MUTED
            if unit == "MB/s" and b >= 50:
                color = "#22d3ee"
            return num, unit, color
        b /= 1024
    return f"{b:.1f}", "GB/s", "#fb923c"


def fmt_gb(used, total):
    """已用/总量 (字节) -> "2.4/8.0 GB"。"""
    if used is None or not total:
        return ""
    return f"{used / 1024 ** 3:.1f}/{total / 1024 ** 3:.1f} GB"


def temp_color(t):
    if t is None:
        return MUTED
    return "#4ade80" if t < 60 else "#fbbf24" if t < 80 else RED


def _rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def check_png(size, gap, fill, border, check=None):
    """画勾选框图标, 返回 PNG 字节: 圆角方框 + 可选的对号, 右侧留 gap 像素透明间距。
    clam 主题自带的勾选标记是 ×, 只能换成图片; 不依赖 Pillow, 4x4 超采样抗锯齿后用 zlib 手写 PNG。"""
    import struct
    import zlib
    fill, border = _rgb(fill), _rgb(border)
    check = check and _rgb(check)
    radius, bw, cw = size * 0.2, max(1.0, size / 14), size * 0.13  # 圆角半径、边框宽、对号笔画半宽
    pts = [(size * 0.26, size * 0.52), (size * 0.44, size * 0.70), (size * 0.76, size * 0.33)]

    def seg_dist(px, py, a, b):
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5

    def sample(x, y):
        # 圆角矩形的有向距离: 负数在内部
        qx, qy = abs(x - size / 2) - (size / 2 - radius), abs(y - size / 2) - (size / 2 - radius)
        d = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - radius
        if d > 0:
            return None
        if check and min(seg_dist(x, y, pts[0], pts[1]), seg_dist(x, y, pts[1], pts[2])) < cw:
            return check
        return border if d > -bw else fill

    n = 4
    rows = []
    for py in range(size):
        row = bytearray(b"\x00")  # 每行前的过滤类型字节
        for px in range(size + gap):
            acc, hit = [0, 0, 0], 0
            for sy in range(n):
                for sx in range(n):
                    c = sample(px + (sx + 0.5) / n, py + (sy + 0.5) / n) if px < size else None
                    if c:
                        hit += 1
                        acc = [a + v for a, v in zip(acc, c)]
            row += bytes([a // hit for a in acc] + [255 * hit // (n * n)]) if hit else b"\x00\x00\x00\x00"
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size + gap, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class App(Dialogs):
    def __init__(self):
        self.root = r = tk.Tk()
        # 开机自启 (计划任务带 --autostart) 时直接待在托盘, 不弹窗口
        self.hidden_start = "--autostart" in sys.argv
        if self.hidden_start:
            r.withdraw()
        self.settings = config.load()
        self.root.report_callback_exception = lambda *args: logging.getLogger(__name__).error("Tk callback failed", exc_info=args)
        self.libre = LibreProcess()
        self.launch_until = 0.0
        self.data = None
        self.metric_blocks = {}
        self.metric_titles = {}
        self.sensor_names = {}
        r.title("硬件监控")
        r.configure(bg=BG)
        # 尺寸用点 (p) 而非像素, 高 DPI 下按比例放大。完整界面建好后还会
        # 按实际内容补齐最小高度，避免把窗口缩到卡片或底栏看不见。
        r.minsize(r.winfo_pixels("300p"), r.winfo_pixels("220p"))
        r.resizable(True, True)  # 双向可调；最小尺寸由当前可见内容保证，不能把卡片或底栏拉没
        self._style()
        set_icon(r)

        main = tk.Frame(r, bg=BG, padx="10p", pady="10p")
        main.pack(fill="both", expand=True)

        self.values = {}
        self.bars = {}
        self._devices(main)

        # 网卡实时网速 (所有物理网卡之和): 只有两个数, 直接放在标题行右侧, 整张卡片只占一行
        self.net_box = self._card(main, "网络")
        self.net_box.pack_forget()
        self.net_vals = {}
        head = self.net_box.head
        for key, arrow in (("net_up", "↑"), ("net_down", "↓")):  # 从右往左排
            # 单位小字、随量级变色, 数字粗体; 各自定宽, 位数或单位变化时不跳
            unit = tk.Label(head, text="", bg=CARD, fg=MUTED, font=(NUM, 8, "bold"), width=4, anchor="w")
            unit.pack(side="right", anchor="s", pady=(0, "1p"))
            num = tk.Label(head, text="--", bg=CARD, fg=MUTED, font=(NUM, 11, "bold"), width=4, anchor="e")
            num.pack(side="right", padx=(0, "2p"))
            tk.Label(head, text=arrow, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(
                side="right", padx=("10p" if key == "net_down" else "6p", 0))
            self.net_vals[key] = (num, unit)

        self.case_box = self._card(main, "机箱风扇")

        # 水冷泵: 只有检测到时才显示, 插在机箱风扇下面
        self.pump_box = self._card(main, "水冷泵")

        # 硬盘 / 内存 / 网卡各一项, 可点击标题收起或展开 (记住上次的状态)
        self.extra_boxes = {k: self._card(main, f"{label}温度", collapse_key=k)
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
        self.history_btn = ttk.Button(self.btn_bar, text="历史", command=self.show_history, cursor="hand2")
        self.history_btn.pack(side="right", padx=(0, "6p"))
        self.float_btn = ttk.Button(self.btn_bar, text="悬浮窗", command=self.toggle_floating, cursor="hand2")
        self.float_btn.pack(side="right", padx=(0, "6p"))
        # 窗口置顶开关 (原生标题栏放不了按钮, 放在底栏), 开启时高亮
        self.pin_btn = ttk.Button(self.btn_bar, text="置顶", command=self.toggle_topmost, cursor="hand2")
        self.pin_btn.pack(side="right", padx=(0, "6p"))
        ttk.Button(self.btn_bar, text="设置", command=self.show_settings, cursor="hand2").pack(side="left")
        # 可选分组: 没有数据就不显示。按此顺序排在底栏之前
        self.optional = [b.master for b in (self.net_box, self.case_box, self.pump_box, *self.extra_boxes.values())]
        self.shown = ()

        # 仅当 LibreHardwareMonitor 由 start.bat 拉起时, 关闭本窗口才一并关闭它
        # Ownership lives in LibreProcess; legacy --own-libre never grants permission to kill a process.
        r.protocol("WM_DELETE_WINDOW", self.close)

        # 托盘图标常驻; 点最小化时隐藏到托盘。托盘不可用 (极少见) 时保持普通的最小化和关闭即退出
        self.tray = tray.Tray(ICON, "硬件监控")
        if self.tray.ok:
            # Tk 没有"最小化"事件, 只能监听 <Unmap>; 子控件的 Unmap 也会冒泡上来, 按 widget 过滤
            r.bind("<Unmap>", lambda e: e.widget is r and r.state() == "iconic" and r.withdraw())
        elif self.hidden_start:
            r.iconify()
        if "--self-test" not in sys.argv:
            threading.Thread(target=autostart.sync, daemon=True).start()
        self.gpu_sel = self.settings["gpu_id"]
        self.gpu_cur = None
        self.history = deque(maxlen=HISTORY_MAX)
        self.recent_alerts = deque(maxlen=100)
        self.alert_engine = AlertEngine()
        self.collector = Collector(self.settings)
        self.collector.start()
        self._storage_notice = None
        self._apply_display()
        if "--self-test" not in sys.argv and (self.hidden_start or "--start-libre" in sys.argv or (getattr(sys, "frozen", False) and "--no-start-libre" not in sys.argv)):
            r.after(100, self.launch_libre)
        self.float_win = None
        self._set_float_button()
        if self.settings.get("floating", {}).get("enabled"):
            self.show_floating()
        self.centered = False
        self._update_main_minimum()
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
        for w in ("TCheckbutton", "TRadiobutton"):
            # 单选框: clam 默认的圆圈只有十来个像素, 高 DPI 下太小; 边框调亮, 未选中时在深色底上也看得见 (勾选框见下面的自绘图标)
            s.configure(w, background=BG, foreground=FG, indicatorbackground=CARD, indicatorforeground=FG,
                        indicatorsize=self.root.winfo_pixels("9p"), indicatormargin=(0, 0, "5p", 0),
                        upperbordercolor="#5a6070", lowerbordercolor="#5a6070", focuscolor=BG, padding=(0, "2p"))
            s.map(w, background=[("active", BG)], indicatorbackground=[("selected", ACCENT), ("active", TRACK)],
                  indicatorforeground=[("selected", "white")], foreground=[("disabled", MUTED)],
                  upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
        s.configure("Hint.TLabel", foreground=MUTED)
        # 数值输入框: clam 默认白底, 在深色界面里很突兀
        s.configure("TSpinbox", fieldbackground=CARD, background=TRACK, foreground=FG, insertcolor=FG,
                    arrowcolor=SUB, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                    arrowsize=self.root.winfo_pixels("8p"), padding=("3p", "2p"))
        s.map("TSpinbox", fieldbackground=[("disabled", BG)], foreground=[("disabled", MUTED)],
              background=[("pressed", BORDER), ("active", "#2a2e37")], arrowcolor=[("active", FG)],
              bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
              selectbackground=[("focus", ACCENT)], selectforeground=[("focus", "white")])
        for kind in ("TEntry", "TCombobox"):
            s.configure(kind, fieldbackground=CARD, foreground=FG, background=TRACK,
                        insertcolor=FG, arrowcolor=FG, bordercolor=BORDER, lightcolor=CARD,
                        darkcolor=CARD, padding=("5p", "4p"), arrowsize=self.root.winfo_pixels("10p"))
            s.map(kind, fieldbackground=[("disabled", BG), ("readonly", CARD)],
                  foreground=[("disabled", MUTED), ("readonly", FG)],
                  bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
                  darkcolor=[("focus", ACCENT)], selectbackground=[("focus", ACCENT)],
                  selectforeground=[("focus", "white")])
        s.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG)
        s.configure("TNotebook.Tab", background=CARD, foreground=SUB, padding=("14p", "7p"),
                    bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD, focuscolor=TRACK)
        s.map("TNotebook.Tab", background=[("selected", TRACK), ("active", BORDER)],
              foreground=[("selected", FG)], lightcolor=[("selected", TRACK), ("active", BORDER), ("!selected", CARD)],
              darkcolor=[("selected", TRACK), ("active", BORDER), ("!selected", CARD)],
              bordercolor=[("selected", TRACK), ("active", BORDER), ("!selected", CARD)])
        # Native clam drawing avoids repeatedly tiling a 1-pixel image on Windows.
        s.map("TNotebook.Tab", expand=[("selected", (0, 0, 0, 0))],
              padding=[("selected", ("14p", "7p")), ("!selected", ("14p", "7p"))])
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                    rowheight=self.root.winfo_pixels("24p"), borderwidth=0,
                    bordercolor=CARD, lightcolor=CARD, darkcolor=CARD, relief="flat")
        s.map("Treeview", background=[("selected", PIN)], foreground=[("selected", "#bfdbfe")])
        s.configure("Treeview.Heading", background=TRACK, foreground=SUB,
                    bordercolor=BORDER, lightcolor=TRACK, darkcolor=TRACK, padding=("6p", "5p"))
        s.map("Treeview.Heading", background=[("active", BORDER)], relief=[("active", "flat")])
        s.configure("Vertical.TScrollbar", background=TRACK, troughcolor=BG,
                    bordercolor=BG, lightcolor=TRACK, darkcolor=TRACK, arrowcolor=SUB)
        s.map("Vertical.TScrollbar", background=[("active", BORDER), ("pressed", MUTED)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", PIN)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG)
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 9))
        # 勾选框换成自绘图标 (clam 自带的选中标记是 ×, 不是对号)
        size, gap, border = self.root.winfo_pixels("9p"), self.root.winfo_pixels("5p"), "#5a6070"
        img = lambda *a: tk.PhotoImage(data=check_png(size, gap, *a), format="png")
        self._check_imgs = (img(CARD, border), img(ACCENT, ACCENT, "#ffffff"), img(TRACK, border),
                            img(BG, BORDER), img(TRACK, TRACK, MUTED))  # 未选、选中、悬停、禁用、禁用且选中
        off, on, hover, dis, dis_on = self._check_imgs
        s.element_create("Check.indicator", "image", off, ("disabled", "selected", dis_on), ("disabled", dis),
                         ("selected", on), ("active", hover))
        s.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
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

    def launch_libre(self):
        if time.monotonic() < self.launch_until:
            return
        exe = config.RUNTIME / "LibreHardwareMonitor" / "LibreHardwareMonitor.exe"
        if not exe.exists():  # Compatible with an older portable installation.
            exe = config.BASE / "LibreHardwareMonitor" / "LibreHardwareMonitor.exe"
        if not exe.exists():
            self._flash("找不到 LibreHardwareMonitor，请先运行 start.bat 完成安装。")
            return
        self.launch_until = float("inf")
        self._update_launch_btn(False)
        def job():
            if not sensors.lhm_running(force=True):
                self.libre.launch(exe)
            return True
        def done(ok):
            self.launch_until = time.monotonic() + 6 if ok else 0
            self.collector.configure(self.settings)
            if not ok:
                self._flash("启动 LibreHardwareMonitor 失败或已取消")
        self._background(self.root, job, done)

    def _save_settings(self):
        try:
            config.save(self.settings)
            return True
        except (OSError, ValueError):
            logging.getLogger(__name__).exception("Settings save failed")
            self._flash("设置保存失败，请检查程序目录的写入权限；本次运行仍使用新设置。")
            return False

    def _update_launch_btn(self, running):
        """进程已在运行 (还在启动, 网页服务没起来) 或刚点过启动时置灰。"""
        busy = running or time.monotonic() < self.launch_until
        self.launch_btn.state(["disabled" if busy else "!disabled"])
        self._set(self.launch_btn, "启动中…" if busy else "启动")

    def _flash(self, msg):
        """临时消息显示 5 秒, 否则下一次渲染会立刻被读取到的错误覆盖。"""
        self.status_msg = (msg, time.monotonic() + 5)
        self._set(self.status, msg)
        d = self.data or {}
        self._notice(d.get("error"), d.get("lhm_down", False), d.get("lhm_running", False))

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
            self._update_main_minimum()
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
        mw, mh = max(w.winfo_width(), w.minsize()[0]), max(w.winfo_height(), w.minsize()[1])
        x = self.root.winfo_x() + (self.root.winfo_width() - mw) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - mh) // 2
        w.geometry(f"+{max(0, min(x, w.winfo_screenwidth() - mw - 20))}+{max(0, min(y, w.winfo_screenheight() - mh - 70))}")

    def center(self):
        """窗口居中到屏幕。"""
        r = self.root
        r.update_idletasks()
        w, h = r.winfo_reqwidth(), r.winfo_reqheight()
        x = (r.winfo_screenwidth() - w) // 2
        y = (r.winfo_screenheight() - h) // 2
        r.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _update_main_minimum(self):
        """按当前可见内容更新主窗口的最小尺寸。"""
        r = self.root
        r.update_idletasks()
        r.minsize(max(r.winfo_pixels("300p"), r.winfo_reqwidth()),
                  max(r.winfo_pixels("220p"), r.winfo_reqheight()))

    def show_window(self):
        r = self.root
        r.deiconify()
        r.state("normal")
        # 从托盘恢复时先让 Tk 完成尺寸计算和首帧绘制，减少短暂的白块与空白区域。
        r.update_idletasks()
        r.lift()
        r.focus_force()
        if self.data:
            self.render(self.data)

    def _check_alerts(self, d):
        for msg in self.alert_engine.check(d, self.settings["alerts"], time.monotonic()):
            self.recent_alerts.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": msg})
            logging.getLogger(__name__).warning(msg)
            if not self.tray.notify("硬件告警", msg):
                self._flash(msg)

    def _set_float_button(self):
        active = self.float_win is not None and self.float_win.winfo_exists()
        self.float_btn.config(style="On.TButton" if active else "TButton")
        var = getattr(self, "_float_var", None)
        if var is not None:  # 设置窗里的勾选框跟随悬浮窗实际状态 (如从悬浮窗 × 关闭)
            var.set(active)

    def toggle_floating(self):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.hide_floating()
        else:
            self.show_floating()

    def show_floating(self):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.float_win.deiconify()
            self.float_win.lift()
            return
        saved = self.settings.get("floating", {})
        w = self.float_win = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BORDER)
        x, y = saved.get("x", 40), saved.get("y", 40)
        w.geometry(f"+{x}+{y}")
        shell = tk.Frame(w, bg=CARD, padx="8p", pady="5p")
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(shell, bg=CARD)
        head.pack(fill="x")
        tk.Label(head, text="硬件监控", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(side="left")
        close = tk.Label(head, text="×", bg=CARD, fg=MUTED, font=(FONT, 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _: self.hide_floating())
        self.float_values = {}
        for key, name, color in (("cpu", "CPU", "#60a5fa"), ("gpu", "显卡", "#a78bfa")):
            row = tk.Frame(shell, bg=CARD)
            row.pack(fill="x", pady=("3p", 0))
            tk.Label(row, text=name, bg=CARD, fg=SUB, font=(FONT, 9)).pack(side="left")
            v = tk.Label(row, text="--", bg=CARD, fg=color, font=(NUM, 13, "bold"))
            v.pack(side="right")
            self.float_values[key] = v

        drag = {}
        def begin(event):
            drag["x"], drag["y"] = event.x_root - w.winfo_x(), event.y_root - w.winfo_y()
        def move(event):
            if drag:
                w.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")
        def finish(_):
            drag.clear()
            self._save_floating_geometry()
        for widget in (head, *head.winfo_children()):
            if widget is close:
                continue
            widget.bind("<ButtonPress-1>", begin)
            widget.bind("<B1-Motion>", move)
            widget.bind("<ButtonRelease-1>", finish)
        floating = self.settings.setdefault("floating", {})
        floating["enabled"] = True
        self._save_settings()
        self._set_float_button()
        if self.data:
            self._update_floating(self.data)

    def _save_floating_geometry(self):
        if self.float_win is None or not self.float_win.winfo_exists():
            return
        floating = self.settings.setdefault("floating", {})
        floating.update(enabled=True, x=self.float_win.winfo_x(), y=self.float_win.winfo_y())
        self._save_settings()

    def hide_floating(self):
        self._save_floating_geometry()
        if self.float_win is not None and self.float_win.winfo_exists():
            self.float_win.destroy()
        self.float_win = None
        self.settings.setdefault("floating", {})["enabled"] = False
        self._save_settings()
        self._set_float_button()

    def _update_floating(self, d):
        if self.float_win is None or not self.float_win.winfo_exists():
            return
        for key, temp_key, load_key in (("cpu", "cpu_temp", "cpu_load"), ("gpu", "gpu_temp", "gpu_load")):
            temp, load = d.get(temp_key), d.get(load_key)
            text = "--" if temp is None else f"{temp:.0f}°C"
            if load is not None:
                text += f"  {load:.0f}%"
            self._set(self.float_values[key], text, temp_color(temp))

    def _dialog(self, title, parent=None, resizable=False):
        """深色模态小窗。返回 (窗口, 内容框架); 内容填好后调用 _show_dialog。"""
        parent = parent or self.root
        w = tk.Toplevel(parent)
        w.withdraw()  # 摆好位置再显示, 免得先在左上角闪一下
        w.title(title)
        w.configure(bg=BG)
        w.resizable(resizable, resizable)
        w.transient(parent)
        w.attributes("-topmost", self.root.attributes("-topmost"))
        set_icon(w)
        body = ttk.Frame(w, padding=("16p", "14p", "16p", "12p"))
        body.pack(fill="both", expand=True)
        w.bind("<Escape>", lambda _: w.destroy())
        return w, body

    def _show_dialog(self, w):
        previous = self.root.grab_current()
        def restore_grab(event):
            if event.widget is w and previous and previous.winfo_exists():
                previous.grab_set()
                previous.focus_set()
        w.bind("<Destroy>", restore_grab, add="+")
        self.center_on_main(w)
        w.deiconify()
        set_icon(w)  # Mapping a resizable/transient dialog can recreate its native frame.
        w.grab_set()
        w.focus_set()

    def close(self):
        """点 x: 托盘可用时按设置最小化到托盘或退出, 没选过就询问。"""
        action = self.settings.get("close_action", "ask") if self.tray.ok else "exit"
        if action == "ask":
            action = self._ask_close()
        if action == "tray":
            self.root.withdraw()
        elif action == "exit":
            self.quit()

    def _ask_close(self):
        """询问退出还是最小化到托盘。返回 "tray" / "exit", 取消返回 None。"""
        w, body = self._dialog("关闭硬件监控")
        result = []
        remember = tk.BooleanVar(value=False)
        ttk.Label(body, text="要退出程序, 还是最小化到托盘?", font=(FONT, 10)).pack(anchor="w")
        ttk.Label(body, text="最小化后点击右下角托盘图标可恢复窗口。", style="Hint.TLabel").pack(
            anchor="w", pady=("4p", "10p"))
        ttk.Checkbutton(body, text="记住我的选择 (可在「设置」中修改)", variable=remember,
                        cursor="hand2").pack(anchor="w", pady=(0, "12p"))
        btns = ttk.Frame(body)
        btns.pack(fill="x")

        def choose(action):
            result.append(action)
            if remember.get():
                self.settings["close_action"] = action
                self._save_settings()
            w.destroy()
        ttk.Button(btns, text="退出", style="Accent.TButton", command=lambda: choose("exit"),
                   cursor="hand2").pack(side="right")
        ttk.Button(btns, text="最小化到托盘", command=lambda: choose("tray"),
                   cursor="hand2").pack(side="right", padx=(0, "6p"))
        self._show_dialog(w)
        w.wait_window()
        return result[0] if result else None

    def quit(self):
        if getattr(self, "closing", False):
            return
        self.closing = True
        self._save_floating_geometry()
        self.tray.destroy()
        self.root.withdraw()
        self.collector.stop()
        def stop():
            # A UAC launch can still be pending. Wait for it before closing its owned handle.
            while self.launch_until == float("inf"):
                time.sleep(0.1)
            self.libre.stop()
            self.collector.join(timeout=8)
            return True
        self._background(self.root, stop, lambda _: self.root.destroy())

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

    def _devices(self, parent):
        """CPU / 显卡各一张卡片: 大字温度, 下面是利用率、内存/显存占用 (带进度条) 和风扇。"""
        row = tk.Frame(parent, bg=BG)
        # 多出的纵向空间交给两张主卡片，窗口上下左右拉伸都会同步适应。
        row.pack(fill="both", expand=True)
        row.rowconfigure(0, weight=1)
        for i, (dev, name, accent) in enumerate((("cpu", "CPU", "#60a5fa"), ("gpu", "显卡", "#a78bfa"))):
            row.columnconfigure(i, weight=1, uniform="dev")
            card = tk.Frame(row, bg=CARD, padx="10p", pady="8p", highlightthickness=1, highlightbackground=BORDER)
            # 两侧各留一半间距, 两张卡片才一样宽 (padx 也算在 uniform 列宽里)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, "3p") if i == 0 else ("3p", 0))
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x")
            title_label = tk.Label(head, text=name, bg=CARD, fg=MUTED, font=(FONT, 10, "bold"))
            title_label.pack(side="left")
            self.metric_titles[dev + "_temp"] = title_label
            if dev == "gpu":  # 多显卡时才显示的切换标签, 在卡片右上角, 点击切到下一块
                # 不用 Menubutton: Windows 原生下拉菜单不认配色, 在深色界面里是一块白
                chip = self.gpu_chip = tk.Label(head, bg=TRACK, fg=FG, font=(FONT, 9), padx="5p", cursor="hand2")
                chip.bind("<Button-1>", self._next_gpu)
                chip.bind("<Enter>", lambda _: chip.config(bg=CHIP_HOVER))
                chip.bind("<Leave>", lambda _: chip.config(bg=TRACK))
                self.gpu_list = []
            temp = tk.Frame(card, bg=CARD)
            temp.pack(anchor="w", pady=("2p", "2p"))
            self.metric_blocks[dev + "_temp"] = [temp]
            self.values[f"{dev}_temp"] = tk.Label(temp, text="--", bg=CARD, fg=MUTED, font=(NUM, 24, "bold"))
            self.values[f"{dev}_temp"].pack(side="left")
            # 单位用小字, 底部对齐到数字基线附近
            self.values[f"{dev}_temp_unit"] = tk.Label(temp, text="°C", bg=CARD, fg=MUTED, font=(NUM, 12, "bold"))
            self.values[f"{dev}_temp_unit"].pack(side="left", anchor="s", pady=(0, "6p"))
            for kind, title in (("load", "利用率"), ("mem_load", "内存" if dev == "cpu" else "显存"), ("fan", "风扇")):
                key = f"{dev}_{kind}" if dev == "gpu" or kind != "mem_load" else "mem_load"
                f = tk.Frame(card, bg=CARD)
                f.pack(fill="x", pady=("4p", 0))
                title_label = tk.Label(f, text=title, bg=CARD, fg=SUB, font=(FONT, 9))
                title_label.pack(side="left")
                self.metric_titles[key] = title_label
                self.metric_blocks[key] = [f]
                self.values[key] = self._value(f)
                if kind == "mem_load":  # 已用/总量, 小字放在百分比左边
                    self.values[key + "_gb"] = tk.Label(f, text="", bg=CARD, fg=MUTED, font=(NUM, 8))
                    self.values[key + "_gb"].pack(side="right", padx=(0, "4p"))
                if kind != "fan":
                    track = tk.Frame(card, bg=TRACK, height="3p")
                    track.pack(fill="x", pady=("3p", 0))
                    self.metric_blocks[key].append(track)
                    bar = tk.Frame(track, bg=accent)
                    bar.rel = 0
                    self.bars[key] = bar

    def _next_gpu(self, _=None):
        ids = [h for h, _ in self.gpu_list]
        if len(ids) < 2:
            return
        cur = self.gpu_sel if self.gpu_sel in ids else self.gpu_cur
        self.gpu_sel = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        self._show_gpu(self.gpu_sel)  # 标签先切过去, 数值等下一次读取
        self.settings["gpu_id"] = self.gpu_sel
        self._save_settings()
        self.collector.configure(self.settings)

    def _show_gpu(self, hwid):
        self.gpu_cur = hwid
        name = next((n for h, n in self.gpu_list if h == hwid), "")
        multi = len(self.gpu_list) > 1  # 只有一块时只显示型号, 不带切换箭头
        self._set(self.gpu_chip, f"{short_gpu(name)}  ⇄" if multi else short_gpu(name))
        self.gpu_chip.config(cursor="hand2" if multi else "")

    def _update_gpu(self, d):
        """显示显卡型号标签: 多块时可点击切换, 一块时只显示型号, 没有显卡就隐藏。"""
        self.gpu_list = d["gpus"]
        if self.gpu_list:
            self._show_gpu(d["gpu_id"] if d["gpu_id"] else self.gpu_list[0][0])
            if not self.gpu_chip.winfo_manager():
                self.gpu_chip.pack(side="right")
        elif self.gpu_chip.winfo_manager():
            self.gpu_chip.pack_forget()

    def _card(self, parent, title, collapse_key=None):
        """分组卡片, 返回放内容的 body 框架 (body.master 是卡片本身)。
        collapse_key: 点击标题收起/展开, 状态以此为键存进设置, 下次启动保持; 第一次默认展开。"""
        card = tk.Frame(parent, bg=CARD, padx="10p", pady="7p", highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        title_label = tk.Label(head, text=title, bg=CARD, fg=MUTED, font=(FONT, 9, "bold"))
        title_label.pack(side="left")
        body = tk.Frame(card, bg=CARD)
        body.head = head
        body.title_label = title_label
        collapsed = collapse_key in self.settings.get("collapsed", [])
        if not collapsed:
            body.pack(fill="x", pady=("4p", 0))
        if collapse_key:
            arrow = tk.Label(head, text="▸" if collapsed else "▾", bg=CARD, fg=MUTED, font=(FONT, 12))
            arrow.pack(side="right")

            def toggle(_=None):
                keys = set(self.settings.get("collapsed", []))
                if body.winfo_manager():
                    body.pack_forget()
                    arrow.config(text="▸")
                    keys.add(collapse_key)
                else:
                    body.pack(fill="x", pady=("4p", 0))
                    arrow.config(text="▾")
                    keys.discard(collapse_key)
                self.settings["collapsed"] = sorted(keys)
                self._save_settings()
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

    def _bar(self, key, pct):
        bar = self.bars[key]
        rel = 0 if pct is None else max(0.0, min(pct, 100.0)) / 100
        if bar.rel != rel:
            bar.rel = rel
            if rel:
                bar.place(x=0, y=0, relheight=1, relwidth=rel)
            else:  # relwidth=0 仍会画出 1 像素, 直接隐藏
                bar.place_forget()

    def _tray_events(self):
        """处理托盘点击。选了退出返回 False。"""
        for ev in self.tray.poll():
            if ev == "menu":
                ev = self.tray.menu()
            if ev == "show":
                self.show_window()
            elif ev == "quit":
                self.quit()
                return False
        return True

    def tick(self):
        if getattr(self, "closing", False):
            return
        if not self._tray_events():
            return
        while not self.collector.history_rows.empty():
            action, rows = self.collector.history_rows.get()
            if action == "load":
                self.history.extend(rows)
            else:
                self.history.append(rows)
        try:
            data = self.collector.results.get_nowait()
        except queue.Empty:
            data = None
        if data and data["selection"] == (self.settings["gpu_id"], self.settings["nic_ids"]):
            self.data = data
            self._check_alerts(data)
            self._update_floating(data)
            temps = [f"{name} {data[k]:.0f}°C" for k, name in (("cpu_temp", "CPU"), ("gpu_temp", "显卡")) if data[k] is not None]
            self.tray.set_tip("\n".join(["硬件监控", *temps]))
            if self.root.state() == "normal" and self.root.winfo_viewable():
                self.render(data)
                if not self.centered:
                    self.centered = True
                    self.center()
        error = self.collector.storage_error
        if error and error != self._storage_notice:
            self._storage_notice = error
            self._flash(error)
        self.root.after(100, self.tick)  # 也负责响应托盘点击, 间隔不宜太长

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
        group = getattr(box, "pref_group", "other")
        filtered = []
        for index, (key, name, text, color) in enumerate(rows):
            pref_key = f"sensor:{group}:{key}"
            # Old settings used an enumeration index. Migrate only an unambiguous
            # name; identical devices need their own stable hardware identity.
            prefix = f"sensor:{group}:{name}:"
            old = [k for k in self.settings["display"] if k.startswith(prefix) and k[len(prefix):].isdigit()]
            if pref_key not in self.settings["display"] and len(old) == 1 and sum(r[1] == name for r in rows) == 1:
                self.settings["display"][pref_key] = self.settings["display"].pop(old[0])
            self.sensor_names[pref_key] = name
            pref = self.settings["display"].get(pref_key, {})
            if pref.get("visible", True):
                filtered.append((pref.get("order", 100), index, (key, pref.get("name") or name, text, color)))
        rows = [r for _, _, r in sorted(filtered)]
        keys = [(r[0], r[1]) for r in rows]
        if getattr(box, "_keys", None) != keys:
            for w in box.winfo_children():
                w.destroy()
            box._keys, box._vals = keys, {}
            if not rows:
                tk.Label(box, text=empty_text, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
            for key, name, _, _ in rows:
                f = tk.Frame(box, bg=CARD)
                f.pack(fill="x", pady="1p")
                tk.Label(f, text=name, bg=CARD, fg=SUB, font=(FONT, 9)).pack(side="left")
                box._vals[key] = make_value(f)
        for key, _, text, color in rows:
            self._set(box._vals[key], text, color)

    def render(self, d):
        self._update_gpu(d)
        for k in ("cpu_temp", "gpu_temp"):
            t = d[k]
            self._set(self.values[k], "--" if t is None else f"{t:.0f}", temp_color(t))
            self._set(self.values[k + "_unit"], "°C", temp_color(t))
        for k in ("cpu_load", "gpu_load", "mem_load", "gpu_mem_load"):
            u = d[k]
            self._set(self.values[k], "--" if u is None else f"{u:.0f} %", MUTED if u is None else FG)
            self._bar(k, u)
        for k, used, total in (("mem_load", "mem_used", "mem_total"), ("gpu_mem_load", "gpu_mem_used", "gpu_mem_total")):
            self._set(self.values[k + "_gb"], fmt_gb(d[used], d[total]))
        for k, (num, unit) in self.net_vals.items():
            n, u, color = fmt_rate(d[k])
            self._set(num, n, MUTED if d[k] is None else FG)  # 数字固定白色, 只有单位随量级变色
            self._set(unit, u, color)
        cpu_fan = d["cpu_fan"]
        self._set(self.values["cpu_fan"], "--" if cpu_fan is None else f"{cpu_fan:.0f} RPM", MUTED if cpu_fan is None else FG)
        if d["gpu_fan"] is not None:
            gpu_fan = f"{d['gpu_fan']:.0f} RPM"
        elif d["gpu_fan_pct"] is not None:
            gpu_fan = f"{d['gpu_fan_pct']:.0f} %"
        else:
            gpu_fan = "--"
        self._set(self.values["gpu_fan"], gpu_fan, MUTED if gpu_fan == "--" else FG)

        def sensor_id(group, index, name):
            ids = d.get("sensor_ids", {}).get(group, [])
            return ids[index] if index < len(ids) else "legacy:" + name
        self._rows(self.case_box,
                   [(sensor_id("case_fans", i, n), n, f"{rpm:.0f} RPM", FG if rpm else MUTED)
                    for i, (n, rpm) in enumerate(d["case_fans"])],
                   "未检测到风扇", self._value)
        for kind, box in self.extra_boxes.items():
            self._rows(box,
                       [(sensor_id("extra_temps", i, n), n, f"{t:.0f} °C", temp_color(t))
                        for i, (k, n, t) in enumerate(d["extra_temps"]) if k == kind],
                       "未检测到", self._value)
        pumps = d.get("pumps", [])
        if pumps:
            self._rows(self.pump_box,
                       [(sensor_id("pumps", i, n), n, f"{rpm:.0f} RPM", None) for i, (n, rpm) in enumerate(pumps)],
                       "", self._value)
        has = [d["net_up"] is not None or d["net_down"] is not None, bool(d["case_fans"]), bool(pumps)] + [any(e[0] == k for e in d["extra_temps"]) for k in self.extra_boxes]
        self._layout(has)
        self._notice(d["error"], d["lhm_down"], d["lhm_running"])
        temps = [f"{name} {d[k]:.0f}°C" for k, name in (("cpu_temp", "CPU"), ("gpu_temp", "显卡")) if d[k] is not None]
        self.tray.set_tip("\n".join(["硬件监控", *temps]))  # 托盘悬停即可看温度, 不用打开窗口

    def _apply_display(self):
        from dialogs import METRIC_NAMES, GROUP_NAMES
        prefs = self.settings["display"]
        for key, widgets in self.metric_blocks.items():
            for widget in widgets:
                if not hasattr(widget, "saved_pack"):
                    widget.saved_pack = widget.pack_info()
                widget.pack_forget()
        for key in sorted(self.metric_blocks, key=lambda k: prefs.get(k, {}).get("order", 100)):
            p = prefs.get(key, {})
            if p.get("visible", True):
                for widget in self.metric_blocks[key]:
                    widget.pack(**widget.saved_pack)
            if key in self.metric_titles:
                self.metric_titles[key].config(text=p.get("name") or METRIC_NAMES[key])
        self.groups = dict(zip(GROUP_NAMES, (self.net_box, self.case_box, self.pump_box, *self.extra_boxes.values())))
        for key, box in self.groups.items():
            box.pref_group = key
            box.title_label.config(text=prefs.get(key, {}).get("name") or GROUP_NAMES[key])
            box._keys = None
        self.shown = None

    def _layout(self, has):
        prefs = self.settings["display"]
        keys = list(self.groups)
        shown = tuple(ok and prefs.get(key, {}).get("visible", True) for key, ok in zip(keys, has))
        if shown == self.shown:
            return
        self.shown = shown
        for w in self.optional:
            w.pack_forget()
        order = sorted(zip(keys, self.optional, shown), key=lambda row: prefs.get(row[0], {}).get("order", 100))
        for _, w, ok in order:
            if ok:
                w.pack(fill="x", pady=("6p", 0), before=self.footer)
        self._update_main_minimum()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        config.DATA = config.BASE / ".build" / "self-test"
        tray.APP_ID += "SelfTest"
    diagnostics.setup_logging()
    if "--self-test" not in sys.argv:
        try:
            if elevate_ui(__file__):
                sys.exit(0)
        except OSError:
            logging.getLogger(__name__).exception("UI elevation cancelled or failed")
            sys.exit(1)
    # 独立的任务栏标识, 否则任务栏会归到 pythonw 并显示 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hwmonitor.fan.app")
    # 声明 DPI 感知, 否则缩放 125%/150% 的屏幕上会被系统拉伸而发虚。
    # 开机自启时进程在登录早期启动, 系统级感知 (1) 取到的可能是未就绪的 DPI (如 96), 之后被拉伸; 优先用 per-monitor v2, 按显示器实际 DPI。
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        ok = False
    if not ok:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
    if not tray.acquire_single_instance():  # 已在运行 (如开机自启后又双击了 start.bat): 唤出那个窗口后退出
        sys.exit(0)
    if "--self-test" not in sys.argv:
        try:
            config.migrate_legacy_data()
        except OSError:
            logging.getLogger(__name__).exception("Cannot migrate old data directory")
    tray.dark_menus()
    try:
        grayscale_text()
    except Exception:
        pass
    application = App()
    if "--self-test" in sys.argv:
        errors = []
        application.root.report_callback_exception = lambda *args: errors.append(str(args))
        def finish_test():
            for child in application.root.winfo_children():
                if isinstance(child, tk.Toplevel):
                    child.destroy()
            payload = {"version": config.VERSION, "errors": errors, "sampled": application.data is not None,
                       "icon_available": os.path.exists(ICON)}
            (config.DATA / "self-test.json").write_text(json.dumps(payload), encoding="utf-8")
            application.quit()
        application.root.after(1000, application.show_history)
        application.root.after(1500, application.show_settings)
        application.root.after(4000, finish_test)
    application.root.mainloop()
