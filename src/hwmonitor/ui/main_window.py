"""主窗口: 实时显示 CPU/显卡温度、利用率、内存/显存占用、风扇转速与网速, 以及可选分组卡片和提示条。"""
import ctypes
import logging
import math
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

from hwmonitor import settings
from hwmonitor.core import history
from hwmonitor.core.alerts import AlertEngine
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import lhm, reading
from hwmonitor.system import autostart, deps, pawnio, tray, webserver
from hwmonitor.ui import theme
from hwmonitor.ui.customize_dialog import CustomizeDialogMixin
from hwmonitor.ui.dialogs import DialogMixin
from hwmonitor.ui.display import (DEVICE_TITLES, GROUP_NAMES, METRIC_NAMES, fmt_gb, fmt_rate, rpm_summary,
                                  short_gpu, temp_color, temp_summary, fmt_duration)
from hwmonitor.ui.floating import FloatingWindowMixin
from hwmonitor.ui.history_dialog import HistoryDialogMixin
from hwmonitor.ui.info_window import InfoWindowMixin
from hwmonitor.ui.settings_dialog import SettingsDialogMixin
from hwmonitor.ui.theme import (BG, BORDER, CARD, CHIP_HOVER, ERR_BG, ERR_BORDER, FG, FIELD, FONT, MUTED, NUM,
                                RED, SUB, TRACK, set_icon)


def on_screen(x, y):
    """屏幕坐标 (x, y) 是否落在某个显示器上 (多显示器、拔掉副屏后恢复位置时用)。"""
    from ctypes import wintypes as wt
    user = ctypes.windll.user32
    user.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    user.MonitorFromPoint.restype = wt.HMONITOR
    return bool(user.MonitorFromPoint(wt.POINT(x, y), 0))  # MONITOR_DEFAULTTONULL


class MainWindow(SettingsDialogMixin, CustomizeDialogMixin, HistoryDialogMixin, FloatingWindowMixin,
                 InfoWindowMixin, DialogMixin):
    """主窗口: CPU/显卡卡片、可选分组、提示条与底部工具栏; 各对话框和悬浮窗由混入类提供。"""

    def __init__(self):
        self.root = r = tk.Tk()
        # 开机自启 (计划任务带 --autostart) 时直接待在托盘, 不弹窗口
        self.hidden_start = "--autostart" in sys.argv
        if self.hidden_start:
            r.withdraw()
        self.settings = settings.load()
        self.root.report_callback_exception = lambda *args: logging.getLogger(__name__).error("Tk callback failed", exc_info=args)
        self.pawnio_missing = False  # 用户暂不安装 PawnIO 时, 提示条常驻"安装"按钮
        self.data = None
        self.metric_blocks = {}
        self.metric_titles = {}
        self.sensor_names = {}
        r.title("硬件监控")
        r.configure(bg=BG)
        # 窗口尺寸只由用户决定 (并记住), 内容变化从不改变窗口大小: 卡片区滚动, 底部工具栏固定。
        r.minsize(r.winfo_pixels("300p"), r.winfo_pixels("200p"))
        r.resizable(True, True)
        self._theme_images = theme.apply_style(r)  # 自绘图片须保持引用
        set_icon(r)

        # 底部工具栏 (提示条 + 按钮) 固定在窗口底部, 不随卡片滚动; 先 pack 才不会被卡片区挤掉
        self.footer = tk.Frame(r, bg=BG, padx="10p", pady="8p")
        self.footer.pack(side="bottom", fill="x")
        tk.Frame(r, bg=BORDER, height=1).pack(side="bottom", fill="x")
        self.viewport = tk.Frame(r, bg=BG)
        self.viewport.pack(fill="both", expand=True)
        self.main_canvas = tk.Canvas(self.viewport, bg=BG, highlightthickness=0,
                                     width="300p", height="300p", yscrollincrement="20p")
        # 细滚动条浮在右侧留白上 (类似 Windows 11), 出现/消失时卡片宽度不变, 不会左右跳动
        self.main_scrollbar = ttk.Scrollbar(self.viewport, orient="vertical", style="Thin.Vertical.TScrollbar",
                                            command=self.main_canvas.yview)
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)
        self.main_canvas.pack(fill="both", expand=True)
        self.main_content = main = tk.Frame(self.main_canvas, bg=BG, padx="10p", pady="10p")
        self.main_window = self.main_canvas.create_window(0, 0, window=main, anchor="nw")
        self.main_canvas.bind("<Configure>", self._sync_main_scroll)
        main.bind("<Configure>", self._sync_main_scroll)
        r.bind("<MouseWheel>", self._main_mousewheel, add="+")

        self.values = {}
        self.bars = {}
        self._devices(main)

        # 网卡实时网速 (所有物理网卡之和): 只有两个数, 直接放在标题行右侧, 整张卡片只占一行
        self.net_box = self._card(main, "网络")
        self.net_box.pack_forget()
        self.net_vals = self._rate_labels(self.net_box.head, "net_up", "net_down")

        # 设备 (OpenWrt / 远程电脑): 每台设备一张卡片, 由 _render_routers 按需创建。
        # router_group 只是"设备"分组在 GROUP_NAMES 顺序里的占位, 本身从不显示; 排版见 _layout。
        self.router_group = tk.Frame(main, bg=BG)
        self.router_order = []
        self.router_widgets = {}
        self.router_box = None

        self.case_box = self._card(main, "机箱风扇", collapse_key="case")

        # 水冷泵: 只有检测到时才显示, 插在机箱风扇下面
        self.pump_box = self._card(main, "水冷泵", collapse_key="pump")

        # 硬盘 / 内存 / 网卡各一项, 可点击标题收起或展开 (记住上次的状态)
        self.extra_boxes = {k: self._card(main, f"{label}温度", collapse_key=k)
                            for k, label in reading.EXTRA_LABELS.items()}
        # 可选分组卡片的末尾锚点, 分组按顺序插在它前面
        self.content_end = tk.Frame(main, bg=BG, height=0)
        self.content_end.pack(fill="x")

        # 错误提示条: 有消息才显示, 在按钮上方; 缺少 PawnIO 驱动或 pythonnet 时右侧带"安装"按钮
        self.notice = tk.Frame(self.footer, bg=ERR_BG, padx="8p", pady="5p",
                               highlightthickness=1, highlightbackground=ERR_BORDER)
        self.notice.columnconfigure(0, weight=1)
        self.status = tk.Label(self.notice, text="", bg=ERR_BG, fg=RED, font=(FONT, 9),
                               wraplength="120p", justify="left", anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")
        self.action_btn = ttk.Button(self.notice, text="安装", style="Accent.TButton", width=7,  # 定宽, 切换文字时不跳
                                     command=lambda: self._action(), cursor="hand2")
        self.notice.bind("<Configure>", self._wrap_status)
        self.status_msg = ("", 0.0)  # 启动失败等临时消息, 在到期前盖过读取到的错误
        self._action = self.install_pawnio  # 提示条"安装"按钮当前要装的东西

        self.btn_bar = tk.Frame(self.footer, bg=BG)
        self.btn_bar.pack(fill="x")
        self.info_btn = ttk.Button(self.btn_bar, text="硬件信息", command=self.show_info, cursor="hand2")
        self.info_btn.pack(side="right")
        self.history_btn = ttk.Button(self.btn_bar, text="历史", command=self.show_history, cursor="hand2")
        self.history_btn.pack(side="right", padx=(0, "6p"))
        ttk.Button(self.btn_bar, text="设置", command=self.show_settings, cursor="hand2").pack(side="left")
        # 可选分组: 没有数据就不显示。按此顺序排在底栏之前
        self.optional = [b if b is self.router_group else b.master for b in self._group_boxes()]
        self.shown = ()

        r.protocol("WM_DELETE_WINDOW", self.close)
        self.splash = None
        if "--self-test" not in sys.argv:
            self._show_splash()

        # 托盘图标常驻; 点最小化时隐藏到托盘。托盘不可用 (极少见) 时保持普通的最小化和关闭即退出
        self.tray = tray.Tray(theme.ICON, "硬件监控")
        if self.tray.ok:
            # Tk 没有"最小化"事件, 只能监听 <Unmap>; 子控件的 Unmap 也会冒泡上来, 按 widget 过滤
            r.bind("<Unmap>", lambda e: e.widget is r and r.state() == "iconic" and r.withdraw())
        elif self.hidden_start:
            r.iconify()
        if "--self-test" not in sys.argv:
            threading.Thread(target=autostart.sync, daemon=True).start()
        self.gpu_sel = self.settings["gpu_id"]
        self.gpu_cur = None
        self.history = deque(maxlen=history.MAX_ROWS)
        self.recent_alerts = deque(maxlen=100)
        self.alert_engine = AlertEngine()
        # 硬件传感器在进程内读取 (LibreHardwareMonitorLib); 自检不打开硬件
        self.web = webserver.WebServer()
        self._firewall = None
        self.collector = Collector(self.settings, None if "--self-test" in sys.argv else lhm.Sensors(), self.web)
        self.collector.start()
        self._storage_notice = None
        self._apply_display()
        self._apply_web()
        if "--self-test" not in sys.argv:
            r.after(300, self.check_pawnio)
        self.float_win = None
        if self.settings["topmost"]:
            r.attributes("-topmost", True)
        if self.settings.get("floating", {}).get("enabled"):
            self.show_floating()
        self._restore_geometry()
        r.bind("<Configure>", self._schedule_geometry_save, add="+")
        self.tick()


    def _show_splash(self):
        """启动时整个窗口先盖一层纯色, 中间走进度条, 硬件传感器打开 (首份数据到达) 后再露出界面。
        打开要几秒且拿不到真实进度, 进度条按时间逐渐逼近 95%, 数据到了补满再撤掉。"""
        s = self.splash = tk.Frame(self.root, bg=BG)
        s.place(x=0, y=0, relwidth=1, relheight=1)
        s.lift()
        box = tk.Frame(s, bg=BG)
        box.place(relx=0.5, rely=0.45, anchor="center")
        track = tk.Frame(box, bg=TRACK, width=box.winfo_pixels("160p"), height=box.winfo_pixels("3p"))
        track.pack()
        tk.Label(box, text="加载中…", bg=BG, fg=SUB, font=(FONT, 9)).pack(pady=("10p", 0))
        self._splash_fill = tk.Frame(track, bg="#60a5fa")
        self._splash_start = time.monotonic()
        self._splash_step()

    def _splash_step(self):
        if not self.splash:
            return
        rel = 0.95 * (1 - math.exp(-(time.monotonic() - self._splash_start) / 2.5))
        self._splash_fill.place(x=0, y=0, relheight=1, relwidth=max(rel, 0.02))
        self._splash_job = self.root.after(30, self._splash_step)

    def _hide_splash(self):
        splash, self.splash = self.splash, None
        self.root.after_cancel(self._splash_job)
        self._splash_fill.place(x=0, y=0, relheight=1, relwidth=1)
        self.root.after(200, splash.destroy)  # 让补满的进度条停留一下

    def toggle_topmost(self):
        self.set_topmost(not self.settings["topmost"])

    def set_topmost(self, on):
        """主窗口置顶 (设置页开关和托盘菜单共用), 保存到设置。"""
        self.settings["topmost"] = on
        self._save_settings()
        self.root.attributes("-topmost", on)
        var = getattr(self, "_topmost_var", None)
        if var is not None:  # 设置窗里的开关跟随 (如从托盘菜单切换)
            var.set(on)
        # 已打开的设置、历史、硬件信息等窗口跟随, 免得被置顶的主窗口压在下面; 悬浮窗有自己的置顶选项
        def follow(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Toplevel) and child is not self.float_win:
                    child.attributes("-topmost", on)
                    follow(child)
        follow(self.root)

    def check_pawnio(self):
        """启动时检查 PawnIO 驱动 (同 LibreHardwareMonitor): 未安装或过旧时询问是否安装; 开机自启时只在提示条提醒。"""
        state = pawnio.status()
        if state == "ok":
            return
        self.pawnio_missing = True
        self._notice((self.data or {}).get("error"))
        if not self.hidden_start and self._ask_pawnio(state):
            self.install_pawnio()


    def _install(self, job, done):
        """提示条"安装"按钮的公共流程: 后台执行 job, 期间按钮禁用; 结果交给 done(ok)。"""
        if getattr(self, "installing", False):
            return
        self.installing = True
        self.action_btn.state(["disabled"])
        self._set(self.action_btn, "安装中…")

        def finish(ok):
            self.installing = False
            self.action_btn.state(["!disabled"])
            self._set(self.action_btn, "安装")
            done(ok)
        self._background(self.root, job, finish)

    def install_pawnio(self):
        def job():
            try:
                return pawnio.install()
            except OSError:
                logging.getLogger(__name__).exception("PawnIO install failed")
                return False

        def done(ok):
            if ok:
                self.pawnio_missing = False
                self.collector.reload_hardware()
                self._flash("PawnIO 已安装，正在重新初始化硬件传感器…")
            else:
                self._flash("PawnIO 未安装成功，可稍后点击「安装」重试。")
        self._install(job, done)

    def install_deps(self):
        """源码运行缺少 pythonnet 时: 在程序目录 pip install -e ., 装好后立即重新打开硬件。"""
        def done(ok):
            if ok:
                self.collector.reload_hardware()
                self._flash("依赖已安装，正在初始化硬件传感器…")
            else:
                self._flash("依赖安装失败（详见日志），也可在程序目录手动运行 pip install -e .")
        self._install(deps.install, done)

    def _apply_web(self):
        """按设置启动/停止网页服务; 局域网开关或端口变化时更新防火墙规则。返回错误消息。"""
        web = self.settings["web"]
        error = self.web.apply(web["enabled"], web["lan"], web["port"])
        if error:
            self._flash(error)
        want = web["port"] if web["enabled"] and web["lan"] else None
        # 启动时 (_firewall 为 None) 只在开放局域网时校正规则 (程序目录可能移动过), 平时不必每次调用 netsh
        if want != self._firewall and "--self-test" not in sys.argv:
            threading.Thread(target=webserver.firewall, args=(want,), daemon=True).start()
        self._firewall = want
        return error

    def _save_settings(self):
        try:
            settings.save(self.settings)
            return True
        except (OSError, ValueError):
            logging.getLogger(__name__).exception("Settings save failed")
            self._flash("设置保存失败，请检查程序目录的写入权限；本次运行仍使用新设置。")
            return False

    def _flash(self, msg):
        """临时消息显示 5 秒, 否则下一次渲染会立刻被读取到的错误覆盖。"""
        self.status_msg = (msg, time.monotonic() + 5)
        self._set(self.status, msg)
        self._notice((self.data or {}).get("error"))

    def _notice(self, error):
        """提示条: 有消息才显示; 缺少 pythonnet, 或缺少 PawnIO 且没有其他消息时, 显示提醒和安装按钮。"""
        msg, until = self.status_msg
        msg = msg if time.monotonic() < until else error or ""
        action = None
        if msg == lhm.NO_PYTHONNET:
            action = self.install_deps
        elif self.pawnio_missing and not msg:
            action = self.install_pawnio
            msg = "未安装 PawnIO 驱动，CPU 温度和主板风扇无法读取。"
        if action:
            self._action = action
        action = bool(action) or getattr(self, "installing", False)  # 安装中按钮保持显示 (禁用), 不随消息切换消失
        self._set(self.status, msg)
        if bool(msg) != bool(self.notice.winfo_manager()):
            if msg:
                self.notice.pack(fill="x", pady=(0, "8p"), before=self.btn_bar)
            else:
                self.notice.pack_forget()
            self._sync_main_scroll()
        if action != bool(self.action_btn.winfo_manager()):
            if action:
                self.action_btn.grid(row=0, column=1, padx=("8p", 0))
            else:
                self.action_btn.grid_remove()
            self._wrap_status()

    def _wrap_status(self, _=None):
        """消息按提示条的实际宽度换行 (扣掉按钮), 不把窗口撑宽。"""
        width = self.notice.winfo_width() - 2 * self.notice.winfo_pixels("8p") - 2
        if self.action_btn.winfo_manager():
            width -= self.action_btn.winfo_reqwidth() + self.notice.winfo_pixels("8p")
        if width > 50:
            self.status.config(wraplength=width)


    def _restore_geometry(self):
        """恢复上次的窗口大小和位置; 首次运行或原位置所在的显示器已不存在时, 用默认大小居中。"""
        r = self.root
        r.update_idletasks()
        min_w, min_h = r.minsize()
        saved = self.settings.get("window")
        if saved and on_screen(saved["x"] + saved["width"] // 2, saved["y"] + r.winfo_pixels("10p")):
            w, h, x, y = max(min_w, saved["width"]), max(min_h, saved["height"]), saved["x"], saved["y"]
        else:
            w = min_w
            h = max(min_h, min(r.winfo_pixels("440p"), r.winfo_screenheight() - r.winfo_pixels("80p")))
            x, y = max(0, (r.winfo_screenwidth() - w) // 2), max(0, (r.winfo_screenheight() - h) // 2)
        r.geometry(f"{w}x{h}+{x}+{y}")

    def _schedule_geometry_save(self, event):
        """用户拖动或缩放窗口后稍等再保存, 拖动过程中不反复写盘。"""
        if event.widget is not self.root:
            return
        if getattr(self, "_geometry_job", None):
            self.root.after_cancel(self._geometry_job)
        self._geometry_job = self.root.after(800, self._save_geometry)

    def _save_geometry(self):
        self._geometry_job = None
        r = self.root
        if r.state() != "normal" or not r.winfo_viewable():  # 最大化、最小化、隐藏时保留原来的普通尺寸
            return
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", r.geometry())
        if not m:
            return
        window = dict(zip(("width", "height", "x", "y"), map(int, m.groups())))
        if window != self.settings.get("window"):
            self.settings["window"] = window
            self._save_settings()

    def _sync_main_scroll(self, _=None):
        """内容保持自然高度，超出视口时才显示浮在右侧留白上的细滚动条, 卡片宽度始终不变。"""
        canvas = self.main_canvas
        overflow = self.main_content.winfo_reqheight() > canvas.winfo_height()
        if overflow and not self.main_scrollbar.winfo_manager():
            self.main_scrollbar.place(relx=1, x=-canvas.winfo_pixels("2p"), rely=0, relheight=1, anchor="ne")
        elif not overflow and self.main_scrollbar.winfo_manager():
            self.main_scrollbar.place_forget()
            canvas.yview_moveto(0)
        canvas.itemconfigure(self.main_window, width=canvas.winfo_width())
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _reveal(self, card):
        """展开靠下的分组后自动滚动, 让展开的内容整张露出来 (卡片比视口高时优先露出标题)。"""
        self.root.update_idletasks()
        self._sync_main_scroll()
        canvas = self.main_canvas
        total = self.main_content.winfo_height()
        view_top, view_h = canvas.canvasy(0), canvas.winfo_height()
        top = card.winfo_y()
        bottom = top + card.winfo_height() + self.main_content.winfo_pixels("10p")  # 连同底部留白
        if total <= view_h or bottom <= view_top + view_h:
            return
        # 视图按 yscrollincrement 对齐 (四舍五入), 向上取整到整步, 免得底部被切掉一截
        step = canvas.winfo_pixels(canvas.cget("yscrollincrement")) or 1
        target = min(top, -(-(bottom - view_h) // step) * step)
        canvas.yview_moveto(target / total)

    def _main_mousewheel(self, event):
        # 根窗口的绑定只接收主界面控件的事件，不干扰设置和历史窗口。
        if event.widget.winfo_toplevel() is self.root and self.main_scrollbar.winfo_manager() and event.delta:
            steps = max(1, abs(event.delta) // 120)
            self.main_canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
            return "break"

    def show_window(self):
        r = self.root
        r.deiconify()
        r.state("normal")
        # 从托盘恢复时先让 Tk 完成尺寸计算和首帧绘制，减少短暂的白块与空白区域。
        r.update_idletasks()
        r.lift()
        r.focus_force()
        if self.data:
            self.render(self.data)

    def _check_alerts(self, d):
        for msg in self.alert_engine.check(d, self.settings["alerts"], time.monotonic()):
            self.recent_alerts.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": msg})
            logging.getLogger(__name__).warning(msg)
            if not self.tray.notify("硬件告警", msg):
                self._flash(msg)


    def close(self):
        """点 x: 托盘可用时按设置最小化到托盘或退出, 没选过就询问。"""
        action = self.settings.get("close_action", "ask") if self.tray.ok else "exit"
        if action == "ask":
            action = self._ask_close()
        if action == "tray":
            self.root.withdraw()
        elif action == "exit":
            self.quit()


    def quit(self):
        if getattr(self, "closing", False):
            return
        self.closing = True
        self._save_geometry()
        self.tray.destroy()
        self.root.withdraw()
        self.collector.stop()
        def stop():
            self.web.stop()
            # 端口已空出, 先让出单实例: 新实例不必等下面关闭硬件 (采集线程可能正在打开内存条, 要几秒)
            tray.release_single_instance()
            self.collector.join(timeout=8)  # 采集线程退出时关闭硬件, 释放驱动
            return True
        self._background(self.root, stop, lambda _: self.root.destroy())


    def _devices(self, parent):
        """CPU / 显卡各一张卡片: 大字温度, 下面是利用率、内存/显存占用 (带进度条) 和风扇。"""
        row = tk.Frame(parent, bg=BG)
        # 主卡片保持自然高度，收起分组或拉高窗口时不产生卡片内空白。
        row.pack(fill="x")
        row.rowconfigure(0, weight=1)
        for i, (dev, name, accent) in enumerate((("cpu", "CPU", "#60a5fa"), ("gpu", "显卡", "#a78bfa"))):
            row.columnconfigure(i, weight=1, uniform="dev")
            card = tk.Frame(row, bg=CARD, padx="10p", pady="8p", highlightthickness=1, highlightbackground=BORDER)
            # 两侧各留一半间距, 两张卡片才一样宽 (padx 也算在 uniform 列宽里)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, "3p") if i == 0 else ("3p", 0))
            head = tk.Frame(card, bg=CARD)
            head.pack(fill="x")
            title_label = tk.Label(head, text=name, bg=CARD, fg=MUTED, font=(FONT, 10, "bold"))
            title_label.pack(side="left")
            self.metric_titles[dev + "_temp"] = title_label
            if dev == "gpu":  # 显卡型号标签在卡片右上角; 多显卡时点击弹出菜单选择
                # 不用 Menubutton: 按钮本体不认深色配色; 弹出菜单跟随 dark_menus 的系统深色主题
                chip = self.gpu_chip = tk.Label(head, bg=TRACK, fg=FG, font=(FONT, 9), padx="5p")
                chip.bind("<Button-1>", self._gpu_menu)
                chip.bind("<Enter>", lambda _: len(self.gpu_list) > 1 and chip.config(bg=CHIP_HOVER))
                chip.bind("<Leave>", lambda _: chip.config(bg=TRACK))
                self.gpu_list = []
            temp = tk.Frame(card, bg=CARD)
            temp.pack(anchor="w", pady=("2p", "2p"))
            self.metric_blocks[dev + "_temp"] = [temp]
            self.values[f"{dev}_temp"] = tk.Label(temp, text="--", bg=CARD, fg=MUTED, font=(NUM, 24, "bold"))
            self.values[f"{dev}_temp"].pack(side="left")
            # 单位用小字, 底部对齐到数字基线附近
            self.values[f"{dev}_temp_unit"] = tk.Label(temp, text="°C", bg=CARD, fg=MUTED, font=(NUM, 12, "bold"))
            self.values[f"{dev}_temp_unit"].pack(side="left", anchor="s", pady=(0, "6p"))
            for kind, title in (("load", "利用率"), ("mem_load", "内存" if dev == "cpu" else "显存"), ("fan", "风扇")):
                key = f"{dev}_{kind}" if dev == "gpu" or kind != "mem_load" else "mem_load"
                f = tk.Frame(card, bg=CARD)
                f.pack(fill="x", pady=("4p", 0))
                title_label = tk.Label(f, text=title, bg=CARD, fg=SUB, font=(FONT, 9))
                title_label.pack(side="left")
                self.metric_titles[key] = title_label
                self.metric_blocks[key] = [f]
                self.values[key] = self._value(f)
                if kind == "mem_load":  # 已用/总量, 小字放在百分比左边
                    self.values[key + "_gb"] = tk.Label(f, text="", bg=CARD, fg=MUTED, font=(NUM, 8))
                    self.values[key + "_gb"].pack(side="right", padx=(0, "4p"))
                if kind != "fan":
                    track = tk.Frame(card, bg=TRACK, height="3p")
                    track.pack(fill="x", pady=("3p", 0))
                    self.metric_blocks[key].append(track)
                    bar = tk.Frame(track, bg=accent)
                    bar.rel = 0
                    self.bars[key] = bar

    def _gpu_menu(self, _=None):
        """多显卡时在型号标签下方弹出菜单, 列出完整型号, 当前显卡带选中标记。"""
        if len(self.gpu_list) < 2:
            return
        names = [n for _, n in self.gpu_list]
        menu = tk.Menu(self.root, tearoff=0)
        self._gpu_var = tk.StringVar(value=self.gpu_cur)  # 保持引用, 否则选中标记不显示
        for i, (hwid, name) in enumerate(self.gpu_list):
            label = f"{name} #{names[:i + 1].count(name)}" if names.count(name) > 1 else name  # 同型号多卡加序号区分
            menu.add_radiobutton(label=label, value=hwid, variable=self._gpu_var,
                                 command=lambda h=hwid: self._select_gpu(h))
        chip = self.gpu_chip
        menu.tk_popup(chip.winfo_rootx(), chip.winfo_rooty() + chip.winfo_height())

    def _select_gpu(self, hwid):
        self.gpu_sel = hwid
        self._show_gpu(hwid)  # 标签先切过去, 数值等下一次读取
        self.settings["gpu_id"] = hwid
        self._save_settings()
        self.collector.configure(self.settings)

    def _show_gpu(self, hwid):
        self.gpu_cur = hwid
        name = next((n for h, n in self.gpu_list if h == hwid), "")
        multi = len(self.gpu_list) > 1  # 只有一块时只显示型号, 不带下拉标记
        self._set(self.gpu_chip, f"{short_gpu(name)}  ▾" if multi else short_gpu(name))
        self.gpu_chip.config(cursor="hand2" if multi else "")

    def _update_gpu(self, d):
        """显示显卡型号标签: 多块时可点击切换, 一块时只显示型号, 没有显卡就隐藏。"""
        self.gpu_list = d["gpus"]
        if not self.gpu_list:
            if self.gpu_chip.winfo_manager():
                self.gpu_chip.pack_forget()
            return
        if d["gpu_id"] is None and self.gpu_sel:
            # 用户固定的显卡暂时不在列表里 (断连/重新编号): 不冒充另一块卡。
            self.gpu_cur = self.gpu_sel
            self._set(self.gpu_chip, "已选显卡（暂未连接）  ▾" if len(self.gpu_list) > 1 else "已选显卡（暂未连接）")
            self.gpu_chip.config(cursor="hand2" if len(self.gpu_list) > 1 else "")
        else:
            self._show_gpu(d["gpu_id"] or self.gpu_list[0][0])
        if not self.gpu_chip.winfo_manager():
            self.gpu_chip.pack(side="right")

    def _card(self, parent, title, collapse_key=None):
        """分组卡片, 返回放内容的 body 框架 (body.master 是卡片本身)。
        collapse_key: 点击标题行收起/展开 (左侧箭头指示), 状态以此为键存进设置, 下次启动保持; 第一次默认展开。
        收起后标题行右侧显示摘要 (body.summary), 不展开也能看到关键读数。"""
        card = tk.Frame(parent, bg=CARD, padx="10p", pady="7p", highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x")
        expanded_glyph, collapsed_glyph, glyph_font = self._chevrons()
        collapsed = collapse_key in self.settings.get("collapsed", [])
        # 不可收起的卡片也放一个同色 (看不见) 的箭头占位, 各卡片标题左对齐
        arrow = tk.Label(head, text=collapsed_glyph if collapsed else expanded_glyph, bg=CARD,
                         fg=MUTED if collapse_key else CARD, font=glyph_font)
        gap = head.winfo_pixels("4p")
        arrow.pack(side="left", padx=(0, gap))
        title_label = tk.Label(head, text=title, bg=CARD, fg=MUTED, font=(FONT, 9, "bold"))
        title_label.pack(side="left")
        body = tk.Frame(card, bg=CARD)
        body.head = head
        body.arrow = arrow
        body.title_label = title_label
        # 收起后的摘要: [MAX] 粗体数字 + 小号单位, 与网速、设备徽标的样式一致
        body.summary = summary = tk.Frame(head, bg=CARD)
        summary.unit = tk.Label(summary, text="", bg=CARD, fg=FG, font=(NUM, 8, "bold"), width=3, anchor="w")
        summary.unit.pack(side="right", anchor="s", pady=(0, "1p"))
        summary.num = tk.Label(summary, text="", bg=CARD, fg=FG, font=(NUM, 10, "bold"))
        summary.num.pack(side="right", padx=(0, "2p"))
        summary.tag = tk.Label(summary, text="", bg=FIELD, fg=SUB, font=(NUM, 7, "bold"), padx="3p", pady=0)
        summary.parts = ("", "", "", FG)
        # 内容行与标题文字左对齐 (箭头悬在左侧), 层级一目了然
        body_pack = {"fill": "x", "pady": ("4p", 0), "padx": (arrow.winfo_reqwidth() + gap, 0)}
        if not collapsed:
            body.pack(**body_pack)
        if collapse_key:
            def set_summary(tag, num, unit, color):
                summary.parts = (tag, num, unit, color)
                self._set(summary.num, num, color)
                self._set(summary.unit, unit, color)
                self._set(summary.tag, tag)
                if bool(tag) != bool(summary.tag.winfo_manager()):
                    if tag:
                        summary.tag.pack(side="right", padx=(0, "5p"))
                    else:
                        summary.tag.pack_forget()
                show = bool(num) and not body.winfo_manager()
                if show != bool(body.summary.winfo_manager()):
                    if show:
                        body.summary.pack(side="right")
                    else:
                        body.summary.pack_forget()

            def toggle(_=None):
                keys = set(self.settings.get("collapsed", []))
                if body.winfo_manager():
                    body.pack_forget()
                    arrow.config(text=collapsed_glyph)
                    keys.add(collapse_key)
                else:
                    body.pack(**body_pack)
                    arrow.config(text=expanded_glyph)
                    keys.discard(collapse_key)
                set_summary(*summary.parts)
                if hasattr(body, "sync_header"):  # 设备卡片: 展开后标题行只保留名称
                    body.sync_header()
                self.settings["collapsed"] = sorted(keys)
                self._sync_main_scroll()
                if body.winfo_manager():
                    self._reveal(card)
                self._save_settings()

            def hover(on):
                for w in (arrow, title_label):
                    w.config(fg=FG if on else MUTED)
            body.toggle, body.set_summary, body.hover = toggle, set_summary, hover
            self._bind_toggle(body)
        return body

    def _chevrons(self):
        """展开/收起箭头: 优先用 Windows 自带的 Segoe MDL2 Assets 图标字体 (系统设置等程序同款), 没有就用三角符号。"""
        if not hasattr(self, "_chevron_glyphs"):
            from tkinter import font
            if "Segoe MDL2 Assets" in font.families(self.root):
                self._chevron_glyphs = ("", "", ("Segoe MDL2 Assets", 7))  # ChevronDownMed / ChevronRightMed
            else:
                self._chevron_glyphs = ("▾", "▸", (FONT, 10))
        return self._chevron_glyphs

    @staticmethod
    def _bind_toggle(body):
        """整个标题行都可点击, 悬停时标题和箭头高亮。标题行后加的控件需再调用一次。"""
        pending = [body.head]
        while pending:
            w = pending.pop()
            pending.extend(w.winfo_children())
            w.config(cursor="hand2")
            w.bind("<Button-1>", body.toggle)
            w.bind("<Enter>", lambda _: body.hover(True))
            w.bind("<Leave>", lambda _: body.hover(False))

    def _group_boxes(self):
        """可选分组卡片, 顺序与 dialogs.GROUP_NAMES 一致。"""
        return (self.net_box, self.router_group, self.case_box, self.pump_box, *self.extra_boxes.values())

    @staticmethod
    def _rate_labels(head, up, down, compact=False):
        """标题行右侧的 ↑ / ↓ 速率, 返回 {键: (数字标签, 单位标签)}。"""
        labels = {}
        for key, arrow in ((up, "↑"), (down, "↓")):  # 从右往左排
            # 单位小字、随量级变色, 数字粗体; 各自定宽, 位数或单位变化时不跳
            unit = tk.Label(head, text="", bg=CARD, fg=MUTED, font=(NUM, 8, "bold"), width=4, anchor="w")
            unit.pack(side="right", anchor="s", pady=(0, "1p"))
            num = tk.Label(head, text="--", bg=CARD, fg=MUTED, font=(NUM, 10 if compact else 11, "bold"), width=4, anchor="e")
            num.pack(side="right", padx=(0, 0 if compact else "2p"))
            tk.Label(head, text=arrow, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(
                side="right", padx=(("1p" if key == down else 0) if compact else ("10p" if key == down else "6p"), 0))
            labels[key] = (num, unit)
        return labels

    @staticmethod
    def _value(parent):
        """右对齐的数值标签。"""
        v = tk.Label(parent, text="--", bg=CARD, fg=FG, font=(NUM, 10, "bold"))
        v.pack(side="right")
        return v

    def _bar(self, key, pct):
        bar = self.bars[key]
        rel = 0 if pct is None else max(0.0, min(pct, 100.0)) / 100
        if bar.rel != rel:
            bar.rel = rel
            if rel:
                bar.place(x=0, y=0, relheight=1, relwidth=rel)
            else:  # relwidth=0 仍会画出 1 像素, 直接隐藏
                bar.place_forget()

    def _tray_events(self):
        """处理托盘点击。选了退出返回 False。"""
        for ev in self.tray.poll():
            if ev == "menu":
                ev = self.tray.menu(floating=self.float_win is not None, topmost=self.settings["topmost"])
            if ev == "show":
                self.show_window()
            elif ev == "floating":
                self.toggle_floating()
            elif ev == "topmost":
                self.toggle_topmost()
            elif ev == "quit":
                self.quit()
                return False
        return True

    def tick(self):
        if getattr(self, "closing", False):
            return
        if not self._tray_events():
            return
        self._device_notifications()
        while not self.collector.history_rows.empty():
            action, rows = self.collector.history_rows.get()
            if action == "load":
                self.history.extend(rows)
            else:
                self.history.append(rows)
        try:
            data = self.collector.results.get_nowait()
        except queue.Empty:
            data = None
        if data and data["selection"] == (self.settings["gpu_id"], self.settings["nic_ids"]):
            self.data = data
            self._check_alerts(data)
            self._update_floating(data)
            temps = [f"{name} {data[k]:.0f}°C" for k, name in (("cpu_temp", "CPU"), ("gpu_temp", "显卡")) if data[k] is not None]
            self.tray.set_tip("\n".join(["硬件监控", *temps]))
            if self.root.state() == "normal" and self.root.winfo_viewable():
                self.render(data)
        if data and self.splash and not data.get("initializing"):
            self._hide_splash()
        error = self.collector.storage_error
        if error and error != self._storage_notice:
            self._storage_notice = error
            self._flash(error)
        self.root.after(100, self.tick)  # 也负责响应托盘点击, 间隔不宜太长

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
            self.recent_alerts.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": message})
            if not self.tray.notify("设备上下线", message):
                self._flash(message)

    @staticmethod
    def _set(label, text, fg=None):
        """只在内容变化时才更新控件, 避免整页重绘闪烁。"""
        if label.cget("text") != text:
            label.config(text=text)
        if fg is not None and str(label.cget("fg")) != fg:
            label.config(fg=fg)

    def _rows(self, box, rows, empty_text, make_value, summarize=None):
        """动态行: 仅当行集合变化时才重建, 否则只更新数值。rows: [(key, 名称, 数值文本, 颜色, 数值)]
        key 须包含名称: 只用序号的话, 数量不变但换了风扇时名称会对不上。
        summarize(可见行的数值列表) -> (文本, 颜色): 卡片收起时显示在标题行右侧。"""
        group = getattr(box, "pref_group", "other")
        filtered = []
        for index, (key, name, text, color, value) in enumerate(rows):
            pref_key = f"sensor:{group}:{key}"
            # Old settings used an enumeration index. Migrate only an unambiguous
            # name; identical devices need their own stable hardware identity.
            prefix = f"sensor:{group}:{name}:"
            old = [k for k in self.settings["display"] if k.startswith(prefix) and k[len(prefix):].isdigit()]
            if pref_key not in self.settings["display"] and len(old) == 1 and sum(r[1] == name for r in rows) == 1:
                self.settings["display"][pref_key] = self.settings["display"].pop(old[0])
            self.sensor_names[pref_key] = name
            pref = self.settings["display"].get(pref_key, {})
            if pref.get("visible", True):
                filtered.append((pref.get("order", 100), index, (key, pref.get("name") or name, text, color), value))
        if summarize and hasattr(box, "set_summary"):
            box.set_summary(*summarize([v for *_, v in filtered if v is not None]))
        rows = [r for _, _, r, _ in sorted(filtered, key=lambda f: f[:2])]
        keys = [(r[0], r[1]) for r in rows]
        if getattr(box, "_keys", None) != keys:
            for w in box.winfo_children():
                w.destroy()
            box._keys, box._vals = keys, {}
            if not rows:
                tk.Label(box, text=empty_text, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
            for key, name, _, _ in rows:
                f = tk.Frame(box, bg=CARD)
                f.pack(fill="x", pady="1p")
                tk.Label(f, text=name, bg=CARD, fg=SUB, font=(FONT, 9)).pack(side="left")
                box._vals[key] = make_value(f)
        for key, _, text, color in rows:
            self._set(box._vals[key], text, color)

    def render(self, d):
        self._update_gpu(d)
        for k in ("cpu_temp", "gpu_temp"):
            t = d[k]
            self._set(self.values[k], "--" if t is None else f"{t:.0f}", temp_color(t))
            self._set(self.values[k + "_unit"], "°C", temp_color(t))
        for k in ("cpu_load", "gpu_load", "mem_load", "gpu_mem_load"):
            u = d[k]
            self._set(self.values[k], "--" if u is None else f"{u:.0f} %", MUTED if u is None else FG)
            self._bar(k, u)
        for k, used, total in (("mem_load", "mem_used", "mem_total"), ("gpu_mem_load", "gpu_mem_used", "gpu_mem_total")):
            self._set(self.values[k + "_gb"], fmt_gb(d[used], d[total]))
        self._render_rates(self.net_vals, d)
        routers = d.get("routers", [d["router"]] if d.get("router") else [])
        router = bool(routers)
        self._render_routers(routers)
        cpu_fan = d["cpu_fan"]
        self._set(self.values["cpu_fan"], "--" if cpu_fan is None else f"{cpu_fan:.0f} RPM", MUTED if cpu_fan is None else FG)
        if d["gpu_fan"] is not None:
            gpu_fan = f"{d['gpu_fan']:.0f} RPM"
        elif d["gpu_fan_pct"] is not None:
            gpu_fan = f"{d['gpu_fan_pct']:.0f} %"
        else:
            gpu_fan = "--"
        self._set(self.values["gpu_fan"], gpu_fan, MUTED if gpu_fan == "--" else FG)

        def sensor_id(group, index, name):
            ids = d.get("sensor_ids", {}).get(group, [])
            return ids[index] if index < len(ids) else "legacy:" + name
        self._rows(self.case_box,
                   [(sensor_id("case_fans", i, n), n, f"{rpm:.0f} RPM", FG if rpm else MUTED, rpm)
                    for i, (n, rpm) in enumerate(d["case_fans"])],
                   "未检测到风扇", self._value, rpm_summary)
        for kind, box in self.extra_boxes.items():
            self._rows(box,
                       [(sensor_id("extra_temps", i, n), n, f"{t:.0f} °C", temp_color(t), t)
                        for i, (k, n, t) in enumerate(d["extra_temps"]) if k == kind],
                       "未检测到", self._value, temp_summary)
        pumps = d.get("pumps", [])
        self._rows(self.pump_box,
                   [(sensor_id("pumps", i, n), n, f"{rpm:.0f} RPM", None, rpm) for i, (n, rpm) in enumerate(pumps)],
                   "", self._value, rpm_summary)
        # 卡片是否显示按"过滤后还有没有可见行"判断: 全部隐藏时不该再提示"未检测到"。
        has = [d["net_up"] is not None or d["net_down"] is not None, bool(router),
               bool(self.case_box._keys), bool(self.pump_box._keys)] + [
            bool(box._keys) for box in self.extra_boxes.values()]
        self._layout(has)
        self._notice(d["error"])
        temps = [f"{name} {d[k]:.0f}°C" for k, name in (("cpu_temp", "CPU"), ("gpu_temp", "显卡")) if d[k] is not None]
        self.tray.set_tip("\n".join(["硬件监控", *temps]))  # 托盘悬停即可看温度, 不用打开窗口

    def _render_rates(self, labels, d):
        for k, (num, unit) in labels.items():
            n, u, color = fmt_rate(d[k])
            self._set(num, n, MUTED if d[k] is None else FG)  # 数字固定白色, 只有单位随量级变色
            self._set(unit, u, color)

    def _device_header(self, box, kind):
        """单行标题: 箭头 | 名称 (弹性占宽, 过长省略) | 利用率徽标 | 分隔线 | ↓ ↑ 网速。
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
        box.header_divider = tk.Frame(head, bg=BORDER, width=1, height="12p")
        box.header_divider.grid(row=0, column=3, padx="6p")
        rates_frame = tk.Frame(head, bg=CARD)
        rates_frame.grid(row=0, column=4, sticky="e")
        box.header_rates = rates_frame
        rates = {}
        keys = ("net_down", "net_up") if kind == "hwmonitor" else ("down_rate", "up")
        for column, (key, arrow) in enumerate(zip(keys, ("↓", "↑"))):
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
        offline = box.header_state.cget("text") in ("未连接", "WAN断开", "连接中", "断开中", "WAN异常")
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
                key = "router" if ident == "primary" else "router:" + ident
                box = self._card(self.main_content, r.get("name") or "路由器", collapse_key=key)
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
        # 便于单路由器界面访问首台；卡片和收起状态始终按设备 ID 保存。
        self.router_box = self.router_widgets[routers[0].get("id", "primary")][0] if routers else None
        if routers:
            self.router_state, self.router_rates, self.router_vals = self.router_widgets[routers[0].get("id", "primary")][1]

    def _render_remote(self, box, data, widgets):
        state, rates, vals = widgets
        text = "未连接" if data["offline"] else ""
        if state.cget("text") != text:
            self._set(state, text, RED)
            box.sync_header()
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
        text = "未连接" if data["offline"] else {"0": "WAN断开", "2": "连接中", "3": "断开中", "4": "WAN异常"}.get(data.get("wan_status"), "WAN")
        if state.cget("text") != text:
            self._set(state, text, RED if data["offline"] or data.get("wan_connected") is False else MUTED)
            box.sync_header()
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
            tree = ttk.Treeview(box.terminal_panel, columns=("down", "up"), style="Router.Treeview",
                                show="tree headings", selectmode="browse", height=1)
            tree.heading("#0", text="设备", anchor="w")
            tree.column("#0", width=1, minwidth=0, stretch=False)
            for key, label in (("down", "↓ 下载"), ("up", "↑ 上传")):
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
                # 按控件实际宽度分配, 避免 Treeview 的默认最小列宽把上传列挤出窗口。
                available = max(3, tree.winfo_width() - 2)
                rate_width = min(box.winfo_pixels("65p"), available // 3)
                name_width = available - 2 * rate_width
                tree.column("#0", width=name_width)
                for key in ("down", "up"):
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
            values = tuple(" ".join(fmt_rate(terminal.get(key))[:2]).strip() for key in ("down_rate", "up"))
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
        """展开内容里的网速行: "↓ 52 KB/s  ↑ 5.1 KB/s", 都没有数据时为 "--"。"""
        if d.get(down) is None and d.get(up) is None:
            return "--"
        parts = []
        for key, arrow in ((down, "↓"), (up, "↑")):
            num, unit, _ = fmt_rate(d.get(key))
            parts.append(f"{arrow} {num} {unit}".rstrip())
        return "  ".join(parts)

    def _render_router(self, r, widgets=None, box=None):
        state, rates, vals = widgets or (self.router_state, self.router_rates, self.router_vals)
        text = "未连接" if r["offline"] else r["nic"] or ""
        if widgets:
            # 徽标固定宽度, 各设备的列保持对齐; pppoe-wan 等较长的网卡名截断显示。
            fnt = state.text_font
            text = self._elide(fnt, text, fnt.measure("0") * int(state.cget("width")))
        if state.cget("text") != text:
            self._set(state, text, RED if r["offline"] else MUTED)
            if box is not None:
                box.sync_header()
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

    def _apply_display(self):
        prefs = self.settings["display"]
        for key, widgets in self.metric_blocks.items():
            for widget in widgets:
                if not hasattr(widget, "saved_pack"):
                    widget.saved_pack = widget.pack_info()
                widget.pack_forget()
        for key in sorted(self.metric_blocks, key=lambda k: prefs.get(k, {}).get("order", 100)):
            p = prefs.get(key, {})
            if p.get("visible", True):
                for widget in self.metric_blocks[key]:
                    widget.pack(**widget.saved_pack)
            if key in self.metric_titles:
                # CPU/显卡温度块的标题就是卡片标题: 温度隐藏时退回设备名, 否则卡片会顶着一行
                # "CPU 温度" 却看不到温度。
                if key in DEVICE_TITLES and not p.get("visible", True):
                    title = DEVICE_TITLES[key]
                else:
                    title = p.get("name") or METRIC_NAMES[key]
                self.metric_titles[key].config(text=title)
        self.groups = dict(zip(GROUP_NAMES, self._group_boxes()))
        for key, box in self.groups.items():
            box.pref_group = key
            if key != "router":
                box.title_label.config(text=prefs.get(key, {}).get("name") or GROUP_NAMES[key])
            box._keys = None
        self.shown = None

    def _layout(self, has):
        prefs = self.settings["display"]
        items = []
        for (key, _), widget, available in zip(self.groups.items(), self.optional, has):
            if key == "router":
                for ident in self.router_order:
                    pref_key = "router:" + ident
                    pref = prefs.get(pref_key, {})
                    items.append((pref_key, self.router_widgets[ident][0].master,
                                  available and pref.get("visible", True), pref.get("order", 100)))
            else:
                pref = prefs.get(key, {})
                items.append((key, widget, available and pref.get("visible", True), pref.get("order", 100)))
        shown = tuple(items)
        if shown == self.shown:
            return
        self.shown = shown
        for widget in (*self.optional, *(box.master for box, _ in self.router_widgets.values())):
            widget.pack_forget()
        for _, widget, visible, _ in sorted(items, key=lambda item: item[3]):
            if visible:
                widget.pack(fill="x", pady=("6p", 0), before=self.content_end)
        self._sync_main_scroll()
