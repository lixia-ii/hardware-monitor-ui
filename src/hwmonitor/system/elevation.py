"""以管理员身份重新启动界面: 进程内读取硬件传感器 (LibreHardwareMonitorLib) 需要管理员权限。"""
import ctypes
from ctypes import wintypes as wt
from pathlib import Path
import subprocess
import sys


class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", wt.ULONG), ("hwnd", wt.HWND),
                ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                ("lpDirectory", wt.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", wt.LPVOID), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]


def elevate_ui(entry):
    """未以管理员运行时用 UAC 重新启动自己 (源码与 EXE 均适用); 已重新启动返回 True。"""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, sys.argv[1:]
    else:
        candidate = Path(sys.executable).with_name("pythonw.exe")
        exe = str(candidate) if candidate.exists() else sys.executable
        args = [str(Path(entry).resolve()), *sys.argv[1:]]
    info = SHELLEXECUTEINFO(cbSize=ctypes.sizeof(SHELLEXECUTEINFO), fMask=0x40 | 0x100,
                           lpVerb="runas", lpFile=exe, lpParameters=subprocess.list2cmdline(args), nShow=1)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "启动已取消或管理员权限未确认")
    if info.hProcess:
        kernel = ctypes.WinDLL("kernel32")
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(info.hProcess)
    return True
