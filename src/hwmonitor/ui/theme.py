"""界面主题: 深色配色、字体、ttk 样式、窗口图标与标题栏, 以及开关/勾选框的自绘图片。"""
import ctypes
import tkinter as tk
from tkinter import ttk

from hwmonitor import paths

ICON = str(paths.ICON)

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
FIELD = "#1b1d23"  # 卡片里的输入框底色
ERR_BG = "#2a1d21"  # 错误提示条
ERR_BORDER = "#4a2a2f"
NAV_HOVER = "#1d2027"  # 设置导航项悬停
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


def _rgb(c):
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
    """ttk 控件 (按钮、输入框、开关等) 换成深色, 与主界面一致。返回自绘图片, 调用方须保持引用。"""
    # 下拉框收起时滚轮只用于滚动页面，避免悬停就改动并保存选项。
    # 弹出的列表有独立的绑定，仍可滚动；方向键选择也不受影响。
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.unbind_class("TCombobox", sequence)
    s = ttk.Style(root)
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
                    indicatorsize=root.winfo_pixels("9p"), indicatormargin=(0, 0, "5p", 0),
                    upperbordercolor="#5a6070", lowerbordercolor="#5a6070", focuscolor=BG, padding=(0, "2p"))
        s.map(w, background=[("active", BG)], indicatorbackground=[("selected", ACCENT), ("active", TRACK)],
              indicatorforeground=[("selected", "white")], foreground=[("disabled", MUTED)],
              upperbordercolor=[("selected", ACCENT)], lowerbordercolor=[("selected", ACCENT)])
    s.configure("Hint.TLabel", foreground=MUTED)
    # 数值输入框: clam 默认白底, 在深色界面里很突兀
    s.configure("TSpinbox", fieldbackground=CARD, background=TRACK, foreground=FG, insertcolor=FG,
                arrowcolor=SUB, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                arrowsize=root.winfo_pixels("8p"), padding=("3p", "2p"))
    s.map("TSpinbox", fieldbackground=[("disabled", BG)], foreground=[("disabled", MUTED)],
          background=[("pressed", BORDER), ("active", "#2a2e37")], arrowcolor=[("active", FG)],
          bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)],
          selectbackground=[("focus", ACCENT)], selectforeground=[("focus", "white")])
    for kind in ("TEntry", "TCombobox"):
        s.configure(kind, fieldbackground=CARD, foreground=FG, background=TRACK,
                    insertcolor=FG, arrowcolor=FG, bordercolor=BORDER, lightcolor=CARD,
                    darkcolor=CARD, padding=("5p", "4p"), arrowsize=root.winfo_pixels("10p"))
        s.map(kind, fieldbackground=[("disabled", BG), ("readonly", CARD)],
              foreground=[("disabled", MUTED), ("readonly", FG)],
              bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)], selectbackground=[("focus", ACCENT)],
              selectforeground=[("focus", "white")])
    # 下拉箭头按下/悬停: clam 默认是浅色块, 改成与数值框一致的深色; 弹出列表用深色细边框
    s.map("TCombobox", background=[("pressed", BORDER), ("active", "#2a2e37")],
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
    s.map("Treeview", background=[("selected", PIN)], foreground=[("selected", "#bfdbfe")])
    s.configure("Treeview.Heading", background=TRACK, foreground=SUB,
                bordercolor=BORDER, lightcolor=TRACK, darkcolor=TRACK, padding=("6p", "5p"))
    s.map("Treeview.Heading", background=[("active", BORDER)], relief=[("active", "flat")])
    s.configure("Vertical.TScrollbar", background=TRACK, troughcolor=BG,
                bordercolor=BG, lightcolor=TRACK, darkcolor=TRACK, arrowcolor=SUB)
    s.map("Vertical.TScrollbar", background=[("active", BORDER), ("pressed", MUTED)])
    # 主界面的细滚动条: 无箭头, 只有滑块, 浮在卡片右侧留白上
    s.layout("Thin.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    s.configure("Thin.Vertical.TScrollbar", background=TRACK, troughcolor=BG, bordercolor=BG,
                lightcolor=TRACK, darkcolor=TRACK, gripcount=0, arrowsize=root.winfo_pixels("5p"))
    s.map("Thin.Vertical.TScrollbar", background=[("pressed", MUTED), ("active", "#4a505d")],
          lightcolor=[("pressed", MUTED), ("active", "#4a505d")], darkcolor=[("pressed", MUTED), ("active", "#4a505d")])
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", PIN)
    root.option_add("*TCombobox*Listbox.selectForeground", FG)
    root.option_add("*TCombobox*Listbox.font", (FONT, 9))
    # 勾选框换成自绘图标 (clam 自带的选中标记是 ×, 不是对号)
    size, gap, border = root.winfo_pixels("9p"), root.winfo_pixels("5p"), "#5a6070"
    img = lambda *a: tk.PhotoImage(data=check_png(size, gap, *a), format="png")
    check_imgs = (img(CARD, border), img(ACCENT, ACCENT, "#ffffff"), img(TRACK, border),
                        img(BG, BORDER), img(TRACK, TRACK, MUTED))  # 未选、选中、悬停、禁用、禁用且选中
    off, on, hover, dis, dis_on = check_imgs
    s.element_create("Check.indicator", "image", off, ("disabled", "selected", dis_on), ("disabled", dis),
                     ("selected", on), ("active", hover))
    s.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Check.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
            ("Checkbutton.label", {"sticky": "nswe"})]})]})])
    # 设置页的开关 (Windows 11 样式), 代替勾选框表示即时生效的开/关
    w, h = root.winfo_pixels("28p"), root.winfo_pixels("14p")
    sw = lambda *a: tk.PhotoImage(data=switch_png(w, h, *a), format="png")
    switch_imgs = (sw(FIELD, MUTED, SUB, False), sw(TRACK, SUB, FG, False),
                         sw(ACCENT, ACCENT, "#ffffff", True), sw("#3b82f6", "#3b82f6", "#ffffff", True),
                         sw(CARD, CHIP_HOVER, CHIP_HOVER, False), sw(TRACK, TRACK, MUTED, True))
    off, off_hover, on, on_hover, dis_off, dis_on = switch_imgs
    s.element_create("Switch.indicator", "image", off, ("disabled", "selected", dis_on), ("disabled", dis_off),
                     ("selected", "active", on_hover), ("selected", on), ("active", off_hover))
    s.layout("Switch.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Switch.indicator", {"side": "left", "sticky": ""})]})])
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
    s.configure("Accent.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT,
                lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT, anchor="center")
    hover = [("disabled", CARD), ("pressed", "#1d4ed8"), ("active", "#3b82f6")]
    s.map("Accent.TButton", background=hover, lightcolor=hover, darkcolor=hover,
          bordercolor=[("disabled", BORDER)], foreground=[("disabled", MUTED)])
    return check_imgs + switch_imgs
