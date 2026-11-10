"""读取另一台运行本软件的电脑所提供的 /data.json，不读取本机硬件。"""
import http.client
import json
import logging
import time
from types import SimpleNamespace
import urllib.request

from hwmonitor.sensors import reading

MAX_BODY = 4 * 1024 * 1024


def empty_reading():
    return dict(reading.empty_reading(), offline=True)


def validate_tree(tree):
    if not isinstance(tree, dict) or not isinstance(tree.get("Children"), list):
        raise ValueError("Not a hardware sensor tree")
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if not isinstance(node, dict) or count > 10000 or depth > 64:
            raise ValueError("Invalid sensor tree")
        children = node.get("Children", [])
        if not isinstance(children, list):
            raise ValueError("Invalid sensor children")
        pending.extend((child, depth + 1) for child in children)


class Device:
    def __init__(self):
        self.url = None
        self.failures = 0
        self.retry_at = 0.0

    def read(self, url, nic=None, timeout=1.0):
        now = time.monotonic()
        if url != self.url:
            self.url, self.failures, self.retry_at = url, 0, 0.0
        if now < self.retry_at:
            return empty_reading()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("Sensor response too large")
            tree = json.loads(body)
            validate_tree(tree)
            result = reading.read(source=SimpleNamespace(sample=lambda: tree), allow_nvidia=False)
            if result["error"]:
                raise ValueError("Invalid sensor data")
        except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
            logging.getLogger(__name__).debug("Remote device unavailable", exc_info=True)
            self.failures += 1
            self.retry_at = now + min(30, 2 ** min(self.failures, 5))
            return empty_reading()
        self.failures, self.retry_at = 0, 0.0
        return dict(result, offline=False)
