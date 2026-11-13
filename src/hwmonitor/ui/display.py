"""界面显示用的名称和格式: 指标/分组默认名称、数值格式化、按温度着色和收起分组的摘要。"""
import re

from hwmonitor.ui.theme import FG, MUTED, RED

from hwmonitor.core.metadata import METRIC_NAMES, GROUP_NAMES

DEVICE_TITLES = {"cpu_temp": "CPU", "gpu_temp": "显卡"}  # 温度隐藏后卡片标题退回设备名


def fmt_duration(seconds):
    if seconds is None:
        return "--"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天" if days else "") + (f"{hours}小时" if hours or days else "") + f"{minutes}分{seconds}秒"


def short_gpu(name):
    """显卡名去掉厂商前缀等, 放进小标签: "NVIDIA GeForce RTX 4060" -> "RTX 4060", "Intel(R) UHD Graphics 770" -> "UHD 770"。"""
    s = re.sub(r"\((?:R|TM)\)|\b(?:NVIDIA|AMD|Intel|GeForce|Graphics)\b|\bRadeon\s+(?=RX\b)", " ", name, flags=re.I)
    return " ".join(s.split()) or name


def fmt_rate(b):
    """字节/秒 -> (数字, 单位, 单位颜色), 如 ("12.5", "MB/s", FG)。数到 1000 进一级, 数字最多三位。
    单位按量级变色, 扫一眼就知道大概多快: B/KB 灰 (基本空闲), MB 白, 50 MB/s 以上青, GB 橙。"""
    if b is None:
        return "--", "", MUTED
    for unit in ("B/s", "KB/s", "MB/s"):
        if b < 1000:
            num = f"{b:.1f}" if b < 10 and unit != "B/s" else f"{b:.0f}"
            color = FG if unit == "MB/s" else MUTED
            if unit == "MB/s" and b >= 50:
                color = "#22d3ee"
            return num, unit, color
        b /= 1024
    return f"{b:.1f}", "GB/s", "#fb923c"


def fmt_gb(used, total):
    """已用/总量 (字节) -> "2.4/8.0 GB"。"""
    if used is None or not total:
        return ""
    return f"{used / 1024 ** 3:.1f}/{total / 1024 ** 3:.1f} GB"


def temp_color(t):
    if t is None:
        return MUTED
    return "#4ade80" if t < 60 else "#fbbf24" if t < 80 else RED


def temp_summary(temps):
    """收起的温度分组标题行右侧: 只显示最高温度, 多个传感器时加 MAX 标记。
    返回 (标记, 数字, 单位, 颜色), 没有数据时数字为空。"""
    if not temps:
        return "", "", "", MUTED
    t = max(temps)
    return ("MAX" if len(temps) > 1 else ""), f"{t:.0f}", "°C", temp_color(t)


def rpm_summary(rpms):
    """收起的风扇/水泵分组标题行右侧: 一个显示转速, 多个显示范围。返回值同 temp_summary。"""
    if not rpms:
        return "", "", "", MUTED
    low, high = min(rpms), max(rpms)
    num = f"{high:.0f}" if round(low) == round(high) else f"{low:.0f}–{high:.0f}"
    return "", num, "RPM", FG if high else MUTED
