"""桌面悬浮窗: 圆角小卡片显示 CPU/显卡温度与利用率、内存和网速。整窗可拖动, 松手时靠近屏幕边缘自动吸附;
贴住左/右/上边后鼠标移开会缩进屏幕外, 只露出一条按 CPU 温度着色的细边, 鼠标碰到再滑出;
两块屏幕相接的边也能缩, 改为原地淡入淡出 (不滑到隔壁屏幕上), 细边要鼠标停一下才出来, 免得跨屏时误触。
置顶时如果所在屏幕被全屏程序 (游戏、视频、演示) 占着就暂时隐藏, 退出全屏后再出来。
右键菜单切换置顶、贴边隐藏、显示项目和不透明度, 选项保存在 settings["floating"]。"""
import ctypes
from ctypes import wintypes as wt
import time
import tkinter as tk
from types import SimpleNamespace

from hwmonitor.ui.display import fmt_gb, fmt_rate, temp_color
from hwmonitor.ui.theme import BORDER, CARD, FG, FONT, MUTED, NUM, SUB, TRACK

SNAP = "24p"  # 松手时离工作区边缘这么近就吸附 (Tk 距离, 随 DPI 缩放)
STRIP = 4  # 隐藏后露出的细边宽度
HIDE_DELAY = 0.5  # 鼠标离开多久后缩进去 (秒)
SHARED_DWELL, SHARED_PAD = 0.1, "16p"  # 两块屏幕相接处的细边: 鼠标在附近停留多久才出来 (跨屏划过不触发), 感应范围向两侧放宽多少
POLL_MS = 100  # 轮询鼠标位置判断是否离开; 缩进后细边的 <Enter> 直接触发滑出, 不等轮询
SLIDE_TIME, FRAME_MS = 0.15, 8  # 滑动时长 (秒) 按实际流逝时间算, 不受系统定时器精度 (约 15.6ms) 影响
OPACITIES = (1.0, 0.9, 0.8, 0.7)
FULLSCREEN_EVERY = 5  # 每隔几次轮询检查一次前台是否全屏 (约 0.5 秒)
DESKTOP_CLASSES = {"Progman", "WorkerW"}
TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def snap(x, y, w, h, work, dist):
    """松手位置 -> (x, y, 贴住的边)。先把窗口收回工作区 (work = 左, 上, 右, 下), 再吸附到 dist 像素以内的边。
    左右优先于上边; 底边只吸附不算贴边, 不在任务栏上方缩进去。"""
    left, top, right, bottom = work
    x, y = max(left, min(x, right - w)), max(top, min(y, bottom - h))
    edge = None
    if x - left <= dist:
        x, edge = left, "left"
    elif right - w - x <= dist:
        x, edge = right - w, "right"
    if y - top <= dist:
        y, edge = top, edge or "top"
    elif bottom - h - y <= dist:
        y = bottom - h
    return x, y, edge


def tucked(x, y, w, h, edge, work):
    """缩进屏幕外时整窗的位置 (只露出 STRIP 像素), 滑动动画的终点。"""
    left, top, right, _ = work
    return {"left": (left - w + STRIP, y), "right": (right - STRIP, y), "top": (x, top - h + STRIP)}[edge]


def strip_rect(x, y, w, h, edge, work):
    """缩进后细边的 (x, y, 宽, 高)。"""
    left, top, right, _ = work
    return {"left": (left, y, STRIP, h), "right": (right - STRIP, y, STRIP, h), "top": (x, top, w, STRIP)}[edge]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _monitor_at(x, y, nearest=True):
    user = ctypes.windll.user32
    user.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    user.MonitorFromPoint.restype = ctypes.c_void_p
    return user.MonitorFromPoint(wt.POINT(int(x), int(y)), 2 if nearest else 0)  # MONITOR_DEFAULTTONEAREST / NULL


def work_area(win, x, y):
    """点 (x, y) 所在 (或最近) 显示器去掉任务栏后的区域 (左, 上, 右, 下)。多屏、任务栏在任意一边都适用。"""
    try:
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        user = ctypes.windll.user32
        user.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        if user.GetMonitorInfoW(_monitor_at(x, y), ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    except (AttributeError, OSError):
        pass
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight()


def edge_is_free(edge, work):
    """这条边外侧没有别的显示器 (也不是任务栏)。否则滑进去会露在隔壁屏幕上, 改为原地淡入淡出。"""
    left, top, right, bottom = work
    mid_x, mid_y = (left + right) // 2, (top + bottom) // 2
    x, y = {"left": (left - 1, mid_y), "right": (right, mid_y), "top": (mid_x, top - 1)}[edge]
    try:
        return not _monitor_at(x, y, nearest=False)
    except (AttributeError, OSError):
        return True


def covers(rect, monitor):
    """窗口矩形 (左, 上, 右, 下) 盖满整个显示器。普通最大化只占工作区, 任务栏还露着, 不算。"""
    return rect[0] <= monitor[0] and rect[1] <= monitor[1] and rect[2] >= monitor[2] and rect[3] >= monitor[3]


def fullscreen_in(windows, monitor):
    """windows 按 Z 序从上往下排 (rect, cls, topmost, framed_max)。这块屏幕上最上面的普通窗口盖满整块屏幕就是全屏,
    跟焦点在哪块屏幕无关。压在上面的置顶小窗 (通知、别的悬浮工具) 和任务栏跳过; 遇到没盖满的普通窗口、
    桌面 (点桌面时它也盖满屏幕) 或带标题栏的最大化窗口 (任务栏自动隐藏时也盖满) 就不是。
    全屏程序自己的附属小窗 (远程桌面顶部的连接栏、播放器控制条) 也压在它上面: 有 owner 的先记下,
    最后盖满屏幕的窗口正好是它们的 owner 才算全屏。"""
    owners = set()
    for w in windows:
        r = w.rect
        if r[2] <= monitor[0] or r[0] >= monitor[2] or r[3] <= monitor[1] or r[1] >= monitor[3]:
            continue  # 不在这块屏幕上
        if w.cls in TASKBAR_CLASSES:
            continue
        if w.cls in DESKTOP_CLASSES:
            return False
        if covers(r, monitor):
            return not w.framed_max and owners <= {getattr(w, "hwnd", None)}
        owner = getattr(w, "owner", None)
        if owner:
            owners.add(owner)
        elif not w.topmost:
            return False
    return False


def _windows(skip):
    """所有看得见的顶层窗口, 按 Z 序从上往下。跳过 skip (悬浮窗自己)、最小化、被 DWM 隐藏 (别的虚拟桌面、
    后台 UWP) 和鼠标可穿透的透明覆盖层。"""
    user, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    for name in ("IsWindowVisible", "IsIconic", "IsZoomed"):
        getattr(user, name).argtypes = [wt.HWND]
    user.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user.GetAncestor.restype = wt.HWND
    dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
    out = []
    def visit(hwnd, _):
        if hwnd == skip or not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd):
            return True
        cloaked = wt.DWORD()
        if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return True  # DWMWA_CLOAKED
        ex = user.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        if ex & 0x20:  # WS_EX_TRANSPARENT
            return True
        rect = wt.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rect)) or rect.right <= rect.left or rect.bottom <= rect.top:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user.GetClassNameW(hwnd, cls, 64)
        framed = user.GetWindowLongW(hwnd, -16) & 0x00C00000 == 0x00C00000  # GWL_STYLE, WS_CAPTION
        root_owner = user.GetAncestor(hwnd, 3)  # GA_ROOTOWNER: 附属窗口最顶层的 owner, 没有 owner 时是自己
        out.append(SimpleNamespace(rect=(rect.left, rect.top, rect.right, rect.bottom), cls=cls.value,
                                   topmost=bool(ex & 0x8), framed_max=framed and bool(user.IsZoomed(hwnd)),
                                   hwnd=hwnd, owner=root_owner if root_owner and root_owner != hwnd else None))
        return True
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(visit)
    user.EnumWindows(proc, 0)
    return out


def fullscreen_app_on(win, x, y):
    """点 (x, y) 所在显示器正被全屏程序占着 (不管焦点在不在它上面)。"""
    try:
        user = ctypes.windll.user32
        user.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not user.GetMonitorInfoW(_monitor_at(x, y), ctypes.byref(info)):
            return False
        m = info.rcMonitor
        return fullscreen_in(_windows(_root_hwnd(win)), (m.left, m.top, m.right, m.bottom))
    except (AttributeError, OSError):
        return False


def _root_hwnd(win):
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    user.GetAncestor.restype = wt.HWND
    return user.GetAncestor(win.winfo_id(), 2)  # GA_ROOT


def _round_corners(win, on=True):
    """Windows 11 上让 DWM 画圆角和细边框; 旧系统不支持时返回 False, 改用 Tk 画 1 像素边框。
    缩成细边时要关掉 (on=False), 否则 4 像素宽的窗口几乎被圆角裁光。"""
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        hwnd = _root_hwnd(win)
        pref = ctypes.c_int(2 if on else 1)  # DWMWA_WINDOW_CORNER_PREFERENCE: DWMWCP_ROUND / DONOTROUND
        if dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)) != 0:
            return False
        r, g, b = (int(BORDER[i:i + 2], 16) for i in (1, 3, 5))
        color = wt.DWORD(r | g << 8 | b << 16)
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))  # DWMWA_BORDER_COLOR
        return True
    except (AttributeError, OSError):
        return False


def _set_alpha(win, alpha):
    """不透明度用分层窗口实现, 100% 也保持分层 (Tk 在 -alpha 1.0 时会去掉): 普通窗口不绘制屏幕外的部分,
    从屏幕边滑出来时新露出的一截要晚一两帧才画上, 期间透出背后的内容 (常见为一道白影); 分层窗口整窗都画好。"""
    try:
        user = ctypes.windll.user32
        user.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        user.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        user.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, ctypes.c_ubyte, wt.DWORD]
        hwnd = _root_hwnd(win)
        user.SetWindowLongW(hwnd, -20, user.GetWindowLongW(hwnd, -20) | 0x80000)  # GWL_EXSTYLE, WS_EX_LAYERED
        user.SetLayeredWindowAttributes(hwnd, 0, round(alpha * 255), 2)  # LWA_ALPHA
    except (AttributeError, OSError):
        win.attributes("-alpha", alpha)


def _set(label, text, fg=None):
    if label.cget("text") != text:
        label.config(text=text)
    if fg is not None and str(label.cget("fg")) != fg:
        label.config(fg=fg)


class FloatingWindow(tk.Toplevel):
    def __init__(self, master, options, on_save, on_close, on_open):
        super().__init__(master)
        self.options, self.on_save, self.on_close, self.on_open = options, on_save, on_close, on_open
        self.alive = True
        self.drag = None
        self.hidden = self.sliding = self.menu_open = False
        self.covered = False  # 为全屏程序让路而暂时隐藏
        self.ticks = 0
        self.leave_since = self.hover_since = None
        self.edge = None
        self.shared = False  # 贴的边外侧是别的屏幕或任务栏
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=BORDER)
        self.attributes("-topmost", options["topmost"])
        self.body = tk.Frame(self, bg=CARD, padx="8p", pady="6p")
        tk.Frame(self.body, bg=CARD, width="120p", height=0).pack()  # 固定宽度, 数值变长时窗口不伸缩, 贴右边时不错位
        self.rows = {"cpu": self._device_row("CPU", "#60a5fa"), "gpu": self._device_row("显卡", "#a78bfa"),
                     "mem": self._mem_row(), "net": self._net_row()}
        self.strip = tk.Frame(self, bg=MUTED, cursor="hand2")  # 缩进后露出的细边
        self.strip.bind("<Enter>", lambda _: self.covered or self.menu_open or self.shared or self.reveal())
        self._layout()
        self.deiconify()
        self.update_idletasks()
        self.border = 0 if _round_corners(self) else 1
        _set_alpha(self, options["opacity"])
        self.body.pack_configure(padx=self.border, pady=self.border)
        self._settle(options["x"], options["y"], save=False)
        # 子控件的事件也会经过所在顶层窗口的绑定标签, 绑在窗口上就覆盖了整张卡片和细边
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", lambda _: self.on_open())
        self.bind("<Button-3>", self._menu)
        self.after(POLL_MS, self._poll)

    # ---------- 内容 ----------
    # 每行: 名称 | 主数值 (大号数字 + 小号单位) | 次要信息 | 利用率, 下方一条圆头进度条; 各行用同一组列宽对齐
    def _line(self, name):
        row = tk.Frame(self.body, bg=CARD)
        top = tk.Frame(row, bg=CARD)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        tk.Label(top, text=name, bg=CARD, fg=MUTED, font=(FONT, 8), width=4, anchor="w").grid(row=0, column=0, sticky="sw", pady=(0, "1p"))
        return row, top

    @staticmethod
    def _pair(parent, num_font, unit_font, unit, fg=FG, top=False):
        """大号数字 + 小号灰色单位, 单位默认底部对齐, top=True 时像上标一样靠上 (°C)。"""
        box = tk.Frame(parent, bg=CARD)
        num = tk.Label(box, text="--", bg=CARD, fg=fg, font=num_font, padx=0, pady=0, borderwidth=0)
        num.pack(side="left", anchor="s")
        box.unit = unit
        box.unit_label = tk.Label(box, text="", bg=CARD, fg=MUTED, font=unit_font, padx=0, pady=0, borderwidth=0)
        box.unit_label.pack(side="left", anchor="n" if top else "s", pady=("3p", 0) if top else (0, "1p"))
        box.num = num
        return box

    @staticmethod
    def _show(box, value, fmt="{:.0f}", fg=None):
        _set(box.num, "--" if value is None else fmt.format(value), fg)
        _set(box.unit_label, "" if value is None else box.unit)

    def _bar(self, row, color):
        bar = tk.Canvas(row, bg=CARD, width=0, height="3p", highlightthickness=0, borderwidth=0)
        bar.pack(fill="x", pady=("2p", "5p"))
        bar.track = bar.create_line(0, 0, 0, 0, fill=TRACK, capstyle="round")
        bar.fill = bar.create_line(0, 0, 0, 0, fill=color, capstyle="round")
        bar.frac = 0
        bar.bind("<Configure>", lambda _: self._draw_bar(bar), add="+")
        return bar

    @staticmethod
    def _draw_bar(bar, percent=...):
        if percent is not ...:
            bar.frac = max(0.0, min(1.0, (percent or 0) / 100))
        w, h = bar.winfo_width(), bar.winfo_height()
        r = h / 2
        bar.itemconfig(bar.track, width=h)
        bar.itemconfig(bar.fill, width=h, state="normal" if bar.frac > 0 else "hidden")
        bar.coords(bar.track, r, r, max(r, w - r), r)
        bar.coords(bar.fill, r, r, r + (w - h) * bar.frac, r)

    def _load(self, top):
        load = self._pair(top, (NUM, 10, "bold"), (NUM, 7), "%")
        load.grid(row=0, column=3, sticky="se", padx=("4p", 0))
        return load

    def _device_row(self, name, color):
        row, top = self._line(name)
        value = self._pair(top, (NUM, 13, "bold"), (NUM, 8), "°C", MUTED, top=True)
        value.grid(row=0, column=1, sticky="sw")
        fan = tk.Label(top, text="", bg=CARD, fg=MUTED, font=(NUM, 8), anchor="e")
        fan.grid(row=0, column=2, sticky="se", padx=("4p", 0), pady=(0, "1p"))
        return SimpleNamespace(frame=row, value=value, load=self._load(top), fan=fan, bar=self._bar(row, color))

    def _mem_row(self):
        row, top = self._line("内存")
        gb = tk.Label(top, text="", bg=CARD, fg=SUB, font=(NUM, 9), anchor="w")
        gb.grid(row=0, column=1, columnspan=2, sticky="sw", pady=(0, "1p"))
        return SimpleNamespace(frame=row, gb=gb, load=self._load(top), bar=self._bar(row, "#34d399"))

    def _net_row(self):
        row, top = self._line("网络")
        row.pack_configure(pady=(0, "3p"))
        cells = tk.Frame(top, bg=CARD)
        cells.grid(row=0, column=1, columnspan=3, sticky="ew")
        cells.columnconfigure((0, 1), weight=1, uniform="net")
        out = []
        for col, (arrow, color) in enumerate((("↑", "#fb923c"), ("↓", "#22d3ee"))):
            cell = tk.Frame(cells, bg=CARD)
            cell.grid(row=0, column=col, sticky="e")
            tk.Label(cell, text=arrow, bg=CARD, fg=color, font=(NUM, 9, "bold")).pack(side="left", anchor="s")
            out.append(self._pair(cell, (NUM, 9, "bold"), (NUM, 7), ""))
            out[-1].pack(side="left", anchor="s", padx=("1p", 0))
        return SimpleNamespace(frame=row, up=out[0], down=out[1])

    def _layout(self):
        shown = [k for k in ("cpu", "gpu", "mem", "net") if self.options.get(f"show_{k}", True)]
        for key, row in self.rows.items():
            row.frame.pack_forget()
        for key in shown:
            self.rows[key].frame.pack(fill="x")
        if not self.hidden:
            self.body.pack(fill="both", expand=True, padx=getattr(self, "border", 1), pady=getattr(self, "border", 1))

    def refresh(self, d):
        for key in ("cpu", "gpu"):
            row, temp, load = self.rows[key], d.get(f"{key}_temp"), d.get(f"{key}_load")
            self._show(row.value, temp, fg=temp_color(temp))
            self._show(row.load, load)
            self._draw_bar(row.bar, load)
            rpm, pct = d.get(f"{key}_fan"), d.get(f"{key}_fan_pct")  # 部分显卡只报告转速百分比
            _set(row.fan, f"{rpm:.0f} rpm" if rpm is not None else f"风扇 {pct:.0f}%" if pct is not None else "",
                 SUB if rpm or pct else MUTED)
        row, mem = self.rows["mem"], d.get("mem_load")
        self._show(row.load, mem)
        _set(row.gb, fmt_gb(d.get("mem_used"), d.get("mem_total")).replace("/", " / "))
        self._draw_bar(row.bar, mem)
        row = self.rows["net"]
        for box, key in ((row.down, "net_down"), (row.up, "net_up")):
            num, unit, color = fmt_rate(d.get(key))
            _set(box.num, num, color)
            _set(box.unit_label, f" {unit}" if unit else "")
        color = temp_color(d.get("cpu_temp"))
        if str(self.strip.cget("bg")) != color:
            self.strip.config(bg=color)

    # ---------- 拖动、吸附与菜单 ----------
    def _press(self, event):
        if self.hidden or self.sliding:
            return
        self.drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _move(self, event):
        if self.drag:
            self.geometry(f"+{event.x_root - self.drag[0]}+{event.y_root - self.drag[1]}")

    def _release(self, _):
        if self.drag:
            self.drag = None
            self._settle(self.winfo_x(), self.winfo_y())

    def _settle(self, x, y, save=True):
        """把窗口放到 (x, y), 收回屏幕内并吸附到附近的边, 记下贴住的边用于自动隐藏。"""
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        self.work = work_area(self, x + w // 2, y + h // 2)
        x, y, edge = snap(x, y, w, h, self.work, self.winfo_pixels(SNAP))
        self.edge = edge
        self.shared = bool(edge) and not edge_is_free(edge, self.work)
        self.pos = (x, y)
        self._move_to(f"+{x}+{y}")
        self.leave_since = None
        if save:
            self.options.update(x=x, y=y)
            self.on_save()

    def _menu(self, event):
        menu = tk.Menu(self.master, tearoff=0)  # 挂在主窗口下: 菜单里"关闭悬浮窗"会销毁本窗口
        self._vars = {k: tk.BooleanVar(value=self.options[k]) for k in ("topmost", "autohide", "show_mem", "show_net")}
        self._opacity = tk.DoubleVar(value=self.options["opacity"])  # 保持引用, 否则选中标记不显示
        menu.add_command(label="打开主窗口", command=self.on_open)
        menu.add_separator()
        menu.add_checkbutton(label="置顶显示", variable=self._vars["topmost"], command=lambda: self._toggle("topmost"))
        menu.add_checkbutton(label="贴边自动隐藏", variable=self._vars["autohide"], command=lambda: self._toggle("autohide"))
        items = tk.Menu(menu, tearoff=0)
        items.add_checkbutton(label="内存", variable=self._vars["show_mem"], command=lambda: self._toggle("show_mem"))
        items.add_checkbutton(label="网速", variable=self._vars["show_net"], command=lambda: self._toggle("show_net"))
        menu.add_cascade(label="显示项目", menu=items)
        alpha = tk.Menu(menu, tearoff=0)
        for value in OPACITIES:
            alpha.add_radiobutton(label=f"{value:.0%}", value=value, variable=self._opacity,
                                  command=lambda v=value: self._set_opacity(v))
        menu.add_cascade(label="不透明度", menu=alpha)
        menu.add_separator()
        menu.add_command(label="关闭悬浮窗", command=self.on_close)
        self.menu_open = True
        try:
            menu.tk_popup(event.x_root, event.y_root)  # Windows 上菜单关闭后才返回
        finally:
            menu.grab_release()
            self.menu_open = False
            self.leave_since = None

    def _toggle(self, key):
        self.options[key] = self._vars[key].get()
        self._apply(key)
        self.on_save()

    def _apply(self, key):
        if key == "topmost":
            if not self.hidden:  # 缩进时细边始终置顶, 滑出来再生效
                self.attributes("-topmost", self.options["topmost"])
        elif key == "autohide":
            if not self.options["autohide"]:
                self.reveal()
        elif key == "opacity":
            if not self.sliding:  # 滑动中的渐变结束时自己会用到新值
                _set_alpha(self, self.options["opacity"])
        else:
            self._layout()
            if not self.hidden:
                self._settle(*self.pos, save=False)  # 高度变了, 贴底边时重新收回屏幕内

    def apply_options(self, before):
        """选项在别处 (网页管理端) 被修改后, 按右键菜单相同的方式让已打开的窗口立即生效。"""
        for key in ("topmost", "autohide", "opacity", "show_mem", "show_net"):
            if self.options.get(key) != before.get(key):
                if key in getattr(self, "_vars", {}):
                    self._vars[key].set(self.options[key])
                self._apply(key)

    def _set_opacity(self, value):
        self.options["opacity"] = value
        self._apply("opacity")
        self.on_save()

    # ---------- 贴边自动隐藏 ----------
    def _poll(self):
        if not self.alive:
            return
        self.after(POLL_MS, self._poll)
        self.ticks += 1
        if self.ticks % FULLSCREEN_EVERY == 0:
            self._check_fullscreen()
        if self.covered or self.drag or self.sliding or self.menu_open:
            return
        if not (self.options["autohide"] and self.edge):
            self.leave_since = None
            return
        px, py = self.winfo_pointerxy()
        x, y = self.winfo_rootx(), self.winfo_rooty()
        inside = x - 1 <= px <= x + self.winfo_width() and y - 1 <= py <= y + self.winfo_height()
        if self.hidden:
            if not self.shared:
                if inside:
                    self.reveal()
                return
            pad = self.winfo_pixels(SHARED_PAD)  # 细边只有几像素, 往右一推就过到隔壁屏幕, 放宽了才好停住
            near = x - pad <= px <= x + self.winfo_width() + pad and y - pad <= py <= y + self.winfo_height() + pad
            if not near:
                self.hover_since = None
            elif self.hover_since is None:
                self.hover_since = time.monotonic()
            elif time.monotonic() - self.hover_since >= SHARED_DWELL:
                self.reveal()
        elif inside:
            self.leave_since = None
        elif self.leave_since is None:
            self.leave_since = time.monotonic()
        elif time.monotonic() - self.leave_since >= HIDE_DELAY:
            self.conceal()

    def _check_fullscreen(self):
        """置顶 (缩进后的细边也始终置顶) 时会压在全屏游戏、视频上, 这时暂时藏起来, 退出全屏再出来。"""
        if self.drag or self.menu_open:
            return
        left, top, right, bottom = self.work
        covered = (self.options["topmost"] or self.hidden) and fullscreen_app_on(self, (left + right) // 2, (top + bottom) // 2)
        if covered == self.covered:
            return
        self.covered = covered
        self.leave_since = None
        try:  # 不用 withdraw/deiconify: 重新显示时会抢走全屏程序刚交出的焦点
            ctypes.windll.user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
            ctypes.windll.user32.ShowWindow(_root_hwnd(self), 0 if covered else 4)  # SW_HIDE / SW_SHOWNOACTIVATE
        except (AttributeError, OSError):
            pass

    def _move_to(self, spec):
        """连续改位置时 Tk 会合并成一次, 之前移动的 ConfigureNotify 回来又把位置改回旧值; 每次都立即生效。"""
        self.geometry(spec)
        self.update_idletasks()

    def _slide(self, start, end, done, fade=None):
        """从 start 滑到 end; fade=(起, 止) 时同时渐变不透明度。"""
        self.sliding = True
        began = time.monotonic()
        def step():
            if not self.alive:
                return
            p = min(1.0, (time.monotonic() - began) / SLIDE_TIME)
            t = 1 - (1 - p) ** 3  # 先快后慢
            if start != end:
                self._move_to(f"+{round(start[0] + (end[0] - start[0]) * t)}+{round(start[1] + (end[1] - start[1]) * t)}")
            if fade:
                _set_alpha(self, fade[0] + (fade[1] - fade[0]) * t)
            if p < 1:
                self.after(FRAME_MS, step)
            else:
                done()  # done 里可能 update(), 结束前一直算在滑动中, 挡住轮询和 <Enter>
                self.sliding = False
        step()

    def conceal(self):
        """缩进屏幕外, 动画结束后整窗换成一条细边 (始终置顶, 免得被别的窗口盖住找不回来)。
        贴的边外侧是别的屏幕或任务栏时改为原地淡出, 不滑到隔壁屏幕上。"""
        if self.hidden or self.sliding or not self.edge:
            return
        w, h = self.winfo_width(), self.winfo_height()
        opacity = self.options["opacity"]
        def done():
            self.hidden = True
            self.body.pack_forget()
            self.strip.pack(fill="both", expand=True)
            if not self.border:
                _round_corners(self, False)
            self.update()  # 先在原尺寸下把细边颜色画满, 再缩小, 露出来的那条不会先白一下
            if not self.alive:
                return
            sx, sy, sw, sh = strip_rect(*self.pos, w, h, self.edge, self.work)
            self._move_to(f"{sw}x{sh}+{sx}+{sy}")
            if self.shared:
                _set_alpha(self, opacity)
            self.attributes("-topmost", True)
        if self.shared:
            self._slide(self.pos, self.pos, done, fade=(opacity, 0))
        else:
            self._slide(self.pos, tucked(*self.pos, w, h, self.edge, self.work), done)

    def reveal(self):
        if not self.hidden or self.sliding:
            return
        self.hidden = False
        self.leave_since = self.hover_since = None
        self.sliding = True  # 下面 update() 期间别让轮询、菜单等再进来
        self.strip.pack_forget()
        if not self.border:
            _round_corners(self)
        self._layout()
        self.attributes("-topmost", self.options["topmost"])
        self.update_idletasks()
        # 尺寸和起点一次设好: 先取消固定尺寸会让整张卡片在细边位置闪一下再跳出去
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        start = self.pos if self.shared else tucked(*self.pos, w, h, self.edge, self.work)
        if self.shared:
            _set_alpha(self, 0)  # 原地淡入
        self._move_to(f"{w}x{h}+{start[0]}+{start[1]}")
        self.lift()
        self.update()  # 放大后新增的部分要等重绘, 先在屏幕外画好再滑进来, 不然会白一下
        if not self.alive:
            return
        self._slide(start, self.pos, lambda: self.geometry(""),  # 取消固定尺寸, 恢复按内容计算
                    fade=(0, self.options["opacity"]) if self.shared else None)

    def present(self):
        """从主窗口/设置里再次打开: 已缩进的先滑出来。"""
        self.reveal()
        self.lift()

    def destroy(self):
        self.alive = False
        super().destroy()


class FloatingWindowMixin:
    def _sync_floating(self):
        active = self.float_win is not None and self.float_win.winfo_exists()
        var = getattr(self, "_float_var", None)
        if var is not None:  # 设置窗里的开关跟随悬浮窗实际状态 (如从悬浮窗右键菜单关闭)
            var.set(active)

    def toggle_floating(self):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.hide_floating()
        else:
            self.show_floating()

    def show_floating(self):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.float_win.present()
            return
        options = self.settings.setdefault("floating", {})
        options["enabled"] = True
        self.float_win = FloatingWindow(self.root, options, self._save_settings, self.hide_floating, self.show_window)
        self._save_settings()
        self._sync_floating()
        if self.data:
            self.float_win.refresh(self.data)

    def hide_floating(self):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.float_win.destroy()
        self.float_win = None
        self.settings.setdefault("floating", {})["enabled"] = False
        self._save_settings()
        self._sync_floating()

    def _update_floating(self, d):
        if self.float_win is not None and self.float_win.winfo_exists():
            self.float_win.refresh(d)
