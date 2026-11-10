"""Settings, sensor customisation, charts and diagnostic export UI."""
import copy
from bisect import bisect_left
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

import config
import diagnostics
import history
import hwinfo
import autostart

BG, CARD, FG, MUTED, TRACK = "#16181d", "#20232a", "#e6e8ee", "#8a90a0", "#2f333d"
SUB, BORDER, ACCENT, RED = "#b4b9c5", "#2c3039", "#2563eb", "#f87171"
NAV_HOVER = "#1d2027"  # 设置导航项悬停
FONT, NUM = "Microsoft YaHei UI", "Segoe UI"
METRIC_NAMES = {"cpu_temp": "CPU 温度", "cpu_load": "CPU 利用率", "mem_load": "内存占用", "cpu_fan": "CPU 风扇",
                "gpu_temp": "显卡温度", "gpu_load": "显卡利用率", "gpu_mem_load": "显存占用", "gpu_fan": "显卡风扇"}
GROUP_NAMES = {"network": "网络", "router": "路由器", "case": "机箱风扇", "pump": "水冷泵", "storage": "硬盘温度", "memory": "内存温度", "network_temp": "网卡温度"}


class Dialogs:
    @staticmethod
    def _hint(parent, text):
        label = ttk.Label(parent, text=text, style="Hint.TLabel", justify="left", wraplength=300)
        label.pack(fill="x", pady=("4p", 0))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(80, e.width)))
        return label

    @staticmethod
    def _size_dialog(w, width, height):
        # Leave room for the title bar/taskbar, also at 150–200% DPI.
        max_w, max_h = w.winfo_screenwidth() - 60, w.winfo_screenheight() - 100
        w.geometry(f"{min(w.winfo_pixels(width), max_w)}x{min(w.winfo_pixels(height), max_h)}")
        w.minsize(min(w.winfo_pixels("360p"), max_w), min(w.winfo_pixels("300p"), max_h))

    def _background(self, window, job, done):
        result = queue.SimpleQueue()
        def work():
            try:
                result.put((True, job()))
            except Exception as e:
                logging.getLogger(__name__).exception("Background action failed")
                result.put((False, str(e)))
        threading.Thread(target=work, daemon=True).start()
        def wait():
            if not window.winfo_exists():
                return
            try:
                ok, value = result.get_nowait()
            except queue.Empty:
                window.after(100, wait)
                return
            if ok:
                done(value)
            else:
                self._flash("操作失败：" + value)
                done(None)
        wait()

    # 设置窗口左侧导航: (键, 标题, Segoe MDL2 Assets 图标)
    SETTINGS_PAGES = (("general", "常规", "\ue713"), ("monitor", "监控", "\ue9d9"), ("alerts", "告警", "\uea8f"),
                      ("router", "路由器", "\ue968"), ("data", "数据与关于", "\ue946"))

    def show_settings(self, page=None):
        """设置窗口 (参考 Windows 11 设置): 左侧导航切换页面, 每项设置一行, 左边标题和说明、右边控件,
        开关代替勾选框; 修改即时生效并保存, 依赖总开关的选项在关闭时置灰。"""
        if getattr(self, "settings_win", None) and self.settings_win.winfo_exists():
            self.settings_win.lift()
            if page:
                self.settings_select(page)
            return
        w, body = self._dialog("设置", resizable=True)
        self.settings_win = w
        self._size_dialog(w, "560p", "470p")
        w.minsize(min(w.winfo_pixels("470p"), w.winfo_screenwidth() - 60),
                  min(w.winfo_pixels("340p"), w.winfo_screenheight() - 100))
        body.configure(padding=0)
        footer = tk.Frame(body, bg=BG, padx="16p", pady="10p")
        footer.pack(side="bottom", fill="x")
        tk.Frame(body, bg=BORDER, height=1).pack(side="bottom", fill="x")
        tk.Label(footer, text="修改即时生效并自动保存", bg=BG, fg=MUTED, font=(FONT, 9)).pack(side="left")
        ttk.Button(footer, text="完成", style="Accent.TButton", command=w.destroy).pack(side="right")
        nav = tk.Frame(body, bg=BG, padx="8p", pady="10p")
        nav.pack(side="left", fill="y")
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")
        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        from tkinter import font
        icon_font = ("Segoe MDL2 Assets", 10) if "Segoe MDL2 Assets" in font.families(w) else None
        builders = {"general": self._general_page, "monitor": self._monitor_page, "alerts": self._alerts_page,
                    "router": self._router_page, "data": self._data_page}
        self.settings_pages, self._settings_nav = {}, {}
        for key, title, icon in self.SETTINGS_PAGES:
            self._settings_nav[key] = self._nav_item(nav, key, title, icon if icon_font else None, icon_font)
            page = tk.Frame(content, bg=BG)
            tk.Label(page, text=title, bg=BG, fg=FG, font=(FONT, 15, "bold"), anchor="w").pack(
                fill="x", padx="20p", pady=("12p", "2p"))
            builders[key](self._scroll_area(page))
            self.settings_pages[key] = page
        keys = [k for k, _, _ in self.SETTINGS_PAGES]
        def cycle(step):
            self.settings_select(keys[(keys.index(self._settings_page) + step) % len(keys)])
            return "break"
        for seq, step in (("<Control-Tab>", 1), ("<Control-Next>", 1), ("<Control-Shift-Tab>", -1), ("<Control-Prior>", -1)):
            w.bind(seq, lambda _, s=step: cycle(s))
        self.settings_select(page or getattr(self, "_settings_page", "general"))
        self._show_dialog(w)

    def _nav_item(self, nav, key, title, icon, icon_font):
        """导航项: 选中时卡片底色 + 左侧强调色竖条, 悬停时变亮。"""
        item = tk.Frame(nav, bg=BG, cursor="hand2")
        item.pack(fill="x", pady=(0, "2p"))
        item.bar = tk.Frame(item, bg=BG, width=nav.winfo_pixels("3p"))
        item.bar.pack(side="left", fill="y", pady="7p")
        item.labels = []
        if icon:
            item.labels.append(tk.Label(item, text=icon, bg=BG, fg=SUB, font=icon_font, padx="8p"))
        item.labels.append(tk.Label(item, text=title, bg=BG, fg=SUB, font=(FONT, 10), anchor="w",
                                    padx="2p" if icon else "10p", pady="6p", width=8))
        for label in item.labels:
            label.pack(side="left")
        item.selected = False
        def paint(bg):
            for widget in (item, *item.labels):
                widget.config(bg=bg)
            item.bar.config(bg=ACCENT if item.selected else bg)
        def leave(event):
            # 从导航项移到它的子标签时也会收到 Leave, 鼠标仍在项内就不恢复底色
            inside = item.winfo_containing(event.x_root, event.y_root)
            if not item.selected and not (inside and str(inside).startswith(str(item))):
                paint(BG)
        for widget in (item, *item.labels):
            widget.bind("<Button-1>", lambda _: self.settings_select(key))
            widget.bind("<Enter>", lambda _: item.selected or paint(NAV_HOVER))
            widget.bind("<Leave>", leave)
        item.paint = paint
        return item

    def settings_select(self, key):
        self._settings_page = key
        for k, page in self.settings_pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            elif page.winfo_manager():
                page.pack_forget()
        for k, item in self._settings_nav.items():
            item.selected = k == key
            item.paint(CARD if item.selected else BG)
            for label in item.labels:
                label.config(fg=FG if item.selected else SUB)

    def _scroll_area(self, parent):
        """可滚动区域: 内容超出时右侧浮出细滚动条 (与主界面一致, 不改变内容宽度), 返回放内容的框架。"""
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, borderwidth=0)
        canvas.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(parent, orient="vertical", style="Thin.Vertical.TScrollbar", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        body = tk.Frame(canvas, bg=BG, padx="20p", pady="2p")
        tk.Frame(body, bg=BG, height=body.winfo_pixels("14p")).pack(side="bottom")  # 页面底部留白
        item = canvas.create_window(0, 0, window=body, anchor="nw")
        def sync(_=None):
            canvas.itemconfigure(item, width=canvas.winfo_width())
            canvas.configure(scrollregion=canvas.bbox("all"))
            overflow = body.winfo_reqheight() > canvas.winfo_height()
            if overflow and not scroll.winfo_manager():
                scroll.place(in_=canvas, relx=1, x=-canvas.winfo_pixels("3p"), rely=0, relheight=1, anchor="ne")
            elif not overflow and scroll.winfo_manager():
                scroll.place_forget()
                canvas.yview_moveto(0)
        body.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        def wheel(event):
            if canvas.winfo_ismapped() and scroll.winfo_manager():
                if event.widget.winfo_class() not in ("TCombobox", "TSpinbox"):
                    canvas.yview_scroll(-int(event.delta / 120), "units")
        parent.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        # Keyboard navigation keeps focused controls inside the viewport.
        def reveal(event):
            widget = event.widget
            if not canvas.winfo_ismapped() or not str(widget).startswith(str(body) + "."):
                return
            y = widget.winfo_rooty() - body.winfo_rooty()
            top, height = canvas.canvasy(0), canvas.winfo_height()
            if y < top or y + widget.winfo_height() > top + height:
                canvas.yview_moveto(max(0, y - height / 3) / max(body.winfo_height(), 1))
        parent.winfo_toplevel().bind("<FocusIn>", reveal, add="+")
        return body

    @staticmethod
    def _group(page, title):
        """设置分组: 小标题 + 一组紧挨着的设置行。"""
        tk.Label(page, text=title, bg=BG, fg=SUB, font=(FONT, 9, "bold"), anchor="w").pack(fill="x", pady=("12p", "6p"))
        group = tk.Frame(page, bg=BG)
        group.pack(fill="x")
        return group

    @staticmethod
    def _row(group, title, desc="", stacked=False):
        """设置行: 左侧标题和说明, 右侧放控件 (row.control); stacked 时控件放在下方并占满宽度。"""
        row = tk.Frame(group, bg=CARD, padx="12p", pady="9p")
        row.pack(fill="x", pady=(0, 2))
        row.columnconfigure(0, weight=1)
        text = tk.Frame(row, bg=CARD)
        text.grid(row=0, column=0, sticky="ew")
        wrap = row.winfo_pixels("240p")
        row.title = tk.Label(text, text=title, bg=CARD, fg=FG, font=(FONT, 9), anchor="w", justify="left", wraplength=wrap)
        row.title.pack(fill="x")
        row.desc = tk.Label(text, text=desc, bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w", justify="left", wraplength=wrap)
        row.desc_text = desc
        if desc:
            row.desc.pack(fill="x")
        row.control = tk.Frame(row, bg=CARD)
        if stacked:
            row.control.grid(row=1, column=0, columnspan=2, sticky="ew", pady=("7p", 0))
        else:
            row.control.grid(row=0, column=1, sticky="e", padx=("16p", 0))
        def rewrap(_=None):
            width = row.winfo_width() - 2 * row.winfo_pixels("12p")
            if not stacked:
                width -= row.control.winfo_reqwidth() + row.winfo_pixels("16p")
            if width > 60:
                row.title.config(wraplength=width)
                row.desc.config(wraplength=width)
        row.bind("<Configure>", rewrap)
        return row

    @staticmethod
    def _row_message(row, text=None, error=False):
        """改写设置行的说明文字; 不传 text 时恢复默认说明。"""
        row.desc.config(text=row.desc_text if text is None else text, fg=RED if error else MUTED)
        if row.desc.cget("text") and not row.desc.winfo_manager():
            row.desc.pack(fill="x")

    @staticmethod
    def _enable(rows, on):
        """依赖总开关的设置行: 关闭时标题和控件一起置灰。"""
        for row in rows:
            row.title.config(fg=FG if on else MUTED)
            stack = list(row.control.winfo_children())
            while stack:
                widget = stack.pop()
                stack.extend(widget.winfo_children())
                if isinstance(widget, ttk.Widget):
                    widget.state(["!disabled" if on else "disabled"])
                elif isinstance(widget, tk.Label):
                    widget.config(fg=SUB if on else MUTED)

    @staticmethod
    def _switch(row, value, command):
        """开关: 左侧显示"开/关"文字 (同 Windows 11), command(新状态)。"""
        var = tk.BooleanVar(value=value)
        state = tk.Label(row.control, text="开" if value else "关", bg=CARD, fg=SUB, font=(FONT, 9))
        state.pack(side="left", padx=(0, "8p"))
        row.switch = ttk.Checkbutton(row.control, style="Switch.TCheckbutton", variable=var, cursor="hand2",
                                     command=lambda: command(var.get()))
        row.switch.pack(side="left")
        var.trace_add("write", lambda *_: state.config(text="开" if var.get() else "关"))
        return var

    @staticmethod
    def _number(row, value, low, high, unit, save, increment=1, cast=int):
        """数值输入框 + 单位; 失去焦点、回车或点箭头时校验范围后保存。"""
        var, current = tk.StringVar(value=str(value)), [value]
        def commit(_=None):
            number = cast(config.number(var.get(), current[0], low, high))
            var.set(str(number))
            if number != current[0]:
                current[0] = number
                save(number)
        spin = ttk.Spinbox(row.control, from_=low, to=high, increment=increment, textvariable=var, width=6,
                           style="Card.TSpinbox", command=commit)
        spin.pack(side="left")
        spin.bind("<FocusOut>", commit)
        spin.bind("<Return>", commit)
        # 单位定宽, 各行输入框右侧对齐
        tk.Label(row.control, text=unit, bg=CARD, fg=SUB, font=(FONT, 9), width=4, anchor="w").pack(side="left", padx=("6p", 0))
        return spin

    @staticmethod
    def _choice(row, labels, index, command, width=18):
        """只读下拉框, command(选中序号)。"""
        combo = ttk.Combobox(row.control, values=labels, state="readonly", width=width, style="Card.TCombobox")
        combo.current(index)
        combo.pack(side="left", fill="x", expand=True)
        def chosen(_):
            combo.selection_clear()  # 只读下拉框选中后文字会保持反色高亮, 看着像被选中待编辑
            command(combo.current())
        combo.bind("<<ComboboxSelected>>", chosen)
        return combo

    def _general_page(self, page):
        group = self._group(page, "窗口")
        row = self._row(group, "关闭主窗口时", "点击标题栏 × 时的操作")
        actions = ("ask", "tray", "exit")
        combo = self._choice(row, ("每次询问", "最小化到托盘", "直接退出"), actions.index(self.settings["close_action"]),
                             lambda i: self._save_option("close_action", actions[i]), width=14)
        if not self.tray.ok:  # 托盘不可用时关闭一律退出, 别让选项点了不生效
            combo.state(["disabled"])
            self._row_message(row, "系统托盘不可用，关闭窗口时直接退出。")
        row = self._row(group, "桌面悬浮窗", "置顶显示 CPU 与显卡的温度和利用率，拖动标题栏可移动")
        self._float_var = self._switch(row, self.float_win is not None, lambda _: self.toggle_floating())

        group = self._group(page, "启动")
        row = self._row(group, "开机自动启动", "登录后在托盘中自动开始监控；开启或关闭时需确认一次 UAC")
        def loaded(value):
            auto.set(bool(value))
            row.switch.state(["!disabled"])
        def toggle(want):
            row.switch.state(["disabled"])
            def done(result):
                ok = bool(result and result[0])
                self._row_message(row, result[1] if result else "设置失败，详见本地日志", error=not ok)
                self._background(row, autostart.is_enabled, loaded)
            self._background(row, lambda: autostart.set_enabled(want), done)
        auto = self._switch(row, False, toggle)
        row.switch.state(["disabled"])  # 查询到实际状态前不可操作
        self._background(row, autostart.is_enabled, loaded)

    def _monitor_page(self, page):
        group = self._group(page, "采集")
        row = self._row(group, "刷新间隔", "0.5–30 秒；间隔越短读数越及时，占用略高")
        self._number(row, self.settings["interval"], 0.5, 30, "秒", lambda v: self._save_option("interval", v),
                     increment=0.5, cast=float)
        row = self._row(group, "默认显卡", "主界面、悬浮窗和告警使用的显卡；也可在主界面显卡卡片右上角切换")
        selected_gpu = self.settings["gpu_id"] or None  # 空串按"自动选择"处理
        gpu_ids = [None] + [h for h, _ in self.gpu_list]
        gpu_names = ["自动选择"] + [n + (f"（{i + 1}）" if sum(name == n for _, name in self.gpu_list) > 1 else "")
                                  for i, (_, n) in enumerate(self.gpu_list)]
        if selected_gpu and selected_gpu not in gpu_ids:
            gpu_ids.append(selected_gpu)
            gpu_names.append("已选显卡（暂未连接）")
        def select_gpu(index):
            self.gpu_sel = gpu_ids[index]
            self._save_option("gpu_id", self.gpu_sel)
        self._choice(row, gpu_names, gpu_ids.index(selected_gpu), select_gpu, width=22)

        group = self._group(page, "网速统计")
        row = self._row(group, "统计方式", "自动汇总全部物理网卡；手动选择时可包含 VPN 或虚拟网卡")
        nic_row = self._row(group, "参与统计的网卡", "未勾选任何网卡时不显示网速", stacked=True)
        nic_vars = {}
        def save_nics():
            ids = None if mode.current() == 0 else [h for h, v in nic_vars.items() if v.get()]
            self._save_option("nic_ids", ids)
        def set_mode(index):
            self._enable([nic_row], index == 1)
            save_nics()
        mode = self._choice(row, ("自动汇总物理网卡", "手动选择网卡"), 0 if self.settings["nic_ids"] is None else 1,
                            set_mode, width=16)
        nics = dict((self.data or {}).get("nics", []))
        for h in self.settings["nic_ids"] or []:
            nics.setdefault(h, "暂未连接 · " + h)
        for h, name in nics.items():
            var = nic_vars[h] = tk.BooleanVar(value=h in (self.settings["nic_ids"] or []))
            ttk.Checkbutton(nic_row.control, text=name, variable=var, style="Card.TCheckbutton",
                            command=save_nics).pack(anchor="w")
        if not nics:
            tk.Label(nic_row.control, text="连接 LibreHardwareMonitor 后可选择网卡。", bg=CARD, fg=MUTED,
                     font=(FONT, 9)).pack(anchor="w")
        self._enable([nic_row], self.settings["nic_ids"] is not None)

        group = self._group(page, "主界面")
        row = self._row(group, "显示内容、名称和顺序", "隐藏的指标仍会采集、告警并记录历史")
        ttk.Button(row.control, text="自定义…", style="Card.TButton", command=self.show_customization).pack()

    def _alerts_page(self, page):
        cfg = self.settings["alerts"]
        def number_row(group, key, title, desc, low, high, unit):
            row = self._row(group, title, desc)
            self._number(row, cfg[key], low, high, unit, lambda v: self._save_alert(key, v))
            return row
        def refresh():
            self._enable(temp_rows, cfg["enabled"])
            self._enable(fan_rows, cfg["fan_enabled"])
            self._enable(shared_rows, cfg["enabled"] or cfg["fan_enabled"])
        def switch(key):
            return lambda on: (self._save_alert(key, on), refresh())

        group = self._group(page, "温度阈值")
        shared_rows = [number_row(group, "cpu_temp", "CPU 温度阈值", "高温提醒与风扇异常共用", 40, 110, "°C"),
                       number_row(group, "gpu_temp", "显卡温度阈值", "高温提醒与风扇异常共用", 40, 110, "°C"),
                       number_row(group, "hysteresis", "恢复温差", "降到阈值以下这么多度才解除告警，避免反复提醒", 1, 20, "°C")]
        group = self._group(page, "持续高温")
        row = self._row(group, "持续高温提醒", "温度持续超过阈值时通过托盘通知提醒")
        self._switch(row, cfg["enabled"], switch("enabled"))
        temp_rows = [number_row(group, "duration", "持续时间", "超过阈值持续多久才提醒，忽略短暂峰值", 0, 300, "秒")]
        group = self._group(page, "风扇异常")
        row = self._row(group, "高温时风扇低转速提醒", "仅在高温且识别到 RPM 传感器时检测；显卡低温停转、传感器缺失不会触发")
        self._switch(row, cfg["fan_enabled"], switch("fan_enabled"))
        fan_rows = [number_row(group, "fan_min", "最低转速", "风扇低于该转速视为异常", 1, 5000, "RPM"),
                    number_row(group, "fan_duration", "持续时间", "高温且低转速持续多久才提醒", 5, 600, "秒")]
        group = self._group(page, "通知")
        shared_rows.append(number_row(group, "cooldown", "提醒间隔", "同一项告警两次提醒之间的最短间隔", 30, 7200, "秒"))
        refresh()

    def _router_page(self, page):
        cfg = self.settings["router"]
        def save(**changes):
            self._save_option("router", {**self.settings["router"], **changes})
        group = self._group(page, "OpenWrt 路由器")
        row = self._row(group, "显示路由器状态", "在主界面显示路由器温度、CPU、内存、负载和 WAN 网速")
        self._switch(row, cfg["enabled"], lambda on: (save(enabled=on), self._enable(rows, on)))
        url_row = self._row(group, "指标地址", "路由器上 prometheus-node-exporter-lua 的 /metrics 地址", stacked=True)
        url = tk.StringVar(value=cfg["url"])
        entry = ttk.Entry(url_row.control, textvariable=url, style="Card.TEntry")
        entry.pack(fill="x")
        def save_url(_=None):
            value = url.get().strip()
            if value.lower().startswith(("http://", "https://")):
                self._row_message(url_row)
                if value[:200] != self.settings["router"]["url"]:
                    save(url=value[:200])
            else:
                url.set(self.settings["router"]["url"])
                self._row_message(url_row, "地址需以 http:// 或 https:// 开头，已恢复为上次保存的地址。", error=True)
        entry.bind("<FocusOut>", save_url)
        entry.bind("<Return>", save_url)
        nic_row = self._row(group, "WAN 网卡", "用于统计上下行网速；自动时优先 pppoe-wan / wan，否则取流量最大的网卡")
        router = (self.data or {}).get("router") or {}
        nics = [None] + router.get("nics", [])
        if cfg["nic"] and cfg["nic"] not in nics:
            nics.append(cfg["nic"])
        names = ["自动选择"] + [n if n in router.get("nics", []) else f"{n}（暂未连接）" for n in nics[1:]]
        self._choice(nic_row, names, nics.index(cfg["nic"]), lambda i: save(nic=nics[i]), width=16)
        rows = [url_row, nic_row]
        self._enable(rows, cfg["enabled"])

        group = self._group(page, "配置帮助")
        self._row(group, "在路由器上安装数据接口",
                  "1. 安装 prometheus-node-exporter-lua 和 prometheus-node-exporter-lua-thermal（温度）\n"
                  "2. 把监听接口设为 lan：uci set prometheus-node-exporter-lua.main.listen_interface='lan'，"
                  "uci commit 后重启该服务\n"
                  "3. 在浏览器打开上面的地址，能看到 node_load1 等文本即可；启用并连接后可选择 WAN 网卡")

    def _data_page(self, page):
        group = self._group(page, "历史数据")
        row = self._row(group, "保存历史到本地", "保存最近 24 小时，重启后自动恢复；关闭后仍保留本次运行的历史，已有数据不会删除")
        self._switch(row, self.settings["history_enabled"], lambda on: self._save_option("history_enabled", on))
        group = self._group(page, "诊断")
        row = self._row(group, "导出诊断报告", "包含版本、硬件概览、当前读数、最近告警和采集耗时，不含环境变量和私有路径")
        ttk.Button(row.control, text="导出…", style="Card.TButton", command=self.export_diagnostics).pack()
        row = self._row(group, "数据目录", f"配置、历史与日志保存在 {config.DATA}")
        def open_data():
            try:
                config.DATA.mkdir(parents=True, exist_ok=True)
                os.startfile(config.DATA)
            except OSError:
                logging.getLogger(__name__).exception("Cannot open data directory")
                self._row_message(row, "无法打开数据目录，详见本地日志。", error=True)
        ttk.Button(row.control, text="打开", style="Card.TButton", command=open_data).pack()
        group = self._group(page, "关于")
        self._row(group, f"硬件监控 {config.VERSION}",
                  "传感器数据由 LibreHardwareMonitor（MPL-2.0）提供，本程序只读取并展示，不直接访问硬件。")

    def _save_option(self, key, value):
        self.settings[key] = value
        self._save_settings()
        self.collector.configure(self.settings)

    def _save_alert(self, key, value):
        self.settings["alerts"][key] = value
        self._save_settings()

    def show_customization(self):
        existing = getattr(self, "customization_win", None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        parent = getattr(self, "settings_win", None)
        parent = parent if parent and parent.winfo_exists() else self.root
        w, body = self._dialog("自定义显示", parent=parent, resizable=True)
        self.customization_win = w
        self._size_dialog(w, "460p", "420p")
        names = {**METRIC_NAMES, **GROUP_NAMES, **self.sensor_names}
        prefs = copy.deepcopy(self.settings["display"])
        # Only offer known sensors; disconnected preferences remain preserved.
        categories = {"CPU 指标": [k for k in METRIC_NAMES if not k.startswith("gpu_")],
                      "显卡指标": [k for k in METRIC_NAMES if k.startswith("gpu_")],
                      "可选分组": list(GROUP_NAMES)}
        for group, label in GROUP_NAMES.items():
            keys = [k for k in self.sensor_names if k.startswith(f"sensor:{group}:")]
            if keys:
                categories[label + " · 传感器"] = keys
        defaults = {label: list(keys) for label, keys in categories.items()}  # "恢复默认"用的原始顺序
        for keys in categories.values():
            keys.sort(key=lambda k: prefs.get(k, {}).get("order", 100))
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, "8p"))
        ttk.Label(top, text="编辑分组").pack(side="left", padx=(0, "8p"))
        category = tk.StringVar(value=next(iter(categories)))
        selector = ttk.Combobox(top, textvariable=category, values=list(categories), state="readonly", width=22)
        selector.pack(side="left", fill="x", expand=True)
        self._hint(body, "双击或按空格切换显示；选中后可改名称，上移、下移只调整当前分组。")
        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True, pady="10p")
        tree = ttk.Treeview(tree_frame, columns=("name", "shown"), show="headings", height=7, selectmode="browse")
        tree.heading("name", text="指标名称", anchor="w")
        tree.heading("shown", text="显示", anchor="center")
        tree.column("name", width=w.winfo_pixels("260p"), minwidth=120)
        tree.column("shown", width=w.winfo_pixels("55p"), minwidth=50, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tree.tag_configure("hidden", foreground=MUTED)  # 隐藏的指标整行变暗, 一眼区分
        def row_values(key, p):
            shown = p.get("visible", True)
            return {"values": (p.get("name") or names[key], "显示" if shown else "已隐藏"), "tags": () if shown else ("hidden",)}
        name, visible = tk.StringVar(), tk.BooleanVar(value=True)
        selected, loading = [None], [False]
        edit = ttk.LabelFrame(body, text="所选指标", padding="10p")
        edit.pack(fill="x")
        edit.columnconfigure(1, weight=1)
        ttk.Label(edit, text="显示名称").grid(row=0, column=0, padx=(0, "8p"))
        entry = ttk.Entry(edit, textvariable=name, width=20)
        entry.grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(edit, text="显示", variable=visible).grid(row=0, column=2, padx=("10p", 0))
        original = ttk.Label(edit, text="", style="Hint.TLabel")
        original.grid(row=1, column=0, columnspan=3, sticky="w", pady=("6p", 0))
        buttons = ttk.Frame(body, padding=(0, "12p", 0, 0))
        buttons.pack(fill="x")
        # Reserve the editor/actions before giving the remaining height to the list.
        buttons.pack_configure(side="bottom", before=tree_frame)
        edit.pack_configure(side="bottom", before=tree_frame)
        def stage(*_):
            key = selected[0]
            if key and not loading[0]:
                prefs[key] = {**prefs.get(key, {}), "name": name.get().strip()[:60], "visible": visible.get()}
                tree.item(key, **row_values(key, prefs[key]))
        name.trace_add("write", stage)
        visible.trace_add("write", stage)
        def toggle_visible(_=None):
            if selected[0]:
                visible.set(not visible.get())
            return "break"
        tree.bind("<Double-1>", toggle_visible)
        tree.bind("<space>", toggle_visible)
        def choose(_=None):
            selection = tree.selection()
            if not selection:
                return
            key = selected[0] = selection[0]
            loading[0] = True
            try:
                name.set(prefs.get(key, {}).get("name") or names[key])
                visible.set(prefs.get(key, {}).get("visible", True))
                original.config(text="默认名称：" + names[key])
            finally:
                loading[0] = False
            keys = categories[category.get()]
            index = keys.index(key)
            up.state(["disabled"] if index == 0 else ["!disabled"])
            down.state(["disabled"] if index == len(keys) - 1 else ["!disabled"])
        tree.bind("<<TreeviewSelect>>", choose)
        def redraw(select=None):
            selected[0] = None
            tree.delete(*tree.get_children())
            keys = categories[category.get()]
            for key in keys:
                tree.insert("", "end", iid=key, **row_values(key, prefs.get(key, {})))
            key = select if select in keys else keys[0]
            tree.selection_set(key)
            tree.focus(key)
            tree.see(key)
            choose()
        def move(delta):
            keys = categories[category.get()]
            key = selected[0]
            if key:
                old = keys.index(key)
                new = min(len(keys) - 1, max(0, old + delta))
                keys.insert(new, keys.pop(old))
                redraw(key)
        up = ttk.Button(buttons, text="上移", command=lambda: move(-1))
        up.pack(side="left")
        down = ttk.Button(buttons, text="下移", command=lambda: move(1))
        down.pack(side="left", padx="5p")
        def reset():
            """当前分组恢复默认名称、显示状态和顺序 (仍需保存才生效)。"""
            label = category.get()
            for key in defaults[label]:
                prefs.pop(key, None)
            categories[label][:] = defaults[label]
            redraw(selected[0])
        ttk.Button(buttons, text="恢复默认", command=reset).pack(side="left")
        def save():
            for keys in categories.values():
                for order, key in enumerate(keys):
                    prefs.setdefault(key, {})["order"] = order
            self.settings["display"] = prefs
            saved = self._save_settings()
            self._apply_display()
            if self.data:
                self.render(self.data)
            if saved:
                w.destroy()
            else:
                original.config(text="保存失败，请检查目录权限后重试。")
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=w.destroy).pack(side="right", padx="6p")
        selector.bind("<<ComboboxSelected>>", lambda _: redraw())
        redraw()
        self._show_dialog(w)

    def export_diagnostics(self):
        path = filedialog.asksaveasfilename(parent=self.settings_win if getattr(self, "settings_win", None) and self.settings_win.winfo_exists() else self.root,
                                          title="导出诊断报告", defaultextension=".json",
                                          filetypes=[("JSON", "*.json"), ("纯文本", "*.txt")], initialfile="hardware-diagnostics.json")
        if not path:
            return
        data, events = copy.deepcopy(self.data), list(self.recent_alerts)
        def work():
            payload = diagnostics.report(data, hwinfo.load(), events, {"last_read_ms": (data or {}).get("collection_ms"),
                                                                     "history_samples": len(self.history)})
            diagnostics.export(path, payload)
            return True
        self._background(self.root, work, lambda ok: self._flash("诊断报告已导出" if ok else "诊断导出失败，详见本地日志"))

    def show_history(self):
        if getattr(self, "history_win", None) and self.history_win.winfo_exists():
            self.history_win.deiconify()
            self.history_win.lift()
            self.history_win.focus_set()
            return
        w = self.history_win = tk.Toplevel(self.root)
        w.title("历史数据")
        w.configure(bg=BG)
        w.minsize(w.winfo_pixels("480p"), w.winfo_pixels("400p"))
        w.geometry(f"{w.winfo_pixels('600p')}x{w.winfo_pixels('500p')}")
        w.attributes("-topmost", self.root.attributes("-topmost"))
        from monitor import set_icon
        set_icon(w)
        controls = ttk.Frame(w, padding=10)
        controls.pack(fill="x")
        controls.columnconfigure(4, weight=1)
        metric, span, gpu_var = tk.StringVar(value="温度"), tk.StringVar(value="1 小时"), tk.StringVar()
        for i, (label, var, values) in enumerate((("指标", metric, ("温度", "利用率", "网络")), ("范围", span, ("1 小时", "24 小时")))):
            ttk.Label(controls, text=label).grid(row=0, column=i * 2, padx=(0, 8))
            combo = ttk.Combobox(controls, textvariable=var, values=values, state="readonly", width=9)
            combo.grid(row=0, column=i * 2 + 1, padx=(0, 16), sticky="w")
        interval = tk.StringVar(value="自动")
        intervals = {"原始": 0, "30 秒": 30, "1 分钟": 60, "5 分钟": 300, "15 分钟": 900}
        ttk.Label(controls, text="显示间隔").grid(row=1, column=0, padx=(0, 8), pady=(8, 0))
        ttk.Combobox(controls, textvariable=interval, values=("自动", *intervals), state="readonly", width=9).grid(
            row=1, column=1, padx=(0, 16), pady=(8, 0), sticky="w")
        summary_note = ttk.Label(controls, style="Hint.TLabel")
        summary_note.grid(row=1, column=2, columnspan=4, pady=(8, 0), sticky="w")
        def display_interval():
            return intervals.get(interval.get(), 300 if span.get() == "24 小时" else 30)
        scope = ttk.Label(w, font=(FONT, 10, "bold"), padding=(10, 2, 10, 6))
        scope.pack(fill="x")
        legend = ttk.Frame(w, padding=(10, 0, 10, 6))
        legend.pack(fill="x")
        legend.columnconfigure(4, weight=1)
        legend_labels = {}
        for key, name, color in (("cpu", "CPU", "#60a5fa"), ("memory", "内存", "#4ade80"),
                                 ("vram", "显存", "#fbbf24"), ("upload", "上传", "#60a5fa"),
                                 ("download", "下载", "#a78bfa")):
            legend_labels[key] = ttk.Label(legend, text="● " + name, foreground=color)
        gpu_group = ttk.Frame(legend)
        gpu_label = ttk.Label(gpu_group, text="● 显卡", foreground="#a78bfa", wraplength=w.winfo_pixels("200p"))
        gpu_label.pack(side="left")
        gpu = ttk.Combobox(gpu_group, textvariable=gpu_var, state="readonly", width=20)
        gpu_note = ttk.Label(w, style="Hint.TLabel", padding=(10, 0, 10, 6))
        gpu_map = {}
        gpu_groups = {}
        chosen_id = [None]  # 上次选中的显卡身份; 显示名会随同名卡增减变化, 身份不会
        canvas = tk.Canvas(w, bg=BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=10, pady=8)
        def hover_hint():
            seconds = display_interval()
            if not seconds:
                return "显示原始曲线；移动鼠标查看原始读数，断连处留空。"
            label = f"{seconds // 60} 分钟" if seconds >= 60 else f"{seconds} 秒"
            return f"每 {label}精简绘制，保留高低点；移动鼠标查看原始读数。"
        hover_text = tk.StringVar(value=hover_hint())
        hover_label = ttk.Label(w, textvariable=hover_text, padding=(10, 0, 10, 8), wraplength=400)
        hover_label.pack(fill="x")
        hover_label.bind("<Configure>", lambda e: hover_label.configure(wraplength=max(100, e.width - 20)))
        chart = {}
        snapshot, line_cache = {}, {}
        pending = [None]
        def selected_rows():
            if span.get() == snapshot.get("span"):
                return snapshot["start"], snapshot["end"], snapshot["rows"]
            end = time.time()
            start = end - (86400 if span.get() == "24 小时" else 3600)
            rows = sorted(((t, v) for t, v in self.history if start <= t <= end), key=lambda row: row[0])
            devices = {v["gpu_id"]: v.get("gpu_name") for _, v in self.history if v.get("gpu_id")}
            snapshot.update(span=span.get(), start=start, end=end, rows=rows, times=[t for t, _ in rows], devices=devices)
            line_cache.clear()
            return start, end, rows
        def export():
            path = filedialog.asksaveasfilename(parent=w, title="导出当前范围", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
            if path:
                _, _, rows = selected_rows()
                selected = gpu_map.get(gpu_var.get())
                selected_ids = gpu_groups.get(selected, (None, frozenset((selected,))))[1]
                filtered = [(t, history.filter_sample(values, selected_ids, (self.data or {}).get("net_selection")))
                            for t, values in rows]
                self._background(w, lambda: (history.export_csv(path, filtered), True)[1],
                                 lambda ok: hover_text.set("CSV 已导出" if ok else "导出失败，详见本地日志"))
        ttk.Button(controls, text="导出 CSV…", command=export).grid(row=0, column=5, sticky="e")
        def draw(*_):
            if not w.winfo_exists():
                return
            start, end, rows = selected_rows()
            old_id = gpu_map.get(gpu_var.get()) or chosen_id[0] or (self.data or {}).get("gpu_id")
            gpu_groups.clear()
            gpu_groups.update(history.gpu_groups([*snapshot["devices"].items(), *self.gpu_list]))
            old_id = next((identity for identity, (_, members) in gpu_groups.items() if old_id in members), old_id)
            names = [" ".join(name.casefold().split()) for name, _ in gpu_groups.values()]
            gpu_map.clear()
            gpu_map.update({f"{i + 1} · {name}" if names.count(" ".join(name.casefold().split())) > 1 else name: identity
                            for i, (identity, (name, _)) in enumerate(gpu_groups.items())})
            gpu.configure(values=list(gpu_map))
            gpu_var.set(next((n for n, h in gpu_map.items() if h == old_id), next(iter(gpu_map), "无显卡数据")))
            chosen_id[0] = gpu_map.get(gpu_var.get())
            selected = gpu_map.get(gpu_var.get())
            selected_ids = gpu_groups.get(selected, (None, frozenset((selected,))))[1]
            seconds = display_interval()
            summary_note.config(text="折线显示，保留区间高低点" if seconds else "原始曲线，保留峰值")
            view = (metric.get(), span.get(), selected_ids, seconds)
            if chart.get("view") != view:
                hover_text.set(hover_hint())
            scope.config(text={"温度": "CPU 与显卡温度", "利用率": "CPU、显卡、内存与显存利用率",
                               "网络": "网络上传与下载速度"}[metric.get()])
            for widget in legend.winfo_children():
                widget.grid_remove()
            gpu_note.pack_forget()
            if metric.get() == "网络":
                legend_labels["upload"].grid(row=0, column=0, sticky="w", padx=(0, 18))
                legend_labels["download"].grid(row=0, column=1, sticky="w")
            else:
                legend_labels["cpu"].grid(row=0, column=0, sticky="w", padx=(0, 18))
                gpu_group.grid(row=0, column=1, sticky="w", padx=(0, 18))
                if len(gpu_map) > 1:
                    gpu_label.config(text="● 显卡曲线")
                    gpu.pack(side="left", padx=(6, 0))
                    gpu_note.config(text="显卡选择仅影响显卡" + ("和显存" if metric.get() == "利用率" else "") + "曲线，其他曲线保持显示。")
                    gpu_note.pack(fill="x", before=canvas)
                else:
                    gpu.pack_forget()
                    from monitor import short_gpu
                    gpu_label.config(text="● 显卡" + (" · " + short_gpu(gpu_var.get()) if selected else "（暂无数据）"))
                if metric.get() == "利用率":
                    legend_labels["memory"].grid(row=0, column=2, sticky="w", padx=(0, 18))
                    legend_labels["vram"].grid(row=0, column=3, sticky="w")
            specs = {"温度": (("cpu_temp", "CPU", "#60a5fa"), ("gpu_temp", "显卡", "#a78bfa")),
                     "利用率": (("cpu_load", "CPU", "#60a5fa"), ("gpu_load", "显卡", "#a78bfa"), ("mem_load", "内存", "#4ade80"), ("gpu_mem_load", "显存", "#fbbf24")),
                     "网络": (("net_up", "上传", "#60a5fa"), ("net_down", "下载", "#a78bfa"))}[metric.get()]
            unit = {"温度": "°C", "利用率": "%", "网络": "MB/s"}[metric.get()]
            factor = 1024 ** 2 if metric.get() == "网络" else 1
            width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 150)
            px = lambda points: canvas.winfo_pixels(f"{points}p")
            left, right = px(42), width - px(12)
            net_selection = (self.data or {}).get("net_selection")
            cache_key = (metric.get(), right - left, selected_ids, net_selection, seconds)
            if line_cache.get("key") != cache_key:
                lines, peaks = [], {}
                for key, name, color in specs:
                    runs = history.segments(rows, key, start, end, right - left, selected, net_selection,
                                            gpu_ids=selected_ids, interval=seconds)
                    lines.append((key, name, color, runs))
                    peaks[key] = max((v for run in runs for _, v in run), default=None)
                line_cache.update(key=cache_key, lines=lines, peaks=peaks)
            lines = line_cache["lines"]
            values = [v / factor for v in line_cache["peaks"].values() if v is not None]
            # Keep a common scale within each view so the separate panels remain
            # comparable. Percentages always use the full 0–100% range.
            lo = 0
            hi = 100 if metric.get() == "利用率" else max(max(values, default=0) * 1.1,
                                                            .01 if metric.get() == "网络" else 1)
            canvas.delete("all")
            gap, axis_height = px(7), px(22)
            panel_height = (height - axis_height - gap * (len(lines) - 1)) / len(lines)
            panels = []
            hover_points = []
            for index, (key, name, color, runs) in enumerate(lines):
                panel_top = index * (panel_height + gap)
                top, bottom = panel_top + px(26), panel_top + panel_height - px(10)
                panels.append((top, bottom))
                canvas.create_rectangle(0, panel_top, width, panel_top + panel_height,
                                        fill=CARD, outline="", tags="panel")
                canvas.create_text(px(10), panel_top + px(11), text="● " + name, anchor="w",
                                   fill=color, font=(FONT, 9, "bold"))
                peak = line_cache["peaks"][key]
                canvas.create_text(right, panel_top + px(11),
                                   text=f"最高 {peak / factor:.1f} {unit}" if peak is not None else "暂无数据",
                                   anchor="e", fill=MUTED, font=(NUM, 8))
                ticks = (0, .5, 1) if bottom - top >= px(45) else (0, 1)
                for fraction in ticks:
                    value = hi * fraction
                    y = bottom - (bottom - top) * fraction
                    label = f"{value:.0f}" if metric.get() == "利用率" else f"{value:.2f}" if hi < 1 else f"{value:.1f}"
                    canvas.create_text(left - px(6), y, text=label, anchor="e", fill=MUTED, font=(NUM, 8))
                    canvas.create_line(left, y, right, y, fill=TRACK)
                canvas.create_line(left, top, left, bottom, fill=TRACK)
                if peak is None:
                    canvas.create_text((left + right) / 2, (top + bottom) / 2,
                                       text="当前范围暂无数据", fill=MUTED, font=(FONT, 9))
                for run in runs:
                    pts = []
                    for t, v in run:
                        value = min(hi, max(lo, v / factor))
                        x = left + (t - start) / (end - start) * (right - left)
                        y = bottom - (value - lo) / (hi - lo) * (bottom - top)
                        pts.extend((x, y))
                        hover_points.append((x, y, t, v, name))
                    if len(pts) >= 4:
                        canvas.create_line(*pts, fill=color, width=1.5, tags=("series", key))
                    elif pts:
                        x, y = pts
                        canvas.create_line(x - 1, y, x + 1, y, fill=color, width=1.5, tags=("series", key))
            for i in range(5):
                t = start + (end - start) * i / 4
                label = span.get() + "前" if i == 0 else "现在" if i == 4 else time.strftime("%H:%M", time.localtime(t))
                canvas.create_text(left + (right - left) * i / 4, height - px(8), text=label,
                                   anchor="w" if i == 0 else "e" if i == 4 else "center", fill=MUTED, font=(NUM, 8))
            hover_points.sort(key=lambda point: point[0])
            chart.update(view=view, rows=rows, times=snapshot["times"], start=start, end=end, left=left, right=right,
                         specs=specs, selected_ids=selected_ids, factor=factor, unit=unit, panels=panels, interval=seconds,
                         hover_points=hover_points, point_xs=[point[0] for point in hover_points], hit_radius=px(4))
        def hover(event):
            if not chart or not chart["rows"]:
                return
            c = chart
            radius = c["hit_radius"]
            # Snap to actual vertices so a narrow spike remains inspectable in
            # a 24-hour view, without drawing any permanent point markers.
            first = bisect_left(c["point_xs"], event.x - radius)
            last = bisect_left(c["point_xs"], event.x + radius)
            nearby = [point for point in c["hover_points"][first:last] if abs(point[1] - event.y) <= radius]
            if nearby:
                x, y, t, v, name = min(nearby, key=lambda point: ((point[0] - event.x) ** 2 + (point[1] - event.y) ** 2, -point[3]))
                canvas.delete("crosshair")
                for top, bottom in c["panels"]:
                    canvas.create_line(x, top, x, bottom, fill=MUTED, dash=(3, 4), tags="crosshair")
                hover_text.set(time.strftime("%m-%d %H:%M:%S", time.localtime(t)) +
                               f" · {name} {v / c['factor']:.1f}{c['unit']} · 原始采样")
                return
            x = min(c["right"], max(c["left"], event.x))
            at = c["start"] + (x - c["left"]) / (c["right"] - c["left"]) * (c["end"] - c["start"])
            index = bisect_left(c["times"], at)
            candidates = c["rows"][max(0, index - 1):index + 1]
            t, values = min(candidates, key=lambda row: abs(row[0] - at))
            canvas.delete("crosshair")
            for top, bottom in c["panels"]:
                canvas.create_line(x, top, x, bottom, fill=MUTED, dash=(3, 4), tags="crosshair")
            if abs(t - at) > 35:
                hover_text.set(time.strftime("%m-%d %H:%M:%S", time.localtime(at)) + " · 此时无采样")
                return
            items = [time.strftime("%m-%d %H:%M:%S", time.localtime(t))]
            if c["interval"]:
                items.append("原始采样")
            values = history.filter_sample(values, c["selected_ids"], (self.data or {}).get("net_selection"))
            for key, name, _ in c["specs"]:
                v = values.get(key)
                items.append(name + (f" {v / c['factor']:.1f}{c['unit']}" if v is not None else " --"))
            hover_text.set(" · ".join(items))
        def schedule(*_):
            if pending[0] is not None:
                w.after_cancel(pending[0])
            def redraw():
                pending[0] = None
                draw()
            pending[0] = w.after(80, redraw)
        for var in (metric, span, interval):
            var.trace_add("write", schedule)
        gpu.bind("<<ComboboxSelected>>", schedule)
        canvas.bind("<Configure>", schedule)
        canvas.bind("<Motion>", hover)
        def leave(_):
            canvas.delete("crosshair")
            hover_text.set(hover_hint())
        canvas.bind("<Leave>", leave)
        refresh_id = [None]
        def refresh():
            if w.winfo_exists():
                snapshot.clear()
                schedule()
                refresh_id[0] = w.after(5000, refresh)
        def cleanup(event):
            if event.widget is w:
                for timer in (pending[0], refresh_id[0]):
                    if timer is not None:
                        w.after_cancel(timer)
        w.bind("<Destroy>", cleanup, add="+")
        refresh()
        self.center_on_main(w)
