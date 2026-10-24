from __future__ import annotations

import time
from pathlib import Path

from turzx_monitor.config import AppConfig
from turzx_monitor.energy import EnergyAccumulator
from turzx_monitor.metrics import MetricsCollector
from turzx_monitor.render import ScreenRenderer


def render_preview(config: AppConfig, path: str | Path, now: float | None = None) -> Path:
    timestamp = time.time() if now is None else now
    collector = MetricsCollector(config.monitor.gpus)
    sample = collector.sample(timestamp)
    reading = EnergyAccumulator(config.energy).reading

    renderer = ScreenRenderer(
        gauges=config.monitor.gauges,
        gpu_indices=config.monitor.gpus,
    )
    frame = renderer.render_frame(sample, reading, timestamp)

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.save(destination)
    return destination
