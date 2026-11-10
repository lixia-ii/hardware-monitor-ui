"""温度告警与风扇异常检测。"""
import copy
import unittest

from hwmonitor import settings
from hwmonitor.core import alerts


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = alerts.AlertEngine()
        self.cfg = copy.deepcopy(settings.DEFAULT_ALERTS)
        self.data = {"cpu_temp": 86, "gpu_temp": None, "cpu_fan": 1000, "gpu_id": "g1"}

    def check(self, at, **values):
        self.data.update(values)
        return self.engine.check(self.data, self.cfg, at)

    def test_sustained_only(self):
        self.assertFalse(self.check(0))
        self.assertFalse(self.check(9))
        self.assertEqual(len(self.check(10)), 1)

    def test_threshold_jitter_does_not_bypass_cooldown(self):
        self.check(0)
        self.assertTrue(self.check(10))
        self.assertFalse(self.check(11, cpu_temp=84))
        self.assertFalse(self.check(12, cpu_temp=86))
        self.assertFalse(self.check(13, cpu_temp=78))
        self.assertFalse(self.check(14, cpu_temp=86))
        self.assertFalse(self.check(24))
        for t in range(44, 305, 20):
            self.check(t)
        self.assertTrue(self.check(310))

    def test_missing_or_long_gap_breaks_duration(self):
        self.check(0)
        self.check(9, cpu_temp=None)
        self.assertFalse(self.check(10, cpu_temp=90))
        self.assertFalse(self.check(19))
        self.assertTrue(self.check(20))
        self.engine = alerts.AlertEngine()
        self.check(0)
        self.assertFalse(self.check(100))
        self.assertTrue(self.check(110))

    def test_fan_missing_and_low_temperature_stop_are_normal(self):
        self.cfg.update(enabled=False, fan_enabled=True)
        for t in (0, 30, 60):
            self.assertFalse(self.check(t, cpu_temp=50, cpu_fan=0, gpu_temp=50, gpu_fan=0))
        self.assertFalse(self.check(61, cpu_temp=90, cpu_fan=None, gpu_temp=90, gpu_fan=None))
        self.assertFalse(self.check(100))

    def test_hot_low_fan_sustained_and_recovery(self):
        self.cfg.update(enabled=False, fan_enabled=True)
        self.assertFalse(self.check(0, cpu_fan=0))
        self.assertFalse(self.check(29))
        self.assertTrue(self.check(30))
        self.assertFalse(self.check(31, cpu_fan=1000))
        self.assertFalse(self.check(32, cpu_fan=0))
        self.assertFalse(self.check(62))

    def test_gpu_switch_does_not_inherit_sustained_state(self):
        self.data["cpu_temp"] = None
        self.assertFalse(self.check(0, gpu_temp=90))
        self.assertFalse(self.check(9))
        self.assertFalse(self.check(10, gpu_id="g2"))
        self.assertTrue(self.check(20))
        self.assertFalse(self.check(21, gpu_id="g1"))
