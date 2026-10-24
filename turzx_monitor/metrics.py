from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
GPU_QUERY_FIELDS = (
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
)
CPU_THERMAL_HINTS = ("x86_pkg_temp", "package", "coretemp", "cpu", "acpitz")
UNAVAILABLE = {"", "n/a", "[n/a]", "[not supported]"}


class MetricsError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryMetrics:
    percent: float
    used_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class GpuMetrics:
    index: int
    name: str | None = None
    utilization_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    temperature_c: float | None = None
    power_w: float | None = None


@dataclass(frozen=True)
class MetricsSample:
    timestamp: float
    cpu_percent: float
    ram: MemoryMetrics
    swap: MemoryMetrics
    gpus: tuple[GpuMetrics, ...]
    cpu_temp_c: float | None

    @property
    def gpu_power_w(self) -> float:
        return sum(gpu.power_w for gpu in self.gpus if gpu.power_w is not None)


def parse_proc_stat(text: str) -> tuple[int, int]:
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        if len(fields) < 4:
            raise ValueError("linha 'cpu' de /proc/stat incompleta")
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle
    raise ValueError("linha 'cpu' não encontrada em /proc/stat")


def cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return 0.0
    usage = 100.0 * (1.0 - idle_delta / total_delta)
    return max(0.0, min(100.0, usage))


def parse_meminfo(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, remainder = line.partition(":")
        if not separator:
            continue
        fields = remainder.split()
        if fields:
            result[key] = int(fields[0]) * 1024
    return result


def memory_metrics(total: int, available: int) -> MemoryMetrics:
    total = max(total, 0)
    used = max(total - max(available, 0), 0)
    percent = 100.0 * used / total if total else 0.0
    return MemoryMetrics(percent=percent, used_bytes=used, total_bytes=total)


def parse_nvidia_smi(text: str) -> list[GpuMetrics]:
    gpus: list[GpuMetrics] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < len(GPU_QUERY_FIELDS):
            raise ValueError(f"linha inesperada do nvidia-smi: {line!r}")

        index = _optional_int(fields[0])
        if index is None:
            raise ValueError(f"índice de GPU inválido: {fields[0]!r}")

        memory_used = _optional_float(fields[3])
        memory_total = _optional_float(fields[4])

        gpus.append(
            GpuMetrics(
                index=index,
                name=fields[1] or None,
                utilization_percent=_optional_float(fields[2]),
                memory_used_bytes=int(memory_used * MIB) if memory_used is not None else None,
                memory_total_bytes=int(memory_total * MIB) if memory_total is not None else None,
                temperature_c=_optional_float(fields[5]),
                power_w=_optional_float(fields[6]),
            )
        )
    return gpus


def read_cpu_temperature(thermal_path: str | Path = "/sys/class/thermal") -> float | None:
    best: tuple[int, float] | None = None
    for zone in sorted(Path(thermal_path).glob("thermal_zone*")):
        try:
            zone_type = (zone / "type").read_text(encoding="utf-8").strip().lower()
            raw = (zone / "temp").read_text(encoding="utf-8").strip()
            temperature = int(raw) / 1000.0
        except (OSError, ValueError):
            continue

        priority = next(
            (order for order, hint in enumerate(CPU_THERMAL_HINTS) if hint in zone_type),
            None,
        )
        if priority is None:
            continue

        candidate = (priority, -temperature)
        if best is None or candidate < best:
            best = candidate

    return -best[1] if best is not None else None


class MetricsCollector:
    def __init__(
        self,
        gpu_indices: tuple[int, ...] = (0, 1),
        *,
        proc_path: str | Path = "/proc",
        thermal_path: str | Path = "/sys/class/thermal",
        gpu_command: list[str] | None = None,
        gpu_timeout: float = 2.0,
    ) -> None:
        self.gpu_indices = tuple(gpu_indices)
        self.proc_path = Path(proc_path)
        self.thermal_path = Path(thermal_path)
        self.gpu_timeout = gpu_timeout
        self.gpu_command = gpu_command or [
            "nvidia-smi",
            f"--query-gpu={','.join(GPU_QUERY_FIELDS)}",
            "--format=csv,noheader,nounits",
        ]
        self._previous_cpu: tuple[int, int] | None = None
        self._nvidia_missing = False

    def sample(self, now: float | None = None) -> MetricsSample:
        timestamp = time.time() if now is None else now

        cpu_times = parse_proc_stat(self._read_text(self.proc_path / "stat"))
        cpu_usage = 0.0
        if self._previous_cpu is not None:
            cpu_usage = cpu_percent(self._previous_cpu, cpu_times)
        self._previous_cpu = cpu_times

        meminfo = parse_meminfo(self._read_text(self.proc_path / "meminfo"))
        try:
            ram = memory_metrics(meminfo["MemTotal"], meminfo["MemAvailable"])
            swap = memory_metrics(meminfo["SwapTotal"], meminfo["SwapFree"])
        except KeyError as exc:
            raise MetricsError(f"campo ausente em /proc/meminfo: {exc}") from exc

        return MetricsSample(
            timestamp=timestamp,
            cpu_percent=cpu_usage,
            ram=ram,
            swap=swap,
            gpus=self._collect_gpus(),
            cpu_temp_c=read_cpu_temperature(self.thermal_path),
        )

    def _collect_gpus(self) -> tuple[GpuMetrics, ...]:
        fallback = tuple(GpuMetrics(index=index) for index in self.gpu_indices)
        if self._nvidia_missing:
            return fallback

        try:
            completed = subprocess.run(
                self.gpu_command,
                capture_output=True,
                text=True,
                timeout=self.gpu_timeout,
                check=False,
            )
        except FileNotFoundError:
            self._nvidia_missing = True
            logger.warning("nvidia-smi não encontrado; métricas de GPU indisponíveis")
            return fallback
        except subprocess.TimeoutExpired:
            logger.warning("nvidia-smi excedeu %.1fs; mantendo GPUs sem dados", self.gpu_timeout)
            return fallback

        if completed.returncode != 0:
            logger.warning(
                "nvidia-smi retornou %s: %s",
                completed.returncode,
                completed.stderr.strip() or "sem mensagem",
            )
            return fallback

        try:
            parsed = {gpu.index: gpu for gpu in parse_nvidia_smi(completed.stdout)}
        except ValueError as exc:
            logger.warning("saída inválida do nvidia-smi: %s", exc)
            return fallback

        return tuple(parsed.get(index, GpuMetrics(index=index)) for index in self.gpu_indices)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MetricsError(f"não foi possível ler {path}: {exc}") from exc


def _optional_float(value: str) -> float | None:
    cleaned = value.strip().lower()
    if cleaned in UNAVAILABLE:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _optional_int(value: str) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None
