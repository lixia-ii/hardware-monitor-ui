"""测试共用的模拟数据: data.json 结构的传感器树, 以及模拟 LibreHardwareMonitorLib 的硬件/传感器对象。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def leaf(text, value, typ):
    return {"Text": text, "Value": value, "Type": typ, "Children": []}


def hardware_node(hwid, name, children):
    return {"HardwareId": hwid, "Text": name, "Children": children}


def sample_tree():
    """一台 AMD CPU + NVIDIA 显卡 + 两块网卡 (含虚拟网卡) + NVMe 硬盘的传感器树。"""
    return {"Children": [hardware_node("/amdcpu/0", "Ryzen", [leaf("Core (Tctl)", "100 °C", "Temperature"),
                leaf("Core (Tdie)", "158 °F", "Temperature"), leaf("CPU Total", "25 %", "Load")]),
            hardware_node("/gpu-nvidia/0", "NVIDIA one", [leaf("GPU Core", "65 °C", "Temperature"),
                leaf("GPU Core", "50 %", "Load"), leaf("GPU Fan", "0 RPM", "Fan"),
                leaf("GPU Memory Used", "1024 MB", "SmallData"), leaf("GPU Memory Total", "8192 MB", "SmallData")]),
            hardware_node("/nic/1", "Ethernet", [leaf("Upload Speed", "1 MB/s", "Throughput"), leaf("Download Speed", "2 MB/s", "Throughput")]),
            hardware_node("/nic/2", "Hyper-V Virtual", [leaf("Upload Speed", "10 MB/s", "Throughput")]),
            hardware_node("/nvme/0", "SSD", [leaf("Composite Temperature", "40 °C", "Temperature"), leaf("Critical Temperature", "90 °C", "Temperature")])]}


class FakeId:
    def __init__(self, text):
        self.text = text

    def ToString(self):
        return self.text


def fake_sensor(kind, name, value, index=0, ident=None):
    return SimpleNamespace(SensorType=kind, Name=name, Value=value, Min=value, Max=value, Index=index,
                           Identifier=FakeId(ident or f"/x/{kind}/{index}"))


def fake_hardware(kind, name, ident, sensors=(), sub=()):
    return SimpleNamespace(HardwareType=kind, Name=name, Identifier=FakeId(ident), Sensors=list(sensors),
                           SubHardware=list(sub), Update=Mock())
