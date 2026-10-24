import json
import tempfile
import time
import unittest
from pathlib import Path

from turzx_monitor.config import EnergyConfig
from turzx_monitor.energy import EnergyAccumulator, EnergyError


def timestamp(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class EnergyTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rapl = self.root / "intel-rapl:0"
        self.rapl.mkdir()
        self.set_rapl(0, 1_000_000_000)
        self.state_file = self.root / "energy.json"

    def set_rapl(self, energy_uj: int, max_range_uj: int | None = None) -> None:
        (self.rapl / "energy_uj").write_text(str(energy_uj), encoding="utf-8")
        if max_range_uj is not None:
            (self.rapl / "max_energy_range_uj").write_text(
                str(max_range_uj), encoding="utf-8"
            )

    def make_accumulator(self, **kwargs) -> EnergyAccumulator:
        config = EnergyConfig(state_file=str(self.state_file), rapl_path=str(self.rapl))
        return EnergyAccumulator(config, **kwargs)

    def saved_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))


class AccumulationTest(EnergyTestBase):
    def test_first_update_sets_baseline_only(self):
        accumulator = self.make_accumulator()
        reading = accumulator.update(now=timestamp(2026, 10, 24), gpu_power_w=200.0)
        self.assertEqual(reading.day_joules, 0.0)
        self.assertEqual(reading.month_joules, 0.0)
        self.assertEqual(self.saved_state()["rapl_uj"], 0)

    def test_accumulates_cpu_and_gpu(self):
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)

        self.set_rapl(5_000_000)
        reading = accumulator.update(now=start + 10, gpu_power_w=100.0)

        self.assertAlmostEqual(reading.day_joules, 1005.0)
        self.assertAlmostEqual(reading.month_joules, 1005.0)
        self.assertAlmostEqual(reading.day_wh, 1005.0 / 3600.0)

    def test_no_cpu_counter_change_adds_nothing(self):
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)
        self.set_rapl(5_000_000)
        accumulator.update(now=start + 10, gpu_power_w=10.0)
        reading = accumulator.update(now=start + 20, gpu_power_w=10.0)
        self.assertAlmostEqual(reading.day_joules, 205.0)

    def test_large_gap_skips_gpu_energy(self):
        accumulator = self.make_accumulator(max_gap=5.0)
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)
        reading = accumulator.update(now=start + 10, gpu_power_w=100.0)
        self.assertEqual(reading.day_joules, 0.0)

    def test_missing_rapl_file_still_accumulates_gpu(self):
        (self.rapl / "energy_uj").unlink()
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)
        reading = accumulator.update(now=start + 2, gpu_power_w=50.0)
        self.assertAlmostEqual(reading.day_joules, 100.0)


class RolloverTest(EnergyTestBase):
    def test_day_rollover_keeps_month(self):
        accumulator = self.make_accumulator()
        day_one = timestamp(2026, 10, 24, 23, 59)
        accumulator.update(now=day_one)
        self.set_rapl(5_000_000)
        accumulator.update(now=day_one + 5)

        self.set_rapl(15_000_000)
        reading = accumulator.update(now=timestamp(2026, 10, 25, 0, 0))

        self.assertAlmostEqual(reading.day_joules, 10.0)
        self.assertAlmostEqual(reading.month_joules, 15.0)
        state = self.saved_state()
        self.assertEqual(state["day"], "2026-10-25")
        self.assertEqual(state["month"], "2026-10")

    def test_month_rollover_resets_month(self):
        accumulator = self.make_accumulator()
        end_of_month = timestamp(2026, 10, 31, 23, 59)
        accumulator.update(now=end_of_month)
        self.set_rapl(5_000_000)
        accumulator.update(now=end_of_month + 5)

        self.set_rapl(12_000_000)
        reading = accumulator.update(now=timestamp(2026, 11, 1, 0, 1))

        self.assertAlmostEqual(reading.day_joules, 7.0)
        self.assertAlmostEqual(reading.month_joules, 7.0)
        state = self.saved_state()
        self.assertEqual(state["day"], "2026-11-01")
        self.assertEqual(state["month"], "2026-11")


class RaplWrapTest(EnergyTestBase):
    def test_wrap_adds_remaining_range(self):
        self.set_rapl(990, max_range_uj=1000)
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)

        self.set_rapl(5)
        reading = accumulator.update(now=start + 1)

        self.assertAlmostEqual(reading.day_joules, 15e-6)

    def test_small_drop_is_treated_as_reset(self):
        self.set_rapl(600, max_range_uj=1000)
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)

        self.set_rapl(100)
        reading = accumulator.update(now=start + 1)

        self.assertEqual(reading.day_joules, 0.0)
        self.assertEqual(self.saved_state()["rapl_uj"], 100)


class PersistenceTest(EnergyTestBase):
    def test_state_is_reloaded(self):
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)
        self.set_rapl(5_000_000)
        accumulator.update(now=start + 10, gpu_power_w=100.0)

        reloaded = self.make_accumulator()
        self.assertAlmostEqual(reloaded.reading.day_joules, 1005.0)
        self.assertAlmostEqual(reloaded.reading.month_joules, 1005.0)
        self.assertEqual(reloaded._state.rapl_uj, 5_000_000)

    def test_continues_after_reload(self):
        accumulator = self.make_accumulator()
        start = timestamp(2026, 10, 24)
        accumulator.update(now=start)
        self.set_rapl(5_000_000)
        accumulator.update(now=start + 10)

        reloaded = self.make_accumulator()
        self.set_rapl(9_000_000)
        reading = reloaded.update(now=start + 20)

        self.assertAlmostEqual(reading.day_joules, 9.0)

    def test_corrupt_state_is_replaced(self):
        self.state_file.write_text("{invalido", encoding="utf-8")
        accumulator = self.make_accumulator()
        reading = accumulator.update(now=timestamp(2026, 10, 24))
        self.assertEqual(reading.day_joules, 0.0)
        self.assertIn("day", self.saved_state())

    def test_missing_state_file_is_created(self):
        accumulator = self.make_accumulator()
        self.assertFalse(self.state_file.exists())
        accumulator.update(now=timestamp(2026, 10, 24))
        self.assertTrue(self.state_file.exists())
        self.assertFalse(self.state_file.with_name("energy.json.tmp").exists())

    def test_creates_parent_directories(self):
        nested = self.root / "var" / "lib" / "turzx-monitor" / "energy.json"
        config = EnergyConfig(state_file=str(nested), rapl_path=str(self.rapl))
        accumulator = EnergyAccumulator(config)
        accumulator.update(now=timestamp(2026, 10, 24))
        self.assertTrue(nested.exists())

    def test_save_error_is_reported(self):
        target = self.root / "energy.json"
        target.mkdir()
        accumulator = self.make_accumulator()
        with self.assertRaises(EnergyError):
            accumulator.save()


if __name__ == "__main__":
    unittest.main()
