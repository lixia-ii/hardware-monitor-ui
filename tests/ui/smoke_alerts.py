"""Real Tk alert log QA with isolated persisted records; --preview leaves the window open."""
from pathlib import Path
import sys
import tempfile
import tkinter as tk

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]

from hwmonitor.core.runtime import RuntimeState
from hwmonitor.ui.alerts_dialog import AlertsWindow
from hwmonitor.ui.theme import apply_style


root = tk.Tk()
root.withdraw()
images = apply_style(root)
errors = []
root.report_callback_exception = lambda *args: errors.append(args)


def poll_now(window):
    window.after_cancel(window._job)
    window._poll()
    root.update()


try:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "alerts.sqlite3"
        original = RuntimeState()
        original.load_alerts(path)
        for i in range(53):
            original.record(f"告警 {i}：CPU 温度超过阈值，监控服务持续检测。")
        original.close_alerts(timeout=None)
        state = RuntimeState()
        state.load_alerts(path)
        window = AlertsWindow(root, state)
        root.update()
        assert "共 53 条" in window.summary.cget("text")
        assert "告警 52：" in window.text.get("1.0", "end")
        assert "告警 32：" not in window.text.get("1.0", "end")
        assert window.previous.instate(["disabled"])
        window.next.invoke()
        assert window.page == 2 and "告警 32：" in window.text.get("1.0", "end")
        saved_text = window.text.get("1.0", "end")
        state.record("新告警：" + "设备终端上线与下线详情，长消息自动换行。" * 30)
        poll_now(window)
        assert window.text.get("1.0", "end") == saved_text
        window.page_var.set("3")
        window.page_select.event_generate("<<ComboboxSelected>>")
        root.update()
        assert window.page == 3 and window.next.instate(["disabled"])
        assert "告警 0：" in window.text.get("1.0", "end")
        window.refresh_latest()
        assert "共 54 条" in window.summary.cget("text")
        assert "新告警：" in window.text.get("1.0", "end")
        assert window.text.cget("state") == "disabled"
        state.record("首页自动更新验证")
        poll_now(window)
        assert "首页自动更新验证" in window.text.get("1.0", "end")
        window.geometry(f"{window.minsize()[0]}x{window.minsize()[1]}")
        root.update()
        for widget in (window.previous, window.next, window.page_select):
            assert widget.winfo_ismapped() and widget.winfo_width() > 10
            assert widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        window.destroy()
        state.close_alerts(timeout=None)
        empty = RuntimeState()
        empty.alert_storage_error = "告警保存失败，当前仅在内存中保留。"
        window = AlertsWindow(root, empty)
        root.update()
        assert "暂无告警" in window.text.get("1.0", "end")
        assert window.previous.instate(["disabled"]) and window.next.instate(["disabled"])
        assert "告警保存失败" in window.storage_note.cget("text")
        window.destroy()
        root.update()
        assert not errors, errors
        print("Desktop alert log passed: persisted records, paging, stable old pages, live first page, long messages, minimum size, empty/error states and shutdown.", flush=True)
        if "--preview" in sys.argv:
            window = AlertsWindow(root, state)
            window.geometry("900x680+400+120")
            window.protocol("WM_DELETE_WINDOW", root.quit)
            window.after(180000, root.quit)
            root.mainloop()
finally:
    root.destroy()
