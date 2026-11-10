"""自定义显示: 指标与分组的名称、显示状态和顺序。"""
import copy
import tkinter as tk
from tkinter import ttk

from hwmonitor.ui.display import GROUP_NAMES, METRIC_NAMES
from hwmonitor.ui.theme import MUTED


class CustomizeDialogMixin:
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
