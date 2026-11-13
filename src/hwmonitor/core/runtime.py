"""Shared history and alert consumption; all state mutations have one owner."""
from collections import deque
import logging
import queue
import sqlite3
import threading
import time

from hwmonitor.core import history
from hwmonitor.core.alerts import AlertEngine
from hwmonitor.core.alert_store import AlertStore, MAX_ALERTS


class RuntimeState:
    def __init__(self):
        self.history = deque(maxlen=history.MAX_ROWS)
        self.recent_alerts = deque(maxlen=MAX_ALERTS)
        self.alert_store = None
        self.alert_writes = None
        self.alert_writer = None
        self.alert_storage_error = None
        self.alert_revision = time.time_ns()
        self.alert_engine = AlertEngine()

    def load_alerts(self, path):
        try:
            store = AlertStore(path)
            self.recent_alerts.extend(store.load())
            self.alert_store = store
            self.alert_revision += 1
            # SQLite commits stay off the Tk / control loop; a busy disk only delays the log.
            self.alert_writes = queue.SimpleQueue()
            self.alert_writer = threading.Thread(target=self._write_alerts, name="alert-writer", daemon=True)
            self.alert_writer.start()
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).exception("Alert storage unavailable")
            self.alert_storage_error = "告警存储不可用，当前仅在内存中保留；请检查目录权限或数据库。"

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
        event = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "message": message}
        self.recent_alerts.append(event)
        self.alert_revision += 1
        if self.alert_writes is not None:
            self.alert_writes.put(event)

    def _write_alerts(self):
        store, writes, running = self.alert_store, self.alert_writes, True
        while running:
            batch = [writes.get()]
            while not writes.empty():
                batch.append(writes.get())
            running = batch[-1] is not None
            batch = [event for event in batch if event is not None]
            if store is None or not batch:
                continue
            try:
                store.append(*batch)
            except (OSError, sqlite3.Error):
                logging.getLogger(__name__).exception("Alert write failed")
                self.alert_storage_error = "告警保存失败，当前仅在内存中保留；请检查目录权限或数据库。"
                store.close()
                store = self.alert_store = None
        if store is not None:
            store.close()

    def close_alerts(self, timeout=5):
        """Flush queued alerts and close the database; later alerts stay in memory only.

        Returns False if the writer is still busy after timeout (database left open).
        """
        writes, writer = self.alert_writes, self.alert_writer
        self.alert_writes = self.alert_writer = None
        if writer is None:
            return True
        writes.put(None)
        writer.join(timeout)
        return not writer.is_alive()

    def storage_error(self, collector):
        """Both stores fail independently; report each instead of letting one hide the other."""
        return " ".join(filter(None, (self.alert_storage_error, collector.storage_error))) or None

    def latest(self, collector, options):
        self.consume_history(collector)
        try:
            data = collector.results.get_nowait()
        except queue.Empty:
            return None
        return data if data["selection"] == (options["gpu_id"], options["nic_ids"]) else None
