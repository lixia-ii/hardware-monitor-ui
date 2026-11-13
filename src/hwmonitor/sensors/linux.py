"""Unprivileged Linux sampling. Optional sensors never break basic monitoring."""
import math
from pathlib import Path
import re
import time

from hwmonitor.sensors import nvidia, reading
from hwmonitor.sensors.backend import Snapshot


def read_text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def compatibility_tree(data):
    """Serialize the SAME sample for older remote clients; never read hardware here."""
    nodes = []

    def hardware(name, identity, metrics):
        children = [{"Text": label, "Type": kind, "Value": f"{data[key]} {unit}",
                     "SensorId": identity + "/" + key} for key, label, kind, unit in metrics if data.get(key) is not None]
        nodes.append({"Text": name, "HardwareId": identity, "Children": children})

    hardware("CPU", "/intelcpu/linux", [("cpu_load", "CPU Total", "Load", "%"),
                                        ("cpu_temp", "CPU Package", "Temperature", "°C"),
                                        ("cpu_fan", "CPU Fan", "Fan", "RPM")])
    if data.get("gpu_id"):
        hardware(data["gpu_name"], "/gpu-nvidia/" + data["gpu_id"],
                 [("gpu_temp", "GPU Core", "Temperature", "°C"), ("gpu_load", "GPU Core", "Load", "%"),
                  ("gpu_mem_used", "GPU Memory Used", "Data", "B"), ("gpu_mem_total", "GPU Memory Total", "Data", "B")])
    data = dict(data, mem_available=(data["mem_total"] - data["mem_used"]) if data["mem_total"] else None)
    hardware("Memory", "/ram", [("mem_load", "Memory", "Load", "%"), ("mem_used", "Memory Used", "Data", "B"),
                                 ("mem_available", "Memory Available", "Data", "B")])
    hardware("Selected interfaces", "/nic/linux-selected", [("net_up", "Upload Speed", "Throughput", "B/s"),
                                                            ("net_down", "Download Speed", "Throughput", "B/s")])
    for group, kind, typ, unit in (("case_fans", "board", "Fan", "RPM"), ("pumps", "board", "Fan", "RPM"),
                                    ("extra_temps", "other", "Temperature", "°C")):
        for index, item in enumerate(data[group]):
            name, value = item[-2:]
            category = item[0] if group == "extra_temps" else kind
            prefix = {"storage": "nvme", "memory": "memory/dimm", "network": "nic"}.get(category, "motherboard")
            identity = data["sensor_ids"][group][index]
            nodes.append({"Text": name, "HardwareId": "/" + prefix + "/" + identity,
                          "Children": [{"Text": name, "Type": typ, "Value": f"{value} {unit}", "SensorId": identity}]})
    return {"Text": "Linux", "Children": nodes}


class LinuxBackend:
    ready = True

    def __init__(self, api=None, sysroot=Path("/sys"), procroot=Path("/proc"), clock=time.monotonic, enable_nvidia=False):
        if api is None:
            import psutil
            api = psutil
        self.api, self.sysroot, self.procroot = api, Path(sysroot), Path(procroot)
        self.clock, self.enable_nvidia = clock, enable_nvidia
        self.previous = {}
        self.selection = None
        self.cpu_warm = False

    def _identity(self, name):
        path = self.sysroot / "class/net" / name
        device = str((path / "device").resolve()) if (path / "device").exists() else "virtual"
        return "linux:nic:" + name + ":" + read_text(path / "address") + ":" + device

    def _network(self, data, nic_ids):
        counters = self.api.net_io_counters(pernic=True, nowrap=False)
        stats = self.api.net_if_stats()
        available = {name: self._identity(name) for name in counters if name != "lo"}
        data["nics"] = [(ident, name) for name, ident in available.items()]
        # Default route is a single accounting boundary (bridges/VLANs are not summed).
        routes = read_text(self.procroot / "net/route").splitlines()[1:]
        defaults = []
        for line in routes:
            fields = line.split()
            if len(fields) > 7 and fields[1] == "00000000":
                try:
                    if int(fields[3], 16) & 1:
                        defaults.append((int(fields[6]), fields[0]))
                except ValueError:
                    pass
        up = [name for name in available if name in stats and stats[name].isup]
        physical = [name for name in up if (self.sysroot / "class/net" / name / "device").exists()]
        default = next((name for _, name in sorted(defaults) if name in up), None)
        automatic = [default] if default else physical[:1]
        chosen = sorted(ident for name, ident in available.items()
                        if (ident in nic_ids if nic_ids is not None else name in automatic))
        selection = (nic_ids is None, tuple(sorted(nic_ids or [])), tuple(chosen))
        data["net_selection"] = None if nic_ids is None else "|".join(sorted(nic_ids))
        data["network_basis"] = {"mode": "manual" if nic_ids is not None else "default-route-or-first-physical",
                                  "interfaces": [n for n, i in available.items() if i in chosen]}
        now = self.clock()
        current, rates = {}, []
        for name, ident in available.items():
            c = counters[name]
            active = name in up
            current[ident] = (now, c.bytes_sent, c.bytes_recv, active)
            old = self.previous.get(ident)
            if ident in chosen and active and old and old[3] and selection == self.selection:
                dt, sent, recv = now - old[0], c.bytes_sent - old[1], c.bytes_recv - old[2]
                if dt > 0 and sent >= 0 and recv >= 0:
                    rates.append((sent / dt, recv / dt))
        if chosen and len(rates) == len(chosen):
            data["net_up"], data["net_down"] = map(sum, zip(*rates))
        self.previous, self.selection = current, selection

    def _sensors(self, data):
        cpu_temps = []
        for hw in sorted((self.sysroot / "class/hwmon").glob("hwmon*")):
            chip = read_text(hw / "name") or "unknown"
            # Remove volatile hwmon enumeration, retaining the real owning device.
            owner = hw.resolve().as_posix()
            if "/devices/" in owner:
                # Class-device parents (nvme0, thermal_zone0) have no "hwmon" glue directory.
                owner = re.sub(r"(/hwmon)?/hwmon\d+$", "", owner)
            for typ, pattern, scale in (("temp", "temp*_input", 1000), ("fan", "fan*_input", 1)):
                for channel in sorted(hw.glob(pattern)):
                    value = number(read_text(channel))
                    if value is None:
                        continue
                    value /= scale
                    stem = channel.name.removesuffix("_input")
                    label = read_text(hw / (stem + "_label"))
                    name = chip + " · " + (label or stem)
                    identity = "linux:hwmon:" + owner + ":" + chip + ":" + stem
                    if typ == "temp" and chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
                        cpu_temps.append((label, value))
                    elif typ == "temp":
                        category = "storage" if chip in ("nvme", "drivetemp") else "other"
                        data["extra_temps"].append((category, name, value))
                        data["sensor_ids"]["extra_temps"].append(identity)
                    elif "cpu" in label.lower():
                        data["cpu_fan"] = max(value, data["cpu_fan"] or 0)
                    else:
                        group = "pumps" if "pump" in label.lower() else "case_fans"
                        data[group].append((name, value))
                        data["sensor_ids"][group].append(identity)
        data["cpu_temp"] = reading._pick_temp(cpu_temps, ["package", "tdie", "tctl", "physical"])

    def sample(self, gpu_id=None, nic_ids=None):
        data = reading.empty_reading()
        missing = {}
        cpu = self.api.cpu_percent(interval=None)
        data["cpu_load"] = cpu if self.cpu_warm else None
        data["warming_up"] = not self.cpu_warm
        self.cpu_warm = True
        mem = self.api.virtual_memory()
        data.update(mem_total=mem.total, mem_used=mem.total - mem.available,
                    mem_load=(mem.total - mem.available) / mem.total * 100 if mem.total else None)
        for key, operation in (("network", lambda: self._network(data, nic_ids)), ("hwmon", lambda: self._sensors(data))):
            try:
                operation()
            except (OSError, AttributeError, ValueError):
                missing[key] = "不可读取或权限不足"
        if self.enable_nvidia:
            cards = nvidia.cards()
            data["gpus"] = [("nvidia:" + c[6], c[0]) for c in cards]
            card = nvidia.card_for(None, gpu_id)
            if card:
                name, temp, fan, load, used, total, uuid = card
                data.update(gpu_id="nvidia:" + uuid, gpu_name=name, gpu_temp=temp, gpu_fan_pct=fan,
                            gpu_load=load, gpu_mem_used=used, gpu_mem_total=total,
                            gpu_mem_load=used / total * 100 if used is not None and total else None)
        for key in ("cpu_temp", "cpu_fan", "gpu_temp"):
            if data[key] is None:
                missing[key] = "未检测到可读传感器或未启用"
        data["capabilities"] = {"missing": missing, "nvidia": self.enable_nvidia}
        data["sample_status"] = "warming_up" if data["warming_up"] else "partial" if missing else "ok"
        return Snapshot(data, compatibility_tree(data))

    def request_reopen(self):
        self.previous.clear()
        self.cpu_warm = False

    def close(self):
        pass
