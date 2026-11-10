"""LibreHardwareMonitorLib 封装: data.json 结构、数值格式、初始化重试。"""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from hwmonitor import settings
from hwmonitor.core.collector import Collector
from hwmonitor.sensors import lhm, reading
from tests.support import fake_hardware, fake_sensor


class HardwareTests(unittest.TestCase):
    def machine(self):
        board = fake_hardware("Motherboard", "Board", "/motherboard", sub=[
            fake_hardware("SuperIO", "Nuvoton", "/lpc/nct6798d/0", [
                fake_sensor("Fan", "Fan #2", 900, 1, "/lpc/nct6798d/0/fan/1"),
                fake_sensor("Fan", "CPU Fan", 1250.4, 0, "/lpc/nct6798d/0/fan/0")])])
        cpu = fake_hardware("Cpu", "Intel Core", "/intelcpu/0", [
            fake_sensor("Load", "CPU Total", 12.34, 0, "/intelcpu/0/load/0"),
            fake_sensor("Temperature", "CPU Package", 66.04, 18, "/intelcpu/0/temperature/18"),
            fake_sensor("Clock", "Bus Speed", 100, 0, "/intelcpu/0/clock/0")])
        gpu = fake_hardware("GpuNvidia", "NVIDIA GeForce RTX 4060", "/gpu-nvidia/0", [
            fake_sensor("Temperature", "GPU Core", 43, 0, "/gpu-nvidia/0/temperature/0"),
            fake_sensor("SmallData", "GPU Memory Used", 4074.37, 1, "/gpu-nvidia/0/smalldata/1"),
            fake_sensor("SmallData", "GPU Memory Total", 8188, 2, "/gpu-nvidia/0/smalldata/2")])
        ram = fake_hardware("Memory", "Total Memory", "/ram", [
            fake_sensor("Data", "Memory Used", 33.3, 0, "/ram/data/0"),
            fake_sensor("Data", "Memory Available", 30.4, 1, "/ram/data/1"),
            fake_sensor("Load", "Memory", 52.3, 0, "/ram/load/0")])
        nic = fake_hardware("Network", "Ethernet", "/nic/%7B1%7D", [
            fake_sensor("Throughput", "Upload Speed", 2 * 1024 ** 2, 7, "/nic/%7B1%7D/throughput/7"),
            fake_sensor("Throughput", "Download Speed", 512, 8, "/nic/%7B1%7D/throughput/8"),
            fake_sensor("Throughput", "Connection Speed", 1000000000, 9, "/nic/%7B1%7D/throughput/9")])
        return [gpu, nic, cpu, board, ram]

    def test_tree_matches_lhm_data_json_layout(self):
        tree = lhm.build_tree(self.machine(), "PC")
        self.assertEqual([tree[k] for k in ("id", "Text", "Min", "Value", "Max")], [0, "Sensor", "Min", "Value", "Max"])
        computer = tree["Children"][0]
        self.assertEqual((computer["Text"], computer["ImageURL"]), ("PC", "images_icon/computer.png"))
        self.assertEqual([n["HardwareId"] for n in computer["Children"]],
                         ["/motherboard", "/intelcpu/0", "/ram", "/gpu-nvidia/0", "/nic/%7B1%7D"])
        cpu = computer["Children"][1]
        self.assertEqual([g["Text"] for g in cpu["Children"]], ["Clocks", "Temperatures", "Load"])
        package = cpu["Children"][1]["Children"][0]
        self.assertEqual({k: package[k] for k in ("Text", "Value", "Type", "SensorId", "RawValue")},
                         {"Text": "CPU Package", "Value": "66.0 °C", "Type": "Temperature",
                          "SensorId": "/intelcpu/0/temperature/18", "RawValue": "66.0 °C"})
        superio = computer["Children"][0]["Children"][0]
        self.assertEqual([s["Text"] for s in superio["Children"][0]["Children"]], ["CPU Fan", "Fan #2"])
        self.assertEqual(superio["Children"][0]["Children"][0]["Value"], "1250 RPM")
        speeds = computer["Children"][4]["Children"][0]["Children"]
        self.assertEqual([s["Value"] for s in speeds], ["2.0 MB/s", "0.5 KB/s", "1Gbps"])
        self.assertEqual(speeds[0]["RawValue"], "2097152.0 B/s")
        ids = []
        def walk(node):
            ids.append(node["id"])
            for child in node["Children"]:
                walk(child)
        walk(tree)
        self.assertEqual(ids, list(range(len(ids))))
        json.dumps(tree)

    def test_parser_reads_built_tree(self):
        with patch("hwmonitor.sensors.nvidia.cards", return_value=[]):
            data = reading.read(source=Mock(sample=Mock(return_value=lhm.build_tree(self.machine()))))
        self.assertFalse(data["error"])
        self.assertEqual((data["cpu_temp"], data["cpu_load"], data["cpu_fan"]), (66, 12.3, 1250))
        self.assertEqual(data["case_fans"], [("Fan #2", 900)])
        self.assertAlmostEqual(data["mem_used"] / 1024 ** 3, 33.3)
        self.assertAlmostEqual(data["gpu_mem_load"], 4074.4 / 8188 * 100, places=3)
        self.assertEqual((data["net_up"], data["net_down"]), (2 * 1024 ** 2, 0.5 * 1024))
        self.assertEqual((data["gpu_id"], data["gpu_temp"]), ("/gpu-nvidia/0", 43))

    def test_value_formats(self):
        self.assertEqual(lhm.format_value("Temperature", "x", None), "-")
        self.assertEqual(lhm.format_value("Voltage", "x", 1.23456), "1.235 V")
        self.assertEqual(lhm.format_value("Throughput", "Connection Speed", 100000000), "100Mbps")
        self.assertEqual(lhm.format_value("Throughput", "Connection Speed", 2500000000), "2.3 Gbps")
        self.assertEqual(lhm.format_value("TimeSpan", "x", 3725), "1:02:05")

    def test_open_failure_is_retried_later_and_reopen_closes_first(self):
        source = lhm.Sensors(Path("missing"))
        with patch("hwmonitor.sensors.lhm._load", side_effect=lhm.Unavailable("no library")) as load, self.assertRaises(lhm.Unavailable):
            source.sample()
        with patch("hwmonitor.sensors.lhm._load") as load, self.assertRaises(lhm.Unavailable):
            source.sample()
        load.assert_not_called()  # 失败后 RETRY 秒内不反复初始化
        computer = Mock(Hardware=self.machine())
        with patch("hwmonitor.sensors.lhm._load", return_value=Mock(return_value=computer)):
            source.failed_at = time.monotonic() - lhm.RETRY - 1
            tree = source.sample()
            self.assertIs(source.last_tree, tree)
            computer.Open.assert_called_once()
            self.assertTrue(all(getattr(computer, name) is True for name in lhm.ENABLED))
            source.request_reopen()
            source.sample()
        computer.Close.assert_called_once()
        self.assertEqual(computer.Open.call_count, 2)
        source.close()
        self.assertIsNone(source.computer)

    def test_one_device_update_error_does_not_stop_sampling(self):
        machine = self.machine()
        machine[0].Update.side_effect = RuntimeError("unplugged")
        source = lhm.Sensors(Path("missing"))
        with patch("hwmonitor.sensors.lhm._load", return_value=Mock(return_value=Mock(Hardware=machine))):
            tree = source.sample()
        self.assertEqual(len(tree["Children"][0]["Children"]), 5)
        machine[2].Update.assert_called_once()

    def test_collector_publishes_and_closes_source(self):
        source = Mock(ready=False, last_tree={"Text": "Sensor"})
        web = Mock()
        worker = Collector(settings.validate({"history_enabled": False}), source, web)
        with patch("hwmonitor.core.collector.HistoryStore") as store, patch("hwmonitor.core.collector.reading.read", return_value={"sensors_down": False}) as read, patch.object(worker.wake, "wait", side_effect=lambda _: worker.stop()):
            store.return_value.load.return_value = []
            worker.start()
            worker.join(timeout=3)
        self.assertIs(read.call_args.args[2], source)
        web.publish.assert_called_once_with({"Text": "Sensor"})
        source.close.assert_called_once()
        worker.reload_hardware()
        source.request_reopen.assert_called_once()
