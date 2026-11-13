"""主界面的设备卡片 (OpenWrt / 远程电脑 / TP-Link): 单行标题、详情、终端流量榜和在线状态变更提醒。"""
import queue
import tkinter as tk
from tkinter import ttk

from hwmonitor.ui.display import fmt_duration, fmt_gb, fmt_rate, temp_color
from hwmonitor.ui.theme import BORDER, CARD, FG, FIELD, FONT, MUTED, NUM, RED, SUB


class DeviceCardsMixin:
    def _device_notifications(self):
        groups = {}
        configs = {cfg["id"]: cfg for cfg in self.settings["routers"]}
        while True:
            try:
                event = self.collector.device_events.get_nowait()
            except queue.Empty:
                break
            cfg = configs.get(event["router_id"], {})
            if cfg.get("enabled") and cfg.get("notify_clients", True):
                groups.setdefault(event["router_id"], []).append(event)
        for ident, events in groups.items():
            parts = []
            for online, title in ((True, "上线"), (False, "下线")):
                names = [e["name"] for e in events if e["online"] == online]
                if names:
                    shown = "、".join(name[:24] for name in names[:4])
                    parts.append(title + "：" + shown + (f" 等 {len(names)} 台" if len(names) > 4 else ""))
            message = configs[ident]["name"] + " · " + "；".join(parts)
            self.runtime.record(message)
            if not self.tray.notify("设备在线状态变更", message, warning=False):
                self._flash(message)

    def _device_header(self, box, kind):
        """单行标题: 箭头 | 名称 (弹性占宽, 过长省略) | 利用率徽标 | 分隔线 | ↑ ↓ 网速。
        徽标和网速使用固定字符宽度, 多台设备上下对齐; 离线提示占用第二个徽标的位置, 不挤占名称。"""
        from tkinter import font
        head = box.head
        box.arrow.pack_forget()
        box.title_label.pack_forget()
        box.arrow.grid(row=0, column=0, sticky="w", padx=(0, "4p"))
        box.title_label.config(width=1, anchor="w")
        box.title_label.grid(row=0, column=1, sticky="ew", padx=(0, "6p"))
        box.title_font = font.Font(font=box.title_label.cget("font"))
        box.device_name = ""
        box.fit_key = None
        head.columnconfigure(1, weight=1)
        metrics = tk.Frame(head, bg=CARD)
        metrics.grid(row=0, column=2)
        def chip(text, column):
            label = tk.Label(metrics, text=text, bg=FIELD, fg=SUB, font=(NUM, 8),
                             width=6, padx="2p", pady="1p", anchor="center")
            label.grid(row=0, column=column, padx=(0 if column == 0 else "3p", 0))
            return label
        box.cpu_usage = chip("C --%", 0)
        if kind == "hwmonitor":
            box.gpu_usage = chip("G --%", 1)
        state = chip("", 1)
        state.text_font = font.Font(font=state.cget("font"))  # 截断网卡名时测量用
        if kind == "hwmonitor":
            state.grid_remove()  # 只在离线时替换显卡徽标
        box.header_state = state
        box.header_alert = False  # 徽标是否为需常显的状态提示 (离线、WAN 异常), 由 _set_header_state 维护
        box.header_divider = tk.Frame(head, bg=BORDER, width=1, height="12p")
        box.header_divider.grid(row=0, column=3, padx="6p")
        rates_frame = tk.Frame(head, bg=CARD)
        rates_frame.grid(row=0, column=4, sticky="e")
        box.header_rates = rates_frame
        rates = {}
        keys = ("net_up", "net_down") if kind == "hwmonitor" else ("up", "down_rate")
        for column, (key, arrow) in enumerate(zip(keys, ("↑", "↓"))):
            group = tk.Frame(rates_frame, bg=CARD)
            group.grid(row=0, column=column, padx=(0 if column == 0 else "4p", 0))
            tk.Label(group, text=arrow, bg=CARD, fg=SUB, font=(NUM, 8)).pack(side="left")
            num = tk.Label(group, text="--", bg=CARD, fg=FG, font=(NUM, 9, "bold"), width=4, anchor="e")
            num.pack(side="left")
            unit = tk.Label(group, bg=CARD, fg=MUTED, font=(NUM, 7), width=4, anchor="w")
            unit.pack(side="left", padx=("1p", 0))
            rates[key] = (num, unit)
        box.title_label.bind("<Configure>", lambda _: self._fit_device_name(box))
        box.sync_header = lambda: self._sync_device_header(box)
        box.sync_header()
        return state, rates

    @staticmethod
    def _sync_device_header(box):
        """收起时标题行显示利用率、网卡和网速摘要; 展开后这些读数都在下方, 标题行只留名称。
        离线提示是状态而非读数, 展开时也保留。"""
        expanded = bool(box.winfo_manager())
        offline = box.header_alert
        shown = {box.cpu_usage: not expanded, box.header_divider: not expanded, box.header_rates: not expanded,
                 box.header_state: offline or (not expanded and box.device_type != "hwmonitor")}
        if box.device_type == "hwmonitor":
            shown[box.gpu_usage] = not expanded and not offline  # 离线提示占用显卡徽标的位置
        for widget, show in shown.items():
            if show != bool(widget.winfo_manager()):
                if show:
                    widget.grid()
                else:
                    widget.grid_remove()

    def _set_header_state(self, box, text, color, alert):
        """更新状态徽标; 是否常显只看 alert, 不从文案反推。"""
        self._set(box.header_state, text, color)
        if box.header_alert != alert:
            box.header_alert = alert
            box.sync_header()

    def _fit_device_name(self, box):
        width = box.title_label.winfo_width()
        if box.fit_key == (box.device_name, width):  # 名称和宽度都没变时不必重新测量
            return
        box.fit_key = (box.device_name, width)
        text = self._elide(box.title_font, box.device_name, max(0, width - 4)) if width > 1 else box.device_name
        self._set(box.title_label, text)

    @staticmethod
    def _elide(fnt, text, available):
        """超出 available 像素时截断并加省略号。"""
        if fnt.measure(text) <= available:
            return text
        while text and fnt.measure(text + "…") > available:
            text = text[:-1]
        return text + "…"

    def _router_section(self, parent):
        vals = {}
        for key, title in (("temp", "温度"), ("cpu_load", "CPU"), ("mem_load", "内存"), ("load1", "负载"),
                           ("uptime", "运行时间"), ("nic", "WAN 网卡"), ("net", "WAN 网速")):
            frame = tk.Frame(parent, bg=CARD)
            frame.pack(fill="x", pady="1p")
            tk.Label(frame, text=title, bg=CARD, fg=SUB, font=(FONT, 9)).pack(side="left")
            vals[key] = self._value(frame)
        return vals

    def _render_routers(self, routers):
        ids = {r.get("id", "primary") for r in routers}
        for ident in list(self.router_widgets):
            if ident not in ids:
                self.router_widgets.pop(ident)[0].master.destroy()
        self.router_order = [r.get("id", "primary") for r in routers]
        for r in routers:
            ident = r.get("id", "primary")
            kind = r.get("type", "openwrt")
            if ident in self.router_widgets and self.router_widgets[ident][0].device_type != kind:
                self.router_widgets.pop(ident)[0].master.destroy()
            if ident not in self.router_widgets:
                box = self._card(self.main_content, r.get("name") or "路由器", collapse_key="router:" + ident)
                box.device_type = kind
                state, rates = self._device_header(box, kind)
                if kind in ("hwmonitor", "tplink"):
                    box.pref_group = "device:" + ident
                    widgets = (state, rates, {})
                else:
                    widgets = (state, rates, self._router_section(box))
                self._bind_toggle(box)
                self.router_widgets[ident] = (box, widgets)
            box, widgets = self.router_widgets[ident]
            name = r.get("name") or "路由器"
            pref = self.settings["display"].get("router:" + ident, {})
            if box.device_name != (pref.get("name") or name):
                box.device_name = pref.get("name") or name
                self._fit_device_name(box)
            cpu = None if r["offline"] else r.get("cpu_load")
            if kind == "tplink":
                clients = r.get("clients")
                self._set(box.cpu_usage, "设备 --" if clients is None else f"设备 {clients}", MUTED if clients is None else FG)
            else:
                self._set(box.cpu_usage, "C --%" if cpu is None else f"C {cpu:.0f}%", MUTED if cpu is None else FG)
            if kind == "hwmonitor":
                gpu = None if r["offline"] else r.get("gpu_load")
                self._set(box.gpu_usage, "G --%" if gpu is None else f"G {gpu:.0f}%", MUTED if gpu is None else FG)
                self._render_remote(box, r, widgets)
            elif kind == "tplink":
                self._render_tplink(box, r, widgets)
            else:
                self._render_router(r, widgets, box)

    def _render_remote(self, box, data, widgets):
        state, rates, vals = widgets
        self._set_header_state(box, "未连接" if data["offline"] else "", RED, data["offline"])
        self._render_rates(rates, data)
        rows = []
        for key, label in (("cpu_temp", "CPU 温度"), ("cpu_load", "CPU 利用率"),
                           ("gpu_temp", "显卡温度"), ("gpu_load", "显卡利用率"),
                           ("mem_load", "内存"), ("gpu_mem_load", "显存"),
                           ("cpu_fan", "CPU 风扇"), ("gpu_fan", "显卡风扇")):
            value = data[key]
            unit = "°C" if key.endswith("temp") else "RPM" if key.endswith("fan") else "%"
            text = "--" if value is None else f"{value:.0f} {unit}"
            if key in ("mem_load", "gpu_mem_load") and value is not None:
                prefix = "mem" if key == "mem_load" else "gpu_mem"
                gb = fmt_gb(data[prefix + "_used"], data[prefix + "_total"])
                text = (gb + "  " if gb else "") + text
            rows.append((key, label, text, temp_color(value) if unit == "°C" else MUTED if value is None else FG,
                         value if unit == "°C" else None))
        net = self._rate_text(data, "net_down", "net_up")
        rows.append(("net", "网速", net, MUTED if net == "--" else FG, None))
        for group in ("case_fans", "pumps"):
            for index, (name, rpm) in enumerate(data[group]):
                identities = data["sensor_ids"][group]
                identity = identities[index] if index < len(identities) else name
                rows.append((group + ":" + identity, name, f"{rpm:.0f} RPM", FG, None))
        for index, (kind, name, temp) in enumerate(data["extra_temps"]):
            identities = data["sensor_ids"]["extra_temps"]
            identity = identities[index] if index < len(identities) else kind + ":" + name
            rows.append(("temp:" + identity, name, f"{temp:.0f} °C", temp_color(temp), temp))
        self._rows(box, rows, "暂无传感器数据", self._value)
        vals.clear()
        vals.update(box._vals)

    def _render_tplink(self, box, data, widgets):
        state, rates, vals = widgets
        wan_labels = {"0": "WAN 已断开", "1": "已联网", "2": "正在连接", "3": "正在断开", "4": "WAN 连接异常"}
        abnormal = {"0": "WAN断开", "2": "连接中", "3": "断开中", "4": "WAN异常"}
        text = "未连接" if data["offline"] else abnormal.get(data.get("wan_status"), "WAN")
        self._set_header_state(box, text, RED if data["offline"] or data.get("wan_connected") is False else MUTED,
                               data["offline"] or data.get("wan_status") in abnormal)
        self._render_rates(rates, data)
        status = data.get("status") or ("未连接" if data["offline"] else "已连接路由器")
        clients = data.get("clients")
        net = self._rate_text(data, "down_rate", "up")
        rows = [("status", "连接状态", status, RED if data["offline"] else FG, None),
                ("wan_status", "联网状态", "--" if data["offline"] else wan_labels.get(data.get("wan_status"), "未知"),
                 RED if data.get("wan_connected") is False else "#86efac" if data.get("wan_connected") else FG, None),
                ("clients", "在线设备", "--" if clients is None else str(clients), MUTED if clients is None else FG, None),
                ("wan_uptime", "WAN 在线时长", fmt_duration(data.get("wan_uptime")), FG, None),
                ("wan_ip", "WAN IP", data.get("wan_ip") or "--", FG if data.get("wan_ip") else MUTED, None),
                ("net", "WAN 网速", net, MUTED if net == "--" else "#93c5fd", None)]
        def make_value(parent):
            label = self._value(parent)
            label.config(font=(FONT, 9), justify="right", wraplength="180p")
            parent.bind("<Configure>", lambda _: label.config(wraplength=max(
                60, parent.winfo_width() - parent.winfo_children()[0].winfo_reqwidth() - parent.winfo_pixels("12p"))))
            return label
        if not hasattr(box, "tplink_info"):
            box.tplink_info = tk.Frame(box, bg=CARD)
            box.tplink_info.pref_group = box.pref_group
            box.tplink_info.pack(fill="x")
        self._rows(box.tplink_info, rows, "暂无路由器数据", make_value)
        self._layout_tplink_info(box.tplink_info)
        vals.clear()
        vals.update(box.tplink_info._vals)
        self._render_terminal_ranking(box, None if data["offline"] else data.get("terminals"))

    @staticmethod
    def _layout_tplink_info(info):
        """保留字段的自定义名称、顺序和可见性, 将概览排成两列信息块。"""
        if getattr(info, "tile_keys", None) == info._keys:
            return
        info.tile_keys = list(info._keys)
        for column in (0, 1):
            info.columnconfigure(column, weight=1, uniform="overview")
        for value in info._vals.values():
            value.master.pack_forget()
        row, column = 0, 0
        for key, _ in info._keys:
            value = info._vals[key]
            frame = value.master
            title = frame.winfo_children()[0]
            frame.pack_forget()
            wide = key in ("status", "net")
            if wide and column:
                row, column = row + 1, 0
            frame.config(bg=FIELD if key != "status" else CARD, padx="7p", pady="3p")
            frame.grid(row=row, column=column, columnspan=2 if wide else 1, sticky="nsew",
                       padx=(0 if wide or column == 0 else "3p", 0 if wide or column else "3p"),
                       pady=(0, "4p"))
            title.config(bg=frame.cget("bg"), fg=MUTED, font=(FONT, 8))
            value.pack_forget()
            value.config(bg=frame.cget("bg"), font=(FONT, 9), anchor="w", justify="left")
            if wide:
                value.pack(side="right")
            else:
                title.pack_forget()
                title.pack(anchor="w")
                value.pack(fill="x", pady=("2p", 0))
                frame.bind("<Configure>", lambda event, label=value: label.config(
                    wraplength=max(30, event.width - event.widget.winfo_pixels("14p"))))
            if wide or column:
                row, column = row + 1, 0
            else:
                column = 1

    def _render_terminal_ranking(self, box, terminals):
        if not hasattr(box, "terminal_tree"):
            heading = tk.Frame(box, bg=CARD)
            heading.pack(fill="x", pady=("7p", "6p"))
            tk.Label(heading, text="终端流量", bg=CARD, fg=FG,
                     font=(FONT, 9, "bold")).pack(side="left")
            box.terminal_range = tk.Label(heading, text="按总流量排序", bg=CARD, fg=MUTED, font=(FONT, 8))
            box.terminal_range.pack(side="right")
            box.terminal_panel = tk.Frame(box, bg=CARD)
            box.terminal_panel.pack(fill="x")
            tree = ttk.Treeview(box.terminal_panel, columns=("up", "down"), style="Router.Treeview",
                                show="tree headings", selectmode="browse", height=1)
            tree.heading("#0", text="设备", anchor="w")
            tree.column("#0", width=1, minwidth=0, stretch=False)
            for key, label in (("up", "↑ 上传"), ("down", "↓ 下载")):
                tree.heading(key, text=label, anchor="e")
                tree.column(key, width=1, minwidth=0, stretch=False, anchor="e")
            scroll = ttk.Scrollbar(box.terminal_panel, orient="vertical", style="Thin.Vertical.TScrollbar", command=tree.yview)
            box.terminal_scroll = scroll
            box.terminal_top = ttk.Button(heading, text="回到榜首", style="Card.TButton",
                                          command=lambda: tree.yview_moveto(0), cursor="hand2")
            def scroll_state(first, last):
                scroll.set(first, last)
                count = len(tree.get_children())
                start = min(count, int(round(float(first) * count)) + 1)
                end = min(count, int(round(float(last) * count)))
                self._set(box.terminal_range, f"第 {start}–{end} / {count} 台" if count else "暂无终端")
                if float(first) > 0:
                    if not box.terminal_top.winfo_manager():
                        box.terminal_top.pack(side="right", padx=("6p", 0), before=box.terminal_range)
                else:
                    box.terminal_top.pack_forget()
            tree.configure(yscrollcommand=scroll_state)
            def mousewheel(event):
                if event.delta and len(tree.get_children()) > int(tree.cget("height")):
                    before = tree.yview()
                    steps = max(1, abs(event.delta) // 120)
                    tree.yview_scroll(-steps if event.delta > 0 else steps, "units")
                    if tree.yview() != before:
                        return "break"  # 表格滚动时不再触发 Treeview 类绑定和主页面滚动。
                return None  # 到达列表边缘时, 滚轮继续滚动主页面。
            tree.bind("<MouseWheel>", mousewheel)
            tree.pack(fill="x", expand=True)
            tree.tag_configure("even", background=FIELD)
            tree.tag_configure("odd", background=CARD)
            box.terminal_tree = tree
            from tkinter import font
            name_font = font.Font(font=(FONT, 8))
            def fit_columns(_=None):
                # 按控件实际宽度分配, 避免 Treeview 的默认最小列宽把下载列挤出窗口。
                available = max(3, tree.winfo_width() - 2)
                rate_width = min(box.winfo_pixels("65p"), available // 3)
                name_width = available - 2 * rate_width
                tree.column("#0", width=name_width)
                for key in ("up", "down"):
                    tree.column(key, width=rate_width)
                for index, ident in enumerate(tree.get_children()):
                    terminal = box.terminal_data.get(ident)
                    if terminal:
                        tree.item(ident, text=self._elide(name_font, f"{index + 1}. {terminal['name']}",
                                                         max(0, name_width - box.winfo_pixels("12p"))))
            box.fit_terminal_columns = fit_columns
            tree.bind("<Configure>", fit_columns)
            tree.bind("<Button-1>", lambda event: "break" if tree.identify_region(event.x, event.y) == "separator" else None)
            box.terminal_note = tk.Label(box, bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w", justify="left", wraplength="260p")
            box.terminal_note.pack(fill="x", pady=("4p", 0))
            def details(_=None):
                selected = tree.selection()
                terminal = box.terminal_data.get(selected[0]) if selected else None
                if terminal:
                    box.terminal_note.config(text=f"{terminal['name']} · {terminal['ip'] or 'IP 未提供'}")
                elif box.terminal_data:
                    box.terminal_note.config(text="选择终端查看名称和 IP 地址")
            tree.bind("<<TreeviewSelect>>", details)
            box.terminal_details = details
            box.bind("<Configure>", lambda _: box.terminal_note.config(wraplength=max(60, box.winfo_width())), add="+")
        tree = box.terminal_tree
        box.terminal_data = {t["id"]: t for t in (terminals or [])}
        for ident in tree.get_children():
            if ident not in box.terminal_data:
                tree.delete(ident)
        for index, terminal in enumerate(terminals or []):
            ident = terminal["id"]
            values = tuple(" ".join(fmt_rate(terminal.get(key))[:2]).strip() for key in ("up", "down_rate"))
            if not tree.exists(ident):
                tree.insert("", "end", iid=ident)
            tree.item(ident, text=f"{index + 1}. {terminal['name']}", values=values,
                      tags=("even" if index % 2 == 0 else "odd",))
            tree.move(ident, "", index)
        tree.configure(height=max(1, min(6, len(box.terminal_data))))
        if len(box.terminal_data) > 6:
            box.terminal_scroll.pack(side="right", fill="y", before=tree)
        else:
            box.terminal_scroll.pack_forget()
        box.fit_terminal_columns()
        if not terminals:
            box.terminal_note.config(text="终端数据暂不可用" if terminals is None else "暂无在线终端")
        else:
            box.terminal_details()

    @staticmethod
    def _rate_text(d, down, up):
        """展开内容里的网速行: "↑ 5.1 KB/s  ↓ 52 KB/s", 都没有数据时为 "--"。"""
        if d.get(down) is None and d.get(up) is None:
            return "--"
        parts = []
        for key, arrow in ((up, "↑"), (down, "↓")):
            num, unit, _ = fmt_rate(d.get(key))
            parts.append(f"{arrow} {num} {unit}".rstrip())
        return "  ".join(parts)

    def _render_router(self, r, widgets, box):
        state, rates, vals = widgets
        text = "未连接" if r["offline"] else r["nic"] or ""
        # 徽标固定宽度, 各设备的列保持对齐; pppoe-wan 等较长的网卡名截断显示。
        fnt = state.text_font
        text = self._elide(fnt, text, fnt.measure("0") * int(state.cget("width")))
        self._set_header_state(box, text, RED if r["offline"] else MUTED, r["offline"])
        self._render_rates(rates, r)
        nic = None if r["offline"] else r["nic"]
        self._set(vals["nic"], nic or "--", MUTED if nic is None else FG)
        net = self._rate_text(r, "down_rate", "up")
        self._set(vals["net"], net, MUTED if net == "--" else FG)
        self._set(vals["temp"], "--" if r["temp"] is None else f"{r['temp']:.0f} °C", temp_color(r["temp"]))
        cpu = r["cpu_load"]
        self._set(vals["cpu_load"], "--" if cpu is None else f"{cpu:.0f} %", MUTED if cpu is None else FG)
        mem = r["mem_load"]
        gb = fmt_gb(r["mem_used"], r["mem_total"])
        self._set(vals["mem_load"], "--" if mem is None else (gb + "  " if gb else "") + f"{mem:.0f} %",
                  MUTED if mem is None else FG)
        load = r["load1"]
        self._set(vals["load1"], "--" if load is None else f"{load:.2f}", MUTED if load is None else FG)
        up = r["uptime"]
        text = "--" if up is None else f"{int(up // 86400)}天{int(up % 86400 // 3600)}小时{int(up % 3600 // 60)}分"
        self._set(vals["uptime"], text, MUTED if up is None else FG)
