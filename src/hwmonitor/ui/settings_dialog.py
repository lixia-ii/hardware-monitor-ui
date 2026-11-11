"""设置窗口: 左侧导航 + 设置行 (常规、监控、告警、设备、数据与关于), 修改即时生效并保存。"""
import copy
import logging
import os
import tkinter as tk
from uuid import uuid4
from tkinter import ttk, filedialog

from hwmonitor import __version__, paths, settings
from hwmonitor.core import diagnostics
from hwmonitor.sensors import hwinfo
from hwmonitor.system import autostart, credentials
from hwmonitor.system.webserver import lan_addresses
from hwmonitor.ui.theme import ACCENT, BG, BORDER, CARD, FG, FONT, MUTED, NAV_HOVER, RED, SUB

INPUTS = ("TEntry", "TSpinbox", "TCombobox", "Entry", "Spinbox", "Text")  # 点这些控件时保留焦点


class SettingsDialogMixin:


    # 设置窗口左侧导航: (键, 标题, Segoe MDL2 Assets 图标)
    SETTINGS_PAGES = (("general", "常规", "\ue713"), ("monitor", "监控", "\ue9d9"), ("alerts", "告警", "\uea8f"),
                      ("router", "设备", "\ue968"), ("data", "数据与关于", "\ue946"))

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
            frame = tk.Frame(content, bg=BG)
            tk.Label(frame, text=title, bg=BG, fg=FG, font=(FONT, 15, "bold"), anchor="w").pack(
                fill="x", padx="20p", pady=("12p", "2p"))
            builders[key](self._scroll_area(frame))
            self.settings_pages[key] = frame
        keys = [k for k, _, _ in self.SETTINGS_PAGES]
        def cycle(step):
            self.settings_select(keys[(keys.index(self._settings_page) + step) % len(keys)])
            return "break"
        for seq, step in (("<Control-Tab>", 1), ("<Control-Next>", 1), ("<Control-Shift-Tab>", -1), ("<Control-Prior>", -1)):
            w.bind(seq, lambda _, s=step: cycle(s))
        # 点空白处、开关、导航等非输入控件时, 让输入框失去焦点, 触发它的 <FocusOut> 保存 (Tk 点框架不会转移焦点)
        def blur(event):
            # ttk 按钮按下时已自行获得焦点；再抢到窗口会打断这次点击。
            if isinstance(event.widget, tk.Misc) and event.widget.winfo_class() not in (*INPUTS, "TButton"):
                w.focus_set()
        w.bind("<Button-1>", blur, add="+")
        self.settings_select(page or "general")
        self._show_dialog(w)

    @staticmethod
    def _finish_edit(entry, commit):
        """回车或空格结束编辑: 保存并移走焦点 (数值和地址里都不需要空格)。"""
        def finish(_):
            commit()
            entry.winfo_toplevel().focus_set()
            return "break"
        for seq in ("<Return>", "<KP_Enter>", "<space>"):
            entry.bind(seq, finish)

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
                if event.widget.winfo_class() != "TSpinbox":
                    canvas.yview_scroll(-int(event.delta / 120), "units")
        parent.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        # Keyboard navigation keeps focused controls inside the viewport.
        # FocusIn 也会发给获得焦点控件的每一层父容器; 整组、整页这类容器放不进视口, 只处理真正的焦点控件,
        # 否则点按钮或下拉框时页面会跳动 (按钮要再点一次, 下拉列表按跳动前的位置弹出)。
        # 下拉框的弹出列表是独立的顶层窗口, 也不参与。
        toplevel = parent.winfo_toplevel()
        def reveal(event):
            widget = event.widget
            if (not canvas.winfo_ismapped() or not isinstance(widget, tk.Misc) or widget is not widget.focus_get()
                    or widget.winfo_toplevel() is not toplevel or not str(widget).startswith(str(body) + ".")):
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
        """数值输入框 + 单位; 失去焦点、回车/空格或点箭头时校验范围后保存。"""
        var, current = tk.StringVar(value=str(value)), [value]
        def commit(_=None):
            number = cast(settings.number(var.get(), current[0], low, high))
            var.set(str(number))
            if number != current[0]:
                current[0] = number
                save(number)
        spin = ttk.Spinbox(row.control, from_=low, to=high, increment=increment, textvariable=var, width=6,
                           style="Card.TSpinbox", command=commit)
        spin.pack(side="left")
        spin.bind("<FocusOut>", commit)
        SettingsDialogMixin._finish_edit(spin, commit)
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
        row = self._row(group, "桌面悬浮窗", "显示 CPU、显卡、内存和网速；可拖动、贴边隐藏，右键设置置顶等选项")
        self._float_var = self._switch(row, self.float_win is not None, lambda _: self.toggle_floating())
        row = self._row(group, "主窗口置顶", "主窗口和它打开的历史、硬件信息等窗口保持在最前；也可在托盘图标右键菜单切换")
        self._topmost_var = self._switch(row, self.settings["topmost"], self.set_topmost)

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

        group = self._group(page, "网页服务")
        web_row = self._row(group, "启用网页服务", "提供与 LibreHardwareMonitor 兼容的 data.json 接口，供其他程序读取传感器数据")
        lan_row = self._row(group, "允许局域网访问", "同一局域网的设备可通过本机 IP 访问；会添加仅限本地子网的防火墙规则")
        port_row = self._row(group, "端口", "1024–65535")
        url_row = self._row(group, "访问地址", "")
        def refresh(error=None):
            web = self.settings["web"]
            self._enable([lan_row, port_row, url_row], web["enabled"])
            urls = [f"http://127.0.0.1:{web['port']}/data.json"]
            if web["lan"]:
                urls += [f"http://{ip}:{web['port']}/data.json" for ip in lan_addresses()]
            if error:
                self._row_message(url_row, error, error=True)
            else:
                self._row_message(url_row, "\n".join(urls) if web["enabled"] else "开启网页服务后可用")
        def save(**changes):
            if changes.get("port") == self.settings["management"]["port"]:
                self._row_message(url_row, "内网数据端口必须与手机管理端口不同。", error=True)
                return
            self._save_option("web", {**self.settings["web"], **changes})
            refresh(self.web.error)
        self._switch(web_row, self.settings["web"]["enabled"], lambda on: save(enabled=on))
        self._switch(lan_row, self.settings["web"]["lan"], lambda on: save(lan=on))
        self._number(port_row, self.settings["web"]["port"], 1024, 65535, "", lambda v: save(port=v))
        refresh(self.web.error)
        self._management_controls(page)

    def _management_controls(self, page):
        group = self._group(page, "手机管理端")
        cfg = self.settings["management"]
        status_row = self._row(group, "管理地址", "")
        def refresh(error=None):
            current = self.settings["management"]
            urls = [f"http://127.0.0.1:{current['port']}/"]
            urls += [f"http://{ip}:{current['port']}/" for ip in lan_addresses()]
            message = "\n".join(urls) if self.management.server else error or "管理端未启动；启用后可通过局域网访问。"
            self._row_message(status_row, message, error=bool(error))
        def save(**changes):
            current = {**self.settings["management"], **changes}
            if current["port"] == self.settings["web"]["port"]:
                refresh("管理端口必须与内网数据端口不同。")
                return
            self._save_option("management", current)
            refresh(self.management.error)
        row = self._row(group, "启用手机管理端", "默认端口 7076，启用后同一局域网的设备可直接访问")
        self._switch(row, cfg["enabled"], lambda on: save(enabled=on))
        row = self._row(group, "管理端口", "1024–65535，与内网数据端口独立")
        self._number(row, cfg["port"], 1024, 65535, "", lambda v: save(port=v))
        refresh(self.management.error)

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
            tk.Label(nic_row.control, text="硬件传感器初始化完成后可选择网卡。", bg=CARD, fg=MUTED,
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

    def _save_routers(self, items):
        # 与磁盘上读回的格式保持一致 (补全 type 等字段、规范地址), 采集线程和界面拿到的都是校验后的配置
        items = settings.validate({"routers": items})["routers"]
        active_keys = {"router-settings:" + item["id"] for item in items}
        self.settings["collapsed"] = [key for key in self.settings["collapsed"]
                                      if not key.startswith("router-settings:") or key in active_keys]
        # 已删除设备的主界面收起状态、显示偏好和远程指标偏好一并清理, 不在配置里残留。
        removed = {item["id"] for item in self.settings["routers"]} - {item["id"] for item in items}
        if removed:
            stale = {"router:" + ident for ident in removed}
            prefixes = tuple("sensor:device:" + ident + ":" for ident in removed)
            self.settings["collapsed"] = [key for key in self.settings["collapsed"] if key not in stale]
            display = self.settings["display"]
            for key in [key for key in display if key in stale or key.startswith(prefixes)]:
                del display[key]
            for key in [key for key in self.sensor_names if key.startswith(prefixes)]:
                del self.sensor_names[key]
        self.settings["router"] = settings.legacy_router(items)
        self._save_option("routers", items)

    def _router_page(self, page):
        row = self._row(page, "加入设备", "连接路由器或远程电脑，在主界面集中查看设备状态")
        editors = tk.Frame(page, bg=BG)
        editors.pack(fill="x")
        # 每台设备一个独立容器, 只增删或替换对应的那一个: 整体重建会让页面瞬间变短, 滚动条闪烁、页面跳动。
        def build(cfg, before=None):
            box = tk.Frame(editors, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            box.pack(fill="x", pady=("10p", 0), **({"before": before} if before is not None else {}))
            def rebuild():
                current = next(item for item in self.settings["routers"] if item["id"] == cfg["id"])
                build(current, before=box)  # 先建好新的再删旧的, 页面高度不会中途缩短
                box.destroy()
            self._router_editor(box, cfg, rebuild)
        def add():
            cfg = dict(settings.DEFAULT_ROUTER, id=uuid4().hex, name=settings.new_device_name(self.settings["routers"]))
            self._save_routers([*self.settings["routers"], cfg])
            build(cfg)
        ttk.Button(row.control, text="＋ 添加设备", style="Accent.TButton", command=add).pack()
        for cfg in self.settings["routers"]:
            build(cfg)

        # 帮助的折叠键不用 router-settings: 前缀, 否则删除设备时会被当作残留键清掉
        _, group = self._fold_section(page, "配置帮助", "settings:router-help")
        self._row(group, "接入 TP-Link 路由器",
                  "选择“TP-Link（原厂固件）”，填写管理地址，在管理员密码框中输入密码，点击“保存并连接”。\n"
                  "密码由 Windows 加密保存，仅当前 Windows 用户可读取；复制到其他电脑后需重新输入。\n"
                  "适配 TL-XDR3040 易展版的登录与 WAN 接口；登录失败后停止重试，重新输入密码再连接。")
        self._row(group, "接入远程电脑",
                  "1. 在对方电脑打开“硬件监控”，进入 设置 → 常规 → 网页服务\n"
                  "2. 开启“启用网页服务”和“允许局域网访问”\n"
                  "3. 加入设备后，在下方设备内选择“远程电脑”，填写对方显示的地址，例如 http://192.168.1.2:8085/data.json，开启监控")
        self._row(group, "接入 OpenWrt 数据接口",
                  "1. 安装 prometheus-node-exporter-lua 和 prometheus-node-exporter-lua-thermal（温度）\n"
                  "2. 把监听接口设为 lan：uci set prometheus-node-exporter-lua.main.listen_interface='lan'，"
                  "uci commit 后重启该服务\n"
                  "3. 在浏览器打开上面的地址，能看到 node_load1 等文本即可；启用并连接后可选择 WAN 网卡")

    def _fold_section(self, page, title, key, subtitle=None):
        """可折叠的分组: 标题行 (箭头、标题、展开/收起) + 内容框架, 折叠状态以 key 存入 collapsed。
        点击标题行或聚焦后按回车/空格切换。返回 (标题行, 内容框架)。"""
        expanded_glyph, collapsed_glyph, glyph_font = self._chevrons()
        bg = CARD if subtitle is not None else BG
        head = tk.Frame(page, bg=bg, cursor="hand2", takefocus=True,
                        padx="12p" if subtitle is not None else 0, pady="10p" if subtitle is not None else 0)
        head.pack(fill="x", pady=0 if subtitle is not None else ("12p", "6p"))
        arrow = tk.Label(head, bg=bg, fg=SUB, font=glyph_font)
        arrow.pack(side="left", padx=(0, "6p"))
        text = tk.Frame(head, bg=bg)
        text.pack(side="left", fill="x", expand=True)
        head.label = tk.Label(text, text=title, bg=bg, fg=FG if subtitle is not None else SUB,
                              font=(FONT, 10 if subtitle is not None else 9, "bold"), anchor="w", justify="left")
        head.label.pack(fill="x")
        head.subtitle = tk.Label(text, text=subtitle or "", bg=bg, fg=MUTED, font=(FONT, 8), anchor="w")
        if subtitle is not None:
            head.subtitle.pack(fill="x", pady=("3p", 0))
        action = tk.Label(head, bg=bg, fg=MUTED, font=(FONT, 8))
        action.pack(side="right")
        group = tk.Frame(page, bg=bg)
        def rewrap(_=None):
            width = max(40, text.winfo_width())
            head.label.config(wraplength=width)
            head.subtitle.config(wraplength=width)
        text.bind("<Configure>", rewrap)

        def refresh():
            collapsed = key in self.settings["collapsed"]
            arrow.config(text=collapsed_glyph if collapsed else expanded_glyph)
            action.config(text="展开" if collapsed else "收起")
            if collapsed:
                group.pack_forget()
            else:
                group.pack(fill="x", after=head)

        def toggle(_=None):
            # 收起前先结束输入，保存尚未失焦的名称或地址。
            head.focus_set()
            keys = set(self.settings["collapsed"])
            if key in keys:
                keys.remove(key)
            else:
                keys.add(key)
            self.settings["collapsed"] = sorted(keys)
            refresh()
            self._save_settings()
            return "break"

        head.toggle = toggle
        for widget in (head, arrow, text, head.label, head.subtitle, action):
            widget.config(cursor="hand2")
            widget.bind("<Button-1>", toggle)
        head.bind("<Return>", toggle)
        head.bind("<space>", toggle)
        refresh()
        return head, group

    def _router_editor(self, page, cfg, rebuild):
        """单台设备的设置: 可折叠的标题行 + 设置行。page 是这台设备独占的容器; 更换类型时 rebuild() 原位重建。"""
        is_remote = cfg.get("type") == "hwmonitor"
        is_tplink = cfg.get("type") == "tplink"
        kinds = ("openwrt", "hwmonitor", "tplink")
        defaults = {"openwrt": settings.DEFAULT_ROUTER, "hwmonitor": settings.DEFAULT_REMOTE,
                    "tplink": settings.DEFAULT_TPLINK}
        def save(**changes):
            items = self.settings["routers"]
            self._save_routers([{**item, **changes} if item["id"] == cfg["id"] else item for item in items])
            if "enabled" in changes:
                head.subtitle.config(text=kind_label + ("  ·  监控已开启" if changes["enabled"] else "  ·  监控已关闭"))
        kind_label = "TP-Link · 原厂固件" if is_tplink else "远程电脑 · 硬件监控" if is_remote else "OpenWrt · 路由器"
        head, group = self._fold_section(page, cfg["name"], "router-settings:" + cfg["id"],
                                         kind_label + ("  ·  监控已开启" if cfg["enabled"] else "  ·  监控已关闭"))
        title = head.label
        head.router_id = cfg["id"]
        head.router_body = group
        def section(label, before=None):
            label = tk.Label(group, text=label, bg=CARD, fg=SUB, font=(FONT, 8, "bold"), anchor="w",
                             padx="12p", pady="7p")
            label.pack(fill="x", **({"before": before} if before is not None else {}))
        def field(title, desc="", stacked=False):
            row = self._row(group, title, desc, stacked=stacked)
            row.pack_configure(pady=0)
            row.configure(pady="7p")
            return row
        section("连接设置")
        type_row = field("设备类型")
        def change_type(index):
            kind = kinds[index]
            if kind == cfg.get("type", "openwrt"):
                return
            current = next(item for item in self.settings["routers"] if item["id"] == cfg["id"])
            old_default = defaults[cfg.get("type", "openwrt")]
            new_default = defaults[kind]
            url = new_default["url"] if current["url"] == old_default["url"] else current["url"]
            save(type=kind, url=url, nic=None, password_protected="")
            rebuild()
        self._choice(type_row, ("OpenWrt", "远程电脑", "TP-Link（原厂固件）"), kinds.index(cfg.get("type", "openwrt")), change_type)
        row = field("设备名称")
        name = tk.StringVar(value=cfg["name"])
        name_entry = ttk.Entry(row.control, textvariable=name, width=18, style="Card.TEntry")
        name_entry.pack(fill="x")
        def save_name(_=None):
            value = name.get().strip()[:60] or cfg["name"]
            name.set(value)
            save(name=value)
            title.config(text=value)
        name_entry.bind("<FocusOut>", save_name)
        name_entry.bind("<Return>", save_name)
        def remove():
            current = next((item for item in self.settings["routers"] if item["id"] == cfg["id"]), None)
            if current is None or not self._confirm_delete_device(name.get().strip() or current["name"]):
                return
            self._save_routers([item for item in self.settings["routers"] if item["id"] != cfg["id"]])
            page.destroy()
        def finish():
            footer = tk.Frame(group, bg=CARD, padx="12p", pady="10p")
            footer.pack(fill="x")
            tk.Frame(footer, bg=BORDER, height=1).pack(fill="x", pady=(0, "10p"))
            ttk.Button(footer, text="删除设备", style="Card.TButton", command=remove).pack(side="right")
        row = self._row(group, "显示设备状态", "显示 WAN 网速、IP 和在线设备数（以固件提供的数据为准）" if is_tplink else "在主界面显示远程电脑的硬件数据" if is_remote else "在主界面显示路由器温度、CPU、内存、负载和 WAN 网速")
        monitor_row = row
        enabled = self._switch(row, cfg["enabled"], lambda on: (save(enabled=on), self._enable(rows, on)))
        url_row = field("管理地址" if is_tplink else "指标地址", "例如 http://192.168.0.1" if is_tplink else "例如 http://192.168.1.2:8085/data.json" if is_remote else "例如 http://192.168.1.1:9100/metrics", stacked=True)
        url = tk.StringVar(value=cfg["url"])
        entry = ttk.Entry(url_row.control, textvariable=url, style="Card.TEntry")
        entry.pack(fill="x")
        def save_url(_=None):
            value = settings.device_url(url.get(), cfg.get("type", "openwrt"))
            if value:
                url.set(value)
                self._row_message(url_row)
                if value[:200] != next((item["url"] for item in self.settings["routers"] if item["id"] == cfg["id"]), cfg["url"]):
                    save(url=value[:200])
            else:
                url.set(next((item["url"] for item in self.settings["routers"] if item["id"] == cfg["id"]), cfg["url"]))
                self._row_message(url_row, "请输入有效的 http:// 或 https:// 地址，已恢复为上次保存的地址。", error=True)
        entry.bind("<FocusOut>", save_url)
        self._finish_edit(entry, save_url)
        if is_tplink:
            notify_row = self._row(group, "设备上下线提醒", "终端状态持续变化后通过托盘提醒；首次连接和路由器重连时不提醒")
            self._switch(notify_row, cfg.get("notify_clients", True), lambda on: save(notify_clients=on))
            password_row = field("管理员密码", "已加密保存 · 留空保留原密码" if cfg.get("password_protected") else "输入路由器管理密码后连接，密码将加密保存在本机", stacked=True)
            password = tk.StringVar()
            password_entry = ttk.Entry(password_row.control, textvariable=password, show="●", style="Card.TEntry")
            password_entry.pack(fill="x")
            def connect(_=None):
                value = settings.device_url(url.get(), "tplink")
                if not value:
                    self._row_message(url_row, "请输入有效的路由器管理地址。", error=True)
                    return "break"
                current = next(item for item in self.settings["routers"] if item["id"] == cfg["id"])
                try:
                    # Re-protect also on manual retry so the collector clears its login failure latch.
                    plain = password.get() or credentials.unprotect(current.get("password_protected", ""))
                    if not plain:
                        raise ValueError("Empty password")
                    protected = credentials.protect(plain)
                except (OSError, ValueError, UnicodeError):
                    self._row_message(password_row, "请重新输入密码；密码无法保存或读取。", error=True)
                    return "break"
                save(url=value, password_protected=protected, enabled=True)
                url.set(value)
                password.set("")
                enabled.set(True)
                self._enable(rows, True)
                self._row_message(url_row)
                self._row_message(password_row, "密码已加密保存，正在连接；连接结果显示在主界面设备卡片中。")
                return "break"
            ttk.Button(password_row.control, text="保存并连接", style="Accent.TButton", command=connect).pack(anchor="e", pady=("8p", 0))
            password_entry.bind("<Return>", connect)
            # Allow editing credentials while monitoring is off; connecting enables it.
            rows = []
            monitor_row.pack_configure(after=password_row)
            notify_row.pack_configure(after=monitor_row)
            section("监控选项", before=monitor_row)
            finish()
            return
        monitor_row.pack_configure(after=url_row)
        section("监控选项", before=monitor_row)
        if is_remote:
            rows = [url_row]
            self._enable(rows, cfg["enabled"])
            finish()
            return
        nic_row = field("WAN 网卡", "自动选择优先使用 pppoe-wan / wan，也可指定网卡")
        router = next((item for item in (self.data or {}).get("routers", []) if item["id"] == cfg["id"]), {})
        nics = [None] + router.get("nics", [])
        if cfg["nic"] and cfg["nic"] not in nics:
            nics.append(cfg["nic"])
        names = ["自动选择"] + [n if n in router.get("nics", []) else f"{n}（暂未连接）" for n in nics[1:]]
        self._choice(nic_row, names, nics.index(cfg["nic"]), lambda i: save(nic=nics[i]), width=16)
        rows = [url_row, nic_row]
        self._enable(rows, cfg["enabled"])
        finish()


    def _data_page(self, page):
        group = self._group(page, "历史数据")
        row = self._row(group, "保存历史到本地", "保存最近 24 小时，重启后自动恢复；关闭后仍保留本次运行的历史，已有数据不会删除")
        self._switch(row, self.settings["history_enabled"], lambda on: self._save_option("history_enabled", on))
        group = self._group(page, "诊断")
        row = self._row(group, "导出诊断报告", "包含版本、硬件概览、当前读数、最近告警和采集耗时，不含环境变量和私有路径")
        ttk.Button(row.control, text="导出…", style="Card.TButton", command=self.export_diagnostics).pack()
        row = self._row(group, "数据目录", f"配置、历史与日志保存在 {paths.DATA}")
        def open_data():
            try:
                paths.DATA.mkdir(parents=True, exist_ok=True)
                os.startfile(paths.DATA)
            except OSError:
                logging.getLogger(__name__).exception("Cannot open data directory")
                self._row_message(row, "无法打开数据目录，详见本地日志。", error=True)
        ttk.Button(row.control, text="打开", style="Card.TButton", command=open_data).pack()
        group = self._group(page, "关于")
        self._row(group, f"硬件监控 {__version__}",
                  "基于开源项目 LibreHardwareMonitor 开发，内置其硬件读取库 LibreHardwareMonitorLib（MPL-2.0）"
                  "和 PawnIO 驱动安装程序（GPL-2.0）；界面、网页服务和数据整理由本程序实现。")

    def _save_option(self, key, value):
        self.settings[key] = value
        self._save_settings()
        self.collector.configure(self.settings)
        if key == "web":
            self._apply_web()
        elif key == "management":
            self._apply_management()

    def _save_alert(self, key, value):
        self.settings["alerts"][key] = value
        self._save_settings()


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

