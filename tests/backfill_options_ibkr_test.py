from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path


def _load_backfill_module():
    project_root = Path(__file__).resolve().parents[1]
    module_path = project_root / "scripts" / "backfill_options_ibkr.py"
    spec = importlib.util.spec_from_file_location("backfill_options_ibkr", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill_module()


class NormalizeTests(unittest.TestCase):
    def test_option_type(self):
        self.assertEqual(backfill._normalize_option_type('C'), 'call')
        self.assertEqual(backfill._normalize_option_type('P'), 'put')
        self.assertEqual(backfill._normalize_option_type('call'), 'call')
        self.assertEqual(backfill._normalize_option_type('PUT'), 'put')
        self.assertEqual(backfill._normalize_option_type('x'), '')

    def test_right_to_ib(self):
        self.assertEqual(backfill._right_to_ib('call'), 'C')
        self.assertEqual(backfill._right_to_ib('P'), 'P')
        with self.assertRaises(ValueError):
            backfill._right_to_ib('x')

    def test_expiry_to_iso(self):
        self.assertEqual(backfill._normalize_expiry_to_iso('20250620'), '2025-06-20')
        self.assertEqual(backfill._normalize_expiry_to_iso('2025-06-20'), '2025-06-20')
        with self.assertRaises(ValueError):
            backfill._normalize_expiry_to_iso('')

    def test_bar_date_to_iso(self):
        self.assertEqual(backfill._bar_date_to_iso('20250620'), '2025-06-20')
        self.assertEqual(backfill._bar_date_to_iso('2025-06-20'), '2025-06-20')
        self.assertEqual(backfill._bar_date_to_iso(None), '')


class StrikeGridTests(unittest.TestCase):
    def test_inclusive_grid(self):
        self.assertEqual(
            backfill.build_strike_grid(480, 500, 5),
            [480.0, 485.0, 490.0, 495.0, 500.0],
        )

    def test_single_strike(self):
        self.assertEqual(backfill.build_strike_grid(500, 500, 5), [500.0])

    def test_invalid_step(self):
        with self.assertRaises(ValueError):
            backfill.build_strike_grid(480, 500, 0)

    def test_min_gt_max(self):
        with self.assertRaises(ValueError):
            backfill.build_strike_grid(500, 480, 5)


class PacerTests(unittest.TestCase):
    def test_blocks_after_budget(self):
        clock = {'t': 0.0}
        slept: list[float] = []

        def fake_sleep(seconds):
            slept.append(seconds)
            clock['t'] += seconds  # advance virtual time so the window can release

        pacer = backfill.HistoricalPacer(
            max_calls=2, window_seconds=600, safety=0, clock=lambda: clock['t'],
        )
        pacer.acquire(sleep=fake_sleep)
        pacer.acquire(sleep=fake_sleep)
        self.assertEqual(slept, [])  # first two are free
        pacer.acquire(sleep=fake_sleep)  # third must wait one full window
        self.assertTrue(slept and slept[-1] > 0)

    def test_safety_margin_reduces_budget(self):
        pacer = backfill.HistoricalPacer(max_calls=60, window_seconds=600, safety=1)
        self.assertEqual(pacer.max_calls, 59)


class UpsertTests(unittest.TestCase):
    def _make_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE symbols (symbol_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT)")
        conn.execute("CREATE TABLE dates (date_id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT)")
        backfill._ensure_schema(conn)
        return conn

    def test_idempotent_upsert(self):
        conn = self._make_db()
        symbol_id = backfill._get_symbol_id(conn, "SPY")
        cache = backfill._build_existing_date_cache(conn)
        date_ref = backfill._get_or_create_date_id(conn, cache, "2025-06-10")
        exp_ref = backfill._get_or_create_date_id(conn, cache, "2025-06-20")
        row = (symbol_id, date_ref, exp_ref, 'call', 500.0,
               None, None, 4.6, 4.6, None, 123, None, 'ibkr')

        backfill.upsert_option_rows(conn, [row])
        backfill.upsert_option_rows(conn, [row])  # rerun, must not duplicate

        count = conn.execute("SELECT COUNT(*) FROM options_data").fetchone()[0]
        self.assertEqual(count, 1)

    def test_upsert_updates_existing(self):
        conn = self._make_db()
        symbol_id = backfill._get_symbol_id(conn, "SPY")
        cache = backfill._build_existing_date_cache(conn)
        date_ref = backfill._get_or_create_date_id(conn, cache, "2025-06-10")
        exp_ref = backfill._get_or_create_date_id(conn, cache, "2025-06-20")
        key = (symbol_id, date_ref, exp_ref, 'call', 500.0)

        backfill.upsert_option_rows(conn, [key + (None, None, 4.6, 4.6, None, 100, None, 'ibkr')])
        backfill.upsert_option_rows(conn, [key + (None, None, 5.1, 5.1, None, 200, None, 'ibkr')])

        mark, volume = conn.execute(
            "SELECT mark, volume FROM options_data WHERE strike = 500.0"
        ).fetchone()
        self.assertEqual(mark, 5.1)
        self.assertEqual(volume, 200)

    def test_date_id_cache_reuse(self):
        conn = self._make_db()
        cache = backfill._build_existing_date_cache(conn)
        first = backfill._get_or_create_date_id(conn, cache, "2025-06-10")
        second = backfill._get_or_create_date_id(conn, cache, "2025-06-10")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
