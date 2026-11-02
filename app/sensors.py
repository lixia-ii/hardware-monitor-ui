"""读取硬件传感器: 优先 LibreHardwareMonitor 的 data.json, GPU 用 nvidia-smi 兜底。"""
import json
import re
import shutil
import subprocess
import urllib.request

LHM_URL = "http://127.0.0.1:8085/data.json"
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse(value):
    m = _NUM.search(value or "")
    return float(m.group().replace(",", ".")) if m else None


def _kind_of(node):
    """根据硬件节点的图标/名称判断类别: cpu / gpu / board / other"""
    s = (node.get("ImageURL", "") + " " + node.get("Text", "")).lower()
    if "cpu" in s or "ryzen" in s or "intel core" in s:
        return "cpu"
    if any(k in s for k in ("nvidia", "geforce", "radeon", "ati.png", "gpu", "intel arc")):
        return "gpu"
    if any(k in s for k in ("mainboard", "chip", "superio", "motherboard")):
        return "board"
    return None


def _walk(node, kind, out):
    kind = _kind_of(node) or kind
    value = node.get("Value", "")
    img = node.get("ImageURL", "").lower()
    if value and not node.get("Children"):
        v = _parse(value)
        if v is not None:
            if "rpm" in value.lower() or "fan" in img:
                out["fans"].append((kind, node.get("Text", ""), v))
            elif "°" in value or "temperature" in img or value.rstrip().endswith("C"):
                out["temps"].append((kind, node.get("Text", ""), v))
    for c in node.get("Children", []):
        _walk(c, kind, out)


def _pick_temp(temps, kind, prefer):
    items = [(n, v) for k, n, v in temps if k == kind]
    if not items:
        return None
    for p in prefer:
        for n, v in items:
            if p in n.lower():
                return v
    return max(v for _, v in items)


def _lhm():
    with urllib.request.urlopen(LHM_URL, timeout=1.5) as r:
        tree = json.load(r)
    out = {"temps": [], "fans": []}
    _walk(tree, None, out)
    return out


def _nvidia():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        res = subprocess.run(
            [exe, "--query-gpu=temperature.gpu,fan.speed", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t, f = [x.strip() for x in res.stdout.strip().splitlines()[0].split(",")]
        return float(t), (float(f) if f.replace(".", "").isdigit() else None)
    except Exception:
        return None


def read():
    """返回 dict: cpu_temp, gpu_temp (℃), cpu_fan, gpu_fan (RPM), gpu_fan_pct, case_fans [(name, rpm)], error"""
    r = {"cpu_temp": None, "gpu_temp": None, "cpu_fan": None, "gpu_fan": None,
         "gpu_fan_pct": None, "case_fans": [], "error": None}
    try:
        d = _lhm()
        r["cpu_temp"] = _pick_temp(d["temps"], "cpu", ["package", "tctl", "tdie", "average"])
        r["gpu_temp"] = _pick_temp(d["temps"], "gpu", ["core"])
        gpu_fans = [v for k, _, v in d["fans"] if k == "gpu"]
        r["gpu_fan"] = max(gpu_fans) if gpu_fans else None
        for k, name, v in d["fans"]:
            if k == "cpu" or (k == "board" and "cpu" in name.lower()):
                r["cpu_fan"] = max(v, r["cpu_fan"] or 0)
            elif k == "board" and v > 0:  # 转速为 0 视为未接风扇
                r["case_fans"].append((name, v))
    except Exception as e:
        r["error"] = "未连接 LibreHardwareMonitor (需开启 Remote Web Server, 端口 8085)"

    if r["gpu_temp"] is None or r["gpu_fan"] is None:
        nv = _nvidia()
        if nv:
            if r["gpu_temp"] is None:
                r["gpu_temp"] = nv[0]
            r["gpu_fan_pct"] = nv[1]  # nvidia-smi 只给百分比
    return r


if __name__ == "__main__":
    print(json.dumps(read(), ensure_ascii=False, indent=2))
