import io
import unittest
from unittest import mock

from PIL import Image

from turzx_monitor.energy import EnergyReading
from turzx_monitor.metrics import GpuMetrics, MemoryMetrics, MetricsSample
from turzx_monitor.render import (
    CELL_BACKGROUND,
    CELL_HEIGHT,
    CELL_WIDTH,
    DEFAULT_TITLE,
    GREEN,
    HEADER_HEIGHT,
    HEIGHT,
    RED,
    TRACK,
    WIDTH,
    YELLOW,
    Cell,
    ScreenRenderer,
    format_energy,
    format_gib,
    format_memory,
    gradient_color,
    to_rgb565,
)

GIB = 1024 * 1024 * 1024


def make_sample(
    cpu: float = 12.5,
    gpus: tuple[GpuMetrics, ...] | None = None,
    temperature: float | None = 55.0,
) -> MetricsSample:
    return MetricsSample(
        timestamp=1_000_000.0,
        cpu_percent=cpu,
        ram=MemoryMetrics(percent=25.0, used_bytes=8 * GIB, total_bytes=32 * GIB),
        swap=MemoryMetrics(percent=2.0, used_bytes=0, total_bytes=8 * GIB),
        gpus=gpus
        if gpus is not None
        else (
            GpuMetrics(
                index=0,
                utilization_percent=75.0,
                memory_used_bytes=12 * GIB,
                memory_total_bytes=24 * GIB,
                temperature_c=57.0,
                power_w=237.0,
            ),
            GpuMetrics(
                index=1,
                utilization_percent=40.0,
                memory_used_bytes=6 * GIB,
                memory_total_bytes=24 * GIB,
                temperature_c=50.0,
                power_w=100.0,
            ),
        ),
        cpu_temp_c=temperature,
    )


class GradientTest(unittest.TestCase):
    def test_anchors(self):
        self.assertEqual(gradient_color(0), GREEN)
        self.assertEqual(gradient_color(50), YELLOW)
        self.assertEqual(gradient_color(100), RED)

    def test_clamps(self):
        self.assertEqual(gradient_color(-10), GREEN)
        self.assertEqual(gradient_color(180), RED)


class FormattingTest(unittest.TestCase):
    def test_gib(self):
        self.assertEqual(format_gib(None), "--")
        self.assertEqual(format_gib(0), "0.0")
        self.assertEqual(format_gib(2 * GIB), "2.0")
        self.assertEqual(format_gib(150 * GIB), "150")

    def test_memory(self):
        memory = MemoryMetrics(percent=0.0, used_bytes=2 * GIB, total_bytes=32 * GIB)
        self.assertEqual(format_memory(memory), "2.0/32.0 GiB")

    def test_energy(self):
        self.assertEqual(format_energy(0.0), ("0.0", "Wh"))
        self.assertEqual(format_energy(99.94), ("99.9", "Wh"))
        self.assertEqual(format_energy(150.0), ("150", "Wh"))
        self.assertEqual(format_energy(1999.0), ("2.00", "kWh"))


class LayoutTest(unittest.TestCase):
    def test_default_layout(self):
        renderer = ScreenRenderer()
        rects = renderer.layout()
        self.assertEqual(list(rects), ["header", "cpu", "ram", "swap", "gpu0", "gpu1", "energy"])
        self.assertEqual(rects["header"].box, (0, 0, WIDTH - 1, HEADER_HEIGHT - 1))
        self.assertEqual(rects["cpu"].box, (0, HEADER_HEIGHT, CELL_WIDTH - 1, HEADER_HEIGHT + CELL_HEIGHT - 1))
        self.assertEqual(rects["energy"].box, (160, 328, 319, 478))

        for key, rect in rects.items():
            self.assertGreaterEqual(rect.x, 0, key)
            self.assertGreaterEqual(rect.y, 0, key)
            self.assertLessEqual(rect.x1, WIDTH - 1, key)
            self.assertLessEqual(rect.y1, HEIGHT - 1, key)

    def test_custom_gauges_keep_energy_last(self):
        renderer = ScreenRenderer(gauges=("cpu", "ram", "gpu0"), gpu_indices=(0, 1))
        rects = renderer.layout()
        self.assertEqual(list(rects)[-2:], ["gpu0", "energy"])
        self.assertEqual(rects["energy"].box, (160, HEADER_HEIGHT + CELL_HEIGHT, 319, HEADER_HEIGHT + 2 * CELL_HEIGHT - 1))


class HeaderTest(unittest.TestCase):
    def test_default_title(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ScreenRenderer().title, DEFAULT_TITLE)

    def test_title_from_environment(self):
        with mock.patch.dict("os.environ", {"MONITOR_NAME": "HOMELAB"}):
            self.assertEqual(ScreenRenderer().title, "HOMELAB")

    def test_explicit_title_wins_over_environment(self):
        with mock.patch.dict("os.environ", {"MONITOR_NAME": "HOMELAB"}):
            self.assertEqual(ScreenRenderer(title="NAS").title, "NAS")

    def test_blank_title_falls_back_to_default(self):
        with mock.patch.dict("os.environ", {"MONITOR_NAME": "   "}):
            self.assertEqual(ScreenRenderer().title, DEFAULT_TITLE)

    def test_long_title_is_clipped(self):
        renderer = ScreenRenderer(title="NOME MUITO COMPRIDO QUE NAO CABE NO CABECALHO")
        image = renderer.render_header(now=1_000_000.0)
        self.assertEqual(image.size, (WIDTH, HEADER_HEIGHT))


class CellTest(unittest.TestCase):
    def test_build_cells(self):
        renderer = ScreenRenderer()
        cells = renderer.build_cells(make_sample(), EnergyReading(1500.0, 24000.0))

        self.assertEqual(list(cells), ["cpu", "ram", "swap", "gpu0", "gpu1", "energy"])
        self.assertEqual(cells["cpu"].percent, 12.5)
        self.assertEqual(cells["cpu"].detail, "55 C")
        self.assertEqual(cells["ram"].detail, "8.0/32.0 GiB")
        self.assertEqual(cells["gpu0"].label, "GPU 0")
        self.assertEqual(cells["gpu0"].percent, 75.0)
        self.assertEqual(cells["gpu0"].detail, "12.0/24.0 GiB")
        self.assertEqual(cells["gpu0"].caption, "57 C 237 W")
        self.assertEqual(cells["energy"].energy.day_wh, 1500.0 / 3600.0)

    def test_gpu_mapping_follows_configured_indices(self):
        renderer = ScreenRenderer(gauges=("gpu0", "gpu1"), gpu_indices=(1, 0))
        cells = renderer.build_cells(make_sample())
        self.assertEqual(cells["gpu0"].percent, 40.0)
        self.assertEqual(cells["gpu1"].percent, 75.0)

    def test_missing_gpu_and_temperature(self):
        renderer = ScreenRenderer()
        cells = renderer.build_cells(make_sample(temperature=None, gpus=()))
        self.assertIsNone(cells["cpu"].detail or None)
        self.assertIsNone(cells["gpu0"].percent)
        self.assertEqual(cells["gpu0"].detail, "")
        self.assertEqual(cells["gpu0"].caption, "")


class RenderingTest(unittest.TestCase):
    def test_frame_size_and_mode(self):
        frame = ScreenRenderer().render_frame(make_sample(), EnergyReading(1.0, 2.0), now=1_000_000.0)
        self.assertEqual(frame.size, (WIDTH, HEIGHT))
        self.assertEqual(frame.mode, "RGB")

        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        self.assertGreater(len(buffer.getvalue()), 1000)

    def test_cell_size(self):
        renderer = ScreenRenderer()
        cell = renderer.build_cells(make_sample())["cpu"]
        self.assertEqual(renderer.render_cell(cell).size, (CELL_WIDTH, CELL_HEIGHT))

    def test_gauge_arc_colors(self):
        renderer = ScreenRenderer()
        full = renderer.render_cell(Cell(key="cpu", kind="gauge", label="CPU", percent=100.0))
        self.assertEqual(full.getpixel((80, 28)), RED)

        empty = renderer.render_cell(Cell(key="cpu", kind="gauge", label="CPU", percent=0.0))
        self.assertEqual(empty.getpixel((80, 28)), TRACK)
        self.assertEqual(empty.getpixel((80, 116)), CELL_BACKGROUND)

    def test_energy_cell_renders(self):
        renderer = ScreenRenderer()
        cell = renderer.build_cells(make_sample(), EnergyReading(1_800_000.0, 5_400_000.0))["energy"]
        image = renderer.render_cell(cell)
        self.assertEqual(image.size, (CELL_WIDTH, CELL_HEIGHT))


class Rgb565Test(unittest.TestCase):
    def test_known_colors(self):
        self.assertEqual(to_rgb565(Image.new("RGB", (1, 1), (0, 0, 0))), b"\x00\x00")
        self.assertEqual(to_rgb565(Image.new("RGB", (1, 1), (255, 255, 255))), b"\xff\xff")
        self.assertEqual(to_rgb565(Image.new("RGB", (1, 1), (255, 0, 0))), b"\x00\xf8")
        self.assertEqual(to_rgb565(Image.new("RGB", (1, 1), (0, 255, 0))), b"\xe0\x07")
        self.assertEqual(to_rgb565(Image.new("RGB", (1, 1), (0, 0, 255))), b"\x1f\x00")

    def test_frame_length(self):
        frame = ScreenRenderer().render_frame(make_sample(), EnergyReading(0.0, 0.0), now=1_000_000.0)
        self.assertEqual(len(to_rgb565(frame)), WIDTH * HEIGHT * 2)


if __name__ == "__main__":
    unittest.main()
