"""读取硬件/系统概览信息 (型号、系统、处理器、主板、内存、显卡、显示器、磁盘、声卡、网卡)。"""
import sys
import json
import math
import shutil
import subprocess

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$cs  = Get-CimInstance Win32_ComputerSystem
$os  = Get-CimInstance Win32_OperatingSystem
$enc = Get-CimInstance Win32_SystemEnclosure
$cur = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$bb  = Get-CimInstance Win32_BaseBoard
$gpuKeys = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11cd-be6b-00805f4f0b6d}\0*'
$ids = @(Get-CimInstance -Namespace root\wmi WmiMonitorID)
$sz  = @(Get-CimInstance -Namespace root\wmi WmiMonitorBasicDisplayParams)
$mons = @()
for ($i = 0; $i -lt $ids.Count; $i++) {
  $name = -join ($ids[$i].UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ })
  $w = ($sz | Where-Object { $_.InstanceName -eq $ids[$i].InstanceName } | Select-Object -First 1).MaxHorizontalImageSize
  $h = ($sz | Where-Object { $_.InstanceName -eq $ids[$i].InstanceName } | Select-Object -First 1).MaxVerticalImageSize
  $mons += [pscustomobject]@{ name = $name; w = $w; h = $h }
}
[pscustomobject]@{
  vendor   = $cs.Manufacturer
  model    = $cs.Model
  chassis  = @($enc.ChassisTypes)
  os       = $os.Caption
  osver    = $cur.DisplayVersion
  osarch   = $os.OSArchitecture
  cpu      = @(Get-CimInstance Win32_Processor | ForEach-Object { [pscustomobject]@{ name = $_.Name; cores = $_.NumberOfCores } })
  board    = "$($bb.Manufacturer) $($bb.Product)"
  mem      = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [pscustomobject]@{ maker = $_.Manufacturer; cap = $_.Capacity; speed = $_.ConfiguredClockSpeed; type = $_.SMBIOSMemoryType } })
  gpu      = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ name = $_.Name; vram = $_.AdapterRAM } })
  gpuvram  = @($gpuKeys | ForEach-Object { [pscustomobject]@{ name = $_.DriverDesc; vram = $_.'HardwareInformation.qwMemorySize' } })
  mon      = $mons
  disk     = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { [pscustomobject]@{ name = $_.Model; size = $_.Size } })
  sound    = @(Get-CimInstance Win32_SoundDevice | ForEach-Object { $_.Name })
  nic      = @(Get-NetAdapter -Physical | ForEach-Object { $_.InterfaceDescription })
} | ConvertTo-Json -Depth 5 -Compress
"""

_MEM_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2",
              29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
_DESKTOP = {3, 4, 5, 6, 7, 13, 15, 16, 35, 36}
_LAPTOP = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}


def _list(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


def _gb(n):
    return f"{n / 1024 ** 3:.0f}GB"


def _disk_size(n):
    """磁盘按厂商标称的十进制容量显示 (4TB 而不是 3726GB)。"""
    g = n / 1e9
    return f"{round(g / 1000, 2):g}TB" if g >= 1000 else f"{g:.0f}GB"


def _nvidia_vram():
    """AdapterRAM 只有 32 位, 大显存会算错; NVIDIA 卡改用 nvidia-smi 按名称取准确值 {名称: 字节}。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        res = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = {}
        for line in res.stdout.strip().splitlines():
            name, mib = line.rsplit(",", 1)
            out[name.strip()] = int(float(mib)) * 1024 ** 2
        return out
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _clean(s):
    return " ".join((s or "").split())


def _chassis(types):
    ts = set(_list(types))
    if ts & _LAPTOP:
        return "笔记本电脑"
    if ts & _DESKTOP:
        return "台式电脑"
    return ""


def _os_name(d):
    name = _clean(d.get("os")).replace("Microsoft ", "").replace("Windows ", "Win")
    arch = "64位" if "64" in (d.get("osarch") or "") else "32位"
    return " ".join(x for x in (name, d.get("osver"), arch) if x)


def _cn(n):
    """1~99 转中文数字 (16 -> 十六)。"""
    d = "零一二三四五六七八九"
    if n < 10:
        return d[n]
    return ("" if n < 20 else d[n // 10]) + "十" + (d[n % 10] if n % 10 else "")


def _cpu(d):
    c = _list(d.get("cpu"))
    if not c:
        return None
    n = c[0].get("cores")
    return _clean(c[0].get("name")) + (f" {_cn(n)}核" if n else "")


def _mem(d):
    m = _list(d.get("mem"))
    if not m:
        return None
    total = sum(x.get("cap") or 0 for x in m)
    maker = _clean(m[0].get("maker"))
    if maker.startswith(("Unknown", "0000")) or set(maker) <= {"0", " "}:
        maker = ""
    typ = _MEM_TYPES.get(m[0].get("type"), "")
    speed = m[0].get("speed")  # ConfiguredClockSpeed 实际是数据速率 (DDR4-3200 报 3200), 单位应为 MT/s
    parts = " + ".join(_gb(x.get("cap") or 0) for x in m)
    head = " ".join(x for x in (maker, _gb(total), typ, f"{speed}MT/s" if speed else "") if x)
    return f"{head} ( {parts} )"


def _gpus(d):
    vram = {_clean(g.get("name")): g.get("vram") for g in _list(d.get("gpuvram"))}
    out = []
    nv = _nvidia_vram()
    for g in _list(d.get("gpu")):
        name = _clean(g.get("name"))
        size = nv.get(name) or vram.get(name) or g.get("vram")
        out.append(f"{name} ( {_gb(size)} )" if size else name)
    return out


def _monitors(d):
    out = []
    for m in _list(d.get("mon")):
        name = _clean(m.get("name")) or "通用显示器"
        w, h = m.get("w"), m.get("h")
        out.append(f"{name} ( {math.hypot(w, h) / 2.54:.1f}英寸 )" if w and h else name)
    return out


def _disks(d):
    out = []
    for x in _list(d.get("disk")):
        size = x.get("size")
        out.append(f"{_clean(x.get('name'))} ( {_disk_size(size)} )" if size else _clean(x.get("name")))
    return out


def load():
    """返回 dict: model, os, details [(标签, [行])]; 失败时 error 有值。"""
    if sys.platform == "linux":
        from hwmonitor.sensors.linux_info import load as linux_load
        return linux_load()
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        d = json.loads(res.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError, ValueError) as e:  # ValueError 含 JSON 解析失败
        return {"error": f"读取失败: {e}"}

    try:
        return _format(d)
    except Exception as e:  # 单个字段异常 (如 null) 不应让整个窗口卡在"读取中"
        return {"error": f"解析失败: {e!r}"}


def _format(d):
    vendor, mdl = _clean(d.get("vendor")), _clean(d.get("model"))
    if mdl.lower().startswith(vendor.lower()):  # 避免 "HP HP Elite..."
        vendor = ""
    model = " ".join(x for x in (vendor, mdl, _chassis(d.get("chassis"))) if x)
    rows = [
        ("处理器", [_cpu(d)]),
        ("主板", [_clean(d.get("board"))]),
        ("内存", [_mem(d)]),
        ("显卡", _gpus(d)),
        ("显示器", _monitors(d)),
        ("磁盘", _disks(d)),
        ("声卡", [_clean(x) for x in _list(d.get("sound")) if x and "virtual" not in x.lower()]),
        ("网卡", [_clean(x) for x in _list(d.get("nic")) if x]),
    ]
    return {"error": None, "model": model or "--", "os": _os_name(d) or "--",
            "details": [(k, [x for x in v if x]) for k, v in rows]}


if __name__ == "__main__":
    print(json.dumps(load(), ensure_ascii=False, indent=2))
