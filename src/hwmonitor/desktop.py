"""Windows desktop startup, imported only after mode selection."""
import ctypes
import json
import logging
from pathlib import Path
import sys
import tkinter as tk

from hwmonitor import __version__, paths
from hwmonitor.core import diagnostics
from hwmonitor.sensors import lhm
from hwmonitor.system import tray
from hwmonitor.system.elevation import elevate_ui
from hwmonitor.ui.main_window import MainWindow
from hwmonitor.ui.theme import grayscale_text

# UAC 提权重启时重新运行的脚本 (源码运行); 打包后由 elevate_ui 直接重启 EXE
ENTRY = Path(__file__).resolve().parent / "__main__.py"


def desktop_main():
    if "--self-test" in sys.argv:
        paths.DATA = paths.BASE / ".build" / "self-test"
        tray.APP_ID += "SelfTest"
    diagnostics.setup_logging()
    if "--self-test" not in sys.argv:
        try:
            if elevate_ui(ENTRY):
                sys.exit(0)
        except OSError:
            logging.getLogger(__name__).exception("UI elevation cancelled or failed")
            sys.exit(1)
    # 独立的任务栏标识, 否则任务栏会归到 pythonw 并显示 Python 图标
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("hwmonitor.fan.app")
    # 声明 DPI 感知, 否则缩放 125%/150% 的屏幕上会被系统拉伸而发虚。
    # 开机自启时进程在登录早期启动, 系统级感知 (1) 取到的可能是未就绪的 DPI (如 96), 之后被拉伸; 优先用 per-monitor v2, 按显示器实际 DPI。
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        ok = False
    if not ok:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
    if not tray.acquire_single_instance():  # 已在运行 (如开机自启后又双击了 start.bat): 唤出那个窗口后退出
        sys.exit(0)
    if "--self-test" not in sys.argv:
        try:
            paths.migrate_legacy_data()
        except OSError:
            logging.getLogger(__name__).exception("Cannot migrate old data directory")
    tray.dark_menus()
    try:
        grayscale_text()
    except Exception:
        pass
    application = MainWindow()
    if "--self-test" in sys.argv:
        errors = []
        application.root.report_callback_exception = lambda *args: errors.append(str(args))
        def finish_test():
            for child in application.root.winfo_children():
                if isinstance(child, tk.Toplevel):
                    child.destroy()
            try:  # 只加载 .NET 运行时和硬件库, 不打开硬件
                lhm._load(paths.LHM_DIR)
                lhm_loaded = True
            except Exception:
                logging.getLogger(__name__).exception("Self-test cannot load LibreHardwareMonitorLib")
                lhm_loaded = False
            payload = {"version": __version__, "errors": errors, "sampled": application.data is not None,
                       "icon_available": paths.ICON.exists(), "lhm_loaded": lhm_loaded}
            (paths.DATA / "self-test.json").write_text(json.dumps(payload), encoding="utf-8")
            application.quit()
        application.root.after(1000, application.show_history)
        application.root.after(1500, application.show_settings)
        application.root.after(4000, finish_test)
    application.root.mainloop()
