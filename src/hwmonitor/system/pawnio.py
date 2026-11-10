"""PawnIO 驱动: LibreHardwareMonitorLib 靠它读取 CPU 温度和主板风扇。

检测和安装流程与 LibreHardwareMonitor 0.9.6 的 MainForm 相同: 按卸载注册表里的版本判断,
未安装或低于 2.0 时运行随附的 PawnIO_setup.exe -install (官方安装程序, 作者 namazso, GPL-2.0)。
"""
import subprocess
import winreg

from hwmonitor import paths

MIN_VERSION = (2, 0, 0, 0)
KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"
SETUP = paths.LHM_DIR / "PawnIO_setup.exe"


def version():
    """已安装的版本元组; 未安装返回 None。"""
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, KEY, 0, winreg.KEY_READ | view) as key:
                text = winreg.QueryValueEx(key, "DisplayVersion")[0]
            return tuple(int(part) for part in str(text).split("."))
        except (OSError, ValueError):
            continue
    return None


def status():
    """"ok" / "missing" / "outdated"。"""
    v = version()
    if v is None:
        return "missing"
    return "outdated" if v < MIN_VERSION else "ok"


def install(setup=None):
    """运行安装程序并等待结束 (界面已是管理员, 不会再弹 UAC); 返回是否已装好。"""
    setup = setup or SETUP
    if not setup.exists():
        raise OSError(f"找不到 {setup.name}")
    subprocess.run([str(setup), "-install"], cwd=str(setup.parent), check=False)
    return status() == "ok"
