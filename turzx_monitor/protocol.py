from __future__ import annotations

import time
from enum import Enum, IntEnum
from typing import Any

import serial

DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 480
MAX_COORDINATE = 1023
MAX_BRIGHTNESS = 255


class Command(IntEnum):
    HELLO = 69
    RESET = 101
    CLEAR = 102
    TO_BLACK = 103
    SCREEN_OFF = 108
    SCREEN_ON = 109
    SET_BRIGHTNESS = 110
    SET_ORIENTATION = 121
    DISPLAY_BITMAP = 197


class Orientation(IntEnum):
    PORTRAIT = 0
    REVERSE_PORTRAIT = 1
    LANDSCAPE = 2
    REVERSE_LANDSCAPE = 3

    @classmethod
    def from_name(cls, name: str) -> "Orientation":
        key = name.upper().replace("-", "_")
        try:
            return cls[key]
        except KeyError as exc:
            raise ValueError(f"orientação inválida: {name!r}") from exc

    @property
    def is_portrait(self) -> bool:
        return self in (Orientation.PORTRAIT, Orientation.REVERSE_PORTRAIT)


class ScreenModel(Enum):
    TURING_3_5 = (320, 480)
    USBMONITOR_3_5 = (320, 480)
    USBMONITOR_5 = (480, 800)
    USBMONITOR_7 = (600, 1024)

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


_HELLO_RESPONSES = {
    b"\x01" * 6: ScreenModel.USBMONITOR_3_5,
    b"\x02" * 6: ScreenModel.USBMONITOR_5,
    b"\x03" * 6: ScreenModel.USBMONITOR_7,
}


class ScreenError(RuntimeError):
    pass


def pack_command(
    command: int,
    x: int = 0,
    y: int = 0,
    ex: int = 0,
    ey: int = 0,
) -> bytes:
    for name, value in (("x", x), ("y", y), ("ex", ex), ("ey", ey)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"coordenada {name} deve ser inteira")
        if not 0 <= value <= MAX_COORDINATE:
            raise ValueError(f"coordenada {name} fora da faixa 0..{MAX_COORDINATE}: {value}")

    try:
        code = int(command)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"comando inválido: {command!r}") from exc
    if not 0 <= code <= 0xFF:
        raise ValueError(f"código de comando fora da faixa 0..255: {code}")

    return bytes(
        (
            x >> 2,
            ((x & 3) << 6) + (y >> 4),
            ((y & 15) << 4) + (ex >> 6),
            ((ex & 63) << 2) + (ey >> 8),
            ey & 0xFF,
            code,
        )
    )


def pack_orientation(orientation: Orientation, width: int, height: int) -> bytes:
    if not 0 < width <= MAX_COORDINATE or not 0 < height <= MAX_COORDINATE:
        raise ValueError(f"dimensões inválidas: {width}x{height}")

    payload = bytes(
        (
            int(orientation) + 100,
            (width >> 8) & 0xFF,
            width & 0xFF,
            (height >> 8) & 0xFF,
            height & 0xFF,
        )
    )
    return pack_command(Command.SET_ORIENTATION) + payload + bytes(5)


class TuringScreen:
    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        timeout: float = 1.0,
        rtscts: bool = True,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        orientation: Orientation = Orientation.PORTRAIT,
        model: ScreenModel | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.rtscts = rtscts
        self.native_width = width
        self.native_height = height
        self.orientation = Orientation(orientation)
        self.model = model
        self._serial: Any = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    @property
    def width(self) -> int:
        if self.orientation.is_portrait:
            return self.native_width
        return self.native_height

    @property
    def height(self) -> int:
        if self.orientation.is_portrait:
            return self.native_height
        return self.native_width

    def open(self) -> None:
        if self._serial is not None:
            return
        try:
            self._serial = serial.serial_for_url(
                self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                rtscts=self.rtscts,
            )
        except serial.SerialException as exc:
            raise ScreenError(f"não foi possível abrir a porta {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is None:
            return
        serial_port = self._serial
        self._serial = None
        try:
            serial_port.flush()
        except serial.SerialException:
            pass
        finally:
            serial_port.close()

    def __enter__(self) -> "TuringScreen":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def handshake(self, timeout: float | None = None) -> ScreenModel:
        self._write(bytes(Command.HELLO for _ in range(6)))

        serial_port = self._serial
        previous_timeout = serial_port.timeout
        if timeout is not None:
            serial_port.timeout = timeout
        try:
            response = serial_port.read(6)
        except serial.SerialException as exc:
            raise ScreenError(f"falha ao ler o handshake da porta {self.port}: {exc}") from exc
        finally:
            serial_port.timeout = previous_timeout
            serial_port.reset_input_buffer()

        self.model = _HELLO_RESPONSES.get(response, ScreenModel.TURING_3_5)
        self.native_width = self.model.width
        self.native_height = self.model.height
        return self.model

    def send_command(
        self,
        command: int,
        x: int = 0,
        y: int = 0,
        ex: int = 0,
        ey: int = 0,
    ) -> None:
        self._write(pack_command(command, x, y, ex, ey))

    def reset(self, delay: float = 5.0) -> None:
        self.send_command(Command.RESET)
        self.close()
        time.sleep(delay)
        self.open()

    def clear(self) -> None:
        previous = self.orientation
        if previous is not Orientation.PORTRAIT:
            self.set_orientation(Orientation.PORTRAIT)
        self.send_command(Command.CLEAR)
        if previous is not Orientation.PORTRAIT:
            self.set_orientation(previous)

    def screen_on(self) -> None:
        self.send_command(Command.SCREEN_ON)

    def screen_off(self) -> None:
        self.send_command(Command.SCREEN_OFF)

    def to_black(self) -> None:
        self.send_command(Command.TO_BLACK)

    def set_brightness(self, level: int) -> None:
        if not isinstance(level, int) or isinstance(level, bool):
            raise ValueError("brilho deve ser inteiro")
        if not 0 <= level <= MAX_BRIGHTNESS:
            raise ValueError(f"brilho fora da faixa 0..{MAX_BRIGHTNESS}: {level}")
        self.send_command(Command.SET_BRIGHTNESS, x=level)

    def set_orientation(self, orientation: Orientation = Orientation.PORTRAIT) -> None:
        self.orientation = Orientation(orientation)
        self._write(pack_orientation(self.orientation, self.width, self.height))

    def display_bitmap(self, x: int, y: int, width: int, height: int, data: bytes) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"dimensões inválidas: {width}x{height}")
        if x < 0 or y < 0 or x + width > self.width or y + height > self.height:
            raise ValueError(
                f"retângulo {x},{y} {width}x{height} fora da tela {self.width}x{self.height}"
            )

        view = memoryview(data)
        expected = width * height * 2
        if view.nbytes != expected:
            raise ValueError(f"dados RGB565 com {view.nbytes} bytes; esperado {expected}")

        self.send_command(Command.DISPLAY_BITMAP, x, y, x + width - 1, y + height - 1)

        chunk_size = self.width * 8
        for start in range(0, expected, chunk_size):
            self._write(view[start : start + chunk_size])

    def _write(self, data: bytes) -> None:
        if self._serial is None:
            raise ScreenError("porta serial não está aberta")
        try:
            self._serial.write(data)
        except serial.SerialException as exc:
            raise ScreenError(f"falha ao escrever na porta {self.port}: {exc}") from exc
