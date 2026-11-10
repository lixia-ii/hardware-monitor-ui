"""通用对话框: 深色模态小窗、居中显示、后台任务, 以及关闭询问和 PawnIO 安装提示。"""
import logging
import queue
import threading
import tkinter as tk
from tkinter import ttk

from hwmonitor.ui.theme import BG, FONT, set_icon


class DialogMixin:
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

    def center_on_main(self, w):
        """把 w 居中到主窗口中央 (主窗口在哪, 就在哪弹出)。"""
        w.update_idletasks()
        mw, mh = max(w.winfo_width(), w.minsize()[0]), max(w.winfo_height(), w.minsize()[1])
        x = self.root.winfo_x() + (self.root.winfo_width() - mw) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - mh) // 2
        w.geometry(f"+{max(0, min(x, w.winfo_screenwidth() - mw - 20))}+{max(0, min(y, w.winfo_screenheight() - mh - 70))}")

    def _dialog(self, title, parent=None, resizable=False):
        """深色模态小窗。返回 (窗口, 内容框架); 内容填好后调用 _show_dialog。"""
        parent = parent or self.root
        w = tk.Toplevel(parent)
        w.withdraw()  # 摆好位置再显示, 免得先在左上角闪一下
        w.title(title)
        w.configure(bg=BG)
        w.resizable(resizable, resizable)
        w.transient(parent)
        w.attributes("-topmost", self.root.attributes("-topmost"))
        set_icon(w)
        body = ttk.Frame(w, padding=("16p", "14p", "16p", "12p"))
        body.pack(fill="both", expand=True)
        w.bind("<Escape>", lambda _: w.destroy())
        return w, body

    def _show_dialog(self, w):
        previous = self.root.grab_current()
        def restore_grab(event):
            if event.widget is w and previous and previous.winfo_exists():
                previous.grab_set()
                previous.focus_set()
        w.bind("<Destroy>", restore_grab, add="+")
        self.center_on_main(w)
        w.deiconify()
        set_icon(w)  # Mapping a resizable/transient dialog can recreate its native frame.
        w.grab_set()
        w.focus_set()

    def _ask_close(self):
        """询问退出还是最小化到托盘。返回 "tray" / "exit", 取消返回 None。"""
        w, body = self._dialog("关闭硬件监控")
        result = []
        remember = tk.BooleanVar(value=False)
        ttk.Label(body, text="要退出程序, 还是最小化到托盘?", font=(FONT, 10)).pack(anchor="w")
        ttk.Label(body, text="最小化后点击右下角托盘图标可恢复窗口。", style="Hint.TLabel").pack(
            anchor="w", pady=("4p", "10p"))
        ttk.Checkbutton(body, text="记住我的选择 (可在「设置」中修改)", variable=remember,
                        cursor="hand2").pack(anchor="w", pady=(0, "12p"))
        btns = ttk.Frame(body)
        btns.pack(fill="x")

        def choose(action):
            result.append(action)
            if remember.get():
                self.settings["close_action"] = action
                self._save_settings()
            w.destroy()
        ttk.Button(btns, text="退出", style="Accent.TButton", command=lambda: choose("exit"),
                   cursor="hand2").pack(side="right")
        ttk.Button(btns, text="最小化到托盘", command=lambda: choose("tray"),
                   cursor="hand2").pack(side="right", padx=(0, "6p"))
        self._show_dialog(w)
        w.wait_window()
        return result[0] if result else None

    def _ask_pawnio(self, state):
        w, body = self._dialog("安装 PawnIO 驱动" if state == "missing" else "更新 PawnIO 驱动")
        result = []
        ttk.Label(body, text="需要安装 PawnIO 驱动才能读取 CPU 温度和主板风扇转速。" if state == "missing"
                  else "PawnIO 驱动版本过旧，更新后才能读取 CPU 温度和主板风扇转速。",
                  font=(FONT, 10), wraplength="280p", justify="left").pack(anchor="w")
        ttk.Label(body, text="PawnIO 是 LibreHardwareMonitor 使用的开源驱动，只需安装一次。\n"
                  "安装程序是英文界面，按提示点击确定即可。", style="Hint.TLabel", justify="left").pack(
            anchor="w", pady=("4p", "12p"))
        btns = ttk.Frame(body)
        btns.pack(fill="x")

        def choose(ok):
            result.append(ok)
            w.destroy()
        ttk.Button(btns, text="安装", style="Accent.TButton", command=lambda: choose(True),
                   cursor="hand2").pack(side="right")
        ttk.Button(btns, text="暂不安装", command=lambda: choose(False), cursor="hand2").pack(side="right", padx=(0, "6p"))
        self._show_dialog(w)
        w.wait_window()
        return bool(result and result[0])
