"""Portable paths, validated settings and atomic writes (standard library only)."""
import copy
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile

VERSION = "0.2.0"
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HARDWARE_MONITOR_DATA_DIR", BASE / "data"))
RUNTIME = BASE / "runtime"
RESOURCE = Path(__file__).resolve().parent
DEFAULT_ALERTS = {"enabled": True, "cpu_temp": 85, "gpu_temp": 85, "cooldown": 300,
                  "duration": 10, "hysteresis": 5, "fan_enabled": False,
                  "fan_min": 300, "fan_duration": 30}
DEFAULTS = {"close_action": "ask", "collapsed": [], "floating": {"enabled": False, "x": 40, "y": 40},
            "alerts": DEFAULT_ALERTS, "interval": 1.0, "gpu_id": None, "nic_ids": None,
            "display": {}, "history_enabled": True,
            "router": {"enabled": False, "url": "http://192.168.1.1:9100/metrics", "nic": None}}


def migrate_legacy_data(base=None, data=None):
    """After acquiring the instance lock, move old portable data without overwriting files."""
    if base is None and "HARDWARE_MONITOR_DATA_DIR" in os.environ:
        return
    base, data = Path(base or BASE), Path(data or DATA)
    if base.resolve() == data.resolve():
        return
    migrate_db = not (data / "history.sqlite3").exists()
    names = ("settings.json", "history.sqlite3", "history.sqlite3-wal", "history.sqlite3-shm",
             "monitor.log", "monitor.log.1", "monitor.log.2")
    for name in names:
        source, target = base / name, data / name
        if not source.exists() or target.exists() or (name.startswith("history.") and not migrate_db):
            continue
        data.mkdir(parents=True, exist_ok=True)
        source.replace(target)


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
    f = raw.get("floating")
    if isinstance(f, dict):
        out["floating"] = {"enabled": f.get("enabled") is True,
                           "x": int(number(f.get("x"), 40, -100000, 100000)),
                           "y": int(number(f.get("y"), 40, -100000, 100000))}
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
        return validate(json.loads(Path(path or DATA / "settings.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return validate({})
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Cannot load settings; using defaults")
        return validate({})


def save(settings, path=None):
    path = Path(path or DATA / "settings.json")
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
