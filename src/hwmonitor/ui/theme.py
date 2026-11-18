"""界面主题: 深色/浅色配色、字体、ttk 样式、窗口图标与标题栏, 以及开关/勾选框的自绘图片。"""
import ctypes
import tkinter as tk
from tkinter import ttk

from hwmonitor import paths

ICON = str(paths.ICON)

FONT = "Microsoft YaHei UI"
NUM = "Segoe UI"  # 数字等宽, 跳动时不抖
FONT_FACTORS = {"small": 0.9, "normal": 1.0, "large": 1.15}
SCALE_FACTORS = {100: 1.0, 125: 1.25, 150: 1.5, 175: 1.75, 200: 2.0}

# 结构色在前。同一套配色里重复的色值，换主题时按先出现的键对应，避免控件被映射到另一个用途。
DARK = {
    "BG": "#16181d", "CARD": "#20232a", "BORDER": "#2c3039", "TRACK": "#2f333d",
    "FG": "#e6e8ee", "MUTED": "#8a90a0", "SUB": "#b4b9c5", "RED": "#f87171",
    "PIN": "#1e3a5f", "CHIP_HOVER": "#3a3f4b", "ACCENT": "#2563eb", "FIELD": "#1b1d23",
    "ERR_BG": "#2a1d21", "ERR_BORDER": "#4a2a2f", "NAV_HOVER": "#1d2027",
    "BUTTON_HOVER": "#2a2e37", "ON_FG": "#93c5fd", "ON_BORDER": "#3b82f6",
    "ON_HOVER": "#24466f", "ON_PRESSED": "#1a3354", "INDICATOR_BORDER": "#5a6070",
    "SCROLL_ACTIVE": "#4a505d", "ACCENT_HOVER": "#3b82f6", "ACCENT_PRESSED": "#1d4ed8",
    "SELECTED_FG": "#bfdbfe", "WHITE": "#ffffff", "SELECT": "#264b79",
    "SERIES_CPU": "#60a5fa", "SERIES_GPU": "#a78bfa", "TEMP_OK": "#4ade80", "TEMP_WARM": "#fbbf24",
    "RATE_FAST": "#22d3ee", "RATE_GB": "#fb923c", "MEM_BAR": "#34d399", "WAN_OK": "#86efac",
}
LIGHT = {
    "BG": "#f3f3f3", "CARD": "#ffffff", "BORDER": "#e5e5e5", "TRACK": "#e8e8ec",
    "FG": "#1a1a1a", "MUTED": "#6b7280", "SUB": "#3f3f46", "RED": "#dc2626",
    "PIN": "#dbeafe", "CHIP_HOVER": "#e4e4e7", "ACCENT": "#2563eb", "FIELD": "#f4f4f5",
    "ERR_BG": "#fef2f2", "ERR_BORDER": "#fecaca", "NAV_HOVER": "#ececef",
    "BUTTON_HOVER": "#e4e4e7", "ON_FG": "#1d4ed8", "ON_BORDER": "#2563eb",
    "ON_HOVER": "#bfdbfe", "ON_PRESSED": "#93c5fd", "INDICATOR_BORDER": "#a1a1aa",
    "SCROLL_ACTIVE": "#a1a1aa", "ACCENT_HOVER": "#3b82f6", "ACCENT_PRESSED": "#1d4ed8",
    "SELECTED_FG": "#1e3a8a", "WHITE": "#ffffff", "SELECT": "#dbeafe",
    "SERIES_CPU": "#1d4ed8", "SERIES_GPU": "#6d28d9", "TEMP_OK": "#15803d", "TEMP_WARM": "#b45309",
    "RATE_FAST": "#0e7490", "RATE_GB": "#c2410c", "MEM_BAR": "#059669", "WAN_OK": "#15803d",
}
_current = dict(DARK)
resolved = "dark"
_font_factor = 1.0


class Color:
    """当前主题的颜色。导入后仍跟随主题切换，Tk 配置时取当时的色值。"""

    def __init__(self, key):
        self.key = key

    def __str__(self):
        return _current[self.key]

    def __getitem__(self, item):
        return str(self)[item]

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        if isinstance(other, Color):
            return self.key == other.key
        if isinstance(other, str):
            return str(self).lower() == other.lower()
        return NotImplemented

    def __hash__(self):
        return hash(self.key)


BG, CARD, BORDER, TRACK, FG, MUTED, SUB, RED, PIN, CHIP_HOVER, ACCENT, FIELD, ERR_BG, ERR_BORDER, NAV_HOVER = (
    Color(key) for key in ("BG", "CARD", "BORDER", "TRACK", "FG", "MUTED", "SUB", "RED", "PIN", "CHIP_HOVER",
                           "ACCENT", "FIELD", "ERR_BG", "ERR_BORDER", "NAV_HOVER"))
BUTTON_HOVER, ON_FG, ON_BORDER, ON_HOVER, ON_PRESSED, INDICATOR_BORDER = (
    Color(key) for key in ("BUTTON_HOVER", "ON_FG", "ON_BORDER", "ON_HOVER", "ON_PRESSED", "INDICATOR_BORDER"))
SCROLL_ACTIVE, ACCENT_HOVER, ACCENT_PRESSED, SELECTED_FG, WHITE, SELECT = (
    Color(key) for key in ("SCROLL_ACTIVE", "ACCENT_HOVER", "ACCENT_PRESSED", "SELECTED_FG", "WHITE", "SELECT"))
SERIES_CPU, SERIES_GPU, TEMP_OK, TEMP_WARM, RATE_FAST, RATE_GB, MEM_BAR, WAN_OK = (
    Color(key) for key in ("SERIES_CPU", "SERIES_GPU", "TEMP_OK", "TEMP_WARM", "RATE_FAST", "RATE_GB", "MEM_BAR", "WAN_OK"))


def use_font(name):
    """字号系数只在启动时设定。运行中改设置要等下次创建控件才生效。"""
    global _font_factor
    _font_factor = FONT_FACTORS.get(name, 1.0)


def font(size, weight=None, numeric=False, family=None):
    face = family or (NUM if numeric else FONT)
    scaled = max(1, round(size * _font_factor))
    return (face, scaled, weight) if weight else (face, scaled)


def apply_scale(root, percent):
    """叠在系统 DPI 之上。必须在创建任何使用 p 单位的控件之前调用一次。"""
    factor = SCALE_FACTORS.get(percent, 1.0)
    if factor == 1.0:
        return
    base = float(root.tk.call("tk", "scaling"))
    root.tk.call("tk", "scaling", base * factor)


def resolve_appearance(name):
    if name == "system":
        return "light" if windows_app_uses_light_theme() else "dark"
    return name if name in ("dark", "light") else "dark"


def windows_app_uses_light_theme():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except OSError:
        return False


def color_map(old, new):
    """旧色值 -> 新色值。重复色值保留先出现的键，和配色表的顺序一致。"""
    mapping = {}
    for key, value in old.items():
        mapping.setdefault(value.lower(), new[key])
    return mapping


def set_menu_theme(mode):
    """右键菜单跟随当前界面，不跟随系统。失败则保持系统菜单。"""
    try:
        ux = ctypes.WinDLL("uxtheme")
        ux[135](2 if mode == "dark" else 3)  # SetPreferredAppMode: ForceDark / ForceLight
        ux[136]()  # FlushMenuThemes
    except (AttributeError, OSError):
        pass


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
                    if resolved == "dark" and lf[26] == b"\x00":  # 浅色底用系统 ClearType; 深色底改灰度抗锯齿
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
    set_titlebar(win)


def set_titlebar(win):
    """标题栏跟随当前主题。窗口尚未映射时失败，调用方忽略即可。"""
    try:
        from ctypes import wintypes as wt
        win.update_idletasks()
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user.GetAncestor.restype = wt.HWND
        hwnd = user.GetAncestor(win.winfo_id(), 2)  # GA_ROOT: this dialog's native wrapper, not its owner.
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
        dark = ctypes.c_int(1 if resolved == "dark" else 0)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        # Windows 11's explicit caption colors also cover nested/active dialogs
        # when the OS uses a light theme. Older systems ignore these attributes.
        for attribute, color in ((35, str(CARD)), (36, str(FG))):
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            value = wt.DWORD(red | green << 8 | blue << 16)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def _rgb(c):
    c = str(c)
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def render_png(width, height, sample):
    """sample(x, y) -> RGB 或 None (透明), 4x4 超采样抗锯齿后用 zlib 手写 PNG, 返回 PNG 字节。不依赖 Pillow。"""
    import struct
    import zlib
    n = 4
    rows = []
    for py in range(height):
        row = bytearray(b"\x00")  # 每行前的过滤类型字节
        for px in range(width):
            acc, hit = [0, 0, 0], 0
            for sy in range(n):
                for sx in range(n):
                    c = sample(px + (sx + 0.5) / n, py + (sy + 0.5) / n)
                    if c:
                        hit += 1
                        acc = [a + v for a, v in zip(acc, c)]
            row += bytes([a // hit for a in acc] + [255 * hit // (n * n)]) if hit else b"\x00\x00\x00\x00"
        rows.append(bytes(row))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def check_png(size, gap, fill, border, check=None):
    """画勾选框图标: 圆角方框 + 可选的对号, 右侧留 gap 像素透明间距。clam 主题自带的勾选标记是 ×, 只能换成图片。"""
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
        if x >= size:
            return None
        # 圆角矩形的有向距离: 负数在内部
        qx, qy = abs(x - size / 2) - (size / 2 - radius), abs(y - size / 2) - (size / 2 - radius)
        d = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - radius
        if d > 0:
            return None
        if check and min(seg_dist(x, y, pts[0], pts[1]), seg_dist(x, y, pts[1], pts[2])) < cw:
            return check
        return border if d > -bw else fill
    return render_png(size + gap, size, sample)


def switch_png(width, height, track, border, knob, on):
    """画开关图标 (Windows 11 样式): 胶囊形轨道 + 圆形滑块, 开启时滑块在右侧且略大。"""
    track, border, knob = _rgb(track), _rgb(border), _rgb(knob)
    r, bw = height / 2, max(1.0, height / 14)
    kr = height * (0.3 if on else 0.25)
    kx = width - r if on else r

    def sample(x, y):
        qx = max(abs(x - width / 2) - (width / 2 - r), 0)
        d = (qx ** 2 + (y - r) ** 2) ** 0.5 - r
        if d > 0:
            return None
        if (x - kx) ** 2 + (y - r) ** 2 < kr ** 2:
            return knob
        return border if d > -bw else track
    return render_png(width, height, sample)


def apply_style(root):
    """ttk 控件换成当前主题。可重复调用；自绘图片保持同一批对象，调用方须保持引用。"""
    # 下拉框收起时滚轮只用于滚动页面，避免悬停就改动并保存选项。
    # 弹出的列表有独立的绑定，仍可滚动；方向键选择也不受影响。
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.unbind_class("TCombobox", sequence)
    s = ttk.Style(root)
    first = not getattr(apply_style, "ready", False)
    if first:
        s.theme_use("clam")  # 再次调用会清掉自定义元素，换主题时不再切主题引擎
    s.configure(".", background=BG, foreground=FG, font=font(9))
    s.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    s.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=font(9, "bold"))
    s.configure("TButton", background=CARD, foreground=FG, bordercolor=BORDER, lightcolor=CARD,
                darkcolor=CARD, focuscolor=CARD, padding=("10p", "3p"), width=0)  # 宽度随文字, 不用主题默认的最小宽度
    hover = [("pressed", BORDER), ("active", BUTTON_HOVER)]
    s.map("TButton", background=hover, lightcolor=hover, darkcolor=hover)
    # 置顶开启时的按钮样式
    s.configure("On.TButton", background=PIN, foreground=ON_FG, bordercolor=ON_BORDER,
                lightcolor=PIN, darkcolor=PIN, focuscolor=PIN)
    hover = [("pressed", ON_PRESSED), ("active", ON_HOVER)]
    s.map("On.TButton", background=hover, lightcolor=hover, darkcolor=hover)
    for w in ("TCheckbutton", "TRadiobutton"):
        # 单选框: clam 默认的圆圈只有十来个像素, 高 DPI 下太小; 边框调亮, 未选中时在深色底上也看得见 (勾选框见下面的自绘图标)
        s.configure(w, background=BG, foreground=FG, indicatorbackground=CARD, indicatorforeground=FG,
                    indicatorsize=root.winfo_pixels("9p"), indicatormargin=(0, 0, "5p", 0),
                    upperbordercolor=INDICATOR_BORDER, lowerbordercolor=INDICATOR_BORDER, focuscolor=BG, padding=(0, "2p"))
        s.map(w, background=[("active", BG)], indicatorbackground=[("selected", ACCENT), ("active", TRACK)],
              indicatorforeground=[("selected", WHITE)], foreground=[("disabled", MUTED)],
              upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
    s.configure("Hint.TLabel", foreground=MUTED)
    # 数值输入框: clam 默认白底, 在深色界面里很突兀
    s.configure("TSpinbox", fieldbackground=CARD, background=TRACK, foreground=FG, insertcolor=FG,
                arrowcolor=SUB, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                arrowsize=root.winfo_pixels("8p"), padding=("3p", "2p"))
    s.map("TSpinbox", fieldbackground=[("disabled", BG)], foreground=[("disabled", MUTED)],
          background=[("pressed", BORDER), ("active", BUTTON_HOVER)], arrowcolor=[("active", FG)],
          bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
          selectbackground=[("focus", ACCENT)], selectforeground=[("focus", WHITE)])
    for kind in ("TEntry", "TCombobox"):
        s.configure(kind, fieldbackground=CARD, foreground=FG, background=TRACK,
                    insertcolor=FG, arrowcolor=FG, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, padding=("5p", "4p"), arrowsize=root.winfo_pixels("10p"))
        s.map(kind, fieldbackground=[("disabled", BG), ("readonly", CARD)],
              foreground=[("disabled", MUTED), ("readonly", FG)],
              bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)], selectbackground=[("focus", ACCENT)],
              selectforeground=[("focus", WHITE)])
    # 下拉箭头按下/悬停: clam 默认是浅色块, 改成与数值框一致; 弹出列表用细边框
    s.map("TCombobox", background=[("pressed", BORDER), ("active", BUTTON_HOVER)],
          arrowcolor=[("disabled", MUTED)])
    s.configure("ComboboxPopdownFrame", background=CARD, bordercolor=BORDER, lightcolor=CARD,
                darkcolor=CARD, relief="solid", borderwidth=1)
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
                rowheight=root.winfo_pixels("24p"), borderwidth=0,
                bordercolor=CARD, lightcolor=CARD, darkcolor=CARD, relief="flat")
    s.map("Treeview", background=[("selected", PIN)], foreground=[("selected", SELECTED_FG)])
    s.configure("Treeview.Heading", background=TRACK, foreground=SUB,
                bordercolor=BORDER, lightcolor=TRACK, darkcolor=TRACK, padding=("6p", "5p"))
    s.map("Treeview.Heading", background=[("active", BORDER)], relief=[("active", "flat")])
    s.configure("Router.Treeview", font=font(8), rowheight=root.winfo_pixels("23p"))
    s.configure("Router.Treeview.Heading", font=font(8), background=CARD,
                lightcolor=CARD, darkcolor=CARD, bordercolor=CARD, relief="flat", padding=("5p", "5p"))
    s.configure("Vertical.TScrollbar", background=TRACK, troughcolor=BG,
                bordercolor=BG, lightcolor=TRACK, darkcolor=TRACK, arrowcolor=SUB)
    s.map("Vertical.TScrollbar", background=[("active", BORDER), ("pressed", MUTED)])
    s.configure("Thin.Vertical.TScrollbar", background=TRACK, troughcolor=BG, bordercolor=BG,
                lightcolor=TRACK, darkcolor=TRACK, gripcount=0, arrowsize=root.winfo_pixels("5p"))
    s.map("Thin.Vertical.TScrollbar", background=[("pressed", MUTED), ("active", SCROLL_ACTIVE)],
          lightcolor=[("pressed", MUTED), ("active", SCROLL_ACTIVE)], darkcolor=[("pressed", MUTED), ("active", SCROLL_ACTIVE)])
    root.option_add("*TCombobox*Listbox.background", str(CARD))
    root.option_add("*TCombobox*Listbox.foreground", str(FG))
    root.option_add("*TCombobox*Listbox.selectBackground", str(PIN))
    root.option_add("*TCombobox*Listbox.selectForeground", str(FG))
    root.option_add("*TCombobox*Listbox.font", font(9))
    images = _style_images(root)
    if first:
        s.layout("Router.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.layout("Thin.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        off, on, hover, dis, dis_on = images[:5]
        s.element_create("Check.indicator", "image", off, ("disabled", "selected", dis_on), ("disabled", dis),
                         ("selected", on), ("active", hover))
        s.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        off, off_hover, on, on_hover, dis_off, dis_on = images[5:]
        s.element_create("Switch.indicator", "image", off, ("disabled", "selected", dis_on), ("disabled", dis_off),
                         ("selected", "active", on_hover), ("selected", on), ("active", off_hover))
        s.layout("Switch.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Switch.indicator", {"side": "left", "sticky": ""})]})])
        apply_style.ready = True
    s.configure("Switch.TCheckbutton", background=CARD, padding=0)
    s.map("Switch.TCheckbutton", background=[("active", CARD)])
    # 设置行 (卡片底色) 里的控件: 背景跟随卡片, 输入框比卡片暗一档
    s.configure("Card.TCheckbutton", background=CARD, focuscolor=CARD)
    s.map("Card.TCheckbutton", background=[("active", CARD)])
    s.configure("Card.TButton", background=TRACK, lightcolor=TRACK, darkcolor=TRACK)
    hover = [("disabled", CARD), ("pressed", BORDER), ("active", CHIP_HOVER)]
    s.map("Card.TButton", background=hover, lightcolor=hover, darkcolor=hover)
    for kind in ("TEntry", "TCombobox", "TSpinbox"):
        s.configure("Card." + kind, fieldbackground=FIELD, lightcolor=FIELD, darkcolor=FIELD)
        s.map("Card." + kind, fieldbackground=[("disabled", CARD), ("readonly", FIELD)],
              lightcolor=[("focus", ACCENT), ("disabled", CARD)], darkcolor=[("focus", ACCENT), ("disabled", CARD)],
              bordercolor=[("focus", ACCENT)])
    # 主操作按钮 (提示条里的"启动")
    s.configure("Accent.TButton", background=ACCENT, foreground=WHITE, bordercolor=ACCENT,
                lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT, anchor="center")
    hover = [("disabled", CARD), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)]
    s.map("Accent.TButton", background=hover, lightcolor=hover, darkcolor=hover,
          bordercolor=[("disabled", BORDER)], foreground=[("disabled", MUTED)])
    return images


def _style_images(root):
    """勾选框和开关图标。首次创建，之后把新主题画进同一批 PhotoImage，元素不用重建。"""
    size, gap = root.winfo_pixels("9p"), root.winfo_pixels("5p")
    border = INDICATOR_BORDER
    check = (check_png(size, gap, CARD, border), check_png(size, gap, ACCENT, ACCENT, WHITE),
             check_png(size, gap, TRACK, border), check_png(size, gap, BG, BORDER),
             check_png(size, gap, TRACK, TRACK, MUTED))
    w, h = root.winfo_pixels("28p"), root.winfo_pixels("14p")
    switch = (switch_png(w, h, FIELD, MUTED, SUB, False), switch_png(w, h, TRACK, SUB, FG, False),
              switch_png(w, h, ACCENT, ACCENT, WHITE, True), switch_png(w, h, ACCENT_HOVER, ACCENT_HOVER, WHITE, True),
              switch_png(w, h, CARD, CHIP_HOVER, CHIP_HOVER, False), switch_png(w, h, TRACK, TRACK, MUTED, True))
    pngs = check + switch
    images = getattr(apply_style, "images", None)
    if images is None:
        images = tuple(tk.PhotoImage(data=png, format="png") for png in pngs)
        apply_style.images = images
        return images
    for img, png in zip(images, pngs):
        fresh = tk.PhotoImage(data=png, format="png")
        img.configure(width=fresh.width(), height=fresh.height())
        img.blank()
        img.tk.call(img, "copy", fresh)
    return images


_WIDGET_COLORS = ("bg", "fg", "background", "foreground", "highlightbackground", "highlightcolor",
                  "activebackground", "activeforeground", "selectbackground", "selectforeground", "insertbackground")


def recolor_tree(widget, old, new):
    """把已经画上的结构色和语义色换成新主题。ttk 控件靠样式，不逐个改底色。"""
    mapping = color_map(old, new)
    _recolor(widget, mapping)


def _mapped(value, mapping):
    text = str(value).lower()
    new = mapping.get(text)
    return new if new and new.lower() != text else None


def _recolor(widget, mapping):
    if isinstance(widget, (tk.Text, ttk.Treeview)):
        for tag in widget.tag_names():
            for option in ("foreground", "background"):
                try:
                    value = widget.tag_cget(tag, option)
                except tk.TclError:
                    continue
                new = _mapped(value, mapping)
                if new:
                    widget.tag_configure(tag, **{option: new})
    if isinstance(widget, tk.Canvas):
        for item in widget.find_all():
            for option in ("fill", "outline"):
                try:
                    value = widget.itemcget(item, option)
                except tk.TclError:
                    continue
                new = _mapped(value, mapping)
                if new:
                    widget.itemconfigure(item, **{option: new})
    if isinstance(widget, ttk.Widget):
        for option in ("foreground", "background"):
            try:
                value = widget.cget(option)
            except tk.TclError:
                continue
            new = _mapped(value, mapping)
            if new:
                widget.configure(**{option: new})
    elif not isinstance(widget, ttk.Widget):
        for option in _WIDGET_COLORS:
            try:
                value = widget.cget(option)
            except tk.TclError:
                continue
            new = _mapped(value, mapping)
            if new:
                widget.configure(**{option: new})
    for child in widget.winfo_children():
        _recolor(child, mapping)


def refresh_titlebars(root):
    set_titlebar(root)
    for child in root.winfo_children():
        if isinstance(child, tk.Toplevel):
            set_titlebar(child)


def apply_appearance(name, root=None):
    """切换深色或浅色。root 已有控件时重做样式、已绘制的颜色和标题栏。"""
    global resolved
    mode = resolve_appearance(name)
    old = dict(_current)
    _current.clear()
    _current.update(LIGHT if mode == "light" else DARK)
    previous = resolved
    resolved = mode
    set_menu_theme(mode)
    if root is not None and (previous != mode or old != _current):
        apply_style(root)
        recolor_tree(root, old, _current)
        refresh_titlebars(root)
    return mode
