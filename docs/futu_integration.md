# Futu Live Data + IBKR Option Backfill — Operations

This document covers the fork-local additions that sit alongside the upstream
project. Keeping it in `docs/` minimizes merge conflicts with upstream files.

It explains how to:

1. run the Futu live market-data backend (`futu_server.py`)
2. switch the live data source between Futu and IBKR
3. backfill historical option data into `spy_options.db` from IBKR (`scripts/backfill_options_ibkr.py`)
4. understand the relevant subscription tiers and known limits

For the original design rationale and non-goals, see `FUTU_INTEGRATION_PLAN.md`.

---

## 1. Components added by this fork

| File | Role |
| --- | --- |
| `futu_server.py` | Read-only live market-data WS backend sourced from FutuOpenD. Byte-compatible quote frames with `ib_server.py`. No trading. |
| `symbol_mapping.py` | Deterministic IBKR-style ↔ Futu code mapping (`US.SPY`, `US.SPY250620C500000`). |
| `scripts/backfill_options_ibkr.py` | Offline batch: pull option daily bars from IBKR (OPRA) into `sqlite_spy/spy_options.db`. |
| `tests/symbol_mapping_test.py` | Unit tests for the mapping helpers. |
| `tests/backfill_options_ibkr_test.py` | Unit tests for backfill pure logic (pacer, normalization, idempotent upsert). |

Config additions live in `config.ini`:

```ini
[market_data]
# Live data source for the live page: futu | ibkr.
live_source = futu

[futu]
host = 127.0.0.1
port = 11111
# password =   # only if your FutuOpenD requires one
```

---

## 2. FutuOpenD gateway

`futu_server.py` talks to a local **FutuOpenD** gateway, not directly to Futu's
cloud.

1. Download FutuOpenD from <https://www.futunn.com/download/openAPI>.
2. Run it locally and log in. Default listen port is `11111`.
3. Make sure your account has the relevant US market-data permission (free LV3
   stock / LV1 option tiers during the promotion, see §5).

Quick connectivity check (Python):

```python
from futu import OpenQuoteContext, RET_OK
ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
print(ctx.get_market_state(['US.SPY']))
ctx.close()
```

If FutuOpenD is not running, `futu_server.py` still starts and serves the WS
control frames; it just retries the gateway connection with backoff and streams
no quotes until the gateway is reachable.

---

## 3. Switching live data source

The launcher picks the backend by the `DATA_SOURCE` environment variable.
Both backends bind the same `ws://127.0.0.1:8765`, so the frontend needs no
change.

```bash
# Default: Futu research mode (free, no trading)
./start_option_combo_uv.sh

# Explicit Futu
DATA_SOURCE=futu ./start_option_combo_uv.sh

# IBKR live path (requires paid IBKR subscriptions + running TWS/Gateway)
DATA_SOURCE=ibkr ./start_option_combo_uv.sh
```

Logs:

- Futu backend: `logs/futu_server.log`
- IBKR backend: `logs/ib_server.log`
- Frontend HTTP server: `logs/http_server.log`

Run `futu_server.py` directly (without the launcher):

```bash
uv run python futu_server.py
```

### Trading is disabled under Futu

`futu_server.py` reports `managed_accounts_update` with `ibConnected: false` and
empty account list. The frontend uses that signal to keep every order-entry
control disabled. Futu is **data-only**; no order action is implemented in the
Futu backend. Use `DATA_SOURCE=ibkr` (with paid subscriptions) for execution.

---

## 4. Backfilling option history from IBKR

`scripts/backfill_options_ibkr.py` populates the `options_data` table that
`historical_server.py` / `historical_replay_service.py` read back. It requires
a running TWS / IB Gateway with an **OPRA** subscription on the account.

```bash
uv run python scripts/backfill_options_ibkr.py \
    --expiry 20250620 \
    --strike-min 480 --strike-max 560 --strike-step 5 \
    --right both \
    --duration "1 Y" \
    --client-id 1001
```

Key flags:

| Flag | Meaning | Default |
| --- | --- | --- |
| `--expiry` | Option expiry, `YYYYMMDD` or `YYYY-MM-DD` (required) | — |
| `--strike-min` / `--strike-max` | Inclusive strike range (required) | — |
| `--strike-step` | Strike increment | `5` |
| `--right` | `both` / `C` / `P` / `call` / `put` | `both` |
| `--ticker` | Underlying symbol | `SPY` |
| `--duration` | `reqHistoricalData` durationStr | `1 Y` |
| `--client-id` | IBKR clientId (use a value distinct from `ib_server`) | `[tws].client_id` |
| `--market-data-type` | `reqMarketDataType` (1=Live, 0=skip) | `1` |
| `--max-calls` / `--window-seconds` | Pacing budget | `60` / `600` |
| `--db-path` | SQLite DB path | `sqlite_spy/spy_options.db` |

Behavior notes:

- **Idempotent**: rows upsert on `(symbol, quote_date, expiration, type, strike)`,
  so reruns refresh values without duplicating.
- **Pacing**: a sliding-window limiter keeps requests under
  `--max-calls` per `--window-seconds` (IBKR allows ~60 `reqHistoricalData`
  calls / 10 min). Full SPY backfills can take hours.
- **What is stored**: daily `TRADES` bars only — `close` is written to both
  `mark` and `last`; `bid`/`ask`/`implied_volatility`/`open_interest` are left
  null. `historical_data.get_option_snapshot` derives `mark` from `last` when
  bid/ask are absent, so replay still works.
- **Underlying daily bars** and **risk-free rates** come from the existing
  `scripts/import_yahoo_underlying_daily.py` and
  `scripts/import_treasury_risk_free_rate.py`.

The historical replay page is unchanged: it reads the resulting DB directly and
is data-source agnostic.

---

## 5. IBKR subscription matrix

| Source | Used for | Monthly cost |
| --- | --- | --- |
| Futu OpenAPI (free LV3 + LV1) | Real-time SPY + options + Greeks/IV on the live page | $0 |
| IBKR OPRA Top of Book | Backfill `spy_options.db` (option historical) | ~$1.50 |
| IBKR baseline HMDS | Underlying daily bars backfill | $0 |
| IBKR US Securities Snapshot + Futures Value Bundle | Live IBKR data path for real trading | ~$10 |

When real trading is enabled later, switch to `DATA_SOURCE=ibkr` with the paid
Snapshot + OPRA subscriptions so execution-side and analysis-side data share one
source.

---

## 6. Known limits

- **Futu LV3 free tier is promotional.** If it ends, the feed may degrade to
  delayed quotes or require a paid tier. The server logs degraded subscriptions
  and the affected legs show null quotes.
- **Option subscription quota.** Futu's option subscription budget (≈60 chains
  on the 10k-HKD asset tier) is enough for single-underlying SPY research but can
  be exceeded for multi-underlying or wide IV-term-structure use. Failed
  subscriptions degrade gracefully with a warning log.
- **FutuOpenD must run locally.** It is an external dependency for the dev box.
- **Greeks model differences.** Futu and IBKR may compute Greeks/IV with
  different assumptions; switching `DATA_SOURCE` mid-day can cause small chart
  jumps. Acceptable for research mode.
- **US option code format** in `symbol_mapping.option_to_futu_code` is inferred
  (`US.<SYMBOL><YYMMDD><C|P><STRIKE*1000>`). Validate against
  `get_option_chain('US.SPY', ...)` before trusting a new symbol family.
- **No Futu trading path.** By design. Trading stays on IBKR.

---

## 7. Tests

Pure-logic unit tests run offline (no FutuOpenD / no IBKR needed):

```bash
uv run python -m unittest tests.symbol_mapping_test
uv run python -m unittest tests.backfill_options_ibkr_test
```
