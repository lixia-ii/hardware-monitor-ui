"""Desktop alert log with readable wrapped messages and stable paginated browsing."""
import tkinter as tk
from tkinter import ttk

from hwmonitor.core.management import paginate
from hwmonitor.ui.theme import BG, CARD, FG, FONT, MUTED, RED, set_icon


class AlertsWindow(tk.Toplevel):
    def __init__(self, parent, runtime):
        super().__init__(parent)
        self.runtime = runtime
        self.page = 1
        self.rows = []
        self.revision = None
        self._job = None
        self.title("告警记录")
        self.configure(bg=BG)
        self.geometry(f"{self.winfo_pixels('620p')}x{self.winfo_pixels('460p')}")
        self.minsize(self.winfo_pixels("470p"), self.winfo_pixels("320p"))
        self.attributes("-topmost", parent.attributes("-topmost"))
        set_icon(self)

        header = ttk.Frame(self, padding=(16, 14, 16, 10))
        header.pack(fill="x")
        ttk.Label(header, text="最近告警", font=(FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(header, text="保留最近 1000 条 · 重启后恢复 · 每页 20 条", style="Hint.TLabel").pack(anchor="w", pady=(4, 0))
        self.storage_note = ttk.Label(header, foreground=RED, wraplength=self.winfo_pixels("420p"))
        self.storage_note.pack(anchor="w")

        body = ttk.Frame(self, padding=(16, 0, 16, 0))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, bg=CARD, fg=FG, font=(FONT, 10), wrap="word", state="disabled", width=1, height=1,
                            relief="flat", borderwidth=0, highlightthickness=0, padx=14, pady=12,
                            selectbackground="#264b79", selectforeground=FG, cursor="arrow")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("time", foreground=MUTED, spacing1=8, spacing3=4)
        self.text.tag_configure("message", spacing3=10)
        self.text.tag_configure("empty", foreground=MUTED, spacing1=20)

        footer = ttk.Frame(self, padding=(16, 12, 16, 12))
        footer.pack(fill="x")
        self.summary = ttk.Label(footer, style="Hint.TLabel")
        self.summary.pack(anchor="w", pady=(0, 8))
        self.previous = ttk.Button(footer, text="上一页", command=lambda: self.show_page(self.page - 1))
        self.previous.pack(side="left")
        self.page_var = tk.StringVar(self)
        self.page_select = ttk.Combobox(footer, textvariable=self.page_var, state="readonly", width=5)
        self.page_select.pack(side="left", padx=(10, 4))
        self.page_select.bind("<<ComboboxSelected>>", lambda _: self.show_page(int(self.page_var.get())))
        self.page_count = ttk.Label(footer)
        self.page_count.pack(side="left", padx=(0, 10))
        self.next = ttk.Button(footer, text="下一页", command=lambda: self.show_page(self.page + 1))
        self.next.pack(side="left")
        ttk.Button(footer, text="查看最新", command=self.refresh_latest).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _: self.destroy())
        self.refresh_latest()
        self._poll()

    def refresh_latest(self):
        self.rows = list(self.runtime.recent_alerts)
        self.revision = self.runtime.alert_revision
        self.show_page(1)

    def show_page(self, page):
        result = paginate(self.rows, page)
        self.page = result["page"]
        self.page_var.set(str(self.page))
        self.page_select.configure(values=tuple(str(i) for i in range(1, result["pages"] + 1)))
        self.page_count.configure(text=f'/ {result["pages"]} 页')
        self.previous.state(["!disabled"] if self.page > 1 else ["disabled"])
        self.next.state(["!disabled"] if self.page < result["pages"] else ["disabled"])
        start = (self.page - 1) * result["page_size"] + 1 if result["total"] else 0
        end = min(self.page * result["page_size"], result["total"])
        self.summary.configure(text=f'共 {result["total"]} 条 · 当前 {start}–{end} 条 · '
                               + ("首页自动更新" if self.page == 1 else "查看旧记录中，点击「查看最新」更新"))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for event in result["items"]:
            self.text.insert("end", event["time"] + "\n", "time")
            self.text.insert("end", event["message"] + "\n", "message")
        if not result["items"]:
            self.text.insert("end", "暂无告警，监控服务会持续监测。", "empty")
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _poll(self):
        self.storage_note.configure(text=self.runtime.alert_storage_error or "")
        if self.page == 1 and self.revision != self.runtime.alert_revision:
            self.refresh_latest()
        self._job = self.after(1000, self._poll)

    def destroy(self):
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None
        super().destroy()


class AlertsDialogMixin:
    def show_alerts(self):
        # Opened from a modal dialog (settings) the log must take over its grab, otherwise
        # the close button, Escape and paging never receive events.
        modal = self.root.grab_current()
        window = getattr(self, "alerts_win", None)
        if window is not None and window.winfo_exists():
            if not modal or modal is window:
                window.deiconify()
                window.lift()
                window.focus_set()
                return
            window.destroy()
        window = self.alerts_win = AlertsWindow(modal or self.root, self.runtime)
        self.center_on_main(window)
        if modal:
            def restore_grab(event):
                if event.widget is window and modal.winfo_exists():
                    modal.grab_set()
                    modal.focus_set()
            window.bind("<Destroy>", restore_grab, add="+")
            window.transient(modal)
            window.grab_set()
            window.focus_set()
