"""桌面悬浮窗: 置顶显示 CPU 与显卡的温度和利用率, 可拖动, 位置随设置保存。"""
import tkinter as tk

from hwmonitor.ui.display import temp_color
from hwmonitor.ui.theme import BORDER, CARD, FONT, MUTED, NUM, SUB


class FloatingWindowMixin:
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
