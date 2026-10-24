from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PORT = "/dev/serial/by-id/usb-Turing_UsbMonitor_USB35INCHIPSV2-if00"
DEFAULT_STATE_FILE = "/var/lib/turzx-monitor/energy.json"
DEFAULT_RAPL_PATH = "/sys/class/powercap/intel-rapl:0"

ORIENTATIONS = {"portrait": 0}
GAUGES = ("cpu", "ram", "swap", "gpu0", "gpu1")
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

ROOT_KEYS = {"display", "monitor", "energy", "logging"}
DISPLAY_KEYS = {
    "port",
    "baudrate",
    "orientation",
    "brightness",
    "full_refresh_interval",
    "reconnect_delay",
}
MONITOR_KEYS = {"interval", "gauges", "gpus"}
ENERGY_KEYS = {"state_file", "sample_interval", "rapl_path"}
LOGGING_KEYS = {"level", "file"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DisplayConfig:
    port: str = DEFAULT_PORT
    baudrate: int = 115200
    orientation: str = "portrait"
    brightness: int = 0
    full_refresh_interval: float = 300.0
    reconnect_delay: float = 5.0


@dataclass(frozen=True)
class MonitorConfig:
    interval: float = 2.0
    gauges: tuple[str, ...] = GAUGES
    gpus: tuple[int, ...] = (0, 1)


@dataclass(frozen=True)
class EnergyConfig:
    state_file: str = DEFAULT_STATE_FILE
    sample_interval: float = 1.0
    rapl_path: str = DEFAULT_RAPL_PATH


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str | None = None


@dataclass(frozen=True)
class AppConfig:
    display: DisplayConfig = field(default_factory=DisplayConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"arquivo de configuração não encontrado: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido em {path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("a raiz da configuração deve ser um mapa")

    unknown = set(raw) - ROOT_KEYS
    if unknown:
        raise ConfigError(f"chaves desconhecidas na raiz: {', '.join(sorted(unknown))}")

    display = _load_display(_section(raw, "display", DISPLAY_KEYS))
    monitor = _load_monitor(_section(raw, "monitor", MONITOR_KEYS))
    energy = _load_energy(_section(raw, "energy", ENERGY_KEYS))
    logging_config = _load_logging(_section(raw, "logging", LOGGING_KEYS))

    if "gpu1" in monitor.gauges and len(monitor.gpus) < 2:
        raise ConfigError("'monitor.gauges' usa gpu1, mas 'monitor.gpus' tem menos de 2 GPUs")

    return AppConfig(display=display, monitor=monitor, energy=energy, logging=logging_config)


def _load_display(data: dict[str, Any]) -> DisplayConfig:
    defaults = DisplayConfig()
    orientation = _get_str(data, "orientation", defaults.orientation, "display")
    if orientation not in ORIENTATIONS:
        raise ConfigError(
            f"'display.orientation' inválida: {orientation!r} (aceitas: {', '.join(sorted(ORIENTATIONS))})"
        )

    config = DisplayConfig(
        port=_expand(_get_str(data, "port", defaults.port, "display")),
        baudrate=_get_int(data, "baudrate", defaults.baudrate, "display"),
        orientation=orientation,
        brightness=_get_int(data, "brightness", defaults.brightness, "display"),
        full_refresh_interval=_get_float(
            data, "full_refresh_interval", defaults.full_refresh_interval, "display"
        ),
        reconnect_delay=_get_float(data, "reconnect_delay", defaults.reconnect_delay, "display"),
    )

    if config.baudrate <= 0:
        raise ConfigError("'display.baudrate' deve ser maior que zero")
    if not 0 <= config.brightness <= 255:
        raise ConfigError("'display.brightness' deve estar entre 0 e 255")
    if config.full_refresh_interval <= 0:
        raise ConfigError("'display.full_refresh_interval' deve ser maior que zero")
    if config.reconnect_delay <= 0:
        raise ConfigError("'display.reconnect_delay' deve ser maior que zero")

    return config


def _load_monitor(data: dict[str, Any]) -> MonitorConfig:
    defaults = MonitorConfig()
    config = MonitorConfig(
        interval=_get_float(data, "interval", defaults.interval, "monitor"),
        gauges=_get_gauges(data),
        gpus=_get_gpus(data),
    )

    if config.interval <= 0:
        raise ConfigError("'monitor.interval' deve ser maior que zero")
    if not config.gauges:
        raise ConfigError("'monitor.gauges' não pode ser vazio")
    if not config.gpus:
        raise ConfigError("'monitor.gpus' não pode ser vazio")

    return config


def _load_energy(data: dict[str, Any]) -> EnergyConfig:
    defaults = EnergyConfig()
    config = EnergyConfig(
        state_file=_expand(_get_str(data, "state_file", defaults.state_file, "energy")),
        sample_interval=_get_float(data, "sample_interval", defaults.sample_interval, "energy"),
        rapl_path=_expand(_get_str(data, "rapl_path", defaults.rapl_path, "energy")),
    )

    if config.sample_interval <= 0:
        raise ConfigError("'energy.sample_interval' deve ser maior que zero")

    return config


def _load_logging(data: dict[str, Any]) -> LoggingConfig:
    defaults = LoggingConfig()
    level = _get_str(data, "level", defaults.level, "logging").upper()
    if level not in LOG_LEVELS:
        raise ConfigError(
            f"'logging.level' inválido: {level!r} (aceitos: {', '.join(sorted(LOG_LEVELS))})"
        )

    file = data.get("file")
    if file is not None:
        if not isinstance(file, str):
            raise ConfigError("'logging.file' deve ser texto ou nulo")
        file = _expand(file)

    return LoggingConfig(level=level, file=file)


def _get_gauges(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("gauges", list(GAUGES))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("'monitor.gauges' deve ser uma lista de textos")

    invalid = [item for item in value if item not in GAUGES]
    if invalid:
        raise ConfigError(
            f"'monitor.gauges' contém valores inválidos: {', '.join(invalid)} "
            f"(aceitos: {', '.join(GAUGES)})"
        )
    if len(set(value)) != len(value):
        raise ConfigError("'monitor.gauges' não pode ter itens repetidos")

    return tuple(value)


def _get_gpus(data: dict[str, Any]) -> tuple[int, ...]:
    value = data.get("gpus", [0, 1])
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise ConfigError("'monitor.gpus' deve ser uma lista de índices inteiros")

    invalid = [item for item in value if item < 0]
    if invalid:
        raise ConfigError("'monitor.gpus' não pode conter índices negativos")
    if len(set(value)) != len(value):
        raise ConfigError("'monitor.gpus' não pode ter índices repetidos")

    return tuple(value)


def _section(data: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{name}' deve ser um mapa")

    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"chaves desconhecidas em '{name}': {', '.join(sorted(unknown))}")

    return value


def _get_str(data: dict[str, Any], key: str, default: str, section: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"'{section}.{key}' deve ser texto não vazio")
    return value


def _get_int(data: dict[str, Any], key: str, default: int, section: str) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{section}.{key}' deve ser inteiro")
    return value


def _get_float(data: dict[str, Any], key: str, default: float, section: str) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"'{section}.{key}' deve ser numérico")
    return float(value)


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))
