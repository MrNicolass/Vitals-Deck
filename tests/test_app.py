import unittest
from dataclasses import replace

from turzx_monitor.app import MonitorApp
from turzx_monitor.config import (
    AppConfig,
    DisplayConfig,
    EnergyConfig,
    LoggingConfig,
    MonitorConfig,
)
from turzx_monitor.energy import EnergyReading
from turzx_monitor.metrics import GpuMetrics, MemoryMetrics, MetricsSample
from turzx_monitor.protocol import ScreenError, ScreenModel

GIB = 1024 * 1024 * 1024


def make_sample(cpu: float = 12.5) -> MetricsSample:
    return MetricsSample(
        timestamp=1_000_000.0,
        cpu_percent=cpu,
        ram=MemoryMetrics(25.0, 8 * GIB, 32 * GIB),
        swap=MemoryMetrics(2.0, 0, 8 * GIB),
        gpus=(
            GpuMetrics(index=0, utilization_percent=75.0, power_w=200.0),
            GpuMetrics(index=1, utilization_percent=40.0, power_w=100.0),
        ),
        cpu_temp_c=55.0,
    )


def make_config() -> AppConfig:
    return AppConfig(
        display=DisplayConfig(
            port="loop://",
            brightness=7,
            full_refresh_interval=300.0,
            reconnect_delay=5.0,
        ),
        monitor=MonitorConfig(interval=2.0, gpus=(0, 1)),
        energy=EnergyConfig(state_file="/tmp/turzx-monitor-test/energy.json"),
        logging=LoggingConfig(),
    )


class FakeScreen:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.open_count = 0
        self.close_count = 0
        self.on_bitmap = None

    def open(self) -> None:
        self.open_count += 1

    def close(self) -> None:
        self.close_count += 1

    def handshake(self) -> ScreenModel:
        self.calls.append(("handshake",))
        return ScreenModel.USBMONITOR_3_5

    def set_orientation(self, orientation) -> None:
        self.calls.append(("orientation", orientation))

    def set_brightness(self, level: int) -> None:
        self.calls.append(("brightness", level))

    def clear(self) -> None:
        self.calls.append(("clear",))

    def display_bitmap(self, x: int, y: int, width: int, height: int, data: bytes) -> None:
        self.calls.append(("bitmap", x, y, width, height, len(data)))
        if self.on_bitmap is not None:
            self.on_bitmap()


class FlakyScreen(FakeScreen):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def open(self) -> None:
        if self.failures:
            self.failures -= 1
            raise ScreenError("falha simulada")
        super().open()


class FakeCollector:
    def __init__(self) -> None:
        self.calls = 0

    def sample(self, now=None) -> MetricsSample:
        self.calls += 1
        return make_sample()


class FakeAccumulator:
    def __init__(self) -> None:
        self.reading = EnergyReading(0.0, 0.0)
        self.updates = 0

    def update(self, now=None, gpu_power_w: float = 0.0) -> EnergyReading:
        self.updates += 1
        self.reading = EnergyReading(100.0 * self.updates, 1000.0 * self.updates)
        return self.reading


class DrawTest(unittest.TestCase):
    def setUp(self):
        self.screen = FakeScreen()
        self.collector = FakeCollector()
        self.accumulator = FakeAccumulator()
        self.app = MonitorApp(
            make_config(),
            screen=self.screen,
            collector=self.collector,
            accumulator=self.accumulator,
            sleep=lambda _: None,
        )
        self.sample = make_sample()
        self.reading = EnergyReading(0.0, 0.0)

    def bitmaps(self):
        return [call for call in self.screen.calls if call[0] == "bitmap"]

    def test_first_draw_is_full_refresh(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.assertEqual(self.bitmaps(), [("bitmap", 0, 0, 320, 480, 320 * 480 * 2)])

    def test_unchanged_frame_sends_nothing(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.screen.calls.clear()
        self.app._draw(self.sample, self.reading, 1_000_010.0)
        self.assertEqual(self.bitmaps(), [])

    def test_changed_cell_is_sent_alone(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.screen.calls.clear()
        self.app._draw(replace(self.sample, cpu_percent=90.0), self.reading, 1_000_010.0)
        self.assertEqual(self.bitmaps(), [("bitmap", 0, 26, 160, 151, 160 * 151 * 2)])

    def test_changed_energy_is_sent_alone(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.screen.calls.clear()
        self.app._draw(self.sample, EnergyReading(1500.0, 24000.0), 1_000_010.0)
        self.assertEqual(self.bitmaps(), [("bitmap", 160, 328, 160, 151, 160 * 151 * 2)])

    def test_header_updates_on_new_minute(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.screen.calls.clear()
        self.app._draw(self.sample, self.reading, 1_000_060.0)
        self.assertEqual(self.bitmaps(), [("bitmap", 0, 0, 320, 26, 320 * 26 * 2)])

    def test_periodic_full_refresh(self):
        self.app._draw(self.sample, self.reading, 1_000_000.0)
        self.screen.calls.clear()
        self.app._draw(self.sample, self.reading, 1_000_310.0)
        self.assertEqual(self.bitmaps(), [("bitmap", 0, 0, 320, 480, 320 * 480 * 2)])


class SessionTest(unittest.TestCase):
    def test_session_initializes_screen_and_draws(self):
        screen = FakeScreen()
        collector = FakeCollector()
        accumulator = FakeAccumulator()
        app = MonitorApp(
            make_config(),
            screen=screen,
            collector=collector,
            accumulator=accumulator,
            sleep=lambda _: None,
        )
        screen.on_bitmap = app.stop

        app._session()

        self.assertEqual(
            [call[0] for call in screen.calls],
            ["handshake", "orientation", "brightness", "clear", "bitmap"],
        )
        self.assertEqual(screen.calls[1][1], 0)
        self.assertEqual(screen.calls[2][1], 7)
        self.assertEqual(screen.calls[-1], ("bitmap", 0, 0, 320, 480, 320 * 480 * 2))
        self.assertEqual(collector.calls, 1)
        self.assertEqual(accumulator.updates, 1)

    def test_run_reconnects_after_screen_error(self):
        screen = FlakyScreen(failures=1)
        delays: list[float] = []
        holder: dict[str, MonitorApp] = {}

        def sleep(delay: float) -> None:
            delays.append(delay)
            holder["app"].stop()

        app = MonitorApp(
            make_config(),
            screen=screen,
            collector=FakeCollector(),
            accumulator=FakeAccumulator(),
            sleep=sleep,
        )
        holder["app"] = app

        app.run()

        self.assertEqual(delays, [5.0])
        self.assertEqual(screen.close_count, 1)
        self.assertEqual(app._cache, {})

    def test_run_closes_screen_on_stop(self):
        screen = FakeScreen()
        app = MonitorApp(
            make_config(),
            screen=screen,
            collector=FakeCollector(),
            accumulator=FakeAccumulator(),
            sleep=lambda _: None,
        )
        screen.on_bitmap = app.stop

        app.run()

        self.assertEqual(screen.open_count, 1)
        self.assertEqual(screen.close_count, 1)


if __name__ == "__main__":
    unittest.main()
