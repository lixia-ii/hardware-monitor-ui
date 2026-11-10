"""历史曲线窗口: 温度、利用率和网速分图显示, 支持范围、显示间隔、显卡选择、悬停读数与 CSV 导出。"""
from bisect import bisect_left
import time
import tkinter as tk
from tkinter import filedialog, ttk

from hwmonitor.core import history
from hwmonitor.ui.display import short_gpu
from hwmonitor.ui.theme import BG, CARD, FONT, MUTED, NUM, TRACK, set_icon


class HistoryDialogMixin:
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
