"""Fixture host for browser QA. Never uses real hardware or user settings.

Run: python tests/ui/preview_web.py
"""
import math
import os
from pathlib import Path
import sys
import time
from collections import deque
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ["HARDWARE_MONITOR_DATA_DIR"] = str(ROOT / ".build" / "test-data" / "web-preview")

from hwmonitor import settings
from hwmonitor.core import history
from hwmonitor.sensors import reading
from hwmonitor.system.management_server import ManagementServer
from hwmonitor.system.webbridge import Bridge
from hwmonitor.ui.web_management import WebManagementMixin
from tests.support import sample_tree


class FixtureDesktop(WebManagementMixin):
    def __init__(self):
        self.settings = settings.validate({"routers": [dict(settings.DEFAULT_TPLINK, id="primary", name="家庭路由器", enabled=True)]})
        self.data = reading.empty_reading()
        self.history = deque(maxlen=history.MAX_ROWS)
        self.recent_alerts = deque([{"time":"2026-11-11 10:00:00", "message":"测试告警：CPU 温度已恢复"}])
        self.sensor_names = {}
        self.collector = SimpleNamespace(storage_error=None, configure=lambda _: None)
        self.float_win = None
        self._web_hardware = {"error":None, "model":"浏览器验证 · 模拟电脑", "os":"Windows 11", "details":[["处理器",["AMD Ryzen 测试处理器"]],["显卡",["NVIDIA GeForce 测试显卡"]]]}
        now = time.time()
        for i in range(17280):
            values = {"cpu_temp":50+10*math.sin(i/100), "gpu_temp":60+5*math.sin(i/150), "cpu_load":30+20*math.sin(i/55), "gpu_load":50,
                      "mem_load":42,"gpu_mem_load":25,"net_up":100000,"net_down":2000000,"gpu_id":"/gpu-nvidia/0","gpu_name":"NVIDIA one","net_selection":None}
            self.history.append((now-86400+i*5,values))

    def render(self, data):
        pass

    def _apply_display(self):
        pass

    def set_topmost(self, on):
        pass

    def show_floating(self):
        self.float_win = SimpleNamespace(winfo_exists=lambda: True, apply_options=lambda before: None)

    def hide_floating(self):
        self.float_win = None

    def refresh(self):
        self.data = reading.read(source=SimpleNamespace(sample=sample_tree), allow_nvidia=False)
        self.data.update(cpu_temp=55, gpu_temp=65, sample_time=time.time(), collection_ms=12.5,
                         routers=[dict(id="primary", name="家庭路由器", type="tplink", offline=False, wan_ip="203.0.113.8", wan_connected=True, wan_uptime=86400,
                                       clients=8, up=150000, down_rate=2000000, terminals=[dict(id="AA:BB:CC:DD:EE:01",name="测试 iPhone",ip="192.168.1.10",up=2000,down_rate=1200000)])])


if __name__ == "__main__":
    owner, bridge, server = FixtureDesktop(), Bridge(), ManagementServer()
    server.bridge = bridge
    error = server.apply(True, 8843)
    if error:
        raise SystemExit(error)
    print("Fixture preview: http://127.0.0.1:8843/ (simulated data)", flush=True)
    try:
        while True:
            owner.refresh()
            bridge.drain(owner._web_dispatch)
            time.sleep(.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
