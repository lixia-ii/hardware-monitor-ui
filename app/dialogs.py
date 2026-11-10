"""Settings, sensor customisation, charts and diagnostic export UI."""
import copy
from bisect import bisect_left
import logging
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
FONT, NUM = "Microsoft YaHei UI", "Segoe UI"
METRIC_NAMES = {"cpu_temp": "CPU 温度", "cpu_load": "CPU 利用率", "mem_load": "内存占用", "cpu_fan": "CPU 风扇",
                "gpu_temp": "显卡温度", "gpu_load": "显卡利用率", "gpu_mem_load": "显存占用", "gpu_fan": "显卡风扇"}
GROUP_NAMES = {"network": "网络", "router": "路由器", "case": "机箱风扇", "pump": "水冷泵", "storage": "硬盘温度", "memory": "内存温度", "network_temp": "网卡温度"}


class Dialogs:
    def _scroll_page(self, notebook, title):
        page = ttk.Frame(notebook)
        notebook.add(page, text=title)
        canvas = tk.Canvas(page, bg=BG, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        body = ttk.Frame(canvas, padding="12p")
        item = canvas.create_window(0, 0, window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        def wheel(event):
            if canvas.winfo_ismapped() and body.winfo_reqheight() > canvas.winfo_height():
                if event.widget.winfo_class() not in ("TCombobox", "TSpinbox"):
                    canvas.yview_scroll(-int(event.delta / 120), "units")
        page.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
        # Keyboard navigation keeps focused controls inside the viewport.
        def reveal(event):
            widget = event.widget
            if not canvas.winfo_ismapped() or not str(widget).startswith(str(body) + "."):
                return
            y = widget.winfo_rooty() - body.winfo_rooty()
            top, height = canvas.canvasy(0), canvas.winfo_height()
            if y < top or y + widget.winfo_height() > top + height:
                canvas.yview_moveto(max(0, y - height / 3) / max(body.winfo_height(), 1))
        page.winfo_toplevel().bind("<FocusIn>", reveal, add="+")
        return body

    @staticmethod
    def _section(parent, title):
        section = ttk.LabelFrame(parent, text=title, padding="10p")
        section.pack(fill="x", pady=(0, "12p"))
        return section

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

    def show_settings(self):
        if getattr(self, "settings_win", None) and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        w, body = self._dialog("设置", resizable=True)
        self.settings_win = w
        self._size_dialog(w, "460p", "440p")
        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        general, alerts, devices = (self._scroll_page(tabs, text) for text in ("常规", "告警", "采集与显示"))
        close_section = self._section(general, "关闭窗口")
        close = tk.StringVar(value=self.settings["close_action"])
        for value, text in (("ask", "每次询问"), ("tray", "最小化到托盘"), ("exit", "直接退出")):
            radio = ttk.Radiobutton(close_section, text=text, value=value, variable=close,
                                    command=lambda: self._save_option("close_action", close.get()))
            if value == "tray" and not self.tray.ok:  # 托盘不可用时关闭一律退出, 别让选项点了不生效
                radio.state(["disabled"])
            radio.pack(anchor="w")
        if not self.tray.ok:
            self._hint(close_section, "当前系统托盘不可用，关闭窗口时会直接退出。")
        startup = self._section(general, "启动与悬浮窗")
        auto = tk.BooleanVar()
        auto_cb = ttk.Checkbutton(startup, text="开机自动启动，最小化到托盘", variable=auto, state="disabled")
        auto_cb.pack(anchor="w")
        auto_msg = self._hint(startup, "登录后自动开始监控。")
        def loaded(value):
            auto.set(bool(value))
            auto_cb.state(["!disabled"])
        def toggle_auto():
            want = auto.get()
            auto_cb.state(["disabled"])
            def done(result):
                auto_msg.config(text=result[1] if result else "设置失败，详见本地日志")
                self._background(w, autostart.is_enabled, loaded)
            self._background(w, lambda: autostart.set_enabled(want), done)
        auto_cb.config(command=toggle_auto)
        self._background(w, autostart.is_enabled, loaded)
        self._float_var = tk.BooleanVar(value=self.float_win is not None)
        ttk.Checkbutton(startup, text="显示桌面悬浮窗", variable=self._float_var,
                        command=self.toggle_floating).pack(anchor="w", pady=8)
        storage = self._section(general, "历史与诊断")
        persist = tk.BooleanVar(value=self.settings["history_enabled"])
        ttk.Checkbutton(storage, text="保存最近 24 小时历史，重启后恢复", variable=persist,
                        command=lambda: self._save_option("history_enabled", persist.get())).pack(anchor="w")
        self._hint(storage, "关闭保存后仍采集本次运行的历史，已有本地数据保留。")
        ttk.Button(storage, text="导出诊断报告…", command=self.export_diagnostics).pack(anchor="w", pady=("10p", 0))
        self._hint(general, f"版本 {config.VERSION} · 配置、历史与日志保存在 data 目录")

        cfg = self.settings["alerts"]
        switches = self._section(alerts, "提醒方式")
        for key, text in (("enabled", "启用持续高温提醒"), ("fan_enabled", "启用高温时的风扇低转速提醒")):
            var = tk.BooleanVar(value=cfg[key])
            ttk.Checkbutton(switches, text=text, variable=var,
                            command=lambda k=key, v=var: self._save_alert(k, v.get())).pack(anchor="w", pady=3)
        grid = self._section(alerts, "阈值与持续时间")
        grid.columnconfigure(0, weight=1)
        specs = (("cpu_temp", "CPU 温度阈值（°C）", 40, 110), ("gpu_temp", "显卡温度阈值（°C）", 40, 110),
                 ("duration", "持续超温时间（秒）", 0, 300), ("hysteresis", "恢复温差（°C）", 1, 20),
                 ("cooldown", "两次提醒最短间隔（秒）", 30, 7200), ("fan_min", "风扇最低转速（RPM）", 1, 5000),
                 ("fan_duration", "高温且低转速持续时间（秒）", 5, 600))
        for row, (key, label, low, high) in enumerate(specs):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            var = tk.StringVar(value=str(cfg[key]))
            def save(k=key, v=var, lo=low, hi=high):
                value = int(config.number(v.get(), cfg[k], lo, hi))
                v.set(str(value))
                self._save_alert(k, value)
            entry = ttk.Spinbox(grid, from_=low, to=high, textvariable=var, width=7, command=save)
            entry.grid(row=row, column=1, sticky="e")
            entry.bind("<FocusOut>", lambda _, save=save: save())
        self._hint(alerts, "仅在高温且已识别到 RPM 传感器时检测风扇。显卡低温停转、传感器缺失均不会触发风扇告警。")

        interval = tk.StringVar(value=str(self.settings["interval"]))
        sampling = self._section(devices, "采集频率与显卡")
        ttk.Label(sampling, text="刷新间隔（0.5–30 秒）").pack(anchor="w")
        def save_interval(_=None):
            value = config.number(interval.get(), 1, 0.5, 30)
            interval.set(str(value))
            self._save_option("interval", value)
        spin = ttk.Spinbox(sampling, from_=0.5, to=30, increment=0.5, textvariable=interval, command=save_interval, width=8)
        spin.pack(anchor="w", pady=4)
        spin.bind("<FocusOut>", save_interval)
        ttk.Label(sampling, text="默认显卡", padding=(0, 10, 0, 0)).pack(anchor="w")
        selected_gpu = self.settings["gpu_id"] or None  # 空串按"自动选择"处理
        gpu_ids = [None] + [h for h, _ in self.gpu_list]
        gpu_names = ["自动选择"] + [n + (f"（{i + 1}）" if sum(name == n for _, name in self.gpu_list) > 1 else "")
                                  for i, (_, n) in enumerate(self.gpu_list)]
        if selected_gpu and selected_gpu not in gpu_ids:
            gpu_ids.append(selected_gpu)
            gpu_names.append("已选显卡（暂未连接）")
        gpu = ttk.Combobox(sampling, values=gpu_names, state="readonly", width=24)
        gpu.current(gpu_ids.index(selected_gpu))
        gpu.pack(fill="x", pady=4)
        def select_gpu(_):
            self.gpu_sel = gpu_ids[gpu.current()]
            self._save_option("gpu_id", self.gpu_sel)
        gpu.bind("<<ComboboxSelected>>", select_gpu)
        networking = self._section(devices, "网速统计")
        auto_nic = tk.BooleanVar(value=self.settings["nic_ids"] is None)
        nic_vars = {}
        nic_box = ttk.Frame(networking)
        def save_nics():
            ids = None if auto_nic.get() else [h for h, v in nic_vars.items() if v.get()]
            self._save_option("nic_ids", ids)
        ttk.Checkbutton(networking, text="自动汇总物理网卡", variable=auto_nic, command=save_nics).pack(anchor="w")
        self._hint(networking, "勾选下方网卡可切换为手动统计。")
        nic_box.pack(fill="x")
        nics = dict((self.data or {}).get("nics", []))
        for h in self.settings["nic_ids"] or []:
            nics.setdefault(h, "暂未连接 · " + h)
        for h, name in nics.items():
            var = nic_vars[h] = tk.BooleanVar(value=h in (self.settings["nic_ids"] or []))
            ttk.Checkbutton(nic_box, text=name, variable=var,
                            command=lambda: (auto_nic.set(False), save_nics())).pack(anchor="w")
        if not nics:
            ttk.Label(nic_box, text="连接 LibreHardwareMonitor 后可选择网卡。", style="Hint.TLabel").pack(anchor="w")
        self._router_section(devices)
        ttk.Button(devices, text="自定义显示、名称和顺序…", command=self.show_customization).pack(anchor="w", pady=12)
        footer = ttk.Frame(body, padding=(0, "10p", 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, text="设置修改后自动保存", style="Hint.TLabel").pack(side="left")
        ttk.Button(footer, text="完成", style="Accent.TButton", command=w.destroy).pack(side="right")
        self._show_dialog(w)

    def _router_section(self, parent):
        section = self._section(parent, "OpenWrt 路由器")
        cfg = self.settings["router"]
        enabled = tk.BooleanVar(value=cfg["enabled"])
        def save(**changes):
            self._save_option("router", {**self.settings["router"], **changes})
        ttk.Checkbutton(section, text="显示路由器状态", variable=enabled,
                        command=lambda: save(enabled=enabled.get())).pack(anchor="w")
        ttk.Label(section, text="指标地址", padding=(0, "6p", 0, 0)).pack(anchor="w")
        url = tk.StringVar(value=cfg["url"])
        entry = ttk.Entry(section, textvariable=url)
        entry.pack(fill="x", pady=4)
        def save_url(_=None):
            value = url.get().strip()
            if value.lower().startswith(("http://", "https://")):
                save(url=value[:200])
            else:
                url.set(self.settings["router"]["url"])
        entry.bind("<FocusOut>", save_url)
        entry.bind("<Return>", save_url)
        ttk.Label(section, text="WAN 网卡（用于统计网速）", padding=(0, "6p", 0, 0)).pack(anchor="w")
        router = (self.data or {}).get("router") or {}
        nics = [None] + router.get("nics", [])
        if cfg["nic"] and cfg["nic"] not in nics:
            nics.append(cfg["nic"])
        names = ["自动选择"] + [n if n in router.get("nics", []) else f"{n}（暂未连接）" for n in nics[1:]]
        nic = ttk.Combobox(section, values=names, state="readonly", width=24)
        nic.current(nics.index(cfg["nic"]))
        nic.pack(fill="x", pady=4)
        nic.bind("<<ComboboxSelected>>", lambda _: save(nic=nics[nic.current()]))
        self._hint(section, "需在路由器上安装 prometheus-node-exporter-lua（含 thermal），并把监听接口设为 lan；"
                            "启用并连接后可选择网卡。")

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
        for keys in categories.values():
            keys.sort(key=lambda k: prefs.get(k, {}).get("order", 100))
        top = ttk.Frame(body)
        top.pack(fill="x", pady=(0, "8p"))
        ttk.Label(top, text="编辑分组").pack(side="left", padx=(0, "8p"))
        category = tk.StringVar(value=next(iter(categories)))
        selector = ttk.Combobox(top, textvariable=category, values=list(categories), state="readonly", width=22)
        selector.pack(side="left", fill="x", expand=True)
        self._hint(body, "选择指标后修改名称或显示状态；上移、下移只调整当前分组。")
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
                tree.item(key, values=(prefs[key]["name"] or names[key], "显示" if visible.get() else "隐藏"))
        name.trace_add("write", stage)
        visible.trace_add("write", stage)
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
                p = prefs.get(key, {})
                tree.insert("", "end", iid=key, values=(p.get("name") or names[key], "显示" if p.get("visible", True) else "隐藏"))
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
