import sys
import tempfile
import unittest
from pathlib import Path

from turzx_monitor.metrics import (
    GpuMetrics,
    MetricsCollector,
    MetricsError,
    cpu_percent,
    memory_metrics,
    parse_meminfo,
    parse_nvidia_smi,
    parse_proc_stat,
    read_cpu_temperature,
)

PROC_STAT = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n"

MEMINFO = """MemTotal:       1000 kB
MemFree:         100 kB
MemAvailable:    400 kB
SwapTotal:       200 kB
SwapFree:         50 kB
"""

NVIDIA_LINE = "0, NVIDIA GeForce RTX 3090, 81, 19929, 24576, 79, 237.27"


class ParseTest(unittest.TestCase):
    def test_parse_proc_stat(self):
        self.assertEqual(parse_proc_stat(PROC_STAT), (1000, 800))

    def test_parse_proc_stat_missing(self):
        with self.assertRaises(ValueError):
            parse_proc_stat("intr 1 2 3\n")

    def test_cpu_percent(self):
        self.assertAlmostEqual(cpu_percent((100, 50), (200, 60)), 90.0)

    def test_cpu_percent_without_delta(self):
        self.assertEqual(cpu_percent((100, 50), (100, 50)), 0.0)

    def test_cpu_percent_is_clamped(self):
        self.assertEqual(cpu_percent((100, 100), (200, 50)), 100.0)

    def test_parse_meminfo(self):
        meminfo = parse_meminfo(MEMINFO)
        self.assertEqual(meminfo["MemTotal"], 1000 * 1024)
        self.assertEqual(meminfo["SwapFree"], 50 * 1024)

    def test_memory_metrics(self):
        ram = memory_metrics(1000 * 1024, 400 * 1024)
        self.assertAlmostEqual(ram.percent, 60.0)
        self.assertEqual(ram.used_bytes, 600 * 1024)
        swap = memory_metrics(200 * 1024, 50 * 1024)
        self.assertAlmostEqual(swap.percent, 75.0)

    def test_memory_metrics_zero_total(self):
        ram = memory_metrics(0, 0)
        self.assertEqual(ram.percent, 0.0)
        self.assertEqual(ram.used_bytes, 0)

    def test_parse_nvidia_smi(self):
        gpus = parse_nvidia_smi(NVIDIA_LINE)
        self.assertEqual(len(gpus), 1)
        gpu = gpus[0]
        self.assertEqual(gpu.index, 0)
        self.assertEqual(gpu.name, "NVIDIA GeForce RTX 3090")
        self.assertAlmostEqual(gpu.utilization_percent, 81.0)
        self.assertEqual(gpu.memory_used_bytes, 19929 * 1024 * 1024)
        self.assertEqual(gpu.memory_total_bytes, 24576 * 1024 * 1024)
        self.assertAlmostEqual(gpu.temperature_c, 79.0)
        self.assertAlmostEqual(gpu.power_w, 237.27)

    def test_parse_nvidia_smi_unavailable(self):
        gpus = parse_nvidia_smi(
            "1, NVIDIA GeForce RTX 3090, [N/A], 0, 24576, 78, [Not Supported]"
        )
        self.assertIsNone(gpus[0].utilization_percent)
        self.assertIsNone(gpus[0].power_w)
        self.assertEqual(gpus[0].memory_total_bytes, 24576 * 1024 * 1024)

    def test_parse_nvidia_smi_invalid_line(self):
        with self.assertRaises(ValueError):
            parse_nvidia_smi("0, GPU")


class TemperatureTest(unittest.TestCase):
    def write_zone(self, root: Path, name: str, zone_type: str, temp_milli: int) -> None:
        zone = root / name
        zone.mkdir()
        (zone / "type").write_text(zone_type, encoding="utf-8")
        (zone / "temp").write_text(str(temp_milli), encoding="utf-8")

    def test_prefers_cpu_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_zone(root, "thermal_zone0", "acpitz", 42000)
            self.write_zone(root, "thermal_zone1", "x86_pkg_temp", 55000)
            self.assertEqual(read_cpu_temperature(root), 55.0)

    def test_ignores_unknown_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_zone(root, "thermal_zone0", "iwlwifi", 60000)
            self.assertIsNone(read_cpu_temperature(root))

    def test_missing_file(self):
        self.assertIsNone(read_cpu_temperature("/caminho/inexistente"))


class MetricsCollectorTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / "stat").write_text(PROC_STAT, encoding="utf-8")
        (self.proc / "meminfo").write_text(MEMINFO, encoding="utf-8")

        self.thermal = self.root / "thermal"
        self.thermal.mkdir()

        gpu_script = f"print({NVIDIA_LINE!r})"
        self.gpu_command = [sys.executable, "-c", gpu_script]

    def make_collector(self, gpu_indices=(0, 1)):
        return MetricsCollector(
            gpu_indices,
            proc_path=self.proc,
            thermal_path=self.thermal,
            gpu_command=self.gpu_command,
        )

    def test_first_sample_has_no_cpu_history(self):
        sample = self.make_collector().sample(now=100.0)
        self.assertEqual(sample.cpu_percent, 0.0)
        self.assertEqual(sample.timestamp, 100.0)

    def test_sample_reads_memory_and_gpus(self):
        collector = self.make_collector()
        sample = collector.sample(now=100.0)

        self.assertAlmostEqual(sample.ram.percent, 60.0)
        self.assertEqual(sample.ram.total_bytes, 1000 * 1024)
        self.assertAlmostEqual(sample.swap.percent, 75.0)
        self.assertIsNone(sample.cpu_temp_c)

        self.assertEqual(len(sample.gpus), 2)
        self.assertAlmostEqual(sample.gpus[0].utilization_percent, 81.0)
        self.assertEqual(sample.gpus[0].memory_total_bytes, 24576 * 1024 * 1024)
        self.assertEqual(sample.gpus[1], GpuMetrics(index=1))
        self.assertAlmostEqual(sample.gpu_power_w, 237.27)

    def test_second_sample_computes_cpu_delta(self):
        collector = self.make_collector()
        collector.sample(now=100.0)
        (self.proc / "stat").write_text(
            "cpu  200 0 90 810 0 0 0 0 0 0\n", encoding="utf-8"
        )
        sample = collector.sample(now=102.0)
        self.assertAlmostEqual(sample.cpu_percent, 90.0)

    def test_missing_proc_file_raises(self):
        (self.proc / "stat").unlink()
        with self.assertRaises(MetricsError):
            self.make_collector().sample()

    def test_missing_nvidia_smi_returns_empty_gpus(self):
        collector = MetricsCollector(
            (0, 1),
            proc_path=self.proc,
            thermal_path=self.thermal,
            gpu_command=["/caminho/inexistente/nvidia-smi"],
        )
        sample = collector.sample()
        self.assertEqual(sample.gpus, (GpuMetrics(index=0), GpuMetrics(index=1)))
        self.assertEqual(sample.gpu_power_w, 0.0)


if __name__ == "__main__":
    unittest.main()
