from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Callable

from turzx_monitor.config import AppConfig
from turzx_monitor.energy import EnergyAccumulator, EnergyError, EnergyReading
from turzx_monitor.metrics import MetricsCollector, MetricsError, MetricsSample
from turzx_monitor.protocol import Orientation, ScreenError, TuringScreen
from turzx_monitor.render import ScreenRenderer, to_rgb565

logger = logging.getLogger(__name__)

SleepFunction = Callable[[float], None]


def setup_logging(config: AppConfig) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.logging.file:
        path = Path(config.logging.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


class MonitorApp:
    def __init__(
        self,
        config: AppConfig,
        *,
        screen: TuringScreen | None = None,
        collector: MetricsCollector | None = None,
        accumulator: EnergyAccumulator | None = None,
        renderer: ScreenRenderer | None = None,
        clock: Callable[[], float] = time.time,
        sleep: SleepFunction = time.sleep,
    ) -> None:
        self.config = config
        self.orientation = Orientation.from_name(config.display.orientation)
        self.screen = screen or TuringScreen(
            port=config.display.port,
            baudrate=config.display.baudrate,
            orientation=self.orientation,
        )
        self.collector = collector or MetricsCollector(config.monitor.gpus)
        self.accumulator = accumulator or EnergyAccumulator(config.energy)
        self.renderer = renderer or ScreenRenderer(
            gauges=config.monitor.gauges,
            gpu_indices=config.monitor.gpus,
        )
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()
        self._cache: dict[str, bytes] = {}
        self._last_full = float("-inf")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._install_signal_handlers()
        delay = self.config.display.reconnect_delay

        while not self._stop.is_set():
            try:
                self._session()
            except ScreenError as exc:
                logger.warning("comunicação com a tela falhou: %s", exc)
            finally:
                self.screen.close()

            if self._stop.is_set():
                break
            logger.info("reconectando em %.1f s", delay)
            self._sleep(delay)

    def _session(self) -> None:
        self.screen.open()
        model = self.screen.handshake()
        self.screen.set_orientation(self.orientation)
        self.screen.set_brightness(self.config.display.brightness)
        self.screen.clear()
        logger.info("tela conectada (%s)", getattr(model, "name", model))

        self._cache.clear()
        self._last_full = float("-inf")
        self._loop()

    def _loop(self) -> None:
        sample_interval = self.config.energy.sample_interval
        render_interval = self.config.monitor.interval
        tick = min(sample_interval, render_interval)

        sample: MetricsSample | None = None
        reading: EnergyReading = self.accumulator.reading
        next_sample = float("-inf")
        next_render = float("-inf")

        while not self._stop.is_set():
            now = self._clock()

            if now >= next_sample:
                try:
                    sample = self.collector.sample(now)
                    reading = self.accumulator.update(now, sample.gpu_power_w)
                except (MetricsError, EnergyError) as exc:
                    logger.error("falha na coleta de métricas: %s", exc)
                next_sample = now + sample_interval

            if sample is not None and now >= next_render:
                self._draw(sample, reading, now)
                next_render = now + render_interval

            self._sleep(tick)

    def _draw(self, sample: MetricsSample, reading: EnergyReading, now: float) -> None:
        header = self.renderer.render_header(now)
        cells = {
            key: self.renderer.render_cell(cell)
            for key, cell in self.renderer.build_cells(sample, reading).items()
        }
        rects = self.renderer.layout()

        if now - self._last_full >= self.config.display.full_refresh_interval:
            frame = self.renderer.compose(header, cells)
            self.screen.display_bitmap(0, 0, self.renderer.width, self.renderer.height, to_rgb565(frame))
            self._cache = {key: to_rgb565(image) for key, image in {"header": header, **cells}.items()}
            self._last_full = now
            logger.debug("refresh completo enviado")
            return

        for key, image in {"header": header, **cells}.items():
            data = to_rgb565(image)
            if self._cache.get(key) == data:
                continue
            rect = rects[key]
            self.screen.display_bitmap(rect.x, rect.y, rect.width, rect.height, data)
            self._cache[key] = data
            logger.debug("célula %s atualizada", key)

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info("recebido sinal %s; encerrando", signum)
        self.stop()
