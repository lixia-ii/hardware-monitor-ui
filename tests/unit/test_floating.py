"""悬浮窗吸附与贴边隐藏的位置计算。"""
import unittest

from types import SimpleNamespace

from hwmonitor.ui import floating

WORK = (0, 0, 1920, 1040)  # 底部任务栏占 40 像素


class SnapTests(unittest.TestCase):
    def test_free_position_unchanged(self):
        self.assertEqual(floating.snap(500, 300, 200, 150, WORK, 12), (500, 300, None))

    def test_snaps_to_nearby_edges(self):
        self.assertEqual(floating.snap(8, 300, 200, 150, WORK, 12), (0, 300, "left"))
        self.assertEqual(floating.snap(1715, 300, 200, 150, WORK, 12), (1720, 300, "right"))
        self.assertEqual(floating.snap(500, 10, 200, 150, WORK, 12), (500, 0, "top"))
        self.assertEqual(floating.snap(5, 5, 200, 150, WORK, 12), (0, 0, "left"))

    def test_bottom_snaps_but_never_hides(self):
        self.assertEqual(floating.snap(500, 885, 200, 150, WORK, 12), (500, 890, None))

    def test_pulled_back_on_screen(self):
        self.assertEqual(floating.snap(-500, 2000, 200, 150, WORK, 12), (0, 890, "left"))
        self.assertEqual(floating.snap(1900, -80, 200, 150, WORK, 12), (1720, 0, "right"))

    def test_hidden_positions_leave_a_strip(self):
        s = floating.STRIP
        self.assertEqual(floating.tucked(0, 300, 200, 150, "left", WORK), (s - 200, 300))
        self.assertEqual(floating.tucked(1720, 300, 200, 150, "right", WORK), (1920 - s, 300))
        self.assertEqual(floating.tucked(500, 0, 200, 150, "top", WORK), (500, s - 150))
        self.assertEqual(floating.strip_rect(0, 300, 200, 150, "left", WORK), (0, 300, s, 150))
        self.assertEqual(floating.strip_rect(1720, 300, 200, 150, "right", WORK), (1920 - s, 300, s, 150))
        self.assertEqual(floating.strip_rect(500, 0, 200, 150, "top", WORK), (500, 0, 200, s))


class FullscreenTests(unittest.TestCase):
    def test_covers_whole_monitor(self):
        monitor = (0, 0, 1920, 1080)
        self.assertTrue(floating.covers((0, 0, 1920, 1080), monitor))
        self.assertTrue(floating.covers((-8, -8, 1928, 1088), monitor))
        self.assertFalse(floating.covers((0, 0, 1920, 1040), monitor))  # 最大化, 任务栏还露着
        self.assertFalse(floating.covers((1920, 0, 3840, 1080), monitor))  # 隔壁屏幕上的全屏

    def test_topmost_window_on_this_monitor_decides(self):
        def win(rect, cls="App", topmost=False, framed_max=False):
            return SimpleNamespace(rect=rect, cls=cls, topmost=topmost, framed_max=framed_max)
        a = (0, 0, 1920, 1080)
        video = win(a)
        desktop = win((0, 0, 3840, 1080), "Progman")
        browser_b = win((1920, 0, 3840, 1040))  # 焦点在 B 屏的普通窗口
        toast = win((1500, 900, 1900, 1000), topmost=True)
        taskbar = win((0, 1040, 1920, 1080), "Shell_TrayWnd", topmost=True)
        self.assertTrue(floating.fullscreen_in([browser_b, video, desktop], a))
        self.assertTrue(floating.fullscreen_in([taskbar, toast, video, desktop], a))
        self.assertFalse(floating.fullscreen_in([video, desktop], (1920, 0, 3840, 1080)))  # B 屏不受影响
        self.assertFalse(floating.fullscreen_in([win((100, 100, 800, 600)), video, desktop], a))  # 普通窗口压在全屏上
        self.assertFalse(floating.fullscreen_in([win((-8, -8, 1928, 1088), framed_max=True), desktop], a))
        self.assertFalse(floating.fullscreen_in([desktop], a))
        rdp = SimpleNamespace(rect=a, cls="TscShellContainerClass", topmost=False, framed_max=False, hwnd=7, owner=None)
        bar = SimpleNamespace(rect=(700, 0, 1200, 30), cls="BBarWindowClass", topmost=False, framed_max=False, hwnd=8, owner=7)
        dialog = SimpleNamespace(rect=(700, 300, 1200, 600), cls="#32770", topmost=False, framed_max=False, hwnd=9, owner=5)
        self.assertTrue(floating.fullscreen_in([bar, rdp, desktop], a))  # 远程桌面连接栏是它自己的
        self.assertFalse(floating.fullscreen_in([dialog, rdp, desktop], a))  # 别的程序的对话框压在上面


if __name__ == "__main__":
    unittest.main()
