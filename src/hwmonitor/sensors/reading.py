"""读取硬件传感器: 解析内置 LibreHardwareMonitorLib 输出的 data.json 结构树, GPU 用 nvidia-smi 兜底。"""
import json
import logging
import re

from hwmonitor.sensors import lhm, nvidia

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse(value):
    m = _NUM.search(value or "")
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    if "°f" in value.lower():  # LibreHardwareMonitor 可设成华氏度, 统一换算成摄氏度
        v = (v - 32) * 5 / 9
    return v


_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}


def _bytes(value):
    """"2388.0 MB" / "15.8 KB/s" 换算成字节 (或 字节/秒); 单位不认识返回 None。"""
    m = re.match(r"\s*(-?\d+(?:[.,]\d+)?)\s*([kmgt]?b)\b", value or "", re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")) * _UNITS[m.group(2).lower()]


# 按 HardwareId 前缀判断硬件类别 (LibreHardwareMonitor 新版 data.json 的硬件节点都带这个字段)。
# 如 /intelcpu/0, /gpu-nvidia/0, /gpu-intel-integrated/..., /lpc/nct6798d/0 (主板 SuperIO), /memory/dimm/0, /nvme/1, /nic/{...}
# /ram 是内存占用 (单独归为 ram), /vram 是虚拟内存, 都没有温度, 不归入 memory。
_HWID_KINDS = (
    ("/ram", "ram"),
    ("/intelcpu", "cpu"), ("/amdcpu", "cpu"),
    ("/gpu", "gpu"),
    ("/motherboard", "board"), ("/lpc", "board"),
    ("/memory/", "memory"),
    ("/hdd", "storage"), ("/nvme", "storage"), ("/ssd", "storage"), ("/storage", "storage"),
    ("/nic", "network"),
)

# 旧版 LibreHardwareMonitor 没有 HardwareId 时, 退回按硬件图标识别硬盘 / 内存 / 网卡
_EXTRA_KINDS = {
    "storage": ("hdd", "ssd", "nvme"),
    "memory": ("ram", "memory"),
    "network": ("nic", "network"),
}
EXTRA_LABELS = {"storage": "硬盘", "memory": "内存", "network": "网卡"}


def _kind_of(node):
    """判断硬件节点类别: cpu / gpu / board / storage / memory / network / other; 不是硬件节点返回 None。"""
    hwid = node.get("HardwareId")
    if hwid is not None:
        hwid = hwid.lower()
        return next((k for prefix, k in _HWID_KINDS if hwid.startswith(prefix)), "other")
    img = node.get("ImageURL", "").lower()
    for kind, keys in _EXTRA_KINDS.items():
        if any(k in img for k in keys):
            return kind
    s = (img + " " + node.get("Text", "")).lower()
    if "cpu" in s or "ryzen" in s or "intel core" in s:
        return "cpu"
    if any(k in s for k in ("nvidia", "geforce", "radeon", "ati.png", "gpu", "intel arc", "graphics")):
        return "gpu"
    if any(k in s for k in ("mainboard", "chip", "superio", "motherboard")):
        return "board"
    return None


# 核显/集显名称特征, 选显卡时排在独显之后。
# Radeon 核显: "Radeon(TM) Graphics" / "Radeon 780M Graphics" / "Radeon 8060S"; 独显带 RX ("Radeon RX 7600M") 不匹配。
# Intel 核显: UHD / Iris / "Intel(R) Graphics" / "Arc(TM) Graphics"; 独显 Arc 带型号 ("Arc(TM) A770 Graphics") 不匹配。
_IGPU = re.compile(r"\buhd\b|\biris\b|\bvega\b|radeon(?:\(tm\))?\s+(?:graphics|\d{3,4}[ms]\b)"
                   r"|intel(?:\(r\))?\s+graphics|arc(?:\(tm\))?\s+graphics")


def _sensor_type(node, value):
    """传感器类型: 新版直接用 Type 字段; 旧版按单位推断。只关心 Temperature / Fan。"""
    t = node.get("Type")
    if t:
        return t
    if "rpm" in value.lower():
        return "Fan"
    if "°" in value or value.rstrip().endswith("C"):
        return "Temperature"
    return None


def _walk(node, kind, out, hw=None):
    """递归遍历。hw 是当前设备编号 (同型号的两块盘也不会混在一起), (名称, HardwareId) 在 out["hw"] 里。"""
    # 只按硬件节点分类, 不看叶子传感器名: 否则主板 SuperIO 上名为 "CPU" 的温度会被当成 CPU 自身的温度。
    # 有 HardwareId 时只认带 HardwareId 的节点, "Temperatures" 这类分组节点不参与猜测。
    is_hw = node.get("Children") and ("HardwareId" in node or not out["by_id"])
    k = _kind_of(node) if is_hw else None
    if k:
        kind = k
        # Keep the owning device for every sensor, including motherboard fans.
        parent_id = out["hw"][hw][1] if hw is not None else "legacy"
        hw = len(out["hw"])
        out["hw"].append((node.get("Text", ""), (node.get("HardwareId") or
                         parent_id + "/" + node.get("Text", "")).lower()))
    value = node.get("Value", "")
    if value and not node.get("Children"):
        v = _parse(value)
        if v is not None:
            text = node.get("Text", "")
            typ = _sensor_type(node, value)
            if typ == "Fan":  # 显卡的 "Controls / GPU Fan" 是百分比 (Type=Control), 不算转速
                out["fans"].append((kind, text, v, hw))
                owner = out["hw"][hw][1] if hw is not None else "legacy"
                out.setdefault("fan_ids", []).append(node.get("SensorId") or owner + "/fan/" + text)
            elif typ == "Load":
                out["loads"].append((kind, text, v, hw))
            elif typ in ("Data", "SmallData", "Throughput"):  # 内存/显存用量, 网速
                b = _bytes(value)
                if b is not None:
                    out["data"].append((kind, text, b, hw))
            elif typ == "Temperature":
                out["temps"].append((kind, text, v, hw))
                if kind in _EXTRA_KINDS and hw is not None:
                    out["extra"].setdefault((kind, hw), []).append((text, v))
    for c in node.get("Children", []):
        _walk(c, kind, out, hw)


# 硬盘/内存的告警、临界阈值也以"温度"传感器形式出现, 不是当前温度, 必须排除;
# 内存条还有 "Temperature Sensor Resolution" (0.3℃, 传感器精度)
_THRESHOLD = ("warning", "critical", "limit", "threshold", "max", "min", "high", "low", "resolution")


def _pick_extra(sensors):
    """从一个设备的温度传感器里选当前温度: 去掉阈值项, 优先 Temperature / Composite。"""
    cur = [(n, v) for n, v in sensors if not any(k in n.lower() for k in _THRESHOLD)]
    if not cur:
        return None
    for n, v in cur:
        if n.lower() in ("temperature", "composite temperature"):
            return v
    return max(v for _, v in cur)


# "Distance to TjMax" 是距离降频温度还差多少度, 不是温度本身
_NOT_TEMP = ("distance",)

# CPU 温度优先级。老款 Ryzen (1800X/2700X 等) 的 Tctl 带 10~20℃ 偏移, 同时有 Tdie 时取 Tdie;
# "CCD1 (Tdie)" 只是单个 CCD, 不能被 "tdie" 误匹配, 所以用带 "core" 的完整写法。
_CPU_PREFER = ["package", "core (tdie)", "core (tctl/tdie)", "core (tctl)", "ccds average", "average"]


def _pick_temp(items, prefer):
    """items: [(名称, 数值)]; 按 prefer 关键字优先, 否则取最大值。"""
    items = [(n, v) for n, v in items if not any(k in n.lower() for k in _NOT_TEMP)]
    if not items:
        return None
    for p in prefer:
        for n, v in items:
            if p in n.lower():
                return v
    return max(v for _, v in items)


def _gpu_mem(out, gpu):
    """显卡显存 (已用, 总量) 字节。独显用 GPU Memory, 没有时退回 D3D 专用/共享显存 (核显只有共享显存)。"""
    items = {n.lower(): v for k, n, v, h in out["data"] if k == "gpu" and h == gpu}
    for p in ("gpu memory", "d3d dedicated memory", "d3d shared memory"):
        used, total = items.get(p + " used"), items.get(p + " total")
        if used is not None and total:
            return used, total
    return None


# 虚拟网卡 (Hyper-V / WSL / 虚拟机 / VPN 等) 的流量会和物理网卡重复, 不计入网速
_VIRTUAL_NIC = re.compile(r"virtual|vethernet|hyper-v|vmware|virtualbox|loopback|wsl|tap-|tunnel|bluetooth", re.I)


def _net_speed(out, nic_ids=None):
    """所有物理网卡的 (上传, 下载) 字节/秒之和; 没有网卡网速传感器返回 (None, None)。"""
    up = down = None
    for k, n, v, h in out["data"]:
        if k != "network" or h is None:
            continue
        name, hwid = out["hw"][h]
        if (nic_ids is None and _VIRTUAL_NIC.search(name)) or (nic_ids is not None and hwid not in nic_ids):
            continue
        n = n.lower()
        if "upload speed" in n:
            up = (up or 0) + v
        elif "download speed" in n:
            down = (down or 0) + v
    return up, down


def _gpu_ids(out):
    return sorted({h for k, _, _, h in out["temps"] + out["fans"] + out["loads"] if k == "gpu" and h is not None})


def _main_gpu(out, want=None):
    """返回要显示的显卡设备编号, 没有则 None。want 是用户选的 HardwareId (在列表里就用它);
    否则取当前利用率最高的一块, 利用率相同 (如都空闲) 时优先独显。"""
    ids = _gpu_ids(out)
    if not ids:
        return None
    for h in ids:
        if want and out["hw"][h][1] == want:
            return h
    if want:
        return None  # A disconnected pinned device must not silently become another GPU.

    def integrated(h):
        name, hwid = out["hw"][h]
        return "integrated" in hwid or bool(_IGPU.search(name.lower()))  # 如 /gpu-intel-integrated/...

    def load(h):
        v = _pick_temp([(n, v) for k, n, v, hh in out["loads"] if k == "gpu" and hh == h], ["core"])
        return -1 if v is None else round(v)
    return min(ids, key=lambda h: (-load(h), integrated(h), h))


def _has_hwid(node):
    return "HardwareId" in node or any(_has_hwid(c) for c in node.get("Children", []))


def parse_tree(tree):
    out = {"temps": [], "fans": [], "loads": [], "data": [], "extra": {}, "hw": [], "by_id": _has_hwid(tree)}
    _walk(tree, None, out)
    return out


def _lhm(source):
    """刷新传感器并解析; 不可用 (初始化中/失败) 时抛 lhm.Unavailable。"""
    if source is None:
        raise lhm.Unavailable("硬件传感器未启动")
    return parse_tree(source.sample())


def empty_reading():
    return {"cpu_temp": None, "gpu_temp": None, "cpu_fan": None, "gpu_fan": None, "cpu_load": None, "gpu_load": None,
         "gpu_fan_pct": None, "mem_load": None, "mem_used": None, "mem_total": None,
         "gpu_mem_load": None, "gpu_mem_used": None, "gpu_mem_total": None, "net_up": None, "net_down": None, "gpus": [], "gpu_id": None, "sensors_down": False,
         "case_fans": [], "pumps": [], "extra_temps": [], "nics": [], "error": None,
         "gpu_name": None, "net_selection": None,
         "sensor_ids": {"case_fans": [], "pumps": [], "extra_temps": []}}


def read(gpu_id=None, nic_ids=None, source=None, allow_nvidia=True):
    """Read normalized metrics, device identities and sensor status from source (lhm.Sensors); missing values are None."""
    r = empty_reading()
    r["net_selection"] = None if nic_ids is None else "|".join(sorted(nic_ids))
    gpu_name = None
    try:
        d = _lhm(source)
        r["cpu_temp"] = _pick_temp([(n, v) for k, n, v, _ in d["temps"] if k == "cpu"],
                                   _CPU_PREFER)
        gpu = _main_gpu(d, gpu_id)
        uuid_card = nvidia.card_for(None, gpu_id) if allow_nvidia and gpu_id and gpu_id.startswith("nvidia:") else None
        if uuid_card:
            # Names are only a bridge when unique in BOTH providers. Identical cards
            # remain on the UUID provider until a hardware identity can be proven.
            matches = [h for h in _gpu_ids(d) if d["hw"][h][0].casefold() == uuid_card[0].casefold()]
            if len(matches) == 1 and sum(c[0].casefold() == uuid_card[0].casefold() for c in nvidia.cards()) == 1:
                gpu = matches[0]
        r["gpus"] = [(d["hw"][h][1], d["hw"][h][0]) for h in _gpu_ids(d)]  # [(HardwareId, 名称)]
        r["gpu_id"] = d["hw"][gpu][1] if gpu is not None else None
        gpu_name = d["hw"][gpu][0] if gpu is not None else None
        r["gpu_name"] = gpu_name
        if uuid_card:
            r["gpus"] = [("nvidia:" + c[6], c[0]) for c in nvidia.cards()] + [
                (h, n) for h, n in r["gpus"] if not h.startswith("/gpu-nvidia")]
            r["gpu_id"], r["gpu_name"] = gpu_id, uuid_card[0]
        r["nics"] = [(hwid, name) for name, hwid in d["hw"] if any(
            k == "network" and d["hw"][h][1] == hwid for k, _, _, h in d["data"] if h is not None)]
        r["gpu_temp"] = _pick_temp([(n, v) for k, n, v, h in d["temps"] if k == "gpu" and h == gpu], ["core"])
        r["cpu_load"] = _pick_temp([(n, v) for k, n, v, _ in d["loads"] if k == "cpu"], ["total"])
        r["gpu_load"] = _pick_temp([(n, v) for k, n, v, h in d["loads"] if k == "gpu" and h == gpu], ["core"])
        # 内存: 旧版没有 HardwareId 时内存节点被归为 memory, 用名称 "Memory" 区分出总占用
        r["mem_load"] = next((v for k, n, v, _ in d["loads"] if k in ("ram", "memory") and n.lower() == "memory"), None)
        mem = {n.lower(): v for k, n, v, _ in d["data"] if k in ("ram", "memory")}
        used, avail = mem.get("memory used"), mem.get("memory available")
        if used is not None and avail is not None:
            r["mem_used"], r["mem_total"] = used, used + avail
            if r["mem_load"] is None and used + avail:
                r["mem_load"] = used / (used + avail) * 100
        vram = _gpu_mem(d, gpu)
        if vram:
            r["gpu_mem_used"], r["gpu_mem_total"] = vram
            r["gpu_mem_load"] = vram[0] / vram[1] * 100
        r["net_up"], r["net_down"] = _net_speed(d, nic_ids)
        gpu_fans = [v for k, _, v, h in d["fans"] if k == "gpu" and h == gpu]
        r["gpu_fan"] = max(gpu_fans) if gpu_fans else None
        order = list(_EXTRA_KINDS)
        extra = [(k, d["hw"][h][0], _pick_extra(ss), d["hw"][h][1]) for (k, h), ss in d["extra"].items()]
        extra = sorted((e for e in extra if e[2] is not None), key=lambda x: order.index(x[0]))
        r["extra_temps"] = [(k, n, v) for k, n, v, _ in extra]
        r["sensor_ids"]["extra_temps"] = [identity for _, _, _, identity in extra]
        for index, (k, name, v, h) in enumerate(d["fans"]):
            identities = d.get("fan_ids", [])
            identity = identities[index] if index < len(identities) else (d["hw"][h][1] if h is not None else "legacy") + "/fan/" + name
            if k in ("gpu", "storage", "memory", "network"):
                continue
            n = name.lower()
            if "pump" in n:  # 水冷泵 (AIO Pump / W_PUMP+ 等) 单独显示, 不算风扇
                if v > 0:
                    r["pumps"].append((name, v))
                    r["sensor_ids"]["pumps"].append(identity)
            elif k == "cpu" or "cpu" in n:
                r["cpu_fan"] = max(v, r["cpu_fan"] or 0)
            elif v > 0:  # 转速为 0 视为未接风扇; 未识别硬件(独立风扇控制器等)也并入机箱风扇
                r["case_fans"].append((name, v))
                r["sensor_ids"]["case_fans"].append(identity)
    except lhm.Unavailable as e:
        logging.getLogger(__name__).debug("Sensors unavailable: %s", e)
        r["sensors_down"] = True
        r["error"] = str(e)
    except Exception as e:  # 代码自身的问题, 不要伪装成"不可用"
        logging.getLogger(__name__).exception("Sensor parsing failed")
        r["error"] = f"读取传感器出错: {e!r}"

    if allow_nvidia and None in (r["gpu_temp"], r["gpu_fan"], r["gpu_load"], r["gpu_mem_load"]):
        nv = nvidia.card_for(gpu_name, r["gpu_id"] or gpu_id)
        if nv:
            if r["gpu_id"] is None:
                r["gpu_id"], r["gpu_name"] = "nvidia:" + nv[6], nv[0]
                r["gpus"] = [("nvidia:" + c[6], c[0]) for c in nvidia.cards()]
            if r["gpu_temp"] is None:
                r["gpu_temp"] = nv[1]
            if r["gpu_load"] is None:
                r["gpu_load"] = nv[3]
            if r["gpu_mem_load"] is None and nv[4] is not None and nv[5]:
                r["gpu_mem_used"], r["gpu_mem_total"] = nv[4], nv[5]
                r["gpu_mem_load"] = nv[4] / nv[5] * 100
            r["gpu_fan_pct"] = nv[2]  # nvidia-smi 只给百分比
    return r


if __name__ == "__main__":  # 调试: 需要管理员权限
    source = lhm.Sensors()
    try:
        print(json.dumps(read(source=source), ensure_ascii=False, indent=2))
    finally:
        source.close()
