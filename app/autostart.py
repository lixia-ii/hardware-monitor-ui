"""开机自启动: 用"任务计划程序"在登录时以最高权限运行。

LibreHardwareMonitor 需要管理员权限, 写注册表 Run 项的话每次开机都会弹 UAC; 计划任务勾选"最高权限"后开机不弹,
只在开启/关闭这个选项时确认一次 UAC。任务启动的是 monitor.py --autostart: 隐藏到托盘, 并顺带拉起 LibreHardwareMonitor。
"""
import ctypes
import os
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

TASK = "HardwareMonitorUI"
ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.py")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _pythonw():
    """优先用 pythonw.exe, 开机时不闪控制台黑窗。"""
    exe = os.path.abspath(sys.executable)
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def _xml():
    """任务定义。用 XML 而不是 schtasks 的命令行参数: 命令行建的任务默认"使用电池时不启动"、运行满 72 小时强制结束,
    对常驻程序都不合适。"""
    user = escape(os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", ""))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>硬件监控: 登录时启动并最小化到托盘</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT10S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(_pythonw())}</Command>
    <Arguments>{escape(f'"{ENTRY}" --autostart')}</Arguments>
    <WorkingDirectory>{escape(os.path.dirname(ENTRY))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""


def _query_xml():
    """读取已注册任务的 XML; 没有该任务返回 None。"""
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode:
        return None
    raw = r.stdout
    return raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("mbcs", "replace")


def is_enabled():
    return _query_xml() is not None


def _schtasks(args):
    """运行 schtasks, 返回退出码。已是管理员直接运行; 否则经 UAC 提权运行 (取消 UAC 返回非 0)。会阻塞, 放后台线程调用。"""
    try:
        return _run_schtasks(args)
    except (OSError, subprocess.TimeoutExpired):
        return 1


def _run_schtasks(args):
    if ctypes.windll.shell32.IsUserAnAdmin():
        return subprocess.run(["schtasks", *args], capture_output=True, creationflags=_NO_WINDOW, timeout=30).returncode
    # 参数经环境变量传入, 路径里的引号等字符不会破坏 PowerShell 语句
    env = dict(os.environ, ST_ARGS=subprocess.list2cmdline(args))
    ps = ("try { $p = Start-Process schtasks -ArgumentList $env:ST_ARGS -Verb RunAs -WindowStyle Hidden -Wait -PassThru;"
          " exit $p.ExitCode } catch { exit 1223 }")
    # UAC 弹窗等用户确认, 超时给足
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                          capture_output=True, creationflags=_NO_WINDOW, timeout=120).returncode


def enable():
    """创建 (或覆盖) 计划任务。返回 (成功与否, 提示文本)。"""
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(_xml())
        code = _schtasks(["/Create", "/TN", TASK, "/XML", path, "/F"])
    finally:
        os.remove(path)
    if code == 0:
        return True, "已设置开机自动启动"
    return False, "已取消 (UAC 未确认)" if code == 1223 else f"设置失败 (schtasks 返回 {code})"


def disable():
    if not is_enabled():
        return True, "已取消开机自动启动"
    code = _schtasks(["/Delete", "/TN", TASK, "/F"])
    if code == 0:
        return True, "已取消开机自动启动"
    return False, "已取消 (UAC 未确认)" if code == 1223 else f"取消失败 (schtasks 返回 {code})"


def set_enabled(on):
    return enable() if on else disable()


def sync():
    """启动时校正任务里的路径: 程序目录被移动、改名或换了 Python 后, 旧任务会指向不存在的位置, 开机时静默失败。
    只在已开启且本程序是管理员时修正 (不为这个弹 UAC)。"""
    xml = _query_xml()
    if not xml or not ctypes.windll.shell32.IsUserAnAdmin():
        return
    if escape(ENTRY) not in xml or escape(_pythonw()) not in xml:
        enable()
