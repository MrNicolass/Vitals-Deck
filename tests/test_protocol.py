import unittest

import serial

from turzx_monitor.protocol import (
    Command,
    Orientation,
    ScreenError,
    ScreenModel,
    TuringScreen,
    pack_command,
    pack_orientation,
)


class PackCommandTest(unittest.TestCase):
    def test_clear(self):
        self.assertEqual(pack_command(Command.CLEAR).hex(), "000000000066")

    def test_brightness_absolute(self):
        self.assertEqual(pack_command(Command.SET_BRIGHTNESS, x=191).hex(), "2fc00000006e")

    def test_full_screen_portrait_bitmap(self):
        packet = pack_command(Command.DISPLAY_BITMAP, 0, 0, 319, 479)
        self.assertEqual(packet.hex(), "000004fddfc5")

    def test_patch_portrait_bitmap(self):
        packet = pack_command(Command.DISPLAY_BITMAP, 10, 20, 109, 219)
        self.assertEqual(packet.hex(), "028141b4dbc5")

    def test_bounds(self):
        with self.assertRaises(ValueError):
            pack_command(Command.DISPLAY_BITMAP, 0, 0, 1024, 479)
        with self.assertRaises(ValueError):
            pack_command(Command.DISPLAY_BITMAP, -1, 0, 0, 0)
        with self.assertRaises(ValueError):
            pack_command(256)

    def test_coordinates_must_be_int(self):
        with self.assertRaises(ValueError):
            pack_command(Command.CLEAR, x=True)
        with self.assertRaises(ValueError):
            pack_command(Command.CLEAR, x=1.5)


class PackOrientationTest(unittest.TestCase):
    def test_portrait(self):
        packet = pack_orientation(Orientation.PORTRAIT, 320, 480)
        self.assertEqual(packet.hex(), "00000000007964014001e00000000000")
        self.assertEqual(len(packet), 16)

    def test_landscape(self):
        packet = pack_orientation(Orientation.LANDSCAPE, 480, 320)
        self.assertEqual(packet.hex(), "0000000000796601e001400000000000")

    def test_from_name(self):
        self.assertIs(Orientation.from_name("portrait"), Orientation.PORTRAIT)
        self.assertIs(Orientation.from_name("reverse-portrait"), Orientation.REVERSE_PORTRAIT)
        self.assertIs(Orientation.from_name("LANDSCAPE"), Orientation.LANDSCAPE)
        with self.assertRaises(ValueError):
            Orientation.from_name("diagonal")


class TuringScreenTest(unittest.TestCase):
    def make_screen(self, **kwargs):
        screen = TuringScreen("loop://", **kwargs)
        self.addCleanup(screen.close)
        screen.open()
        return screen

    def read_written(self, screen, size):
        data = screen._serial.read(size)
        self.assertEqual(len(data), size)
        return data

    def test_dimensions_follow_orientation(self):
        screen = self.make_screen()
        self.assertEqual((screen.width, screen.height), (320, 480))
        screen.set_orientation(Orientation.LANDSCAPE)
        self.assertEqual((screen.width, screen.height), (480, 320))
        screen.set_orientation(Orientation.REVERSE_PORTRAIT)
        self.assertEqual((screen.width, screen.height), (320, 480))

    def test_handshake_falls_back_to_turing(self):
        screen = self.make_screen()
        model = screen.handshake(timeout=0.1)
        self.assertIs(model, ScreenModel.TURING_3_5)
        self.assertEqual(screen.native_width, 320)
        self.assertEqual(screen.native_height, 480)

    def test_display_bitmap_writes_command_then_data(self):
        screen = self.make_screen()
        pixels = bytes(range(36))
        screen.display_bitmap(10, 20, 3, 6, pixels)

        expected_command = pack_command(Command.DISPLAY_BITMAP, 10, 20, 12, 25)
        written = self.read_written(screen, len(expected_command) + len(pixels))
        self.assertEqual(written, expected_command + pixels)

    def test_display_bitmap_rejects_wrong_size(self):
        screen = self.make_screen()
        with self.assertRaises(ValueError):
            screen.display_bitmap(0, 0, 2, 2, b"\x00" * 7)
        with self.assertRaises(ValueError):
            screen.display_bitmap(0, 0, 2, 2, b"\x00" * 9)

    def test_display_bitmap_rejects_out_of_bounds(self):
        screen = self.make_screen()
        with self.assertRaises(ValueError):
            screen.display_bitmap(319, 0, 2, 2, b"\x00" * 8)
        with self.assertRaises(ValueError):
            screen.display_bitmap(0, 479, 2, 2, b"\x00" * 8)

    def test_clear_uses_portrait_workaround(self):
        screen = self.make_screen()
        screen.set_orientation(Orientation.LANDSCAPE)
        self.read_written(screen, 16)

        screen.clear()

        packet = self.read_written(screen, 16 + 6 + 16)
        self.assertEqual(packet[:16], pack_orientation(Orientation.PORTRAIT, 320, 480))
        self.assertEqual(packet[16:22], pack_command(Command.CLEAR))
        self.assertEqual(packet[22:], pack_orientation(Orientation.LANDSCAPE, 480, 320))
        self.assertIs(screen.orientation, Orientation.LANDSCAPE)

    def test_brightness_and_power(self):
        screen = self.make_screen()
        screen.set_brightness(0)
        screen.set_brightness(255)
        screen.screen_off()
        screen.screen_on()

        expected = (
            pack_command(Command.SET_BRIGHTNESS, x=0)
            + pack_command(Command.SET_BRIGHTNESS, x=255)
            + pack_command(Command.SCREEN_OFF)
            + pack_command(Command.SCREEN_ON)
        )
        self.assertEqual(self.read_written(screen, len(expected)), expected)
        with self.assertRaises(ValueError):
            screen.set_brightness(256)

    def test_write_without_open_fails(self):
        screen = TuringScreen("loop://")
        with self.assertRaises(ScreenError):
            screen.send_command(Command.CLEAR)


if __name__ == "__main__":
    unittest.main()
