"""Own only the process HANDLE returned by our launch, never an image name/PID lookup."""
import ctypes
import logging
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


class LibreProcess:
    def __init__(self):
        self.handle = None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel.GetProcessId.argtypes = [wt.HANDLE]
        self.kernel.GetProcessId.restype = wt.DWORD
        self.kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self.kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        self.kernel.CloseHandle.argtypes = [wt.HANDLE]
        self.shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
        self.user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self.user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]

    def launch(self, exe):
        if self.handle:
            if self.kernel.WaitForSingleObject(self.handle, 0) == 258:
                return
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        info = SHELLEXECUTEINFO(cbSize=ctypes.sizeof(SHELLEXECUTEINFO), fMask=0x40 | 0x100,
                               lpVerb="runas", lpFile=str(exe), lpDirectory=str(Path(exe).parent), nShow=7)
        if not self.shell.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
            raise OSError(ctypes.get_last_error(), "LibreHardwareMonitor 启动失败或已取消")
        self.handle = info.hProcess

    def stop(self):
        handle, self.handle = self.handle, None
        if not handle:
            return
        try:
            if self.kernel.WaitForSingleObject(handle, 0) != 258:
                return
            pid = self.kernel.GetProcessId(handle)
            callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

            @callback_type
            def close_window(hwnd, _):
                owner = wt.DWORD()
                self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value == pid:
                    self.user.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE
                return True
            self.user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
            self.user.EnumWindows(close_window, 0)
            if self.kernel.WaitForSingleObject(handle, 4000) == 258:
                if not self.kernel.TerminateProcess(handle, 0):
                    logging.getLogger(__name__).error("Cannot terminate owned LibreHardwareMonitor: %s", ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(handle)


def elevate_ui(entry):
    """Keep UI and owned hardware process at the same integrity level, including direct EXE starts."""
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
