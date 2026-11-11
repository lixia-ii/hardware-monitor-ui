"""用户设置: 默认值、字段校验与原子写入 (保存在数据目录的 settings.json)。"""
import copy
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, urlunsplit

from hwmonitor import paths

DEFAULT_ALERTS = {"enabled": True, "cpu_temp": 85, "gpu_temp": 85, "cooldown": 300,
                  "duration": 10, "hysteresis": 5, "fan_enabled": False,
                  "fan_min": 300, "fan_duration": 30}
DEFAULT_FLOATING = {"enabled": False, "x": 40, "y": 40, "topmost": True, "autohide": True,
                    "opacity": 1.0, "show_mem": True, "show_net": True}
DEFAULT_ROUTER = {"enabled": False, "url": "http://192.168.1.1:9100/metrics", "nic": None}
DEFAULT_REMOTE = {"enabled": False, "url": "http://192.168.1.2:8085/data.json", "nic": None, "type": "hwmonitor"}
DEFAULT_TPLINK = {"enabled": False, "url": "http://192.168.0.1", "nic": None,
                  "type": "tplink", "password_protected": "", "notify_clients": True}
DEFAULTS = {"close_action": "ask", "topmost": False, "collapsed": [], "floating": DEFAULT_FLOATING,
            "alerts": DEFAULT_ALERTS, "interval": 1.0, "gpu_id": None, "nic_ids": None,
            "display": {}, "history_enabled": True,
            "router": DEFAULT_ROUTER,
            "routers": [{**DEFAULT_ROUTER, "id": "primary", "name": "路由器 1"}],
            "web": {"enabled": False, "lan": False, "port": 8085},
            "window": None}


def number(value, default, low, high):
    try:
        n = float(value)
        return min(high, max(low, n)) if math.isfinite(n) and not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def device_url(value, kind="openwrt"):
    if not isinstance(value, str) or len(value.strip()) > 200:
        return None
    try:
        parts = urlsplit(value.strip())
        parts.port  # 校验端口范围。
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.netloc.endswith(":"):
            return None
        if kind == "tplink":
            if parts.username is not None or parts.password is not None:
                return None
            parts = parts._replace(path="", query="", fragment="")
        if kind == "hwmonitor" and parts.path in ("", "/"):
            parts = parts._replace(path="/data.json")
        return urlunsplit(parts)
    except ValueError:
        return None


def legacy_router(routers):
    """旧版只认识单台 OpenWrt: 取第一台 OpenWrt 设备, 远程电脑的地址不能写进旧字段。"""
    first = next((cfg for cfg in routers if cfg.get("type", "openwrt") == "openwrt"), None)
    return {k: first[k] for k in DEFAULT_ROUTER} if first else copy.deepcopy(DEFAULT_ROUTER)


def new_device_name(routers):
    """返回未被占用的最小编号名称 "设备 N", 删除后再添加不会重名。"""
    names = {cfg.get("name") for cfg in routers}
    n = 1
    while f"设备 {n}" in names:
        n += 1
    return f"设备 {n}"


def validate(raw):
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if raw.get("close_action") in ("ask", "tray", "exit"):
        out["close_action"] = raw["close_action"]
    if isinstance(raw.get("collapsed"), list):
        # 旧版首台设备卡片的收起键是 "router", 现统一为 "router:<设备 ID>"
        out["collapsed"] = list(dict.fromkeys("router:primary" if x == "router" else x
                                              for x in raw["collapsed"] if isinstance(x, str)))
    out["interval"] = number(raw.get("interval"), 1, 0.5, 30)
    gpu = raw.get("gpu_id")
    out["gpu_id"] = gpu if isinstance(gpu, str) and gpu else None
    ids = raw.get("nic_ids")
    out["nic_ids"] = [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else None
    for key in ("history_enabled", "topmost"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    router = raw.get("router")
    if isinstance(router, dict):
        out["router"]["enabled"] = router.get("enabled") is True
        url = router.get("url")
        if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
            out["router"]["url"] = url.strip()[:200]
        nic = router.get("nic")
        out["router"]["nic"] = nic if isinstance(nic, str) and nic else None
    routers = raw.get("routers")
    if isinstance(routers, list):
        out["routers"] = []
        used = set()
        for i, item in enumerate(routers):
            if not isinstance(item, dict):
                continue
            ident = item.get("id")
            if not isinstance(ident, str) or not ident or ident in used:
                ident = f"router-{i + 1}"
                while ident in used:
                    ident += "-copy"
            used.add(ident)
            kind = item.get("type", "openwrt")
            if kind not in ("openwrt", "hwmonitor", "tplink"):
                continue
            cfg = copy.deepcopy(DEFAULT_TPLINK if kind == "tplink" else
                                DEFAULT_REMOTE if kind == "hwmonitor" else DEFAULT_ROUTER)
            if kind == "tplink":
                cfg["notify_clients"] = item.get("notify_clients", True) is True
                password = item.get("password_protected")
                cfg["password_protected"] = password if isinstance(password, str) and len(password) <= 16000 else ""
            cfg["enabled"] = item.get("enabled") is True
            url = item.get("url")
            valid_url = device_url(url, kind)
            if valid_url:
                cfg["url"] = valid_url
            nic = item.get("nic")
            cfg["nic"] = nic if isinstance(nic, str) and nic else None
            name = item.get("name")
            cfg.update(id=ident, name=name.strip()[:60] if isinstance(name, str) and name.strip() else f"设备 {i + 1}")
            out["routers"].append(cfg)
    else:
        out["routers"] = [{**out["router"], "id": "primary", "name": "路由器 1"}]
    # 保留旧字段供旧版本读取；列表为空时不能重新启用已删除的路由器。
    out["router"] = legacy_router(out["routers"])
    web = raw.get("web")
    if isinstance(web, dict):
        out["web"] = {"enabled": web.get("enabled") is True, "lan": web.get("lan") is True,
                      "port": int(number(web.get("port"), 8085, 1024, 65535))}
    f = raw.get("floating")
    if isinstance(f, dict):
        out["floating"] = {"enabled": f.get("enabled") is True,
                           "x": int(number(f.get("x"), 40, -100000, 100000)),
                           "y": int(number(f.get("y"), 40, -100000, 100000)),
                           "opacity": round(number(f.get("opacity"), 1.0, 0.3, 1.0), 2)}
        for key in ("topmost", "autohide", "show_mem", "show_net"):
            out["floating"][key] = f[key] if isinstance(f.get(key), bool) else DEFAULT_FLOATING[key]
    w = raw.get("window")
    if isinstance(w, dict) and all(isinstance(w.get(k), (int, float)) and not isinstance(w.get(k), bool)
                                   for k in ("width", "height", "x", "y")):
        out["window"] = {"width": int(number(w["width"], 0, 100, 20000)),
                         "height": int(number(w["height"], 0, 100, 20000)),
                         "x": int(number(w["x"], 0, -100000, 100000)),
                         "y": int(number(w["y"], 0, -100000, 100000))}
    alerts = raw.get("alerts")
    if isinstance(alerts, dict):
        for k, default in DEFAULT_ALERTS.items():
            if isinstance(default, bool):
                out["alerts"][k] = alerts.get(k, default) if isinstance(alerts.get(k, default), bool) else default
            else:
                bounds = {"cpu_temp": (40, 110), "gpu_temp": (40, 110), "cooldown": (30, 7200),
                          "duration": (0, 300), "hysteresis": (1, 20), "fan_min": (1, 5000), "fan_duration": (5, 600)}
                out["alerts"][k] = int(number(alerts.get(k), default, *bounds[k]))
    if isinstance(raw.get("display"), dict):
        for key, value in raw["display"].items():
            if isinstance(key, str) and isinstance(value, dict):
                out["display"][key] = {"visible": value.get("visible", True) is not False,
                                       "name": str(value.get("name", ""))[:60],
                                       "order": int(number(value.get("order"), 100, 0, 10000))}
    legacy = out["display"].pop("router", None)
    if legacy:
        for cfg in out["routers"]:
            pref = copy.deepcopy(legacy)
            if len(out["routers"]) > 1:
                pref["name"] = ""
            out["display"].setdefault("router:" + cfg["id"], pref)
    return out


def load(path=None):
    try:
        return validate(json.loads(Path(path or paths.DATA / "settings.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return validate({})
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Cannot load settings; using defaults")
        return validate({})


def save(settings, path=None):
    path = Path(path or paths.DATA / "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temp = f.name
            json.dump(validate(settings), f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
