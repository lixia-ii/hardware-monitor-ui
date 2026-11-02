"""读取硬件传感器: 优先 LibreHardwareMonitor 的 data.json, GPU 用 nvidia-smi 兜底。"""
import json
import re
import shutil
import subprocess
import time
import urllib.request

LHM_URL = "http://127.0.0.1:8085/data.json"
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse(value):
    m = _NUM.search(value or "")
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    if "°f" in value.lower():  # LibreHardwareMonitor 可设成华氏度, 统一换算成摄氏度
        v = (v - 32) * 5 / 9
    return v


# 按 HardwareId 前缀判断硬件类别 (LibreHardwareMonitor 新版 data.json 的硬件节点都带这个字段)。
# 如 /intelcpu/0, /gpu-nvidia/0, /gpu-intel-integrated/..., /lpc/nct6798d/0 (主板 SuperIO), /memory/dimm/0, /nvme/1, /nic/{...}
# /ram 和 /vram 是内存/虚拟内存占用, 没有温度, 不归入 memory。
_HWID_KINDS = (
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
        if k in _EXTRA_KINDS or k == "gpu":
            hw = len(out["hw"])
            out["hw"].append((node.get("Text", ""), (node.get("HardwareId") or "").lower()))
    value = node.get("Value", "")
    if value and not node.get("Children"):
        v = _parse(value)
        if v is not None:
            text = node.get("Text", "")
            typ = _sensor_type(node, value)
            if typ == "Fan":  # 显卡的 "Controls / GPU Fan" 是百分比 (Type=Control), 不算转速
                out["fans"].append((kind, text, v, hw))
            elif typ == "Load":
                out["loads"].append((kind, text, v, hw))
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

    def integrated(h):
        name, hwid = out["hw"][h]
        return "integrated" in hwid or bool(_IGPU.search(name.lower()))  # 如 /gpu-intel-integrated/...

    def load(h):
        v = _pick_temp([(n, v) for k, n, v, hh in out["loads"] if k == "gpu" and hh == h], ["core"])
        return -1 if v is None else round(v)
    return min(ids, key=lambda h: (-load(h), integrated(h), h))


def _lhm():
    with urllib.request.urlopen(LHM_URL, timeout=1.5) as r:
        raw = r.read()
    tree = json.loads(raw)
    out = {"temps": [], "fans": [], "loads": [], "extra": {}, "hw": [], "by_id": b'"HardwareId"' in raw}
    _walk(tree, None, out)
    return out


_nv_cache = (0.0, [])


def _num(s):
    try:
        return float(s)
    except ValueError:  # "[N/A]" 等
        return None


def _nvidia():
    """nvidia-smi 兜底: 返回每块卡的 [(名称, 温度, 风扇%, 利用率%)]。结果缓存 5 秒, 避免每秒拉起一个进程。"""
    global _nv_cache
    if time.monotonic() - _nv_cache[0] < 5:
        return _nv_cache[1]
    result = []
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            res = subprocess.run(
                [exe, "--query-gpu=name,temperature.gpu,fan.speed,utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in res.stdout.strip().splitlines():
                name, t, f, u = [x.strip() for x in line.rsplit(",", 3)]
                result.append((name, _num(t), _num(f), _num(u)))
        except Exception:
            pass
    _nv_cache = (time.monotonic(), result)
    return result


def _nvidia_for(gpu_name):
    """取与 LibreHardwareMonitor 选中的显卡同名的那块 N 卡; 未连接 LHM (gpu_name 为 None) 时取第一块。"""
    cards = _nvidia()
    if gpu_name is None:
        return cards[0] if cards else None
    g = gpu_name.lower()
    return next((c for c in cards if c[0].lower() in g or g in c[0].lower()), None)


def lhm_running():
    """LibreHardwareMonitor 进程是否在运行 (启动中、网页服务还没起来时也算)。"""
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq LibreHardwareMonitor.exe"],
                             capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "LibreHardwareMonitor.exe" in out
    except Exception:
        return False


def read(gpu_id=None):
    """返回 dict: cpu_temp, gpu_temp (℃), cpu_fan, gpu_fan (RPM), cpu_load, gpu_load (%), gpus [(HardwareId, 名称)], gpu_id (当前显示的), gpu_fan_pct, case_fans [(name, rpm)],
    pumps [(name, rpm)], extra_temps [(类别, 名称, ℃)], error"""
    r = {"cpu_temp": None, "gpu_temp": None, "cpu_fan": None, "gpu_fan": None, "cpu_load": None, "gpu_load": None,
         "gpu_fan_pct": None, "gpus": [], "gpu_id": None, "lhm_down": False, "lhm_running": False,
         "case_fans": [], "pumps": [], "extra_temps": [], "error": None}
    gpu_name = None
    try:
        d = _lhm()
        r["cpu_temp"] = _pick_temp([(n, v) for k, n, v, _ in d["temps"] if k == "cpu"],
                                   _CPU_PREFER)
        gpu = _main_gpu(d, gpu_id)
        r["gpus"] = [(d["hw"][h][1], d["hw"][h][0]) for h in _gpu_ids(d)]  # [(HardwareId, 名称)]
        r["gpu_id"] = d["hw"][gpu][1] if gpu is not None else None
        gpu_name = d["hw"][gpu][0] if gpu is not None else None
        r["gpu_temp"] = _pick_temp([(n, v) for k, n, v, h in d["temps"] if k == "gpu" and h == gpu], ["core"])
        r["cpu_load"] = _pick_temp([(n, v) for k, n, v, _ in d["loads"] if k == "cpu"], ["total"])
        r["gpu_load"] = _pick_temp([(n, v) for k, n, v, h in d["loads"] if k == "gpu" and h == gpu], ["core"])
        gpu_fans = [v for k, _, v, h in d["fans"] if k == "gpu" and h == gpu]
        r["gpu_fan"] = max(gpu_fans) if gpu_fans else None
        order = list(_EXTRA_KINDS)
        extra = [(k, d["hw"][h][0], _pick_extra(ss)) for (k, h), ss in d["extra"].items()]
        r["extra_temps"] = sorted((e for e in extra if e[2] is not None),
                                  key=lambda x: order.index(x[0]))
        for k, name, v, _ in d["fans"]:
            if k in ("gpu", "storage", "memory", "network"):
                continue
            n = name.lower()
            if "pump" in n:  # 水冷泵 (AIO Pump / W_PUMP+ 等) 单独显示, 不算风扇
                if v > 0:
                    r["pumps"].append((name, v))
            elif k == "cpu" or "cpu" in n:
                r["cpu_fan"] = max(v, r["cpu_fan"] or 0)
            elif v > 0:  # 转速为 0 视为未接风扇; 未识别硬件(独立风扇控制器等)也并入机箱风扇
                r["case_fans"].append((name, v))
    except (OSError, ValueError):  # URLError 是 OSError 子类; ValueError 含 JSON 解析失败
        r["lhm_down"] = True
        r["error"] = "未连接 LibreHardwareMonitor (需开启 Remote Web Server, 端口 8085)"
    except Exception as e:  # 代码自身的问题, 不要伪装成"未连接"
        r["error"] = f"读取传感器出错: {e!r}"

    if r["lhm_down"]:
        r["lhm_running"] = lhm_running()
    if r["gpu_temp"] is None or r["gpu_fan"] is None or r["gpu_load"] is None:
        nv = _nvidia_for(gpu_name)
        if nv:
            if r["gpu_temp"] is None:
                r["gpu_temp"] = nv[1]
            if r["gpu_load"] is None:
                r["gpu_load"] = nv[3]
            r["gpu_fan_pct"] = nv[2]  # nvidia-smi 只给百分比
    return r


if __name__ == "__main__":
    print(json.dumps(read(), ensure_ascii=False, indent=2))
