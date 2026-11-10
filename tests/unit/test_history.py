"""历史存储、显卡身份合并、曲线精简与 CSV 导出。"""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from hwmonitor.core import history


class HistoryTests(unittest.TestCase):
    def test_unique_nvidia_provider_ids_share_one_history_choice(self):
        groups = history.gpu_groups([
            ("/gpu-nvidia/0", "NVIDIA GeForce GTX 1060 6GB"),
            ("nvidia:GPU-A", "nvidia  geforce GTX 1060 6GB"),
            ("/gpu-nvidia/0", "NVIDIA GeForce GTX 1060 6GB"),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups["nvidia:GPU-A"][1], {"/gpu-nvidia/0", "nvidia:GPU-A"})

    def test_same_model_cards_and_ambiguous_provider_matches_stay_separate(self):
        identities = ("/gpu-nvidia/0", "/gpu-nvidia/1", "nvidia:A", "nvidia:B")
        for ids in (identities[:2], identities[2:], identities[:3], identities):
            with self.subTest(ids=ids):
                groups = history.gpu_groups([(identity, "NVIDIA RTX") for identity in ids])
                self.assertEqual(len(groups), len(ids))
                self.assertTrue(all(members == {identity} for identity, (_, members) in groups.items()))

    def test_different_models_or_unknown_providers_never_merge_by_name(self):
        for devices in (
            [("/gpu-nvidia/0", "RTX 4060"), ("nvidia:A", "RTX 4060 Ti")],
            [("/gpu-amd/0", "GPU"), ("nvidia:A", "GPU")],
            [("a", None), ("b", None)],
        ):
            self.assertEqual(len(history.gpu_groups(devices)), 2)

    def test_provider_aliases_keep_both_history_parts_and_filter_other_cards(self):
        ids = frozenset(("/gpu-nvidia/0", "nvidia:A"))
        rows = [(t, dict(gpu_id=identity, gpu_temp=temp, gpu_load=20, gpu_mem_load=30,
                         cpu_temp=60, net_up=100, net_down=200, net_selection="nic1"))
                for t, identity, temp in ((0, "/gpu-nvidia/0", 50), (5, "nvidia:A", 90), (10, "nvidia:B", 100))]
        self.assertEqual(history.segments(rows, "gpu_temp", 0, 10, 100, gpu_ids=ids), [[(0, 50), (5, 90)]])
        filtered = [history.filter_sample(values, ids, "nic1") for _, values in rows]
        self.assertEqual([values["gpu_temp"] for values in filtered], [50, 90, None])
        self.assertEqual([values["cpu_temp"] for values in filtered], [60, 60, 60])
        self.assertEqual([values["gpu_load"] for values in filtered], [20, 20, None])
        self.assertEqual([values["gpu_mem_load"] for values in filtered], [30, 30, None])
        self.assertIsNone(history.filter_sample(rows[0][1], ids, "nic2")["net_up"])
        self.assertEqual(rows[2][1]["gpu_temp"], 100)  # Raw history is retained unchanged.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "aliases.csv"
            history.export_csv(path, [(t, values) for (t, _), values in zip(rows, filtered)])
            import csv
            with path.open(encoding="utf-8-sig", newline="") as f:
                exported = list(csv.DictReader(f))
            self.assertEqual([row["gpu_temp"] for row in exported], ["50", "90", ""])

    def test_row_limit_with_duplicate_and_out_of_order_timestamps(self):
        with tempfile.TemporaryDirectory() as d, patch("hwmonitor.core.history.MAX_ROWS", 3):
            store = history.HistoryStore(Path(d) / "history.db")
            try:
                now = time.time()
                for t in (now, now, now - 5, now + 5, now - 90000):
                    store.append(t, {"cpu_temp": 50})
                self.assertEqual([t for t, _ in store.load()], [now, now, now + 5])
                self.assertEqual(store.row_count, 3)
            finally:
                store.close()

    def test_reduction_preserves_order_extrema_and_gaps_in_same_bucket(self):
        rows = [(i, {"cpu_temp": v}) for i, v in enumerate((50, 90, 10, 50, None, 60, 20, 80, 60))]
        self.assertEqual(history.segments(rows, "cpu_temp", 0, 20, 1),
                         [[(0, 50), (1, 90), (2, 10), (3, 50)], [(5, 60), (6, 20), (7, 80), (8, 60)]])

    def test_restart_and_24_hour_retention(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.db"
            store = history.HistoryStore(path)
            now = time.time()
            store.append(now - 90000, {"gpu_id": "old"})
            store.append(now, {"gpu_id": "a", "gpu_temp": 95})
            store.close()
            store = history.HistoryStore(path)
            try:
                self.assertEqual(store.load(), [(now, {"gpu_id": "a", "gpu_temp": 95})])
            finally:
                store.close()

    def test_reduction_preserves_spike_and_device_boundary(self):
        rows = [(t, {"gpu_temp": 100 if t == 7 else 50, "gpu_id": "a" if t < 15 else "b"}) for t in range(30)]
        runs = history.segments(rows, "gpu_temp", 0, 30, 1, "a")
        points = [p for run in runs for p in run]
        self.assertIn((7, 100), points)
        self.assertTrue(all(t < 15 for t, _ in points))
        self.assertLessEqual(len(points), 4)

    def test_missing_and_sleep_leave_gaps(self):
        rows = [(0, {"cpu_temp": 50}), (5, {"cpu_temp": None}), (10, {"cpu_temp": 60}), (100, {"cpu_temp": 90})]
        self.assertEqual(len(history.segments(rows, "cpu_temp", 0, 100, 100)), 3)

    def test_display_interval_connects_original_extrema_without_averaging(self):
        rows = [(t, {"cpu_temp": v}) for t, v in ((0, 0), (5, 100), (10, 0), (15, 0), (20, 0), (30, 40), (40, 60))]
        self.assertEqual(history.segments(rows, "cpu_temp", 0, 60, 1, interval=30),
                         [[(0, 0), (5, 100), (20, 0), (30, 40), (40, 60)]])
        self.assertEqual(rows[1], (5, {"cpu_temp": 100}))

    def test_display_interval_does_not_fill_gaps_inside_same_bucket(self):
        rows = [(t, {"cpu_temp": v}) for t, v in ((0, 10), (5, 20), (45, 30), (50, None), (55, 50))]
        self.assertEqual(history.segments(rows, "cpu_temp", 0, 60, 100, interval=300),
                         [[(0, 10), (5, 20)], [(45, 30)], [(55, 50)]])

    def test_interval_extrema_keep_time_order_ties_and_visible_range(self):
        rows = [(t, {"cpu_temp": v}) for t, v in ((0, 120), (5, 95), (10, 95), (15, 20), (30, 30), (60, 110))]
        self.assertEqual(history.segments(rows, "cpu_temp", 5, 45, 1, interval=30),
                         [[(5, 95), (15, 20), (30, 30)]])
        for values in ((50, 90, 10, 40), (50, 10, 90, 40)):
            rows = [(i * 5, {"cpu_temp": v}) for i, v in enumerate(values)]
            self.assertEqual(history.segments(rows, "cpu_temp", 0, 30, 1, interval=30),
                             [[(i * 5, v) for i, v in enumerate(values)]])

    def test_interval_extrema_keep_zero_and_singleton_buckets(self):
        rows = [(0, {"cpu_load": 0}), (5, {"cpu_load": 0}), (30, {"cpu_load": 100}), (35, {"cpu_load": None})]
        self.assertEqual(history.segments(rows, "cpu_load", 0, 60, 100, interval=30), [[(0, 0), (5, 0), (30, 100)]])
        self.assertEqual(history.segments(rows, "gpu_load", 0, 60, 100, interval=30), [])

    def test_display_interval_uses_stable_time_buckets_and_visible_samples(self):
        rows = [(t, {"cpu_temp": t}) for t in range(0, 95, 5)]
        first = history.segments(rows, "cpu_temp", 5, 90, 100, interval=30)[0]
        shifted = history.segments(rows, "cpu_temp", 10, 90, 100, interval=30)[0]
        self.assertEqual(first[1:], shifted[1:])
        self.assertEqual(first[0], (5, 5))
        self.assertEqual(shifted[0], (10, 10))
        self.assertEqual(history.segments([], "cpu_temp", 0, 60, 100, interval=30), [])

    def test_display_interval_respects_gpu_aliases_and_network_selection(self):
        rows = [(t, {"gpu_id": gpu, "gpu_temp": v, "net_selection": nic, "net_up": v})
                for t, gpu, nic, v in ((0, "/gpu-nvidia/0", "a", 20), (5, "nvidia:A", "a", 40),
                                      (10, "nvidia:B", "b", 100), (15, "nvidia:A", "a", 60))]
        expected = [[(0, 20), (5, 40)], [(15, 60)]]
        self.assertEqual(history.segments(rows, "gpu_temp", 0, 60, 100, interval=30,
                                          gpu_ids={"/gpu-nvidia/0", "nvidia:A"}), expected)
        self.assertEqual(history.segments(rows, "net_up", 0, 60, 100, interval=30, net_selection="a"), expected)

    def test_csv_is_excel_readable_and_preserves_device(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.csv"
            history.export_csv(path, [(time.time(), {"gpu_id": "a", "gpu_name": "显卡", "gpu_temp": 88})])
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn("显卡", path.read_text(encoding="utf-8-sig"))

    def test_invalid_saved_values_become_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store = history.HistoryStore(Path(d) / "history.db")
            try:
                store.append(time.time(), {"gpu_id": [], "cpu_temp": "bad"})
                self.assertIsNone(store.load()[0][1]["cpu_temp"])
                self.assertIsNone(store.load()[0][1]["gpu_id"])
            finally:
                store.close()
