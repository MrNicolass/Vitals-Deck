from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from turzx_monitor.config import EnergyConfig

logger = logging.getLogger(__name__)

JOULES_PER_WH = 3600.0
DEFAULT_MAX_GAP = 60.0
DEFAULT_RAPL_RANGE_UJ = 2**32
MICROJOULES_PER_JOULE = 1_000_000.0


class EnergyError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnergyReading:
    day_joules: float
    month_joules: float

    @property
    def day_wh(self) -> float:
        return self.day_joules / JOULES_PER_WH

    @property
    def month_wh(self) -> float:
        return self.month_joules / JOULES_PER_WH


@dataclass
class _State:
    day: str
    month: str
    day_joules: float = 0.0
    month_joules: float = 0.0
    rapl_uj: int | None = None
    updated: float = 0.0


class EnergyAccumulator:
    def __init__(self, config: EnergyConfig, *, max_gap: float = DEFAULT_MAX_GAP) -> None:
        self.config = config
        self.path = Path(config.state_file)
        self.rapl_path = Path(config.rapl_path)
        self.max_gap = max_gap
        self._state = self._load_state()
        self._rapl_warned = False

    @property
    def reading(self) -> EnergyReading:
        return EnergyReading(
            day_joules=self._state.day_joules,
            month_joules=self._state.month_joules,
        )

    def update(self, now: float | None = None, gpu_power_w: float = 0.0) -> EnergyReading:
        timestamp = time.time() if now is None else now
        state = self._state

        self._apply_rollover(timestamp)

        elapsed = timestamp - state.updated if state.updated else 0.0

        cpu_joules = self._consume_rapl()

        gpu_joules = 0.0
        if gpu_power_w > 0 and elapsed > 0:
            if elapsed > self.max_gap:
                logger.warning(
                    "intervalo de %.1fs acima do máximo de %.1fs; energia de GPU ignorada",
                    elapsed,
                    self.max_gap,
                )
            else:
                gpu_joules = gpu_power_w * elapsed

        state.day_joules += cpu_joules + gpu_joules
        state.month_joules += cpu_joules + gpu_joules
        state.updated = timestamp

        self.save()
        return self.reading

    def read_rapl_uj(self) -> int | None:
        try:
            return int(self.rapl_path.joinpath("energy_uj").read_text(encoding="utf-8").strip())
        except PermissionError:
            if not self._rapl_warned:
                logger.warning("sem permissão para ler %s/energy_uj; energia de CPU desativada", self.rapl_path)
                self._rapl_warned = True
            return None
        except FileNotFoundError:
            if not self._rapl_warned:
                logger.warning("%s/energy_uj não encontrado; energia de CPU desativada", self.rapl_path)
                self._rapl_warned = True
            return None
        except (OSError, ValueError) as exc:
            logger.warning("falha ao ler %s/energy_uj: %s", self.rapl_path, exc)
            return None

    def read_max_range_uj(self) -> int:
        try:
            value = int(
                self.rapl_path.joinpath("max_energy_range_uj").read_text(encoding="utf-8").strip()
            )
        except (OSError, ValueError):
            return DEFAULT_RAPL_RANGE_UJ
        return value if value > 0 else DEFAULT_RAPL_RANGE_UJ

    def save(self) -> None:
        state = self._state
        payload = {
            "version": 1,
            "day": state.day,
            "month": state.month,
            "day_joules": state.day_joules,
            "month_joules": state.month_joules,
            "rapl_uj": state.rapl_uj,
            "updated": state.updated,
        }

        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            raise EnergyError(f"não foi possível salvar {self.path}: {exc}") from exc

    def _consume_rapl(self) -> float:
        current = self.read_rapl_uj()
        if current is None:
            return 0.0

        state = self._state
        if state.rapl_uj is None:
            state.rapl_uj = current
            return 0.0

        delta = current - state.rapl_uj
        if delta < 0:
            max_range = self.read_max_range_uj()
            if state.rapl_uj - current > max_range / 2:
                delta += max_range
            else:
                logger.info("contador RAPL reiniciado; delta descartado")
                delta = 0

        state.rapl_uj = current
        return delta / MICROJOULES_PER_JOULE

    def _apply_rollover(self, now: float) -> None:
        local = time.localtime(now)
        today = time.strftime("%Y-%m-%d", local)
        month = today[:7]
        state = self._state

        if state.day != today:
            if state.day:
                logger.info("rollover diário: %s -> %s", state.day, today)
            state.day = today
            state.day_joules = 0.0

        if state.month != month:
            if state.month:
                logger.info("rollover mensal: %s -> %s", state.month, month)
            state.month = month
            state.month_joules = 0.0

    def _load_state(self) -> _State:
        now = time.time()
        fresh = _State(
            day=time.strftime("%Y-%m-%d", time.localtime(now)),
            month=time.strftime("%Y-%m", time.localtime(now)),
        )

        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return fresh
        except OSError as exc:
            logger.warning("não foi possível ler %s: %s", self.path, exc)
            return fresh

        try:
            data = json.loads(raw)
            state = _State(
                day=str(data["day"]),
                month=str(data["month"]),
                day_joules=float(data.get("day_joules", 0.0)),
                month_joules=float(data.get("month_joules", 0.0)),
                rapl_uj=int(data["rapl_uj"]) if data.get("rapl_uj") is not None else None,
                updated=float(data.get("updated", 0.0)),
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("estado de energia inválido em %s (%s); recomeçando", self.path, exc)
            return fresh

        if not state.day or not state.month:
            return fresh
        return state
