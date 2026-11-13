"""托盘右键菜单: 开关项按当前状态打勾, 选中项映射为事件名。"""
import unittest
import sys

if sys.platform != "win32":
    raise unittest.SkipTest("Windows 桌面专属测试")
from unittest.mock import patch

from hwmonitor.system import tray


class MenuTests(unittest.TestCase):
    def popup(self, cmd, **state):
        items = []
        host = tray.Tray.__new__(tray.Tray)  # 不真的创建托盘图标
        host.hwnd = None
        with patch.object(tray.u32, "CreatePopupMenu", return_value=1), \
                patch.object(tray.u32, "AppendMenuW", side_effect=lambda m, flags, ident, text: items.append((flags, ident, text))), \
                patch.object(tray.u32, "GetCursorPos"), patch.object(tray.u32, "SetForegroundWindow"), \
                patch.object(tray.u32, "TrackPopupMenu", return_value=cmd), patch.object(tray.u32, "PostMessageW"), \
                patch.object(tray.u32, "DestroyMenu"):
            result = host.menu(**state)
        return result, {ident: flags for flags, ident, text in items if text}

    def test_toggles_show_current_state(self):
        _, flags = self.popup(0, floating=True, topmost=False)
        self.assertEqual(flags[tray.MENU_FLOATING], tray.MF_CHECKED)
        self.assertEqual(flags[tray.MENU_TOPMOST], 0)
        _, flags = self.popup(0, floating=False, topmost=True)
        self.assertEqual((flags[tray.MENU_FLOATING], flags[tray.MENU_TOPMOST]), (0, tray.MF_CHECKED))

    def test_commands(self):
        for cmd, event in ((tray.MENU_SHOW, "show"), (tray.MENU_FLOATING, "floating"),
                           (tray.MENU_TOPMOST, "topmost"), (tray.MENU_QUIT, "quit"), (0, None)):
            self.assertEqual(self.popup(cmd)[0], event)


if __name__ == "__main__":
    unittest.main()
