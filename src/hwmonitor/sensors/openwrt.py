"""读取 OpenWrt 上 prometheus-node-exporter-lua 的 /metrics: 温度、CPU、内存、WAN 网速与运行时长。"""
import http.client
import logging
import re
import time
import urllib.request

_LINE = re.compile(r"^([A-Za-z_:][\w:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_WANTED = {"node_cpu_seconds_total", "node_load1", "node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes",
           "node_network_receive_bytes_total", "node_network_transmit_bytes_total", "node_thermal_zone_temp",
           "node_boot_time_seconds", "node_time_seconds", "node_uname_info"}
# 自动选 WAN 时跳过的回环、网桥、隧道和虚拟网卡; 手动选择不受限制
_VIRTUAL = ("lo", "br-", "utun", "tun", "wg", "docker", "veth", "ifb", "dummy", "sit", "ip6tnl", "gre", "erspan")
_PREFER = ("pppoe-wan", "wan")
_IDLE = ("idle", "iowait")
_GUEST = ("guest", "guest_nice")  # 已计入 user/nice, 不重复累加


def parse(text):
    """Prometheus 文本格式 -> {指标名: [(标签字典, 数值)]}, 只保留用到的指标。"""
    out = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if not m or m.group(1) not in _WANTED:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        out.setdefault(m.group(1), []).append((dict(_LABEL.findall(m.group(2) or "")), value))
    return out


def _one(metrics, name):
    rows = metrics.get(name)
    return rows[0][1] if rows else None


def _temp(metrics):
    zones = [(labels.get("type", "").lower(), v / 1000 if v > 1000 else v)  # 个别版本给的是毫摄氏度
             for labels, v in metrics.get("node_thermal_zone_temp", [])]
    preferred = [v for t, v in zones if "cpu" in t or "soc" in t]
    return max(preferred or [v for _, v in zones], default=None)


def _cpu(metrics):
    idle = total = 0.0
    for labels, v in metrics.get("node_cpu_seconds_total", []):
        mode = labels.get("mode")
        if mode in _GUEST:
            continue
        total += v
        if mode in _IDLE:
            idle += v
    return (idle, total) if total else None


def _counters(metrics):
    rx = {labels.get("device"): v for labels, v in metrics.get("node_network_receive_bytes_total", [])}
    tx = {labels.get("device"): v for labels, v in metrics.get("node_network_transmit_bytes_total", [])}
    return {dev: (rx[dev], tx[dev]) for dev in rx if dev and dev in tx}


def pick_wan(counters):
    """自动选 WAN: 先认 pppoe-wan / wan, 否则取流量最大的物理网卡。"""
    for name in _PREFER:
        if name in counters:
            return name
    physical = [(sum(v), dev) for dev, v in counters.items() if not dev.startswith(_VIRTUAL)]
    best = max(physical, default=None)
    return best[1] if best and best[0] > 0 else None


def empty_reading():
    return {"offline": True, "host": None, "temp": None, "cpu_load": None, "load1": None, "mem_used": None,
            "mem_total": None, "mem_load": None, "up": None, "down_rate": None, "nic": None, "nics": [],
            "uptime": None}


class Router:
    """保存上一次的计数器, 用差值算 CPU 利用率与网速。只在采集线程中使用。"""

    def __init__(self):
        self.prev = None  # (url, monotonic, cpu, counters)
        self.url = None
        self.failures = 0
        self.retry_at = 0.0

    def read(self, url, nic=None, timeout=1.0):
        r = empty_reading()
        now = time.monotonic()
        if url != self.url:  # 改了地址立即重试
            self.url, self.failures, self.retry_at = url, 0, 0.0
        if now < self.retry_at:  # 路由器离线时退避重试, 不让每次采集都卡在超时上
            return r
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                metrics = parse(resp.read(4 * 1024 * 1024).decode("utf-8", "replace"))
        # URLError 是 OSError 子类; 地址格式不对是 ValueError; 响应被截断或格式错误是 HTTPException
        except (OSError, ValueError, http.client.HTTPException) as e:
            logging.getLogger(__name__).debug("Router unavailable: %s", e)
            self.prev = None
            self.failures += 1
            self.retry_at = now + min(30, 2 ** min(self.failures, 5))
            return r
        self.failures, self.retry_at = 0, 0.0
        r["offline"] = False
        r["host"] = next((labels.get("nodename") for labels, _ in metrics.get("node_uname_info", [])), None)
        r["temp"] = _temp(metrics)
        r["load1"] = _one(metrics, "node_load1")
        total, avail = _one(metrics, "node_memory_MemTotal_bytes"), _one(metrics, "node_memory_MemAvailable_bytes")
        if total and avail is not None:
            r["mem_total"], r["mem_used"] = total, total - avail
            r["mem_load"] = (total - avail) / total * 100
        boot, clock = _one(metrics, "node_boot_time_seconds"), _one(metrics, "node_time_seconds")
        if boot and clock and clock >= boot:
            r["uptime"] = clock - boot
        cpu, counters = _cpu(metrics), _counters(metrics)
        r["nics"] = sorted(counters)
        r["nic"] = nic if nic in counters else None if nic else pick_wan(counters)
        prev, self.prev = self.prev, (url, now, cpu, counters)
        if prev and prev[0] == url and now > prev[1]:
            _, then, old_cpu, old_counters = prev
            if cpu and old_cpu and cpu[1] > old_cpu[1]:
                busy = 1 - (cpu[0] - old_cpu[0]) / (cpu[1] - old_cpu[1])
                r["cpu_load"] = max(0.0, min(100.0, busy * 100))
            if r["nic"] in old_counters:
                (rx, tx), (old_rx, old_tx) = counters[r["nic"]], old_counters[r["nic"]]
                if rx >= old_rx and tx >= old_tx:  # 计数器归零 (网卡重建/重启) 时本次不出数
                    r["down_rate"], r["up"] = (rx - old_rx) / (now - then), (tx - old_tx) / (now - then)
        return r
