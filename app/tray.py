"""系统托盘图标与单实例, 纯 ctypes 实现 (不依赖 pywin32)。

tkinter 没有托盘能力, 这里另建一个不显示的 Win32 窗口专收托盘消息。Tk 的事件循环会派发本线程所有窗口的消息,
所以窗口过程会在主线程里被调用; 但回调里只把事件记下, 由 Tk 的 after 循环 poll() 取出处理, 不在 Win32 回调里碰 Tk。
"""
import ctypes
import os
from ctypes import wintypes as wt

APP_ID = "HardwareMonitorUI"  # 互斥量名 / 托盘窗口类名; 改名会让新旧版本互相认不出来
WM_TRAY = 0x0400 + 20  # WM_USER + 20: 托盘图标的回调消息
WM_WAKE = 0x8000 + 1   # WM_APP + 1: 第二个实例请求唤出窗口
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_WARNING = 0x00000002
MENU_SHOW, MENU_QUIT = 1, 2

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.FindWindowW.restype = wt.HWND
u32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.LoadImageW.restype = wt.HANDLE
u32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
u32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
u32.CreatePopupMenu.restype = wt.HMENU
u32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
u32.DestroyMenu.argtypes = [wt.HMENU]
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.DestroyWindow.argtypes = [wt.HWND]
u32.DestroyIcon.argtypes = [wt.HICON]
u32.RegisterWindowMessageW.restype = wt.UINT
u32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
u32.ChangeWindowMessageFilterEx.argtypes = [wt.HWND, wt.UINT, wt.DWORD, wt.LPVOID]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

_mutex = None  # 互斥量句柄要活到进程结束


def acquire_single_instance():
    """成为唯一实例返回 True; 已有实例在运行时通知它唤出窗口, 返回 False (本进程应退出)。"""
    global _mutex
    _mutex = k32.CreateMutexW(None, False, f"Local\\{APP_ID}_SingleInstance")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return True
    u32.AllowSetForegroundWindow(-1)  # ASFW_ANY: 放权给旧实例, 它才能把窗口弹到最前
    hwnd = u32.FindWindowW(f"{APP_ID}TrayHost", None)
    if hwnd:
        u32.PostMessageW(hwnd, WM_WAKE, 0, 0)
    return False


def dark_menus():
    """托盘右键菜单跟随系统深色模式 (uxtheme 未公开的序号 135/136, Win10 1903+; 失败就保持浅色)。"""
    try:
        ux = ctypes.WinDLL("uxtheme")
        ux[135](1)  # SetPreferredAppMode(AllowDark)
        ux[136]()   # FlushMenuThemes
    except Exception:
        pass


class Tray:
    """托盘图标: 程序运行期间常驻。events 里依次记下 "show" / "menu", 由 poll() 取出。失败时 ok=False, 主程序照常运行。"""

    def __init__(self, ico_path, tip):
        self.ok = False
        self.events = []
        self._tip = tip
        self._added = False
        self._taskbar_created = None  # 建窗口期间就会有消息进来, 先占位
        self._proc = WNDPROC(self._wndproc)  # 保持引用, 否则回调被回收
        try:
            hinst = k32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=f"{APP_ID}TrayHost")
            u32.RegisterClassW(ctypes.byref(wc))
            # 普通的不可见顶层窗口 (不用 HWND_MESSAGE): 才收得到资源管理器重启时广播的 TaskbarCreated
            self.hwnd = u32.CreateWindowExW(0, wc.lpszClassName, APP_ID, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                return
            self._taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
            # 本程序常以管理员身份运行, 放行普通权限进程发来的唤醒消息 (UIPI)
            for msg in (WM_WAKE, self._taskbar_created):
                u32.ChangeWindowMessageFilterEx(self.hwnd, msg, 1, None)  # MSGFLT_ALLOW
            # 托盘溢出面板会把图标放大显示, 图源取大一些 (app.ico 里有 64px 这一帧)
            self.hicon = u32.LoadImageW(None, os.path.abspath(ico_path), 1, 64, 64, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            self.ok = self._notify(NIM_ADD)
            self._added = self.ok
        except Exception:
            self.ok = False

    def _nid(self):
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                              uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY, hIcon=self.hicon)
        nid.szTip = self._tip[:127]
        return nid

    def _notify(self, op):
        return bool(shell32.Shell_NotifyIconW(op, ctypes.byref(self._nid())))

    def set_tip(self, tip):
        """更新鼠标悬停提示 (最多 127 字)。"""
        if self.ok and tip != self._tip:
            self._tip = tip
            self._notify(NIM_MODIFY)

    def notify(self, title, message):
        """显示 Windows 托盘提醒；托盘不可用时返回 False。"""
        if not self.ok:
            return False
        nid = self._nid()
        nid.uFlags |= NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_WARNING
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam == WM_LBUTTONUP:
                self.events.append("show")
            elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self.events.append("menu")
            return 0
        if msg == WM_WAKE:
            self.events.append("show")
            return 0
        if msg == self._taskbar_created and self._added:  # 资源管理器重启后托盘图标会丢, 重新挂上
            self._notify(NIM_ADD)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def poll(self):
        """取出积攒的事件。"""
        ev, self.events = self.events, []
        return ev

    def menu(self):
        """弹出右键菜单, 返回选中的 "show" / "quit", 点别处返回 None。"""
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, 0, MENU_SHOW, "显示主窗口")
        u32.AppendMenuW(m, 0x800, 0, None)  # MF_SEPARATOR
        u32.AppendMenuW(m, 0, MENU_QUIT, "退出")
        pt = wt.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)  # 否则点菜单外面时菜单不消失 (Win32 老问题)
        cmd = u32.TrackPopupMenu(m, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)  # TPM_RETURNCMD | TPM_RIGHTBUTTON
        u32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL
        u32.DestroyMenu(m)
        return {MENU_SHOW: "show", MENU_QUIT: "quit"}.get(cmd)

    def destroy(self):
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
            self._added = False
        if getattr(self, "hwnd", None):
            u32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if getattr(self, "hicon", None):
            u32.DestroyIcon(self.hicon)
            self.hicon = None
