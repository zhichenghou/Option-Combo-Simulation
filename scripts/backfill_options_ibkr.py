#!/usr/bin/env python3
"""
Backfill historical option daily bars from IBKR (with an OPRA subscription)
into the project SQLite DB so that ``historical_server.py`` can replay them.

The script sweeps a grid of (expiry, strike, right) contracts for a single
underlying, requests daily bars per contract via ``reqHistoricalDataAsync``,
and upserts the result into ``options_data`` keyed by
(symbol, quote_date, expiration, type, strike).

Design notes:
- Writes ``options_data`` matching the columns ``historical_data.py`` reads
  (bid/ask/mark/last/implied_volatility/volume/open_interest), with foreign
  keys into ``symbols`` and ``dates`` (ISO ``YYYY-MM-DD``).
- Respects IBKR pacing for ``reqHistoricalData``: max 60 requests per rolling
  10-minute window. A sliding-window pacer never bursts.
- Idempotent: rows are upserted on the unique key, so reruns are safe.

Examples:
    python scripts/backfill_options_ibkr.py --expiry 20250620 \
        --strike-min 480 --strike-max 560 --strike-step 5 --right both
    python scripts/backfill_options_ibkr.py --expiry 20250620 \
        --strike-min 500 --strike-max 500 --duration "1 Y" --client-id 1001

Requirements:
    ib-async, plus a running TWS/IB Gateway with an OPRA market-data
    subscription on the connected account.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import os
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime
from typing import Callable, Iterable


OPTIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS options_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_ref INTEGER NOT NULL,
    date_ref INTEGER NOT NULL,
    expiration_ref INTEGER NOT NULL,
    type TEXT NOT NULL,
    strike REAL NOT NULL,
    bid REAL,
    ask REAL,
    mark REAL,
    last REAL,
    implied_volatility REAL,
    volume INTEGER,
    open_interest INTEGER,
    source TEXT NOT NULL DEFAULT 'ibkr',
    FOREIGN KEY (symbol_ref) REFERENCES symbols(symbol_id),
    FOREIGN KEY (date_ref) REFERENCES dates(date_id),
    FOREIGN KEY (expiration_ref) REFERENCES dates(date_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_options_data_unique
    ON options_data(symbol_ref, date_ref, expiration_ref, type, strike);
CREATE INDEX IF NOT EXISTS idx_options_data_symbol_ref ON options_data(symbol_ref);
CREATE INDEX IF NOT EXISTS idx_options_data_date_ref ON options_data(date_ref);
CREATE INDEX IF NOT EXISTS idx_options_data_expiration_ref ON options_data(expiration_ref);
"""


# --- Pure helpers (unit-testable, no IB / no network) ------------------------

def _normalize_option_type(value: object) -> str:
    """Map IBKR right (C/P) or call/put to the stored 'call'/'put' value."""
    text = str(value or '').strip().upper()
    if text == 'C':
        return 'call'
    if text == 'P':
        return 'put'
    lowered = text.lower()
    if lowered in ('call', 'put'):
        return lowered
    return ''


def _right_to_ib(value: object) -> str:
    """Map a CLI right token to the IBKR right code ('C' or 'P')."""
    normalized = _normalize_option_type(value)
    if normalized == 'call':
        return 'C'
    if normalized == 'put':
        return 'P'
    raise ValueError(f"Invalid option right: {value!r}")


def _normalize_expiry_to_iso(value: object) -> str:
    """Accept YYYYMMDD or YYYY-MM-DD and return ISO YYYY-MM-DD."""
    text = str(value or '').strip().replace('-', '').replace('/', '')
    if not text:
        raise ValueError("Empty expiry")
    return datetime.strptime(text, '%Y%m%d').date().isoformat()


def _bar_date_to_iso(value: object) -> str:
    """Normalize an ib_async bar.date (date or string) to ISO YYYY-MM-DD."""
    if value is None:
        return ''
    if hasattr(value, 'date'):
        try:
            return value.date().isoformat()
        except Exception:
            pass
    text = str(value).strip().replace('/', '-')
    if not text:
        return ''
    for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y%m%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ''


def build_strike_grid(strike_min: float, strike_max: float, strike_step: float) -> list[float]:
    """Inclusive strike grid. Rounds to 4 decimals to avoid float drift."""
    if strike_step <= 0:
        raise ValueError("strike-step must be positive")
    if strike_min > strike_max:
        raise ValueError("strike-min cannot exceed strike-max")
    strikes: list[float] = []
    current = strike_min
    while current <= strike_max + 1e-9:
        strikes.append(round(current, 4))
        current += strike_step
    return strikes


class HistoricalPacer:
    """Sliding-window limiter: <= max_calls requests per window_seconds."""

    def __init__(
        self,
        max_calls: int = 60,
        window_seconds: float = 600.0,
        safety: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_calls = max(1, max_calls - safety)
        self.window = window_seconds
        self._clock = clock
        self._calls: deque[float] = deque()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.window:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(self._clock())
                return
            wait = self.window - (now - self._calls[0])
            if wait <= 0:
                # Defensive: window already elapsed; loop re-evicts and proceeds.
                continue
            sleep(wait)


# --- SQLite layer ------------------------------------------------------------

def _project_root() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(script_dir)


def _default_db_path() -> str:
    return os.path.join(_project_root(), "sqlite_spy", "spy_options.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(OPTIONS_SCHEMA_SQL)


def _get_symbol_id(conn: sqlite3.Connection, ticker: str) -> int:
    row = conn.execute(
        "SELECT symbol_id FROM symbols WHERE symbol = ?",
        (ticker,),
    ).fetchone()
    if row:
        return int(row[0])
    cursor = conn.execute("INSERT INTO symbols (symbol) VALUES (?)", (ticker,))
    return int(cursor.lastrowid)


def _build_existing_date_cache(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT date_id, date FROM dates").fetchall()
    return {str(row[1]): int(row[0]) for row in rows}


def _get_or_create_date_id(conn: sqlite3.Connection, date_cache: dict[str, int], iso_date: str) -> int:
    date_id = date_cache.get(iso_date)
    if date_id is not None:
        return date_id
    cursor = conn.execute("INSERT INTO dates (date) VALUES (?)", (iso_date,))
    date_id = int(cursor.lastrowid)
    date_cache[iso_date] = date_id
    return date_id


def upsert_option_rows(conn: sqlite3.Connection, rows: Iterable[tuple[object, ...]]) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    conn.executemany(
        """
        INSERT INTO options_data
            (symbol_ref, date_ref, expiration_ref, type, strike,
             bid, ask, mark, last, implied_volatility, volume, open_interest, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol_ref, date_ref, expiration_ref, type, strike) DO UPDATE SET
            bid = excluded.bid,
            ask = excluded.ask,
            mark = excluded.mark,
            last = excluded.last,
            implied_volatility = excluded.implied_volatility,
            volume = excluded.volume,
            open_interest = excluded.open_interest,
            source = excluded.source
        """,
        materialized,
    )
    return len(materialized)


# --- IB layer ----------------------------------------------------------------

def _read_tws_config() -> tuple[str, int, int]:
    config = configparser.ConfigParser()
    config.read('config.ini')
    host = config.get('tws', 'host', fallback='127.0.0.1').strip() or '127.0.0.1'
    port = config.getint('tws', 'port', fallback=7496)
    client_id = config.getint('tws', 'client_id', fallback=999)
    return host, port, client_id


async def _fetch_contract_daily_bars(ib, contract, *, duration: str, end_datetime: str = ''):
    return await ib.reqHistoricalDataAsync(
        contract,
        endDateTime=end_datetime,
        durationStr=duration,
        barSizeSetting='1 day',
        whatToShow='TRADES',
        useRTH=True,
        formatDate=1,
        keepUpToDate=False,
    )


async def _backfill(args: argparse.Namespace) -> int:
    from ib_async import IB, Option  # imported lazily so pure-logic tests don't need ib_async

    ticker = str(args.ticker).strip().upper()
    expiry_iso = _normalize_expiry_to_iso(args.expiry)
    expiry_ib = expiry_iso.replace('-', '')
    rights = ['C', 'P'] if str(args.right).lower() == 'both' else [_right_to_ib(args.right)]
    strikes = build_strike_grid(args.strike_min, args.strike_max, args.strike_step)

    db_path = os.path.abspath(args.db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    host, port, default_client_id = _read_tws_config()
    client_id = args.client_id if args.client_id is not None else default_client_id

    print(f"DB: {db_path}")
    print(f"Underlying: {ticker}  Expiry: {expiry_iso}  Rights: {rights}")
    print(f"Strikes: {len(strikes)} ({strikes[0]} -> {strikes[-1]} step {args.strike_step})")
    print(f"IBKR: {host}:{port} clientId={client_id}  duration={args.duration}")
    print(f"Total contracts to request: {len(strikes) * len(rights)}")

    ib = IB()
    pacer = HistoricalPacer(max_calls=args.max_calls, window_seconds=args.window_seconds)
    conn = sqlite3.connect(db_path)
    contracts_done = 0
    bars_written = 0

    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=20)
        if args.market_data_type:
            ib.reqMarketDataType(args.market_data_type)

        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_schema(conn)
        symbol_id = _get_symbol_id(conn, ticker)
        date_cache = _build_existing_date_cache(conn)
        expiration_ref = _get_or_create_date_id(conn, date_cache, expiry_iso)

        for right in rights:
            option_type = _normalize_option_type(right)
            for strike in strikes:
                contract = Option(ticker, expiry_ib, strike, right, args.exchange, currency='USD')
                try:
                    qualified = await ib.qualifyContractsAsync(contract)
                except Exception as exc:
                    print(f"  qualify failed {ticker} {expiry_iso} {strike}{right}: {exc}", file=sys.stderr)
                    continue
                if not qualified:
                    print(f"  no contract for {ticker} {expiry_iso} {strike}{right}", file=sys.stderr)
                    continue

                pacer.acquire()
                try:
                    bars = await _fetch_contract_daily_bars(ib, qualified[0], duration=args.duration)
                except Exception as exc:
                    print(f"  history failed {ticker} {expiry_iso} {strike}{right}: {exc}", file=sys.stderr)
                    continue

                rows: list[tuple[object, ...]] = []
                for bar in bars or []:
                    quote_iso = _bar_date_to_iso(getattr(bar, 'date', None))
                    if not quote_iso:
                        continue
                    date_ref = _get_or_create_date_id(conn, date_cache, quote_iso)
                    close = getattr(bar, 'close', None)
                    volume_raw = getattr(bar, 'volume', None)
                    try:
                        volume = int(volume_raw) if volume_raw is not None else None
                    except (TypeError, ValueError):
                        volume = None
                    rows.append((
                        symbol_id, date_ref, expiration_ref, option_type, float(strike),
                        None,                       # bid (TRADES has no bid/ask)
                        None,                       # ask
                        close,                      # mark <- close
                        close,                      # last
                        None,                       # implied_volatility
                        volume,
                        None,                       # open_interest
                        'ibkr',
                    ))

                bars_written += upsert_option_rows(conn, rows)
                conn.commit()
                contracts_done += 1
                if contracts_done % 10 == 0:
                    print(f"  ...{contracts_done} contracts, {bars_written} bars so far")

        print(f"Done. Contracts processed: {contracts_done}  Bars upserted: {bars_written}")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
        if ib.isConnected():
            ib.disconnect()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill option daily bars from IBKR (OPRA) into spy_options.db."
    )
    parser.add_argument("--db-path", default=_default_db_path(),
                        help="SQLite DB path. Defaults to sqlite_spy/spy_options.db")
    parser.add_argument("--ticker", default="SPY", help="Underlying symbol. Defaults to SPY.")
    parser.add_argument("--expiry", required=True, help="Option expiry, YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--strike-min", type=float, required=True, help="Inclusive lowest strike.")
    parser.add_argument("--strike-max", type=float, required=True, help="Inclusive highest strike.")
    parser.add_argument("--strike-step", type=float, default=5.0, help="Strike increment. Defaults to 5.")
    parser.add_argument("--right", default="both", choices=["both", "C", "P", "call", "put"],
                        help="Option right(s) to fetch. Defaults to both.")
    parser.add_argument("--exchange", default="SMART", help="Option exchange. Defaults to SMART.")
    parser.add_argument("--duration", default="1 Y", help="reqHistoricalData durationStr. Defaults to '1 Y'.")
    parser.add_argument("--client-id", type=int, default=None,
                        help="IBKR clientId. Defaults to [tws].client_id; use a distinct value to avoid clashing with ib_server.")
    parser.add_argument("--market-data-type", type=int, default=1,
                        help="reqMarketDataType (1=Live). 0 to skip. Defaults to 1.")
    parser.add_argument("--max-calls", type=int, default=60,
                        help="Max reqHistoricalData calls per pacing window. Defaults to 60.")
    parser.add_argument("--window-seconds", type=float, default=600.0,
                        help="Pacing window in seconds. Defaults to 600 (10 min).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_backfill(args))


if __name__ == "__main__":
    sys.exit(main())
