"""Alert retention, restart recovery, memory fallback and paginated queries."""
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hwmonitor.core.alert_store import AlertStore
from hwmonitor.core.runtime import RuntimeState
from hwmonitor.core import management
from hwmonitor.system.webbridge import WebError


class AlertStorageTests(unittest.TestCase):
    def test_restart_retains_latest_thousand_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "alerts.sqlite3"
            state = RuntimeState()
            state.load_alerts(path)
            for i in range(1005):
                state.record(f"告警 {i}")
            self.assertTrue(state.close_alerts(timeout=None))
            restarted = RuntimeState()
            restarted.load_alerts(path)
            self.assertEqual(list(state.recent_alerts), list(restarted.recent_alerts))
            self.assertEqual(len(restarted.recent_alerts), 1000)
            self.assertEqual(restarted.recent_alerts[0]["message"], "告警 5")
            restarted.record("重启后")
            self.assertTrue(restarted.close_alerts(timeout=None))
            store = AlertStore(path)
            self.assertEqual(store.load()[-1]["message"], "重启后")
            self.assertEqual((len(store.load()), store.row_count), (1000, 1000))
            store.close()

    def test_record_never_writes_on_the_caller_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RuntimeState()
            state.load_alerts(Path(directory) / "alerts.sqlite3")
            caller, writers = threading.get_ident(), []
            original = state.alert_store.append
            def append(*events):
                writers.append(threading.get_ident())
                original(*events)
            with patch.object(state.alert_store, "append", side_effect=append):
                state.record("后台写入")
                state.close_alerts(timeout=None)
            self.assertTrue(writers)
            self.assertNotIn(caller, writers)
            state.record("关闭后仅内存")
            self.assertEqual(state.recent_alerts[-1]["message"], "关闭后仅内存")

    def test_no_age_expiry_and_equal_times_keep_insertion_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.sqlite3"
            store = AlertStore(path)
            for message in ("高温", "已恢复"):
                store.append({"time": "2000-01-01 00:00:00", "message": message})
            store.close()
            store = AlertStore(path)
            self.assertEqual([e["message"] for e in store.load()], ["高温", "已恢复"])
            store.close()

    def test_storage_failures_keep_alerts_in_memory(self):
        state = RuntimeState()
        with patch("hwmonitor.core.runtime.AlertStore", side_effect=sqlite3.DatabaseError), self.assertLogs("hwmonitor.core.runtime", "ERROR"):
            state.load_alerts(Path("unused"))
        state.record("离线")
        self.assertEqual(state.recent_alerts[-1]["message"], "离线")
        self.assertIsNotNone(state.alert_storage_error)
        with tempfile.TemporaryDirectory() as directory:
            state = RuntimeState()
            state.load_alerts(Path(directory) / "alerts.sqlite3")
            with patch.object(state.alert_store, "append", side_effect=sqlite3.OperationalError), self.assertLogs("hwmonitor.core.runtime", "ERROR"):
                state.record("写入失败时")
                state.close_alerts(timeout=None)
            self.assertEqual(state.recent_alerts[-1]["message"], "写入失败时")
            self.assertIsNotNone(state.alert_storage_error)

    def test_alert_and_history_storage_errors_are_both_reported(self):
        state = RuntimeState()
        collector = SimpleNamespace(storage_error=None)
        self.assertIsNone(state.storage_error(collector))
        state.alert_storage_error = "告警保存失败。"
        collector.storage_error = "历史保存失败。"
        message = state.storage_error(collector)
        self.assertIn("告警保存失败", message)
        self.assertIn("历史保存失败", message)

    def test_pagination_boundaries_and_validation(self):
        state = RuntimeState()
        for i in range(53):
            state.record(str(i))
        def query(page):
            return management.alerts_query(state.recent_alerts, {"page": [str(page)]})
        first, second, last = query(1), query(2), query(3)
        self.assertEqual((first["total"], first["pages"]), (53, 3))
        self.assertEqual([len(p["items"]) for p in (first, second, last)], [20, 20, 13])
        self.assertEqual([e["message"] for p in (first, second, last) for e in p["items"]],
                         [str(i) for i in reversed(range(53))])
        self.assertEqual(query(999)["page"], 3)
        self.assertEqual(management.paginate(state.recent_alerts, 5000)["page"], 3)
        self.assertEqual(management.paginate(state.recent_alerts, 0)["page"], 1)
        empty = management.alerts_query([], {})
        self.assertEqual((empty["items"], empty["page"], empty["pages"]), ([], 1, 1))
        for key in ("page", "page_size"):
            for value in ("", "0", "-1", "1.5", "abc", "1001", "999999"):
                with self.subTest(key=key, value=value), self.assertRaises(WebError):
                    management.alerts_query([], {key: [value]})
        with self.assertRaises(WebError):
            management.alerts_query([], {"page": ["1", "2"]})
