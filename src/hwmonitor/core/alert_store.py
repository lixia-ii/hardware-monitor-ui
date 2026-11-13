"""Durable, count-bounded alert log, independent of metric history retention.

One long-lived WAL connection, used only by the runtime's alert writer thread after load.
"""
import sqlite3

MAX_ALERTS = 1000


class AlertStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS alerts (time TEXT NOT NULL, message TEXT NOT NULL)")
            self.db.execute("DELETE FROM alerts WHERE rowid NOT IN (SELECT rowid FROM alerts ORDER BY rowid DESC LIMIT ?)",
                            (MAX_ALERTS,))
            self.db.commit()
            self.row_count = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        except Exception:
            self.db.close()
            raise

    def load(self):
        return [{"time": t, "message": message} for t, message in
                self.db.execute("SELECT time, message FROM alerts ORDER BY rowid")]

    def append(self, *events):
        """One transaction per batch: a burst of alerts costs one disk sync, not one each."""
        self.db.executemany("INSERT INTO alerts VALUES (?, ?)", [(e["time"], e["message"]) for e in events])
        self.row_count += len(events)
        overflow = self.row_count - MAX_ALERTS
        if overflow > 0:
            self.db.execute("DELETE FROM alerts WHERE rowid IN (SELECT rowid FROM alerts ORDER BY rowid LIMIT ?)", (overflow,))
            self.row_count -= overflow
        self.db.commit()

    def close(self):
        self.db.close()
