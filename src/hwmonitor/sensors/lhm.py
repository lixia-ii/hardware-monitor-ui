"""进程内读取硬件传感器: 直接调用 LibreHardwareMonitorLib (MPL-2.0), 输出与 LibreHardwareMonitor data.json 相同结构的树。

所有 .NET 调用只发生在采集线程里; 树的格式参照 LibreHardwareMonitor 0.9.6 的
UI/TypeNode.cs (分组名)、UI/SensorNode.cs (数值格式) 和 Utilities/HttpServer.cs (JSON 字段)。
"""
from datetime import timedelta
import itertools
import logging
import os
from pathlib import Path
import sys
import threading
import time

from hwmonitor import paths

log = logging.getLogger(__name__)

# 按 SensorType 枚举顺序: (分组名, 数值格式, 分组图标)
SENSOR_TYPES = {
    "Voltage": ("Voltages", "{:.3f} V", "voltage.png"),
    "Current": ("Currents", "{:.3f} A", "voltage.png"),
    "Power": ("Powers", "{:.1f} W", "power.png"),
    "Clock": ("Clocks", "{:.1f} MHz", "clock.png"),
    "Temperature": ("Temperatures", "{:.1f} °C", "temperature.png"),
    "Load": ("Load", "{:.1f} %", "load.png"),
    "Frequency": ("Frequencies", "{:.1f} Hz", "power.png"),
    "Fan": ("Fans", "{:.0f} RPM", "fan.png"),
    "Flow": ("Flows", "{:.1f} L/h", "flow.png"),
    "Control": ("Controls", "{:.1f} %", "control.png"),
    "Level": ("Levels", "{:.1f} %", "level.png"),
    "Factor": ("Factors", "{:.3f}", "power.png"),
    "Data": ("Data", "{:.1f} GB", "power.png"),
    "SmallData": ("Data", "{:.1f} MB", "power.png"),
    "Throughput": ("Throughput", "{:.1f} B/s", "throughput.png"),
    "TimeSpan": ("Times", "{}", "power.png"),
    "Timing": ("Timings", "{:.3f} ns", "clock.png"),
    "Energy": ("Capacities", "{:.0f} mWh", "power.png"),
    "Noise": ("Noise Levels", "{:.0f} dBA", "loudspeaker.png"),
    "Conductivity": ("Conductivities", "{:.1f} µS/cm", "voltage.png"),
    "Humidity": ("Humidity Levels", "{:.0f} %", "flow.png"),
}
# 按 HardwareType 枚举顺序: 同级硬件按此排序, 值是 data.json 里的图标
HARDWARE_TYPES = {
    "Motherboard": "mainboard.png", "SuperIO": "chip.png", "Cpu": "cpu.png", "Memory": "ram.png",
    "GpuNvidia": "nvidia.png", "GpuAmd": "ati.png", "GpuIntel": "intel.png", "Storage": "hdd.png",
    "Network": "nic.png", "Cooler": "fan.png", "EmbeddedController": "cpu.png", "Psu": "power-supply.png",
    "Battery": "battery.png", "PowerMonitor": "powermonitor.png",
}
ENABLED = ("IsCpuEnabled", "IsGpuEnabled", "IsMotherboardEnabled", "IsMemoryEnabled", "IsStorageEnabled",
           "IsNetworkEnabled", "IsControllerEnabled", "IsPsuEnabled", "IsBatteryEnabled", "IsPowerMonitorEnabled")
RETRY = 60  # 初始化失败后的重试间隔 (秒)
NO_PYTHONNET = "缺少 pythonnet，CPU、显卡等传感器无法读取，点击「安装」自动安装依赖。"


class Unavailable(OSError):
    """传感器暂不可用; 消息直接显示给用户。"""


def format_value(kind, name, value):
    """与 LibreHardwareMonitor 界面/网页一致的带单位文本。"""
    if value is None:
        return "-"
    if kind == "Throughput":
        if name == "Connection Speed":
            if value == 100000000:
                return "100Mbps"
            if value == 1000000000:
                return "1Gbps"
            for limit, div, unit in ((1024, 1, "bps"), (1048576, 1024, "Kbps"), (1073741824, 1048576, "Mbps")):
                if value < limit:
                    return f"{value / div:.{0 if div == 1 else 1}f} {unit}"
            return f"{value / 1073741824:.1f} Gbps"
        return f"{value / 1024:.1f} KB/s" if value < 1048576 else f"{value / 1048576:.1f} MB/s"
    if kind == "TimeSpan":
        return str(timedelta(seconds=round(value)))
    return SENSOR_TYPES.get(kind, ("", "{:.1f}", ""))[1].format(value)


def _raw(kind, value):
    """RawValue: 不换算单位 (网速固定 B/s), 供外部系统稳定读取。"""
    if value is None:
        return "-"
    if kind == "TimeSpan":
        return str(timedelta(seconds=round(value)))
    return SENSOR_TYPES.get(kind, ("", "{:.1f}", ""))[1].format(value)


def _rank(order, key):
    keys = list(order)
    return keys.index(key) if key in keys else len(keys)


def build_tree(hardware, machine=None):
    """把 IHardware 列表 (或结构相同的对象) 转成 data.json 结构; 只读属性, 不触发刷新。"""
    ids = itertools.count()

    def node(text, image, **fields):
        return {"id": next(ids), "Text": text, "Min": "", "Value": "", "Max": "", **fields,
                "ImageURL": image, "Children": []}

    def add_hardware(parent, items):
        for hw in sorted(items, key=lambda h: _rank(HARDWARE_TYPES, str(h.HardwareType))):  # sorted 稳定, 同类保持原顺序
            kind = str(hw.HardwareType)
            n = node(hw.Name, "images_icon/" + HARDWARE_TYPES.get(kind, "cpu.png"),
                     HardwareId=hw.Identifier.ToString())
            parent["Children"].append(n)
            add_hardware(n, list(hw.SubHardware))
            groups = {}
            for s in hw.Sensors:
                groups.setdefault(str(s.SensorType), []).append(s)
            for kind in sorted(groups, key=lambda k: _rank(SENSOR_TYPES, k)):
                label, _, image = SENSOR_TYPES.get(kind, (kind, "", "power.png"))
                g = node(label, "images_icon/" + image)
                n["Children"].append(g)
                for s in sorted(groups[kind], key=lambda s: s.Index):
                    value, low, high = s.Value, s.Min, s.Max
                    g["Children"].append({
                        "id": next(ids), "Text": s.Name,
                        "Min": format_value(kind, s.Name, low), "Value": format_value(kind, s.Name, value),
                        "Max": format_value(kind, s.Name, high),
                        "SensorId": s.Identifier.ToString(), "Type": kind,
                        "RawMin": _raw(kind, low), "RawValue": _raw(kind, value), "RawMax": _raw(kind, high),
                        "ImageURL": "images/transparent.png", "Children": []})

    root = {"id": next(ids), "Text": "Sensor", "Min": "Min", "Value": "Value", "Max": "Max",
            "ImageURL": "", "Children": []}
    computer = node(machine or os.environ.get("COMPUTERNAME", "Computer"), "images_icon/computer.png")
    root["Children"].append(computer)
    add_hardware(computer, list(hardware))
    return root


_computer_type = None


def _load(lib_dir):
    """加载 .NET Framework 运行时和 LibreHardwareMonitorLib; 整个进程只加载一次。"""
    global _computer_type
    if _computer_type is None:
        try:
            from pythonnet import load
        except ImportError as e:
            raise Unavailable(NO_PYTHONNET) from e
        if not (Path(lib_dir) / "LibreHardwareMonitorLib.dll").exists():
            raise Unavailable("找不到 LibreHardwareMonitor 目录，请保留程序目录完整。")
        load("netfx")
        import clr
        # pythonnet 在 sys.path 里查找依赖程序集 (HidSharp、DiskInfoToolkit 等)
        sys.path.append(str(lib_dir))
        clr.AddReference("LibreHardwareMonitorLib")
        from LibreHardwareMonitor.Hardware import Computer
        _computer_type = Computer
    return _computer_type


class Sensors:
    """持有 LibreHardwareMonitor 的 Computer 对象。sample/close 只能在采集线程调用。"""

    def __init__(self, lib_dir=None):
        self.lib_dir = Path(lib_dir or paths.LHM_DIR)
        self.computer = None
        self.failed_at = None
        self.error = None
        self.last_tree = None  # 最近一次成功采样, 供网页服务发布
        self.reopen = threading.Event()

    @property
    def ready(self):
        return self.computer is not None

    def request_reopen(self):
        """装好驱动 (PawnIO) 后重新打开, 才能读到 CPU 温度和主板风扇。可在任意线程调用。"""
        self.reopen.set()

    def _open(self):
        try:
            computer = _load(self.lib_dir)()
            for name in ENABLED:
                setattr(computer, name, True)
            started = time.monotonic()
            computer.Open()
            log.info("Hardware sensors opened in %.1f s", time.monotonic() - started)
        except Unavailable as e:
            self.failed_at, self.error = time.monotonic(), str(e)
            raise
        except Exception as e:
            log.exception("Cannot open hardware sensors")
            self.failed_at, self.error = time.monotonic(), f"硬件传感器初始化失败：{e}"
            raise Unavailable(self.error) from e
        self.computer, self.failed_at, self.error = computer, None, None

    def sample(self):
        """刷新全部传感器, 返回 data.json 结构的树; 不可用时抛 Unavailable。"""
        self.last_tree = None
        if self.reopen.is_set():
            self.reopen.clear()
            self.close()
            self.failed_at = None
        if self.computer is None:
            if self.failed_at is not None and time.monotonic() - self.failed_at < RETRY:
                raise Unavailable(self.error)
            self._open()

        def update(hw):
            try:
                hw.Update()
            except Exception:  # 单个设备 (如拔出的 U 盘) 出错不影响其他设备
                log.debug("Hardware update failed: %s", hw.Name, exc_info=True)
            for sub in hw.SubHardware:
                update(sub)
        hardware = list(self.computer.Hardware)
        for hw in hardware:
            update(hw)
        self.last_tree = build_tree(hardware)
        return self.last_tree

    def close(self):
        computer, self.computer = self.computer, None
        if computer is not None:
            try:
                computer.Close()
            except Exception:
                log.exception("Cannot close hardware sensors")
