"""用户设置: 默认值、字段校验与原子写入 (保存在数据目录的 settings.json)。"""
import copy
import json
import logging
import math
import os
from pathlib import Path
import tempfile

from hwmonitor import paths

DEFAULT_ALERTS = {"enabled": True, "cpu_temp": 85, "gpu_temp": 85, "cooldown": 300,
                  "duration": 10, "hysteresis": 5, "fan_enabled": False,
                  "fan_min": 300, "fan_duration": 30}
DEFAULTS = {"close_action": "ask", "collapsed": [], "floating": {"enabled": False, "x": 40, "y": 40},
            "alerts": DEFAULT_ALERTS, "interval": 1.0, "gpu_id": None, "nic_ids": None,
            "display": {}, "history_enabled": True,
            "router": {"enabled": False, "url": "http://192.168.1.1:9100/metrics", "nic": None},
            "web": {"enabled": False, "lan": False, "port": 8085},
            "window": None}


def number(value, default, low, high):
    try:
        n = float(value)
        return min(high, max(low, n)) if math.isfinite(n) and not isinstance(value, bool) else default
    except (TypeError, ValueError, OverflowError):
        return default


def validate(raw):
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    if raw.get("close_action") in ("ask", "tray", "exit"):
        out["close_action"] = raw["close_action"]
    if isinstance(raw.get("collapsed"), list):
        out["collapsed"] = [x for x in raw["collapsed"] if isinstance(x, str)]
    out["interval"] = number(raw.get("interval"), 1, 0.5, 30)
    gpu = raw.get("gpu_id")
    out["gpu_id"] = gpu if isinstance(gpu, str) and gpu else None
    ids = raw.get("nic_ids")
    out["nic_ids"] = [x for x in ids if isinstance(x, str)] if isinstance(ids, list) else None
    for key in ("history_enabled",):
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
    web = raw.get("web")
    if isinstance(web, dict):
        out["web"] = {"enabled": web.get("enabled") is True, "lan": web.get("lan") is True,
                      "port": int(number(web.get("port"), 8085, 1024, 65535))}
    f = raw.get("floating")
    if isinstance(f, dict):
        out["floating"] = {"enabled": f.get("enabled") is True,
                           "x": int(number(f.get("x"), 40, -100000, 100000)),
                           "y": int(number(f.get("y"), 40, -100000, 100000))}
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
