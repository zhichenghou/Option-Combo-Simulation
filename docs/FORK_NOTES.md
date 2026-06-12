# Fork 维护备忘 — 本地新增内容与上游同步

本文档是这个 fork 在上游项目（`xuzhe35/Option-Combo-Simulation`）之上所有本地新增内容的统一说明，以及如何与上游保持同步。文档放在 `docs/` 下，尽量减少与上游同文件的合并冲突。

本 fork 新增的能力：

1. 运行 Futu 实时行情后端（`futu_server.py`）
2. 在 Futu 与 IBKR 之间切换实时数据源
3. 用 IBKR（OPRA）把期权历史数据回填进 `spy_options.db`（`scripts/backfill_options_ibkr.py`）
4. 了解相关的行情订阅档位与已知限制
5. 与上游保持同步（见 §8）

原始设计动机与非目标，参见同目录的 `FUTU_INTEGRATION_PLAN.md`。

---

## 1. 本 fork 新增的文件

| 文件 | 作用 |
| --- | --- |
| `futu_server.py` | 只读实时行情 WS 后端，数据来自 FutuOpenD。推送的行情帧与 `ib_server.py` 字节兼容。不含交易。 |
| `symbol_mapping.py` | IBKR 风格合约 ↔ Futu 代码的确定性映射（`US.SPY`、`US.SPY250620C500000`）。 |
| `scripts/backfill_options_ibkr.py` | 离线批处理：从 IBKR 拉取期权**日内 bar 聚合成日线**写入 `sqlite_spy/spy_options.db`（IBKR 对期权无 EOD 日线，详见 §4）。 |
| `tests/symbol_mapping_test.py` | 映射函数的单元测试。 |
| `tests/backfill_options_ibkr_test.py` | backfill 纯逻辑单测（限速器、归一化、幂等 upsert）。 |

`config.ini` 中的新增配置：

```ini
[market_data]
# 实时页的数据源：futu | ibkr（当前默认 ibkr）
live_source = ibkr
# IBKR 行情类型：1=实时 / 2=冻结 / 3=延迟 / 4=延迟冻结（仅 live_source=ibkr 时生效）
data_type = 1

[futu]
host = 127.0.0.1
port = 11111
# password =   # 仅当你的 FutuOpenD 需要密码时填写
```

> 说明：本 fork 最初以 Futu 免费研究模式为默认，后改为 `ibkr` + `data_type=1`（实时）。要回到免费研究模式，把 `live_source` 改回 `futu`；个人偏好建议放进被 gitignore 的 `config.local.ini` 以免与上游同步冲突。

---

## 2. FutuOpenD 网关

`futu_server.py` 连接的是本地 **FutuOpenD** 网关，而不是直接连 Futu 云端。

1. 从 <https://www.futunn.com/download/openAPI> 下载 FutuOpenD。
2. 本地运行并登录，默认监听端口 `11111`。
3. 确认账户拥有相应的美股行情权限（推广期的免费 LV3 股票 / LV1 期权档位，见 §5）。

快速连通性检查（Python）：

```python
from futu import OpenQuoteContext, RET_OK
ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
print(ctx.get_market_state(['US.SPY']))
ctx.close()
```

如果 FutuOpenD 没有运行，`futu_server.py` 仍会启动并处理 WS 控制帧，只是会以退避策略不断重试网关连接，在网关可达之前不推送行情。

---

## 3. 启动模式与数据源

`start_option_combo_uv.sh` 支持两种运行模式，用**位置参数**或 `MODE` 环境变量选择（默认 `live`）。所有后端都绑定同一个 `ws://127.0.0.1:8765`，前端无需改动，脚本会按模式打印对应的前端 URL。

```bash
# live（默认）：实时行情 + 交易。后端由 DATA_SOURCE 决定（默认 ibkr）
./start_option_combo_uv.sh
./start_option_combo_uv.sh live

# live 下换成 Futu 免费研究模式（不交易）
DATA_SOURCE=futu ./start_option_combo_uv.sh live

# historical：只读 SQLite 回放，跑 historical_server.py，DATA_SOURCE 被忽略
./start_option_combo_uv.sh historical
MODE=historical ./start_option_combo_uv.sh
```

| 模式 | 后端脚本 | 前端入口 URL 参数 |
| --- | --- | --- |
| `live` (`DATA_SOURCE=ibkr`，默认) | `ib_server.py` | `entry=live&marketDataMode=live&lockMarketDataMode=1` |
| `live` (`DATA_SOURCE=futu`) | `futu_server.py` | 同上 |
| `historical` | `historical_server.py` | `entry=historical&marketDataMode=historical&lockMarketDataMode=1` |

> 注意：`live` 模式下 `ib_server.py` 本身也实现了 `request_historical_snapshot`，所以已经跑着 `ib_server` 时也能直接做历史回放，不必再起 `historical_server.py`。

日志：

- Futu 后端：`logs/futu_server.log`
- IBKR 后端：`logs/ib_server.log`
- 历史回放后端：`logs/historical_server.log`
- 前端 HTTP 服务：`logs/http_server.log`

直接运行 `futu_server.py`（不经启动脚本）：

```bash
uv run python futu_server.py
```

### Futu 模式下交易被禁用

`futu_server.py` 返回的 `managed_accounts_update` 中 `ibConnected: false` 且账户列表为空。前端据此让所有下单控件保持禁用。Futu 是**纯数据**，Futu 后端没有实现任何下单动作。需要执行交易时用 `DATA_SOURCE=ibkr`（配合付费订阅）。

---

## 4. 从 IBKR 回填期权历史

`scripts/backfill_options_ibkr.py` 负责填充 `options_data` 表，供 `historical_server.py` / `historical_replay_service.py` 回放读取。它需要运行中的 TWS / IB Gateway，且账户带 **OPRA** 订阅。

```bash
uv run python scripts/backfill_options_ibkr.py \
    --expiry 20250620 \
    --strike-min 480 --strike-max 560 --strike-step 5 \
    --right both \
    --duration "1 Y" \
    --client-id 1001
```

主要参数：

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `--expiry` | 期权到期日，`YYYYMMDD` 或 `YYYY-MM-DD`（必填） | — |
| `--strike-min` / `--strike-max` | 行权价范围（含端点，必填） | — |
| `--strike-step` | 行权价步长 | `5` |
| `--right` | `both` / `C` / `P` / `call` / `put` | `both` |
| `--no-quotes` | 跳过 BID/ASK 两趟、只回填 last/volume（每合约 1 趟而非 3 趟） | 关闭 |
| `--ticker` | 标的代码 | `SPY` |
| `--duration` | `reqHistoricalData` 的 durationStr | `1 Y` |
| `--client-id` | IBKR clientId（用与 `ib_server` 不同的值） | `[tws].client_id` |
| `--market-data-type` | `reqMarketDataType`（1=Live，0=跳过） | `1` |
| `--max-calls` / `--window-seconds` | 限速预算 | `60` / `600` |
| `--db-path` | SQLite DB 路径 | `sqlite_spy/spy_options.db` |

行为说明：

- **为什么是日内聚合**：IBKR 的历史服务**对期权不提供 EOD（`1 day`）日线**——任何到期日 / 交易所 / `whatToShow` 请求 `barSizeSetting='1 day'` 都报 `Error 162: No data of type EODChart`（实盘带 OPRA 也一样，不是权限问题）。但**日内 bar 正常**，且回溯范围只受合约自身寿命限制（≈ `reqHeadTimeStamp` 那天到今天）。因此脚本改为请求 `1 hour`（RTH）bar，再按交易日聚合成一行日线。
- **存储内容**：`last` = 当日最后一根 bar 的 close，`volume` = 当日各 bar 求和（来自 TRADES 趟）；`bid`/`ask` 来自额外的 BID/ASK 两趟（取当日末值），`mark` = bid/ask 中值，缺失时回退到 `last`。`--no-quotes` 只跑 TRADES（每合约 1 趟）。`implied_volatility` / `open_interest` 始终留空——IBKR 历史 bar 不提供，只能靠实时端逐日采集或从价格反算 IV。
- **幂等**：按 `(symbol, quote_date, expiration, type, strike)` 唯一键 upsert，重复运行只刷新数值，不会产生重复行。
- **限速**：滑动窗口限速器把请求控制在 `--max-calls` / `--window-seconds` 以内（IBKR 约 60 次 `reqHistoricalData` / 10 分钟）。注意默认每合约 **3 趟**请求（TRADES+BID+ASK），宽行权价网格很快吃满限速预算；`--no-quotes` 可减到 1 趟。

### 全新环境：先 bootstrap 维表

`symbols` / `dates` 两张维表**没有任何脚本会创建**（设计上依赖上游提供的底库）。全新克隆且没有现成 `spy_options.db` 时，直接跑 backfill 会报 `no such table: symbols`。先建维表（DDL 见 `tests/backfill_options_ibkr_test.py`）：

```bash
sqlite3 sqlite_spy/spy_options.db "
CREATE TABLE IF NOT EXISTS symbols (symbol_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS dates   (date_id   INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT UNIQUE);"
```

### 标的日线与无风险利率

历史回放的 `build_snapshot_payload` 在读期权前会先查标的日线和利率表，**这些表缺失或无数据会让整个回放请求失败**：

- `underlying_daily_prices`：**必须有标的数据**，否则标的快照为 `None` → 整个 payload 返回 `None`，期权不显示。由 `scripts/import_yahoo_underlying_daily.py`（需 yfinance）或从 IBKR 股票日线（`reqHistoricalData` 对**股票**的 `1 day` 是正常的）填充。
- `risk_free_daily_rates` / `yield_curve_daily_rates`：**表必须存在**（取值有 `if` 守卫，空表不崩，但缺表会抛错）。由 `scripts/import_treasury_risk_free_rate.py` 填充，或先建空表占位。

历史回放页本身不变：它直接读取生成的 DB，与数据源无关。

---

## 5. IBKR 订阅矩阵

| 来源 | 用途 | 月费 |
| --- | --- | --- |
| Futu OpenAPI（免费 LV3 + LV1） | 实时页的 SPY + 期权 + 希腊值/IV | $0 |
| IBKR OPRA Top of Book | 回填 `spy_options.db`（期权历史，**仅日内可得**，聚合成日线） | 约 $1.50 |
| IBKR 基础 HMDS | 回填标的日 bar（股票 `1 day` 正常） | $0 |
| IBKR US Securities Snapshot + Futures Value Bundle | 真实交易用的 IBKR 实时数据 | 约 $10 |

将来要做真实交易时，切到 `DATA_SOURCE=ibkr` 并配合付费 Snapshot + OPRA 订阅，让执行侧与分析侧共用同一数据源。

---

## 6. 已知限制

- **Futu LV3 免费档是推广性质的。** 如果取消，数据可能降级为延迟报价或需要付费档。服务端会记录降级的订阅，受影响的腿显示为空报价。
- **期权订阅额度。** Futu 的期权订阅预算（1 万港元资产档约 60 条期权链）足够单标的 SPY 研究，但多标的或宽 IV 期限结构可能超限。订阅失败会优雅降级并记录告警日志。
- **FutuOpenD 必须本地运行。** 它是开发机上的一个外部依赖。
- **希腊值模型差异。** Futu 与 IBKR 计算希腊值/IV 的假设可能不同，盘中切换 `DATA_SOURCE` 会导致图表轻微跳变，研究模式下可接受。
- **美股期权代码格式** 在 `symbol_mapping.option_to_futu_code` 中是推断得到的（`US.<SYMBOL><YYMMDD><C|P><STRIKE*1000>`）。引入新的标的族之前，先用 `get_option_chain('US.SPY', ...)` 校验。
- **没有 Futu 交易路径。** 这是有意为之，交易始终走 IBKR。
- **IBKR 不提供期权 EOD 日线。** 期权历史只能靠日内 bar 聚合（见 §4），且 `implied_volatility` / `open_interest` 历史回填不了。股票/标的日线则正常。
- **回放依赖标的与利率表。** `spy_options.db` 只有 `options_data` 时回放页会报错；需要 `underlying_daily_prices`（有数据）和 `risk_free_daily_rates` / `yield_curve_daily_rates`（至少建表），见 §4。

---

## 7. 测试

纯逻辑单测可离线运行（不需要 FutuOpenD / IBKR）：

```bash
uv run python -m unittest tests.symbol_mapping_test
uv run python -m unittest tests.backfill_options_ibkr_test
```

---

## 8. 与上游保持同步

上游仓库是 `xuzhe35/Option-Combo-Simulation`。你 fork 的 `origin` 是 `zhichenghou/Option-Combo-Simulation`。本地功能开发在 `uv` 分支上进行。

### 一次性配置（已完成）

```bash
git remote add upstream git@github.com:xuzhe35/Option-Combo-Simulation.git
git remote -v   # origin -> 你的 fork，upstream -> xuzhe35
```

### 日常同步流程

1. 拉取上游：

   ```bash
   git fetch upstream
   ```

2. 把你的 `main` 快进到与上游一致（保持干净）：

   ```bash
   git checkout main
   git merge --ff-only upstream/main
   git push origin main
   ```

   如果 `--ff-only` 失败，说明你在 `main` 上有本地提交，改用 `git merge upstream/main` 并解决冲突。

3. 把上游改动合并进工作分支 `uv`（`uv` 已经 push 过，用 merge 最稳妥）：

   ```bash
   git checkout uv
   git merge upstream/main
   # 解决冲突 -> git add <文件> -> git commit
   git push origin uv
   ```

   也可以用 rebase 得到线性历史，但它会改写提交、需要 `git push --force-with-lease`；如果分支有他人协作就别用。

### 本 fork 采用的冲突规避约定

- 新功能放在**新文件**里（`futu_server.py`、`symbol_mapping.py`、`scripts/backfill_options_ibkr.py`、本文档），而不是改上游文件，这样多数上游合并能干净应用。
- 对上游共享文件的改动保持小而集中：
  - `config.ini`：新增 `[market_data].live_source` / `data_type` 和 `[futu]` 段。
  - `start_option_combo_uv.sh`：用 `MODE`（live/historical）+ `DATA_SOURCE`（futu/ibkr）切换。
- `.DS_Store` 已加入 gitignore，不要再提交它。

### 随时检查分叉情况

```bash
git fetch upstream
git rev-list --left-right --count uv...upstream/main
# 左边 = uv 独有的提交（你的工作）
# 右边 = 你还没合并的上游提交（0 表示完全同步）
```
