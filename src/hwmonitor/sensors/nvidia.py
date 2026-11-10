"""nvidia-smi 兜底: 硬件库不可用时提供 NVIDIA 显卡的温度、风扇百分比、利用率和显存。"""
import shutil
import subprocess
import time


_nv_cache = (0.0, [])


def _num(s):
    try:
        return float(s)
    except ValueError:  # "[N/A]" 等
        return None


def cards():
    """nvidia-smi 兜底: 返回每块卡的 [(名称, 温度, 风扇%, 利用率%, 显存已用, 显存总量 (字节))]。结果缓存 5 秒, 避免每秒拉起一个进程。"""
    global _nv_cache
    if time.monotonic() - _nv_cache[0] < 5:
        return _nv_cache[1]
    result = []
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            res = subprocess.run(
                [exe, "--query-gpu=name,temperature.gpu,fan.speed,utilization.gpu,memory.used,memory.total,uuid", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in res.stdout.strip().splitlines():
                name, t, f, u, mu, mt, uuid = [x.strip() for x in line.rsplit(",", 6)]
                mu, mt = _num(mu), _num(mt)  # 单位 MiB
                result.append((name, _num(t), _num(f), _num(u),
                               mu and mu * 1024 ** 2, mt and mt * 1024 ** 2, uuid))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    _nv_cache = (time.monotonic(), result)
    return result


def card_for(gpu_name, gpu_id=None):
    """取与 LibreHardwareMonitor 选中的显卡同名的那块 N 卡; 传感器不可用 (gpu_name 为 None) 时取第一块。"""
    found = cards()
    if gpu_id and gpu_id.startswith("nvidia:"):
        return next((c for c in found if "nvidia:" + c[6] == gpu_id), None)
    if gpu_name is None:
        if gpu_id:
            return None
        return found[0] if found else None
    g = gpu_name.lower()
    matches = [c for c in found if c[0].lower() in g or g in c[0].lower()]
    return matches[0] if len(matches) == 1 else None  # 同型号多卡无法按名称确定身份时不混用读数
