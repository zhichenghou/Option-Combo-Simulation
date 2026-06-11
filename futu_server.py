"""Live market-data WebSocket server backed by the Futu OpenAPI.

This is the ``DATA_SOURCE=futu`` alternative to ``ib_server.py``. It serves the
read-only subset of WS actions the live page needs, sourcing real-time quotes,
IV and Greeks from a local FutuOpenD gateway. It implements **no** trading
endpoints: managed-accounts snapshots report ``ibConnected: false`` so the
frontend disables every order-entry control.

Outgoing main quote frames are byte-compatible with ``ib_server.py`` (see
``runtime_contracts.LiveMarketDataPayload``): no ``action`` key, option keys
equal the leg ``id`` the frontend sent in ``subscribe``.
"""

import asyncio
import configparser
import json
import logging
import math
import signal
import threading
from dataclasses import dataclass, field
from typing import Any

import websockets
from futu import (
    RET_OK,
    OpenQuoteContext,
    OrderBookHandlerBase,
    StockQuoteHandlerBase,
    SubType,
)

import symbol_mapping
from runtime_contracts import LiveMarketDataPayload, ManualUnderlyingSyncPayload, QuoteSnapshot


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

config = configparser.ConfigParser()
config.read('config.ini')

WS_HOST = '127.0.0.1'
WS_PORT = config.getint('server', 'ws_port', fallback=8765)
FUTU_HOST = config.get('futu', 'host', fallback='127.0.0.1').strip() or '127.0.0.1'
FUTU_PORT = config.getint('futu', 'port', fallback=11111)

SUB_TYPES = [SubType.QUOTE, SubType.ORDER_BOOK]


# --- Runtime state -----------------------------------------------------------

MAIN_LOOP: asyncio.AbstractEventLoop | None = None
quote_ctx: OpenQuoteContext | None = None

connected_clients: set[Any] = set()

# Latest values written by Futu callback threads, read by the asyncio loop.
_cache_lock = threading.Lock()
latest_quote_by_code: dict[str, dict[str, Any]] = {}
latest_book_by_code: dict[str, dict[str, Any]] = {}

# futu_code -> set of websockets subscribed to it (used for fan-out + refcount).
futu_code_subscribers: dict[str, set[Any]] = {}


@dataclass
class ClientSubState:
    greeks_enabled: bool = False
    # futu_code -> (kind, key); kind in {underlying, option, future, stock}
    code_to_target: dict[str, tuple[str, str]] = field(default_factory=dict)


client_subscriptions: dict[Any, ClientSubState] = {}


# --- Numeric helpers ---------------------------------------------------------

def _sanitize(value: Any) -> float | None:
    """Coerce a Futu/pandas cell to a positive float, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number) or number <= 0:
        return None
    return round(number, 4)


def _sanitize_signed(value: Any) -> float | None:
    """Coerce to a finite float (Greeks may be negative), else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number):
        return None
    return number


def _build_quote_snapshot(bid: float | None, ask: float | None, last: float | None) -> QuoteSnapshot | None:
    """Mirror ib_server_market_data.extract_quote_snapshot semantics."""
    mark: float | None = None
    if bid is not None and ask is not None:
        mark = round((bid + ask) / 2, 4)
    elif last is not None:
        mark = last
    if mark is None:
        mark = bid if bid is not None else ask
    if mark is None:
        return None

    if bid is None:
        bid = mark
    if ask is None:
        ask = mark
    return {'bid': bid, 'ask': ask, 'mark': round(mark, 4)}


def _empty_live_payload() -> LiveMarketDataPayload:
    return {
        'underlyingPrice': None,
        'underlyingQuote': None,
        'options': {},
        'futures': {},
        'stocks': {},
    }


# --- WS send helper ----------------------------------------------------------

async def send_message_safe(ws, message):
    try:
        await ws.send(message)
    except Exception:
        pass


# --- Futu callback handlers (run on SDK threads) -----------------------------

def _dispatch_from_futu_thread(code: str) -> None:
    loop = MAIN_LOOP
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(_on_futu_code_update, code)


class FutuQuoteHandler(StockQuoteHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK:
            logging.warning("Futu quote callback error: %s", data)
            return ret, data
        try:
            for _, row in data.iterrows():
                code = row.get('code')
                if not code:
                    continue
                with _cache_lock:
                    latest_quote_by_code[code] = {
                        'last': row.get('last_price'),
                        'iv': row.get('implied_volatility'),
                        'delta': row.get('delta'),
                    }
                _dispatch_from_futu_thread(code)
        except Exception:
            logging.exception("Failed to process Futu quote callback")
        return ret, data


class FutuOrderBookHandler(OrderBookHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK:
            logging.warning("Futu order-book callback error: %s", data)
            return ret, data
        try:
            code = data.get('code') if isinstance(data, dict) else None
            if not code:
                return ret, data
            bid_levels = data.get('Bid') or []
            ask_levels = data.get('Ask') or []
            bid = bid_levels[0][0] if bid_levels else None
            ask = ask_levels[0][0] if ask_levels else None
            with _cache_lock:
                latest_book_by_code[code] = {'bid': bid, 'ask': ask}
            _dispatch_from_futu_thread(code)
        except Exception:
            logging.exception("Failed to process Futu order-book callback")
        return ret, data


# --- Quote aggregation + broadcast (runs on MAIN_LOOP) -----------------------

def _fill_payload_for_code(
    payload: LiveMarketDataPayload,
    code: str,
    kind: str,
    key: str,
    greeks_enabled: bool,
) -> bool:
    with _cache_lock:
        quote_row = dict(latest_quote_by_code.get(code) or {})
        book_row = dict(latest_book_by_code.get(code) or {})

    bid = _sanitize(book_row.get('bid'))
    ask = _sanitize(book_row.get('ask'))
    last = _sanitize(quote_row.get('last'))
    snapshot = _build_quote_snapshot(bid, ask, last)
    if snapshot is None:
        return False

    if kind == 'underlying':
        payload['underlyingPrice'] = snapshot['mark']
        payload['underlyingQuote'] = snapshot
        return True
    if kind == 'future':
        payload['futures'][key] = snapshot
        return True
    if kind == 'stock':
        payload['stocks'][key] = snapshot
        return True

    # option
    option_quote: dict[str, Any] = dict(snapshot)
    raw_iv = _sanitize(quote_row.get('iv'))
    if raw_iv is not None:
        option_quote['iv'] = round(raw_iv / 100.0, 6)  # Futu IV is a percentage
    if greeks_enabled:
        delta = _sanitize_signed(quote_row.get('delta'))
        if delta is not None:
            option_quote['delta'] = round(delta, 6)
    payload['options'][key] = option_quote
    return True


def _on_futu_code_update(code: str) -> None:
    subscribers = futu_code_subscribers.get(code)
    if not subscribers:
        return
    for ws in list(subscribers):
        state = client_subscriptions.get(ws)
        if state is None:
            continue
        target = state.code_to_target.get(code)
        if target is None:
            continue
        kind, key = target
        payload = _empty_live_payload()
        if _fill_payload_for_code(payload, code, kind, key, state.greeks_enabled):
            asyncio.create_task(send_message_safe(ws, json.dumps(payload)))


# --- Futu connection lifecycle ----------------------------------------------

async def connect_futu_with_backoff() -> None:
    global quote_ctx
    delay = 5
    while True:
        try:
            ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
            ctx.set_handler(FutuQuoteHandler())
            ctx.set_handler(FutuOrderBookHandler())
            ret, state = await asyncio.to_thread(ctx.get_global_state)
            if ret != RET_OK:
                await asyncio.to_thread(ctx.close)
                raise RuntimeError(f"FutuOpenD not ready: {state}")
            quote_ctx = ctx
            logging.info("Connected to FutuOpenD at %s:%s", FUTU_HOST, FUTU_PORT)
            return
        except Exception as exc:
            logging.error(
                "Cannot connect to FutuOpenD at %s:%s (%s). Retrying in %ss. "
                "Is FutuOpenD running and logged in?",
                FUTU_HOST, FUTU_PORT, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _futu_subscribe(codes: set[str]) -> None:
    if not codes or quote_ctx is None:
        if codes and quote_ctx is None:
            logging.warning("FutuOpenD not connected; deferring subscribe for %d codes", len(codes))
        return
    try:
        ret, msg = await asyncio.to_thread(quote_ctx.subscribe, sorted(codes), SUB_TYPES)
        if ret != RET_OK:
            logging.warning("Futu subscribe degraded: %s (codes=%s)", msg, sorted(codes))
    except Exception:
        logging.exception("Futu subscribe failed for %s", sorted(codes))


async def _futu_unsubscribe(codes: set[str]) -> None:
    if not codes or quote_ctx is None:
        return
    try:
        ret, msg = await asyncio.to_thread(quote_ctx.unsubscribe, sorted(codes), SUB_TYPES)
        if ret != RET_OK:
            logging.warning("Futu unsubscribe issue: %s (codes=%s)", msg, sorted(codes))
    except Exception:
        logging.exception("Futu unsubscribe failed for %s", sorted(codes))


# --- Per-client subscription bookkeeping -------------------------------------

def _register(ws, state: ClientSubState, code: str, kind: str, key: str) -> bool:
    """Register interest in a code. Returns True if this is a new global code."""
    state.code_to_target[code] = (kind, key)
    subs = futu_code_subscribers.setdefault(code, set())
    is_new = len(subs) == 0
    subs.add(ws)
    return is_new


async def _unsubscribe_client(ws) -> None:
    state = client_subscriptions.get(ws)
    if state is None:
        return
    freed: set[str] = set()
    for code in list(state.code_to_target.keys()):
        subs = futu_code_subscribers.get(code)
        if subs is not None:
            subs.discard(ws)
            if not subs:
                futu_code_subscribers.pop(code, None)
                freed.add(code)
    state.code_to_target.clear()
    await _futu_unsubscribe(freed)


# --- WS action handlers ------------------------------------------------------

def _normalize_bool(value: Any, default_value: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default_value
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'y', 'on'):
        return True
    if text in ('0', 'false', 'no', 'n', 'off'):
        return False
    return default_value


def _managed_accounts_payload() -> dict[str, Any]:
    return {'action': 'managed_accounts_update', 'accounts': [], 'ibConnected': False}


def _portfolio_avg_cost_payload() -> dict[str, Any]:
    return {'action': 'portfolio_avg_cost_update', 'items': []}


async def _handle_subscribe(ws, data, client_ip):
    state = client_subscriptions[ws]
    state.greeks_enabled = _normalize_bool(data.get('greeksEnabled'), False)

    await _unsubscribe_client(ws)

    new_codes: set[str] = set()

    underlying = data.get('underlying') or {}
    try:
        u_code = symbol_mapping.underlying_to_futu_code(underlying)
        if _register(ws, state, u_code, 'underlying', ''):
            new_codes.add(u_code)
    except ValueError as exc:
        logging.error("Invalid underlying from %s: %s (%s)", client_ip, underlying, exc)

    for opt in data.get('options', []):
        leg_id = opt.get('id')
        if not leg_id:
            continue
        try:
            code = symbol_mapping.option_to_futu_code(opt)
        except ValueError as exc:
            logging.error("Cannot map option leg %s: %s", leg_id, exc)
            continue
        if _register(ws, state, code, 'option', leg_id):
            new_codes.add(code)

    for fut in data.get('futures', []):
        fut_id = fut.get('id')
        if not fut_id:
            continue
        try:
            code = symbol_mapping.future_to_futu_code(fut)
        except ValueError as exc:
            logging.error("Cannot map future %s: %s", fut_id, exc)
            continue
        if _register(ws, state, code, 'future', fut_id):
            new_codes.add(code)

    for sym in data.get('stocks', []):
        try:
            code = symbol_mapping.stock_symbol_to_futu_code(sym)
        except ValueError as exc:
            logging.error("Cannot map stock %s: %s", sym, exc)
            continue
        if _register(ws, state, code, 'stock', sym):
            new_codes.add(code)

    logging.info(
        "Subscribe from %s: underlying=%s options=%d futures=%d stocks=%d greeks=%s (new codes=%d)",
        client_ip,
        underlying.get('symbol'),
        len(data.get('options', [])),
        len(data.get('futures', [])),
        len(data.get('stocks', [])),
        state.greeks_enabled,
        len(new_codes),
    )

    await _futu_subscribe(new_codes)


async def _handle_sync_underlying(ws, data, client_ip):
    underlying = data.get('underlying') or {}
    try:
        code = symbol_mapping.underlying_to_futu_code(underlying)
    except ValueError as exc:
        logging.error("Invalid sync_underlying from %s: %s (%s)", client_ip, underlying, exc)
        return

    state = client_subscriptions[ws]
    if _register(ws, state, code, 'underlying', ''):
        await _futu_subscribe({code})

    payload = _empty_live_payload()
    if _fill_payload_for_code(payload, code, 'underlying', '', state.greeks_enabled):
        sync_payload: ManualUnderlyingSyncPayload = {
            'underlyingPrice': payload['underlyingPrice'],
            'underlyingQuote': payload['underlyingQuote'],
            'options': {},
        }
        await send_message_safe(ws, json.dumps(sync_payload))


async def dispatch_client_message(ws, data, client_ip):
    action = data.get('action')
    if action == 'subscribe':
        await _handle_subscribe(ws, data, client_ip)
    elif action == 'sync_underlying':
        await _handle_sync_underlying(ws, data, client_ip)
    elif action == 'request_managed_accounts_snapshot':
        await send_message_safe(ws, json.dumps(_managed_accounts_payload()))
    elif action == 'request_portfolio_avg_cost_snapshot':
        await send_message_safe(ws, json.dumps(_portfolio_avg_cost_payload()))
    elif action == 'request_active_hedge_orders_snapshot':
        await send_message_safe(ws, json.dumps({'action': 'active_hedge_orders_snapshot', 'orders': []}))
    elif action in ('request_ib_connection_status', 'connect_ib'):
        await send_message_safe(ws, json.dumps({
            'action': 'ib_connection_status',
            'connected': False,
            'source': 'futu',
            'host': FUTU_HOST,
            'port': FUTU_PORT,
            'message': 'Live data via Futu (research mode). Trading disabled.',
        }))
    else:
        # Trading actions (combo/hedge) are intentionally unsupported; the
        # frontend disables them while ibConnected is false.
        logging.info("Ignoring unsupported action %r from %s", action, client_ip)


async def handle_ws_client(websocket):
    client_ip = websocket.remote_address[0] if websocket.remote_address else 'Unknown'
    logging.info("Live (Futu) client connected: %s", client_ip)
    connected_clients.add(websocket)
    client_subscriptions[websocket] = ClientSubState()

    # Mirror ib_server: proactively push account/portfolio state on connect so
    # the frontend immediately knows trading is unavailable.
    await send_message_safe(websocket, json.dumps(_managed_accounts_payload()))
    await send_message_safe(websocket, json.dumps(_portfolio_avg_cost_payload()))

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logging.warning("Discarding non-JSON message from %s", client_ip)
                continue
            await dispatch_client_message(websocket, data, client_ip)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        logging.info("Live (Futu) client disconnected: %s", client_ip)
        await _unsubscribe_client(websocket)
        connected_clients.discard(websocket)
        client_subscriptions.pop(websocket, None)


async def main():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()

    logging.info("Starting Futu live WebSocket server on ws://%s:%s", WS_HOST, WS_PORT)
    try:
        ws_server = await websockets.serve(handle_ws_client, WS_HOST, WS_PORT)
    except OSError as exc:
        logging.error(
            "Cannot bind WebSocket server on port %s: %s\n"
            "  A previous backend session is likely still running.\n"
            "  Fix: stop the old process, then restart.",
            WS_PORT, exc,
        )
        return

    futu_task = asyncio.create_task(connect_futu_with_backoff())

    try:
        async with ws_server:
            while True:
                await asyncio.sleep(1)
    finally:
        futu_task.cancel()
        if quote_ctx is not None:
            await asyncio.to_thread(quote_ctx.close)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Futu live server stopped by user.")
