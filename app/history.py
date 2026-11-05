"""Bounded SQLite history and peak-preserving chart reduction."""
import csv
import json
import math
import sqlite3
import time

METRICS = ("cpu_temp", "gpu_temp", "cpu_load", "gpu_load", "mem_load", "gpu_mem_load", "net_up", "net_down")
MAX_ROWS = 17280


def sample(data):
    return {k: data.get(k) for k in (*METRICS, "gpu_id", "gpu_name", "net_selection")}


def gpu_groups(devices):
    """Reconcile unique LHM/NVIDIA provider IDs without collapsing same-model cards.

    Use the full retained device catalog, not only the visible time range: another
    same-model card must remain evidence of ambiguity even outside that range.
    """
    names = {identity: name or identity for identity, name in devices if identity}
    models = {}
    for identity, name in names.items():
        models.setdefault(" ".join(name.casefold().split()), []).append(identity)
    groups = {}
    for identities in models.values():
        lhm = [identity for identity in identities if identity.startswith("/gpu-nvidia/")]
        nvidia = [identity for identity in identities if identity.startswith("nvidia:")]
        # Name alone cannot distinguish two cards from the same provider. Only
        # bridge a one-to-one pair from the two known provider namespaces.
        if len(identities) == 2 and len(lhm) == len(nvidia) == 1:
            groups[nvidia[0]] = (names[nvidia[0]], frozenset(identities))
        else:
            groups.update((identity, (names[identity], frozenset((identity,)))) for identity in identities)
    return dict(sorted(groups.items(), key=lambda item: (item[1][0].casefold(), item[0])))


def filter_sample(values, gpu_ids, net_selection):
    """Use the chart's device selection for hover readings and CSV export too."""
    result = dict(values)
    if values.get("gpu_id") not in gpu_ids:
        for key in ("gpu_temp", "gpu_load", "gpu_mem_load"):
            result[key] = None
    if values.get("net_selection") != net_selection:
        result["net_up"] = result["net_down"] = None
    return result


class HistoryStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=1)
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS samples (t REAL NOT NULL, payload TEXT NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS samples_time ON samples(t)")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.prune()
        except Exception:
            self.db.close()
            raise

    def prune(self):
        self.db.execute("DELETE FROM samples WHERE t < ?", (time.time() - 86400,))
        self.db.execute("DELETE FROM samples WHERE rowid NOT IN (SELECT rowid FROM samples ORDER BY t DESC LIMIT ?)", (MAX_ROWS,))
        self.db.commit()
        self.row_count = self.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def load(self):
        rows = []
        for t, payload in self.db.execute("SELECT t, payload FROM samples ORDER BY t"):
            try:
                values = json.loads(payload)
                if isinstance(values, dict):
                    for key in METRICS:
                        if key not in values:
                            continue
                        value = values.get(key)
                        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                            values[key] = None
                    for key in ("gpu_id", "gpu_name", "net_selection"):
                        if key in values and values[key] is not None and not isinstance(values[key], str):
                            values[key] = None
                    rows.append((t, values))
            except ValueError:
                continue
        return rows

    def append(self, t, values):
        self.db.execute("INSERT INTO samples VALUES (?, ?)", (t, json.dumps(values, allow_nan=False)))
        self.row_count += 1
        removed = self.db.execute("DELETE FROM samples WHERE t < ?", (time.time() - 86400,)).rowcount
        self.row_count -= removed
        overflow = self.row_count - MAX_ROWS
        if overflow > 0:
            self.db.execute("DELETE FROM samples WHERE rowid IN (SELECT rowid FROM samples ORDER BY t, rowid LIMIT ?)", (overflow,))
            self.row_count -= overflow
        self.db.commit()

    def close(self):
        self.db.close()


def segments(rows, key, start, end, width, gpu_id=None, net_selection=None, *, gpu_ids=None):
    """Keep each pixel bucket's first/min/max/last, with real gaps and device boundaries."""
    result, run, bucket_points = [], [], []
    previous, bucket_id = None, None
    scale = max(width, 1) / max(end - start, 1)
    gpu_ids = frozenset((gpu_id,)) if gpu_ids is None else gpu_ids
    def flush_bucket():
        if bucket_points:
            # first/min/max/last each carry their original ordinal, including ties.
            for _, point in sorted(dict(bucket_points).items()):
                run.append(point)
            bucket_points.clear()
    for index, (t, values) in enumerate(rows):
        if not start <= t <= end:
            continue
        value = values.get(key)
        if key.startswith("gpu_") and values.get("gpu_id") not in gpu_ids:
            value = None
        if key.startswith("net_") and values.get("net_selection") != net_selection:
            value = None
        if value is None or (previous is not None and t - previous > 35):
            flush_bucket()
            if run:
                result.append(run)
                run = []
            bucket_id = None
        previous = t
        if value is None:
            continue
        bucket = int((t - start) * scale)
        point = (index, (t, value))
        if bucket != bucket_id:
            flush_bucket()
            bucket_id = bucket
        if not bucket_points:
            bucket_points.extend((point, point, point, point))
        else:
            if value < bucket_points[1][1][1]:
                bucket_points[1] = point
            if value > bucket_points[2][1][1]:
                bucket_points[2] = point
            bucket_points[3] = point
    flush_bucket()
    if run:
        result.append(run)
    return result


def export_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(("time", "gpu_id", "gpu_name", "net_selection", *METRICS))
        for t, values in rows:
            w.writerow((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                        values.get("gpu_id"), values.get("gpu_name"), values.get("net_selection"),
                        *(values.get(k) for k in METRICS)))
