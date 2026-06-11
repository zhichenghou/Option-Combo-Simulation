"""Bidirectional mapping between IBKR-style contract requests and Futu codes.

These are pure functions with no Futu SDK dependency so they can be unit tested
in isolation. The frontend sends IBKR-style contract dicts (symbol, expDate,
strike, right, ...); the Futu OpenAPI uses string codes like ``US.SPY`` and
``US.SPY250620C500000``.

US option code format (inferred, validated against ``get_option_chain`` at
runtime):

    US.<SYMBOL><YYMMDD><C|P><STRIKE*1000>

e.g. SPY 2025-06-20 call @ 500.0 -> ``US.SPY250620C500000``.
"""

from __future__ import annotations

import re
from typing import Any

_MARKET_PREFIX = 'US.'

_OPTION_CODE_RE = re.compile(r'^US\.([A-Z]+)(\d{6})([CP])(\d+)$')


def _require(value: Any, field: str) -> str:
    text = str(value).strip() if value is not None else ''
    if not text:
        raise ValueError(f"Missing required option field: {field}")
    return text


def underlying_to_futu_code(underlying: dict[str, Any]) -> str:
    """Map an underlying request dict to a Futu code (e.g. ``US.SPY``)."""
    symbol = _require((underlying or {}).get('symbol'), 'symbol')
    return f"{_MARKET_PREFIX}{symbol.upper()}"


def stock_symbol_to_futu_code(symbol: str) -> str:
    """Map a bare stock symbol (hedge leg) to a Futu code (e.g. ``US.SPY``)."""
    text = _require(symbol, 'symbol')
    return f"{_MARKET_PREFIX}{text.upper()}"


def future_to_futu_code(future: dict[str, Any]) -> str:
    """Map a futures request dict to a Futu code.

    Futu's US futures coverage is limited; this mirrors the stock mapping by
    symbol so callers get a deterministic code. Subscription failures degrade
    gracefully in the server layer.
    """
    symbol = _require((future or {}).get('symbol'), 'symbol')
    return f"{_MARKET_PREFIX}{symbol.upper()}"


def option_to_futu_code(opt: dict[str, Any]) -> str:
    """Map an IBKR-style option request dict to a Futu option code."""
    opt = opt or {}
    symbol = _require(opt.get('symbol'), 'symbol').upper()

    exp_date = _require(opt.get('expDate'), 'expDate')
    if len(exp_date) != 8 or not exp_date.isdigit():
        raise ValueError(f"Invalid expDate (expected YYYYMMDD): {exp_date!r}")
    yymmdd = exp_date[2:8]

    right = _require(opt.get('right'), 'right').upper()
    if right not in ('C', 'P'):
        raise ValueError(f"Invalid right (expected C or P): {right!r}")

    raw_strike = opt.get('strike')
    if raw_strike is None or str(raw_strike).strip() == '':
        raise ValueError("Missing required option field: strike")
    try:
        strike_value = float(raw_strike)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid strike: {raw_strike!r}")
    if strike_value <= 0:
        raise ValueError(f"Invalid strike: {raw_strike!r}")
    strike_milli = round(strike_value * 1000)

    return f"{_MARKET_PREFIX}{symbol}{yymmdd}{right}{strike_milli:d}"


def parse_futu_option_code(code: str) -> dict[str, Any]:
    """Parse a Futu US option code back into IBKR-style fields."""
    match = _OPTION_CODE_RE.match(str(code or '').strip())
    if not match:
        raise ValueError(f"Not a recognized US option code: {code!r}")

    symbol, yymmdd, right, strike_milli = match.groups()
    return {
        'symbol': symbol,
        'expDate': f"20{yymmdd}",
        'right': right,
        'strike': int(strike_milli) / 1000,
    }
