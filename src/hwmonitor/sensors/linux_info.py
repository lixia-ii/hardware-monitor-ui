"""Partial static inventory without subprocesses, root, or a display server."""
import platform
from pathlib import Path

from hwmonitor.sensors.linux import read_text


def load():
    import psutil
    dmi = Path("/sys/class/dmi/id")
    cpu = next((line.split(":", 1)[1].strip() for line in read_text(Path("/proc/cpuinfo")).splitlines()
                if line.startswith("model name")), platform.machine())
    rows = [("处理器", [cpu, f"{psutil.cpu_count()} 逻辑处理器"]),
            ("主板", [read_text(dmi / "board_vendor") + " " + read_text(dmi / "board_name")]),
            ("内存", [f"{psutil.virtual_memory().total / 1024 ** 3:.1f} GB"]),
            ("网卡", sorted(psutil.net_if_stats()))]
    disks = []
    for device in sorted(Path("/sys/block").glob("*")):
        if device.name.startswith(("loop", "ram")):
            continue
        name = read_text(device / "device/model") or device.name
        sectors = read_text(device / "size")
        size = int(sectors) * 512 if sectors.isdigit() else None
        disks.append(f"{name} · {size / 1e9:.1f} GB" if size else name)
    rows.append(("磁盘", disks))
    try:
        os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        os_name = "Linux"
    return {"error": None, "model": (read_text(dmi / "sys_vendor") + " " + read_text(dmi / "product_name")).strip() or "Linux",
            "os": os_name + " · " + platform.release(),
            "details": [(k, [v.strip() for v in values if v.strip()]) for k, values in rows]}
