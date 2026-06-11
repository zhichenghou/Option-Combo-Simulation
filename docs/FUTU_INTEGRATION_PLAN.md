# Futu Live Data + IBKR Historical Integration Plan

## 1. Goal

Add **Futu OpenAPI** as the real-time market data source for the live page, while keeping **IBKR (with OPRA subscription)** as the source for historical option data that backfills `sqlite_spy/spy_options.db`.

This is a **research/learning configuration**. Trading is explicitly out of scope for Futu. When real trading is enabled later, it stays on IBKR with the paid `Snapshot Bundle + OPRA` subscriptions.

## 2. Non-goals (explicit guardrails)

- ❌ **Do NOT route any order placement through Futu.** No Futu trade API calls, no Futu account auth for trading, no Futu order objects in the codebase. Futu is data-only.
- ❌ **Do NOT remove or break the existing IBKR live path (`ib_server.py`).** It remains the future production execution surface. The Futu path runs alongside it as an alternative live data source.
- ❌ **Do NOT mix Greeks sources at runtime.** The live page either reads all Greeks/IV/quotes from Futu, or all from IBKR. Mixing introduces silent model-assumption drift (different `r`, `q`, dividend treatment).
- ❌ **Do NOT change the historical page's data path.** `historical_server.py` already reads `sqlite_spy/spy_options.db` directly and is data-source agnostic. The DB content is the contract.
- ❌ **Do NOT depend on the Futu free tier persisting.** LV3 stock data is described as a promotional offer; design code so the data-type/tier can be downgraded or switched off without code changes.

## 3. Background & decision rationale

### Why split sources

| Concern | Resolution |
|---|---|
| IBKR live SPY without paid Snapshot Bundle returns 15-min delayed data (error 10089). | Futu offers free LV3 (Nasdaq Basic + TotalView + NYSE Arcabook) for US stocks during promotion, and free LV1 US options for users with US-stock assets > 0 or US-option positions. |
| Need full-precision Greeks/IV for IV term structure and delta-hedge analysis. | Futu snapshot response returns `option_delta/gamma/theta/vega/rho/implied_volatility` as `float`. The 3-decimal/integer-IV precision documented in the option-chain `OptionDataFilter` applies only to **filter inputs**, not response data. (Verified at <https://openapi.futunn.com/futu-api-doc/quote/get-market-snapshot.html>.) |
| Want option historical data without paying for IBKR Snapshot ($10/mo). | IBKR OPRA Top of Book is $1.50/mo and unlocks intraday option historical via `reqHistoricalData`. Stock historical works under IBKR's free baseline (HMDS daily bars). |
| Real money still on IBKR. | Trading flow untouched; Futu is purely a quote feed for the live page. |

### Cost summary

| Source | Used for | Monthly cost |
|---|---|---|
| Futu OpenAPI (free LV3 + LV1) | Real-time SPY + options + Greeks/IV on live page | $0 |
| IBKR OPRA Top of Book | Backfill `spy_options.db` (option historical) | $1.50 |
| IBKR baseline HMDS | Underlying daily bars backfill | $0 |
| **Total** | | **$1.50/mo** |

When trading is later enabled, add **IBKR US Securities Snapshot and Futures Value Bundle ($10/mo)** so execution-side data and analysis-side data come from the same source. At that point the Futu live path can be disabled or kept as a backup.

## 4. Current architecture (as of this document)

```
                    ┌─ marketDataMode=live ───────────► ib_server.py            :8765
                    │                                    ↳ IBKR live data + trade execution
index.html ─WS─────┤
                    └─ marketDataMode=historical ─────► historical_server.py    :8765
                                                         ↳ reads sqlite_spy/spy_options.db
```

- **Frontend** (`index.html`): vanilla JS with `marketDataMode` selector (`live` / `historical`). Selecting a mode chooses which backend the WebSocket client connects to.
- **`ib_server.py`** (~1400 lines + helpers in `ib_server_market_data.py`, `ib_server_ws.py`, `ib_server_order_tracking.py`, `ib_server_iv_term_structure.py`): single persistent IBKR connection via `ib_async`, broadcasts to many WS clients.
- **`historical_server.py`**: independent server, reads `spy_options.db` only. Does not touch IBKR.
- Both servers bind to `[server] ws_port` from `config.ini` — currently **mutually exclusive**, user starts one or the other.
- Existing WS actions implemented by `ib_server.py`:
  - `connect_ib`, `subscribe`, `sync_underlying`
  - `request_managed_accounts_snapshot`, `request_portfolio_avg_cost_snapshot`
  - `subscribe_iv_term_structure`
  - `request_active_hedge_orders_snapshot`, `cancel_hedge_order`, `preview_hedge_order`
  - `cancel_managed_combo_order`, `concede_managed_combo_order`, `resume_managed_combo_order`
  - `request_historical_bars` (still in source; live path)
  - `request_ib_connection_status`

## 5. Target architecture

```
                    ┌─ marketDataMode=live + DATA_SOURCE=futu ───► futu_server.py       :8765   [NEW]
                    │                                              ↳ FutuOpenD (local gateway)
                    │                                                  ↳ Futu cloud (LV3 / LV1)
                    │
index.html ─WS─────┤─ marketDataMode=live + DATA_SOURCE=ibkr ───► ib_server.py         :8765   [unchanged, future trading]
                    │
                    └─ marketDataMode=historical ────────────────► historical_server.py :8765   [unchanged]
                                                                    ↳ sqlite_spy/spy_options.db
                                                                          ▲
                                                                          │ offline batch
                                                                          │
                                                                   scripts/backfill_options_ibkr.py [NEW]
                                                                          ↳ IBKR + OPRA $1.50/mo
                                                                          ↳ scripts/import_yahoo_underlying_daily.py (existing) for daily stock
```

Key properties:

- The three servers remain mutually exclusive (single port 8765). Selection is by **launcher script + env var** (e.g. `DATA_SOURCE=futu | ibkr`, default `futu` for live).
- **`historical_server.py` is unchanged** — the DB schema is the contract.
- **`ib_server.py` is unchanged** for this milestone — it stays available for the future paid-subscription trading mode.
- `futu_server.py` reimplements **only the read-only WS actions** required by the live page. No order-related actions are implemented.

## 6. Implementation tasks

Tasks are ordered. Each task is independently verifiable.

### T1. Install / verify FutuOpenD gateway (no code)
- Download FutuOpenD from <https://www.futunn.com/download/openAPI>. Run locally; default port `11111`.
- Document the login + market-data-permission steps in `README.md` (or in this file's "Operations" section).
- **Acceptance**: `futu.OpenQuoteContext(host='127.0.0.1', port=11111)` connects and `get_market_state(['US.SPY'])` returns a response.

### T2. Define typed WS protocol contract

`futu-api` is already declared in `pyproject.toml`. Before writing the server, enumerate the **exact set of WS actions** the live page sends to `ib_server.py` and the **exact shape of broadcasts the page expects back**. This becomes the implementation surface for `futu_server.py`.

- Read `js/ws_client.js`, `js/control_panel_ui.js`, `js/group_ui.js`, `js/iv_term_structure.js`, `js/delta_hedge_*.js` to enumerate every `send({action: ...})`.
- Read `ib_server_ws.py` + `ib_server_market_data.py` + `ib_server_iv_term_structure.py` for the corresponding response payloads.
- Produce: `docs/ws_protocol.md` (or inline Pydantic models in a new `protocol.py`) listing each action, request fields, response fields. Pydantic models give type safety and validation at runtime — strongly preferred.
- **Acceptance**: A list of read-only WS actions that `futu_server.py` must implement, plus the data types they exchange.

### T3. Build `futu_server.py`

Mirror the structure of `historical_server.py` (small, single-purpose) plus the live-data subscription model from `ib_server_market_data.py`.

#### Required actions (read-only subset)

Implement only these (no execution endpoints):

| WS action | Notes |
|---|---|
| `subscribe` | Subscribe Futu quote stream for underlying + selected option codes. Pay attention to the **60 option-chain subscription quota** (each `(underlying, expiry)` = 1). Implement LRU eviction when nearing limit. |
| `sync_underlying` | Map IBKR-style request (`secType=STK`, `symbol=SPY`) to Futu code (`US.SPY`). Return current quote + meta. |
| `subscribe_iv_term_structure` | Compose from per-expiry option chain + IV data. Note: Futu returns IV at the **option** level, not term-structure level — server aggregates. |
| `request_managed_accounts_snapshot` | Return an empty/stub payload (`{accounts: []}`) — there are no managed IBKR accounts in this path. Frontend should treat empty as "no IB connection". |
| `request_portfolio_avg_cost_snapshot` | Return empty payload (`{positions: []}`). Same reasoning. |
| `request_active_hedge_orders_snapshot` | Return empty payload (`{orders: []}`). |
| `request_ib_connection_status` | Return a synthetic `{connected: false, source: 'futu'}` so the page knows not to expose trading UI. **The frontend must use this signal to hide order placement controls.** |

#### Symbol mapping

- Futu uses codes like `US.SPY`, `US.SPY250620C500000` (underlying.symbol expiry strike right format varies — see Futu docs).
- IBKR uses `(conId, symbol, lastTradeDateOrContractMonth, strike, right)`.
- Add `symbol_mapping.py` with bidirectional helpers and unit tests covering:
  - SPY ETF stock
  - SPY option weekly + monthly
  - PM- vs AM-settled distinguishing rules where applicable
- **Do NOT call IBKR for `conId` lookup in this server.** Build the mapping deterministically from contract attributes, or pre-populate a static lookup.

#### Quote dispatch

- Futu push callbacks → normalize to the same payload shape `ib_server_market_data.py` produces (`{action: 'quote_update', symbol, bid, ask, last, ts, ...}`).
- Greeks come from Futu's option snapshot (`option_delta`, `option_gamma`, `option_theta`, `option_vega`, `option_rho`, `option_implied_volatility`). All `float`; pass through unchanged.

#### Quota management

- Track `(underlying, expiry)` subscriptions in a dict.
- When near 60: evict the least-recently-used expiry's subscription. Log it. Surface a `quota_warning` action to the frontend.

#### Connection lifecycle

- `FutuOpenD` must be running locally; if not reachable, log a clear error (mirror the `ib_server.py` retry pattern at `connect_ib()`).
- Reconnect with backoff on disconnect.

### T4. Wire `futu_server.py` into launcher

Update `start_option_combo_uv.sh`:

```bash
# Pick live data source: futu | ibkr. Defaults to futu (free).
DATA_SOURCE="${DATA_SOURCE:-futu}"

case "$DATA_SOURCE" in
  futu)  uv run python futu_server.py >>"$IB_LOG" 2>&1 & ;;
  ibkr)  uv run python ib_server.py   >>"$IB_LOG" 2>&1 & ;;
  *)     echo "Unknown DATA_SOURCE: $DATA_SOURCE"; exit 1 ;;
esac
```

- Add a sibling `start_option_combo_uv_historical.sh` that starts `historical_server.py` instead.
- Document in `README.md`: "set `DATA_SOURCE=ibkr` when you have paid IBKR subscriptions and want to use the IBKR live path".

### T5. Frontend: hide trading affordances under Futu mode

The WS server emits `{action: 'ib_connection_status', connected: false, source: 'futu'}`.

Frontend changes (in `js/control_panel_ui.js`, `js/group_ui.js`, `js/delta_hedge_ui.js`):

- When `source === 'futu'`, hide / disable:
  - Combo order submission buttons
  - Hedge order preview / submit
  - Managed account selector
  - Any "place order" / "modify order" controls
- Show a small banner: "Live data via Futu (research mode). Trading disabled."

**Acceptance**: With `DATA_SOURCE=futu`, the UI has no path to submit any order. Manual code review of every `submit/place/order` event handler confirms it short-circuits.

### T6. Add `[market_data]` source toggle to `config.ini`

```ini
[market_data]
# Live data source for the live page: futu | ibkr.
# - futu: read-only research mode using FutuOpenD (free LV3/LV1).
# - ibkr: production mode using ib_server.py (requires paid IBKR subscriptions for full data).
live_source = futu

# IBKR reqMarketDataType (only effective when live_source = ibkr).
# 1 = Live, 2 = Frozen, 3 = Delayed, 4 = Delayed Frozen.
data_type = 3

[futu]
host = 127.0.0.1
port = 11111
# Optional: connection password if your FutuOpenD requires one.
# password =
```

`futu_server.py` reads `[futu]` and `[market_data].live_source`.

### T7. Backfill scripts for `spy_options.db`

Existing scripts:
- `scripts/import_yahoo_underlying_daily.py` — already handles underlying daily bars.
- `scripts/import_treasury_risk_free_rate.py` — risk-free rate.

Add **`scripts/backfill_options_ibkr.py`**:

- Connects to IBKR (TWS/Gateway) — requires the OPRA subscription on the account.
- For a given (underlying, expiry, strike_range) sweep, calls `reqHistoricalData` per contract.
- Writes results into `spy_options.db` matching the schema `historical_data.py` reads (study `historical_data.py` to recover the exact tables/columns — main tables include `symbols`, `dates`, `underlying_daily_prices`, `risk_free_daily_rates`, plus the option-specific tables).
- **Respect IBKR pacing**: max 60 `reqHistoricalData` calls per 10-minute rolling window. Implement a token bucket; never burst.
- **Avoid duplicates**: check existing rows before requesting.
- Idempotent: re-runs skip already-stored bars.

This is the most data-engineering-heavy task. Treat it as a separate work package once Futu live is shipped.

### T8. Operational documentation

Add `docs/operations.md` (or expand `README.md`):

- How to start FutuOpenD.
- How to flip `DATA_SOURCE` between `futu` and `ibkr`.
- How to run `scripts/backfill_options_ibkr.py`.
- IBKR subscription matrix: which subscription unlocks what (table from this doc's §3).
- Known limits: Futu 60-chain subscription quota, FutuOpenD must be local, no Futu trading.

## 7. Data-source contract (what must NOT diverge)

For the **live page** under Futu, the WS payload shape must be **byte-compatible** with what `ib_server.py` already broadcasts. If `ib_server_market_data.py` emits:

```json
{"action": "quote_update", "symbol": "SPY", "bid": 521.10, "ask": 521.12, "last": 521.11, "ts": 1718000000}
```

then `futu_server.py` emits the same shape. **No frontend changes for the message format.** This is what keeps the frontend mode-agnostic.

When fields don't exist on the Futu side (e.g. some IBKR-specific tick types), emit `null` rather than omit the key, so the frontend's null-handling stays consistent.

## 8. Risks & known limits

- **Futu LV3 free tier is promotional.** If it ends, this design falls back to delayed data or requires a paid LV1. Code must already handle "subscription denied" responses gracefully (degrade to delayed quotes, surface a UI warning).
- **60-chain subscription quota.** Adequate for SPY-focused research (one underlying, many expiries). Multi-underlying expansion will hit it. Implement LRU eviction and a `quota_warning` WS broadcast.
- **FutuOpenD must run locally.** Adds an external dependency to the dev environment. Document the install/login flow.
- **Different model assumptions between Futu Greeks and IBKR Greeks.** If a user switches `DATA_SOURCE` mid-day, charts will jump slightly. Acceptable for research mode; document it.
- **No trading path is added.** Any future trading work re-enables the IBKR live path (`DATA_SOURCE=ibkr`) and assumes the user has bought Snapshot + OPRA subscriptions.
- **Symbol mapping bugs are silent and dangerous.** A wrong (underlying, expiry, strike, right) mapping mis-prices an entire group. Unit tests in `symbol_mapping.py` are non-optional.
- **Historical option backfill is rate-limited by IBKR (60 req / 10 min).** Full SPY backfill takes hours, possibly days. Plan accordingly.

## 9. Reference materials

### Futu OpenAPI
- Permissions / data tiers: <https://openapi.futunn.com/futu-api-doc/intro/authority.html>
- Option chain (static): <https://openapi.futunn.com/futu-api-doc/quote/get-option-chain.html>
- Snapshot (dynamic, includes option Greeks): <https://openapi.futunn.com/futu-api-doc/quote/get-market-snapshot.html>
- Python SDK: <https://openapi.futunn.com/futu-api-doc/quick/demo.html>

Key facts confirmed by documentation:
- Option snapshot returns `option_delta`, `option_gamma`, `option_theta`, `option_vega`, `option_rho`, `option_implied_volatility` as `float`.
- The 3-decimal Greeks / integer IV precision is on the **OptionDataFilter input**, **not** the response.

### IBKR
- Market data subscriptions list: <https://www.interactivebrokers.com/en/index.php?f=14193>
- Pacing rules for `reqHistoricalData`: <https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/#hist-pacing-limitations>

### Project files most relevant to this work
| File | Why it matters |
|---|---|
| `ib_server.py` | Reference for live server lifecycle, IB connection management, multi-client WS broadcast. |
| `ib_server_market_data.py` | Quote-update payload shapes to mirror in Futu server. |
| `ib_server_iv_term_structure.py` | IV term structure aggregation logic the Futu server must replicate. |
| `historical_server.py` | Mirror its lightweight structure when scaffolding `futu_server.py`. |
| `historical_replay_service.py` + `historical_data.py` | Define the `spy_options.db` schema the backfill script must respect. |
| `config.ini` | Where the `[market_data]` and `[futu]` sections live. |
| `start_option_combo_uv.sh` | Launcher to extend with `DATA_SOURCE` switch. |
| `js/ws_client.js` | Frontend WS transport; outgoing action enumeration source of truth. |
| `js/control_panel_ui.js` / `js/group_ui.js` / `js/delta_hedge_ui.js` | Where to add the "trading disabled under Futu" gating. |

## 10. Definition of done

The integration is complete when:

1. Running `DATA_SOURCE=futu ./start_option_combo_uv.sh` opens the live page with real-time SPY + options + Greeks streaming from Futu, with no IBKR connection required.
2. The same page under `DATA_SOURCE=ibkr` still works exactly as today (regression: zero changes in behavior when paid IBKR subscriptions are present).
3. The historical replay page is unchanged in behavior.
4. **No order can be submitted under `DATA_SOURCE=futu`.** Code review confirms every order entry point checks the data-source flag and short-circuits, or the UI hides the control entirely.
5. `scripts/backfill_options_ibkr.py` can populate `spy_options.db` from IBKR (with OPRA subscription) and the resulting DB is replayed correctly by `historical_server.py`.
6. `docs/operations.md` (or `README.md`) explains the setup end-to-end for a new developer.
