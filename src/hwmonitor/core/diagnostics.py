"""Local rolling logs and explicitly exported reports; no environment/path dump."""
import json
import logging
from logging.handlers import RotatingFileHandler
import platform
import sys
import time

from hwmonitor import __version__, paths


def setup_logging():
    try:
        paths.DATA.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(paths.DATA / "monitor.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.excepthook = lambda *args: logging.getLogger(__name__).critical("Unhandled exception", exc_info=args)


def report(data, hardware, events, stats):
    # Error strings may contain private paths from OS exceptions; export a generic status instead.
    readings = {k: v for k, v in (data or {}).items() if k not in ("error", "nics")}
    readings["error"] = "采集异常；详情见本地日志" if (data or {}).get("error") else None
    hardware = dict(hardware or {})
    if hardware.get("error"):
        hardware = {"error": "硬件概览读取失败"}
    return {"version": __version__, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "system": platform.system() + " " + platform.release(), "python": platform.python_version(),
            "hardware": hardware, "readings": readings, "recent_alerts": list(events), "collection": stats}


def export(path, payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    if str(path).lower().endswith(".txt"):
        text = "硬件监控诊断报告\n" + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
