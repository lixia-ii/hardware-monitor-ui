"""传感器树解析、显卡选择与 nvidia-smi 兜底。"""
import unittest
from unittest.mock import Mock, patch

from hwmonitor.sensors import lhm, nvidia, reading
from tests.support import hardware_node, leaf, sample_tree


class SensorTests(unittest.TestCase):
    def parse_tree(self, tree):
        out = {"temps": [], "fans": [], "loads": [], "data": [], "extra": {}, "hw": [], "by_id": True}
        reading._walk(tree, None, out)
        return out

    def test_uuid_selection_survives_lhm_reconnection_without_cross_fill(self):
        tree = sample_tree()
        tree["Children"].append(hardware_node("/gpu-nvidia/1", "NVIDIA two", [leaf("GPU Core", "80 °C", "Temperature")]))
        cards = [("NVIDIA one", 65, 20, 50, 1024, 8192, "A"), ("NVIDIA two", 80, 70, 10, 6144, 8192, "B")]
        with patch("hwmonitor.sensors.reading._lhm", return_value=self.parse_tree(tree)), patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
            data = reading.read("nvidia:B")
        self.assertEqual(data["gpu_id"], "nvidia:B")
        self.assertEqual(data["gpu_temp"], 80)
        self.assertEqual(data["gpu_load"], 10)
        self.assertEqual(data["gpu_mem_load"], 75)
        self.assertIsNone(data["gpu_fan"])

    def test_identical_gpu_names_use_uuid_provider_only(self):
        tree = sample_tree()
        tree["Children"][1]["Text"] = "RTX"
        tree["Children"].append(hardware_node("/gpu-nvidia/1", "RTX", [leaf("GPU Core", "75 °C", "Temperature")]))
        cards = [("RTX", 65, 20, 50, 1024, 8192, "A"), ("RTX", 80, 70, 10, 6144, 8192, "B")]
        with patch("hwmonitor.sensors.reading._lhm", return_value=self.parse_tree(tree)), patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
            data = reading.read("nvidia:B")
        self.assertEqual((data["gpu_id"], data["gpu_temp"], data["gpu_mem_load"]), ("nvidia:B", 80, 75))
        self.assertIsNone(data["gpu_fan"])

    def test_pinned_missing_gpu_does_not_select_other_card(self):
        cards = [("NVIDIA one", 65, 20, 50, 1024, 8192, "A")]
        for connection in (dict(return_value=self.parse_tree(sample_tree())), dict(side_effect=lhm.Unavailable("offline"))):
            with patch("hwmonitor.sensors.reading._lhm", **connection), patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
                data = reading.read("/gpu-intel/absent")
            self.assertIsNone(data["gpu_temp"])
            self.assertIsNone(data["gpu_id"])

    def test_dynamic_sensor_identity_survives_other_device_disconnect(self):
        tree = sample_tree()
        tree["Children"].append(hardware_node("/nvme/1", "SSD", [leaf("Composite Temperature", "55 °C", "Temperature")]))
        for i in range(2):
            tree["Children"].append(hardware_node(f"/lpc/{i}", "Board", [leaf("Case Fan", "800 RPM", "Fan")]))
        with patch("hwmonitor.sensors.reading._lhm", return_value=self.parse_tree(tree)), patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            before = reading.read()
        tree["Children"] = [n for n in tree["Children"] if n["HardwareId"] not in ("/nvme/0", "/lpc/0")]
        with patch("hwmonitor.sensors.reading._lhm", return_value=self.parse_tree(tree)), patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            after = reading.read()
        self.assertEqual(before["sensor_ids"]["extra_temps"][1], after["sensor_ids"]["extra_temps"][0])
        self.assertEqual(before["sensor_ids"]["case_fans"][1], after["sensor_ids"]["case_fans"][0])
        self.assertNotEqual(*before["sensor_ids"]["case_fans"])

    def source(self):
        return Mock(sample=Mock(side_effect=sample_tree))

    def test_fahrenheit_tdie_thresholds_memory_and_network(self):
        with patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            data = reading.read(source=self.source())
        self.assertEqual(data["cpu_temp"], 70)
        self.assertEqual(data["gpu_mem_load"], 12.5)
        self.assertEqual(data["gpu_fan"], 0)
        self.assertEqual(data["net_up"], 1024 ** 2)
        self.assertEqual(data["extra_temps"], [("storage", "SSD", 40)])
        self.assertEqual(len(data["nics"]), 2)

    def test_explicit_virtual_nic_selection_and_empty_selection(self):
        with patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            self.assertEqual(reading.read(nic_ids=["/nic/2"], source=self.source())["net_up"], 10 * 1024 ** 2)
            self.assertIsNone(reading.read(nic_ids=[], source=self.source())["net_up"])

    def test_disconnect_with_nvidia_uuid_fallback(self):
        cards = [("GPU", 70, 0, 20, 1024, 8192, "UUID-A"), ("GPU", 80, 10, 50, 2048, 8192, "UUID-B")]
        source = Mock(sample=Mock(side_effect=lhm.Unavailable("正在初始化")))
        with patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
            data = reading.read("nvidia:UUID-B", source=source)
        self.assertTrue(data["sensors_down"])
        self.assertEqual(data["error"], "正在初始化")
        self.assertEqual(data["gpu_temp"], 80)
        self.assertEqual(data["gpu_id"], "nvidia:UUID-B")
        self.assertEqual(len(data["gpus"]), 2)

    def test_missing_source_is_reported_not_raised(self):
        with patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            data = reading.read()
        self.assertTrue(data["sensors_down"])
        self.assertTrue(data["error"])

    def test_same_model_nvidia_cards_do_not_cross_fill(self):
        with patch("hwmonitor.sensors.nvidia.cards", return_value=[("RTX", 60), ("RTX", 80)]):
            self.assertIsNone(nvidia.card_for("NVIDIA RTX"))
            self.assertIsNone(nvidia.card_for(None, "/gpu-nvidia/1"))

    def test_nvidia_fallback_without_hardware_library_takes_first_card(self):
        cards = [("RTX 4060", 50, 30, 10, 1024, 8192, "A"), ("RTX 3060", 60, 40, 20, 2048, 8192, "B")]
        with patch("hwmonitor.sensors.nvidia.cards", return_value=cards):
            self.assertEqual(nvidia.card_for(None)[6], "A")
            self.assertEqual(nvidia.card_for("NVIDIA GeForce RTX 3060")[6], "B")
            self.assertIsNone(nvidia.card_for(None, "/gpu-nvidia/9"))
