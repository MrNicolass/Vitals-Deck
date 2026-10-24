from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from turzx_monitor.config import GAUGES
from turzx_monitor.energy import EnergyReading
from turzx_monitor.metrics import GpuMetrics, MemoryMetrics, MetricsSample

WIDTH = 320
HEIGHT = 480
HEADER_HEIGHT = 26
CELL_WIDTH = 160
CELL_HEIGHT = 151
CELL_COUNT = 6
SUPERSAMPLE = 2

BACKGROUND = (8, 10, 14)
CELL_BACKGROUND = (22, 25, 31)
CELL_BORDER = (38, 43, 52)
TRACK = (44, 49, 58)
TEXT = (236, 239, 244)
TEXT_DIM = (146, 154, 166)
GREEN = (46, 204, 113)
YELLOW = (241, 196, 15)
RED = (231, 76, 60)

GAUGE_START_ANGLE = 135
GAUGE_SWEEP = 270

TITLE_ENV = "MONITOR_NAME"
DEFAULT_TITLE = "XSERVER"

FONT_DIRECTORIES = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
)
FONT_NAMES = {
    False: ("DejaVuSans.ttf", "LiberationSans-Regular.ttf"),
    True: ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"),
}
GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def x1(self) -> int:
        return self.x + self.width - 1

    @property
    def y1(self) -> int:
        return self.y + self.height - 1

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x1, self.y1)


@dataclass(frozen=True)
class Cell:
    key: str
    kind: str
    label: str = ""
    percent: float | None = None
    detail: str = ""
    caption: str = ""
    energy: EnergyReading | None = None


def clamp_percent(percent: float) -> float:
    return max(0.0, min(100.0, float(percent)))


def gradient_color(percent: float) -> tuple[int, int, int]:
    value = clamp_percent(percent)
    if value <= 50.0:
        low, high, ratio = GREEN, YELLOW, value / 50.0
    else:
        low, high, ratio = YELLOW, RED, (value - 50.0) / 50.0
    return tuple(round(a + (b - a) * ratio) for a, b in zip(low, high))


def format_percent(value: float | None) -> str:
    return "--%" if value is None else f"{clamp_percent(value):.0f}%"


def format_gib(value: int | None) -> str:
    if value is None:
        return "--"
    amount = value / GIB
    return f"{amount:.0f}" if amount >= 100 else f"{amount:.1f}"


def format_memory(memory: MemoryMetrics | None) -> str:
    if memory is None:
        return ""
    return f"{format_gib(memory.used_bytes)}/{format_gib(memory.total_bytes)} GiB"


def format_energy(watt_hours: float) -> tuple[str, str]:
    if watt_hours >= 1000.0:
        return f"{watt_hours / 1000.0:.2f}", "kWh"
    if watt_hours >= 100.0:
        return f"{watt_hours:.0f}", "Wh"
    return f"{watt_hours:.1f}", "Wh"


def to_rgb565(image: Image.Image) -> bytes:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint32)
    red = (rgb[..., 0] >> 3) << 11
    green = (rgb[..., 1] >> 2) << 5
    blue = rgb[..., 2] >> 3
    return np.ascontiguousarray(red | green | blue).astype("<u2").tobytes()


@lru_cache(maxsize=64)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONT_NAMES[bold]:
        for directory in FONT_DIRECTORIES:
            path = directory / name
            if path.is_file():
                return ImageFont.truetype(str(path), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def default_title() -> str:
    value = os.environ.get(TITLE_ENV, "").strip()
    return value or DEFAULT_TITLE


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: float,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > max_width:
        trimmed = trimmed[:-1]
    return f"{trimmed}{ellipsis}" if trimmed else ""


def _draw_arc(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    start: float,
    end: float,
    fill: tuple[int, int, int],
    width: int,
) -> None:
    while end > 360.0:
        draw.arc(box, start=start, end=360.0, fill=fill, width=width)
        start = 0.0
        end -= 360.0
    if end > start:
        draw.arc(box, start=start, end=end, fill=fill, width=width)


class ScreenRenderer:
    def __init__(
        self,
        gauges: Sequence[str] = GAUGES,
        gpu_indices: Sequence[int] = (0, 1),
        *,
        width: int = WIDTH,
        height: int = HEIGHT,
        title: str | None = None,
    ) -> None:
        if width % 2 or height < HEADER_HEIGHT + 3 * CELL_HEIGHT:
            raise ValueError(f"dimensões incompatíveis com o layout: {width}x{height}")
        self.gauges = tuple(gauges)
        self.gpu_indices = tuple(gpu_indices)
        self.title = (title if title is not None else default_title()).strip() or DEFAULT_TITLE
        self.width = width
        self.height = height
        self._rects = self._build_layout()

    def _build_layout(self) -> dict[str, Rect]:
        rects = {"header": Rect(0, 0, self.width, HEADER_HEIGHT)}
        for index, key in enumerate((*self.gauges, "energy")):
            column = index % 2
            row = index // 2
            rects[key] = Rect(
                column * CELL_WIDTH,
                HEADER_HEIGHT + row * CELL_HEIGHT,
                CELL_WIDTH,
                CELL_HEIGHT,
            )
        return rects

    def layout(self) -> dict[str, Rect]:
        return dict(self._rects)

    def build_cells(self, sample: MetricsSample, energy: EnergyReading | None = None) -> dict[str, Cell]:
        cells: dict[str, Cell] = {}
        for gauge in self.gauges:
            if gauge == "cpu":
                detail = ""
                if sample.cpu_temp_c is not None:
                    detail = f"{sample.cpu_temp_c:.0f} C"
                cells[gauge] = Cell(
                    key=gauge,
                    kind="gauge",
                    label="CPU",
                    percent=sample.cpu_percent,
                    detail=detail,
                )
            elif gauge == "ram":
                cells[gauge] = Cell(
                    key=gauge,
                    kind="gauge",
                    label="RAM",
                    percent=sample.ram.percent,
                    detail=format_memory(sample.ram),
                )
            elif gauge == "swap":
                cells[gauge] = Cell(
                    key=gauge,
                    kind="gauge",
                    label="SWAP",
                    percent=sample.swap.percent,
                    detail=format_memory(sample.swap),
                )
            elif gauge in ("gpu0", "gpu1"):
                cells[gauge] = self._gpu_cell(gauge, sample)
            else:
                raise ValueError(f"gauge desconhecido: {gauge!r}")

        cells["energy"] = Cell(
            key="energy",
            kind="energy",
            label="ENERGY",
            caption="CPU+GPU",
            energy=energy if energy is not None else EnergyReading(0.0, 0.0),
        )
        return cells

    def _gpu_cell(self, gauge: str, sample: MetricsSample) -> Cell:
        position = 0 if gauge == "gpu0" else 1
        index = self.gpu_indices[position] if position < len(self.gpu_indices) else position
        gpu = next((item for item in sample.gpus if item.index == index), None)
        if gpu is None:
            gpu = GpuMetrics(index=index)

        memory = None
        if gpu.memory_used_bytes is not None and gpu.memory_total_bytes is not None:
            memory = MemoryMetrics(
                percent=0.0,
                used_bytes=gpu.memory_used_bytes,
                total_bytes=gpu.memory_total_bytes,
            )

        caption_parts = []
        if gpu.temperature_c is not None:
            caption_parts.append(f"{gpu.temperature_c:.0f} C")
        if gpu.power_w is not None:
            caption_parts.append(f"{gpu.power_w:.0f} W")

        return Cell(
            key=gauge,
            kind="gauge",
            label=f"GPU {position}",
            percent=gpu.utilization_percent,
            detail=format_memory(memory),
            caption=" ".join(caption_parts),
        )

    def render_header(self, now: float | None = None) -> Image.Image:
        timestamp = time.time() if now is None else now
        scale = SUPERSAMPLE
        image = Image.new("RGB", (WIDTH * scale, HEADER_HEIGHT * scale), BACKGROUND)
        draw = ImageDraw.Draw(image)

        title_font = _font(13 * scale, True)
        clock_font = _font(11 * scale, False)
        clock = time.strftime("%d/%m %H:%M", time.localtime(timestamp))

        left = 8 * scale
        right = (WIDTH - 8) * scale
        max_title = right - left - draw.textlength(clock, font=clock_font) - 12 * scale

        draw.text(
            (left, 6 * scale),
            _fit_text(draw, self.title, title_font, max_title),
            font=title_font,
            fill=TEXT,
            anchor="la",
        )
        draw.text(
            (right, 7 * scale),
            clock,
            font=clock_font,
            fill=TEXT_DIM,
            anchor="ra",
        )
        draw.line((0, (HEADER_HEIGHT - 1) * scale, WIDTH * scale, (HEADER_HEIGHT - 1) * scale), fill=CELL_BORDER)

        return image.resize((WIDTH, HEADER_HEIGHT), Image.LANCZOS)

    def render_cell(self, cell: Cell) -> Image.Image:
        scale = SUPERSAMPLE
        image = Image.new("RGB", (CELL_WIDTH * scale, CELL_HEIGHT * scale), BACKGROUND)
        draw = ImageDraw.Draw(image)

        draw.rounded_rectangle(
            (2 * scale, 2 * scale, (CELL_WIDTH - 2) * scale - 1, (CELL_HEIGHT - 2) * scale - 1),
            radius=9 * scale,
            fill=CELL_BACKGROUND,
            outline=CELL_BORDER,
            width=scale,
        )

        if cell.kind == "gauge":
            self._draw_gauge(draw, cell, scale)
        elif cell.kind == "energy":
            self._draw_energy(draw, cell, scale)

        return image.resize((CELL_WIDTH, CELL_HEIGHT), Image.LANCZOS)

    def _draw_gauge(self, draw: ImageDraw.ImageDraw, cell: Cell, scale: int) -> None:
        draw.text(
            (10 * scale, 7 * scale),
            cell.label,
            font=_font(13 * scale, True),
            fill=TEXT,
            anchor="la",
        )
        if cell.caption:
            draw.text(
                ((CELL_WIDTH - 10) * scale, 10 * scale),
                cell.caption,
                font=_font(9 * scale, False),
                fill=TEXT_DIM,
                anchor="ra",
            )

        center_x, center_y, radius = 80, 72, 44
        box = (
            (center_x - radius - 5) * scale,
            (center_y - radius - 5) * scale,
            (center_x + radius + 5) * scale,
            (center_y + radius + 5) * scale,
        )
        _draw_arc(
            draw,
            box,
            GAUGE_START_ANGLE,
            GAUGE_START_ANGLE + GAUGE_SWEEP,
            TRACK,
            9 * scale,
        )
        if cell.percent is not None and cell.percent > 0:
            color = gradient_color(cell.percent)
            end = GAUGE_START_ANGLE + GAUGE_SWEEP * clamp_percent(cell.percent) / 100.0
            _draw_arc(draw, box, GAUGE_START_ANGLE, end, color, 9 * scale)

        value_color = TEXT
        if cell.percent is not None:
            value_color = gradient_color(cell.percent)
        draw.text(
            (center_x * scale, center_y * scale),
            format_percent(cell.percent),
            font=_font(25 * scale, True),
            fill=value_color,
            anchor="mm",
        )
        if cell.detail:
            draw.text(
                (center_x * scale, 132 * scale),
                cell.detail,
                font=_font(10 * scale, False),
                fill=TEXT_DIM,
                anchor="mm",
            )

    def _draw_energy(self, draw: ImageDraw.ImageDraw, cell: Cell, scale: int) -> None:
        reading = cell.energy or EnergyReading(0.0, 0.0)

        draw.text(
            (10 * scale, 7 * scale),
            cell.label,
            font=_font(13 * scale, True),
            fill=TEXT,
            anchor="la",
        )
        if cell.caption:
            draw.text(
                ((CELL_WIDTH - 10) * scale, 10 * scale),
                cell.caption,
                font=_font(9 * scale, False),
                fill=TEXT_DIM,
                anchor="ra",
            )

        for label, value, offset in (
            ("TODAY", reading.day_wh, 42),
            ("MONTH", reading.month_wh, 92),
        ):
            draw.text(
                (14 * scale, offset * scale),
                label,
                font=_font(9 * scale, False),
                fill=TEXT_DIM,
                anchor="la",
            )
            number, unit = format_energy(value)
            number_width = draw.textlength(number, font=_font(21 * scale, True))
            draw.text(
                (14 * scale, (offset + 11) * scale),
                number,
                font=_font(21 * scale, True),
                fill=TEXT,
                anchor="la",
            )
            draw.text(
                (14 * scale + number_width + 3 * scale, (offset + 22) * scale),
                unit,
                font=_font(10 * scale, False),
                fill=TEXT_DIM,
                anchor="la",
            )

        draw.line(
            (14 * scale, 86 * scale, (CELL_WIDTH - 14) * scale, 86 * scale),
            fill=CELL_BORDER,
        )

    def compose(self, header: Image.Image, cells: dict[str, Image.Image]) -> Image.Image:
        frame = Image.new("RGB", (self.width, self.height), BACKGROUND)
        frame.paste(header, (0, 0))
        rects = self._rects
        for key, image in cells.items():
            rect = rects[key]
            frame.paste(image, (rect.x, rect.y))
        return frame

    def render_frame(
        self,
        sample: MetricsSample,
        energy: EnergyReading | None = None,
        now: float | None = None,
    ) -> Image.Image:
        header = self.render_header(now)
        cells = {
            key: self.render_cell(cell)
            for key, cell in self.build_cells(sample, energy).items()
        }
        return self.compose(header, cells)
