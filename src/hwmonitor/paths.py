"""程序目录、数据目录与内置第三方文件的位置; 兼容源码运行与 PyInstaller 打包。"""
import os
from pathlib import Path
import sys

FROZEN = getattr(sys, "frozen", False)
# 源码运行时是仓库根目录 (src/hwmonitor/paths.py 往上三级), 打包后是 EXE 所在目录
BASE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("HARDWARE_MONITOR_DATA_DIR", BASE / "data"))
# 内置的 LibreHardwareMonitorLib 及其依赖、PawnIO 安装程序; 打包后放在 EXE 旁
LHM_DIR = BASE / "LibreHardwareMonitor" if FROZEN else BASE / "vendor" / "LibreHardwareMonitor"
ASSETS = Path(__file__).resolve().parent / "ui" / "assets"
ICON = ASSETS / "app.ico"


def migrate_legacy_data(base=None, data=None):
    """After acquiring the instance lock, move old portable data without overwriting files."""
    if base is None and "HARDWARE_MONITOR_DATA_DIR" in os.environ:
        return
    base, data = Path(base or BASE), Path(data or DATA)
    if base.resolve() == data.resolve():
        return
    migrate_db = not (data / "history.sqlite3").exists()
    names = ("settings.json", "history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm",
             "monitor.log", "monitor.log.1", "monitor.log.2")
    for name in names:
        source, target = base / name, data / name
        if not source.exists() or target.exists() or (name.startswith("history.") and not migrate_db):
            continue
        data.mkdir(parents=True, exist_ok=True)
        source.replace(target)
