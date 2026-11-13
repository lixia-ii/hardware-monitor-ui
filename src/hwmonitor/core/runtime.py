"""Shared history and alert consumption; all state mutations have one owner."""
from collections import deque
import queue
import time

from hwmonitor.core import history
from hwmonitor.core.alerts import AlertEngine


class RuntimeState:
    def __init__(self):
        self.history = deque(maxlen=history.MAX_ROWS)
        self.recent_alerts = deque(maxlen=100)
        self.alert_engine = AlertEngine()

    def consume_history(self, collector):
        while not collector.history_rows.empty():
            action, rows = collector.history_rows.get()
            if action == "load":
                self.history.extend(rows)
            else:
                self.history.append(rows)

    def alerts(self, data, options):
        messages = self.alert_engine.check(data, options, time.monotonic())
        for message in messages:
            self.record(message)
        return messages

    def record(self, message):
        self.recent_alerts.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": message})

    def latest(self, collector, options):
        self.consume_history(collector)
        try:
            data = collector.results.get_nowait()
        except queue.Empty:
            return None
        return data if data["selection"] == (options["gpu_id"], options["nic_ids"]) else None
