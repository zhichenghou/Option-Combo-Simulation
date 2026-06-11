# 组合收益分析三图（Chart 1 / Chart 2 / Chart 3）工作原理

本文件说明工作区右侧"P&L 曲线 + 概率分析"三个 canvas 图表背后的计算逻辑、数据来源与建模假设，便于使用者准确解读图形与期望值。

> 适用范围：本文档覆盖三张图组成的组合收益分析链路。实盘下单、历史回放数据加载、IV 期限结构等其它子系统请见 `README.md` / `ARCHITECTURE.md`。

涉及的文件：

- `js/chart.js` —— Chart 1：P&L 主曲线 (`PnLChart`) 与摊销图 (`AmortizationChart`)
- `js/prob_charts.js` —— Chart 2：价格概率密度；Chart 3：P&L × 概率密度；以及 E[P&L] badge
- `js/pricing_core.js` / `js/valuation.js` —— 单腿估值与组合聚合
- `js/product_registry.js` —— 产品规格 / 合约乘数
- `scripts/fit_underlying.py` 与 `js/t_params_db.js` —— Student-t 历史拟合参数

---

## 0. 三张图各自回答的问题（Quick Reference）

| 图 | 横轴 | 纵轴 | 类型 | 回答的问题 |
| --- | --- | --- | --- | --- |
| **Chart 1** P&L 曲线 | 标的价 S | 组合 P&L(S) | 解析 | "如果模拟日那天 S 真的到了某个价，组合赚/赔多少？" |
| **Chart 2** 价格密度 | 标的价 S | 概率密度 f(S) | 蒙卡 | "模拟日那天 S 落在哪里的概率有多大？" |
| **Chart 3** P&L × 密度 | 标的价 S | P&L(S) × f(S) | 解析×蒙卡 | "把 P&L 按概率加权后，每个价格区间贡献了多少 E[P&L]？" |
| **E[P&L] Badge** | — | $ | 蒙卡 | "整个组合在模拟日的预期 P&L 是多少？" |

三张图横轴共享 `getGlobalChartRange()`，可叠看：Chart 1 形状 → Chart 2 概率 → Chart 3 加权贡献。

---

## 1. 计算链路总览

```
[ 用户输入合约/成本/数量 ]      [ 实盘 mark / 历史回放快照 ]
            └──────────┬─────────────────┘
                       ▼
   processLegData(...)  →  各腿 (T, simIV, K, posMultiplier, costBasis, ...)
                       │
        ┌──────────────┼──────────────────────────┐
        ▼              ▼                          ▼
   Chart 1 解析    Chart 3 P&L 网格采样        Worker 蒙卡
   500 + strike    (复用 Chart 1 同一函数)     1M 路径 × nDays
                                                │
                                        ┌───────┴────────┐
                                        ▼                ▼
                                  Chart 2 tDensity   exactExpectedPnL
                                                          │
                                                          ▼
                                                       Badge
```

参数链：

```
[ 历史日线 ] ──► fit_underlying.py (MLE) ──► t_params_db.js  (df, loc, scale)
                                                    │
[ 当前各腿 simIV ] ──► computePortfolioMeanSimIV() ──► portfolioIV
                                                    │
                              _calibrateScale(df, IV) ─► newScale
                                                    │
                                                    ▼
                                             (Worker 输入)
```

关键代码位置：

- Chart 1 `PnLChart`：`js/chart.js:18-623`
- Chart 1 派生 `AmortizationChart`：`js/chart.js:629-1069`
- 蒙卡 Worker：`js/prob_charts.js:48-209`
- Scale 校准：`js/prob_charts.js:229-232`
- 对数正态对照：`js/prob_charts.js:243-250`
- 主线程 P&L 重算（Chart 3 用）：`js/prob_charts.js:284-331`
- 主编排 `updateProbCharts`：`js/prob_charts.js:801-994`

---

## 2. Chart 1：P&L 主曲线（解析）

### 2.1 它做的事

> 在 `simulationDate` 这一天，假设标的价 = S，组合 P&L 就是确定的，把 S 在 `[minS, maxS]` 上扫一遍画出曲线。

**不抽样，不涉及任何概率假设**。所有概率假设都在 Chart 2/3 那边。

> ⚠️ **到期日 vs 中间日的本质差异**：
>
> - 当 `simulationDate = 合约到期日`（或所有腿已平仓 / 都是 underlying leg）→ Chart 1 退化为**纯到期损益图（payoff diagram）**，是分段折线，**完全无模型假设**，只用合约结算公式 `max(0, S−K)` / `max(0, K−S)`。
> - 当 `simulationDate < 合约到期日`（看持仓未来某天会怎样）→ 未到期腿走 **BSM / Black-76 解析估值**，曲线平滑、含 theta/vega，**带模型假设**：标的 GBM、常数 IV（按 `simIV`）、常数 r、欧式行权、无 smile/skew/跳跃/spot-vol 反馈、不显式建模分红或借贷成本。
>
> 也就是说 Chart 1 没有概率假设，但**有没有定价模型假设取决于 simulationDate**。

### 2.2 输入

`PnLChart.draw(group, globalState, minS, maxS)`（`js/chart.js:127`）

- `group` —— 单组或全局打平的组（`group.legs[]`）
- `globalState` —— 含 `simulatedDate / baseDate / underlyingPrice / interestRate / ivOffset / marketDataMode / underlyingSymbol`
- `minS, maxS` —— 价格区间，与 Chart 2/3 共用

派生：
- `simulationDate / quoteDate / currentAnchorUnderlying / underlyingProfile`（`js/chart.js:147-167`）

### 2.3 离散化采样（500 + strike 加密）

代码：`js/chart.js:199-215`

1. 等距 500 点：`step = (maxS - minS) / 499`。
2. **强制把每条腿的 `strike` 与 `strike±0.01` 也插入采样点**——避免 0DTE 折角处峰/谷被均匀网格掉。
3. 去重 + 升序排序。

这是 Chart 1 相对通用 plotting 库的精细之处：**对结构性折角的显式打点**。

### 2.4 单腿"参数化"——`processLegData`

代码：`js/chart.js:168-189`

每条腿映射到：
- `T` —— 模拟日到合约到期的剩余年化期限
- `simIV` —— mark 反推的 BSM 隐含波动率，叠加用户 `ivOffset`
- `strike, type, posMultiplier, costBasis`
- `legCurrentUnderlying` —— 多产品下该腿的当前标的价（如锚 SPX、腿 ES）
- `legViewMode` —— `active / amortized / settlement`

如果有腿处于"未到期但 simIV 不可用"，**直接显示空状态**（`js/chart.js:190-197`）—— 没数据就不瞎画。

### 2.5 单点 P&L 计算

代码：`js/chart.js:217-251`

```
for currentS in evalPoints:
    simValue = 0
    totalCostBasis = 0
    for each leg:
        legScenarioUnderlying = resolveLegScenarioUnderlyingPrice(...)
        pricePerShare = computeSimulatedPrice(
            processedLeg, rawLeg, legScenarioUnderlying,
            legInterestRate, legViewMode,
            simulationDate, quoteDate, ivOffset
        )
        simValue += posMultiplier * pricePerShare
        totalCostBasis += costBasis
    pnl = simValue - totalCostBasis
```

`computeSimulatedPrice`（`js/pricing_core.js`）按四种模式分流：

| 情况 | 估值 |
| --- | --- |
| 已到期 (`T<=0`) | 内在价值 `max(0, S-K)` / `max(0, K-S)` |
| `fixedPrice`（已平仓） | 直接用成交价 |
| `isUnderlyingLeg` | 直接是 `legScenarioUnderlying`（线性 1:1） |
| 否则 | BSM/Black-76 解析公式 |

> **与 Chart 2/3 worker 中的 BSM 完全同源**，所以 Chart 1 与 Chart 3 的 P&L 形状必然一致。

### 2.6 派生信息

- **Break-even（盈亏平衡价）**：相邻点 P&L 符号变化 → 线性插值零点 `t = |y1|/(|y1|+|y2|)`，`(maxS-minS)*5%` 的最小间距过滤重复（`js/chart.js:376-421`）。每个 BE 标 `$价 (±%)`。
- **Max profit / Max loss (in range)**：`trueMaxPnL / trueMinPnL`，**仅区间内极值**，不是数学极限（`js/chart.js:434-437`）。
- **当前价参考线**：在 `currentAnchorUnderlying` 处虚线 + label，多产品下 label 文案区分 STK/Future（`js/chart.js:276-297`）。

### 2.7 可视化处理

- **DPR 缩放** (`resize()` `js/chart.js:56-67`)：保证视网膜清晰。
- **坐标映射**：`mapX = padding.left + ((val-minS)/(maxS-minS))*drawW`，Y 反向。
- **裁剪 + 渐变**：用 0 线把曲线分上下两段，绿/红渐变 + 15% 填充（`js/chart.js:320-369`）。
- **网格**：Y 5 ticks，X 10 ticks，X 轴双行（`$价` + `±%相对当前价`，`js/chart.js:583-606`）。
- **离屏缓存**：`offscreenCanvas` 一次绘制后 cache，hover 只重画 tooltip（`js/chart.js:444-450, 458-466`）。
- **Tooltip**：mousemove → priceAtMouse → 最近点；显示 `Price / Theo P&L / ±%`（`js/chart.js:72-109, 458-540`）。

### 2.8 兄弟图：`AmortizationChart`

`js/chart.js:629-1069`。复用 PnLChart 的框架，但每个 `currentS` 调 `calculateAmortizedCost(group, currentS, globalState)`，纵轴是"被指派/行权后形成的标的持仓的有效成本基础"。旁边的 `marginCanvas` 同步十字光标，画 `S - basis` 的 margin 条带。仅在 `viewMode = settlement / amortized` 流程激活。

---

## 3. Chart 2：价格概率密度（蒙卡）

### 3.1 它做的事

> 在"日对数收益服从 Student-t、独立同分布"假设下，把 1M 条路径走到 `simulationDate` 的末日价格落到 500 个 bin 里，归一化为概率密度。

### 3.2 蒙卡 Worker 输入

`js/prob_charts.js:940-947` postMessage：

| 参数 | 来源 |
| --- | --- |
| `df` | `T_DIST_PARAMS_DB[symbol].df`（历史 MLE） |
| `loc` | 同上 / 0（开 Random Walk） |
| `newScale` | `_calibrateScale(df, portfolioIV)` |
| `nDays` | `diffDays(quoteDate, simulationDate)`（自然日） |
| `nPaths` | `1_000_000` |
| `bins` | `500` |
| `currentPrice` | `_getProbabilityAnchorPrice()` |
| `minS, maxS` | `getGlobalChartRange()`（与 Chart 1 同） |
| `legs[]` | 全部 `includedInGlobal` 组的腿打平 |

### 3.3 单步抽样：Student-t 日对数收益

`js/prob_charts.js:91-95`

```
z     = boxMuller()                  # N(0,1)
chi2  = 2 * gammaMS(df / 2)          # χ²(df)
t     = z / sqrt(chi2 / df)          # 标准 t
sample = loc + scale * t             # 缩放 t
```

- Box–Muller：`js/prob_charts.js:53-57`
- Marsaglia–Tsang Gamma：`js/prob_charts.js:67-84`
- df ≈ 2.5 时接受率 ~98%。

### 3.4 多步：累加得到末日价

```
logRet = 0
for d in 0..nDays-1:
    logRet += tSample(df, loc, newScale)
finalPrice = currentPrice * exp(logRet)
```

代码：`js/prob_charts.js:147-152`

> 用日步累加而非"直接抽一个总 t"——因为 t 之和不再是 t，没有解析闭式。

### 3.5 直方图归一化

- 等宽 bin：`binWidth = (maxS - minS) / bins`
- 落格：`counts[binIdx]++`（仅 minS-maxS 内）
- 归一化：`tDensity[i] = counts[i] / (nPaths * binWidth)`，使 `Σ tDensity·binWidth ≈ 1`
- 主线程 `_smooth(.., 5)` 5 点滑动平均，仅做视觉平滑

代码：`js/prob_charts.js:120-122, 152-158, 195-202, 413-414`

### 3.6 对照虚线：对数正态密度（解析，不抽样）

```
σ_total = (IV/√365)·√nDays
μ_total = loc · nDays
f(s) = (1/(s·σ_total·√(2π))) · exp(-0.5·((ln(s/S0) - μ_total)/σ_total)²)
```

代码：`js/prob_charts.js:243-250, 962-968`

> 与 t 实线**同图对比，差距 = 肥尾假设带来的修正幅度**。

---

## 4. Chart 3：P&L × 概率密度（解析×蒙卡）

### 4.1 它做的事

> 对 Chart 2 的每个 bin 中心 S 同时算两件事：
> - 解析 P&L(S)（与 Chart 1 同源）
> - 抽样得到的密度 f(S)
> 
> 相乘画出 `P&L(S)·f(S)`，曲线下面积 ≈ E[P&L]。

### 4.2 解析 P&L：`_computePortfolioPnLAtPrice`

代码：`js/prob_charts.js:284-331, 970-984`

复用 `processLegData + computeSimulatedPrice`，与 Chart 1 完全同一套估值流程。**不再抽样**——P&L 函数是确定的，只需要密度才需要抽样。

### 4.3 Worker 内的精确 E[P&L]

每条路径 `finalPrice` 出来后，对 `legs[]` 每条腿：

| 情形 | 估值 |
| --- | --- |
| `fixedPrice` 已设 | 直接用 |
| `isUnderlyingLeg` | `legPrice`（线性） |
| 已到期 | `max(0, legPrice − K)` 或 `max(0, K − legPrice)` |
| 未到期 | BSM：`v_opt = legPrice·N(d1) − K·e^(−rT)·N(d2)`（call）等 |

腿求和：`pathPnL += posMultiplier · v_opt − costBasis`，路径求和：

```
exactExpectedPnL = exactPnLSum / nPaths
```

代码：`js/prob_charts.js:160-193`

性能：

- BSM 常量（`v_sqrt_T, inv_v_sqrt_T, d1_const, K_exp_rT`）放路径循环外预算（`js/prob_charts.js:131-138`）。
- `normalCDF` 用 A&S 多项式近似（`js/prob_charts.js:100-106`）。
- E[P&L] 用**全部 1M 路径**（不限于 minS-maxS 内），尾部仍计入。

> Chart 3 曲线下面积 与 Worker 直接累加的 `exactExpectedPnL` 互为校验，差异来自"bin 中心 + 抽样直方图"vs"逐路径精确求和"的离散化误差。

---

## 5. Random Walk 开关

UI 上 `randomWalkToggle` 勾选时：

```js
const loc = useRandomWalk ? 0 : rawLoc;
```

代码：`js/prob_charts.js:881-882`

含义：去掉历史漂移，仅保留肥尾形状与 IV 校准的尺度。"无观点"基线。

---

## 6. 怎么读图（功能说明）

### 6.1 Chart 1 P&L 曲线

**它说明的问题**：组合的"形状"——结构性的盈亏分布在哪个价位段。

**怎么看**：
- **曲线绿色段** → 该价位组合赚钱；**红色段** → 亏钱。
- **黄色 BE 圆点** → 盈亏平衡点，点上的 `±%` 告诉你需要标的价相对当前涨/跌多少才到平衡。
- **左上角 Max Profit / Max Loss (in range)** → 区间内极值，注意是**显示范围内**的，对无界结构（卖跨/卖宽跨/买入裸 call/put）真实极值可能更糟。
- **紫色虚线**（当前价参考） → 正好在曲线上的对应纵坐标，就是"现在不动一动到模拟日的理论 P&L"。
- **悬停** → 看任意价位的精确 P&L 和 ±%。

**典型问题与读法**：
- "我赚到最多需要 S 到哪？" → 看绿色峰值横坐标。
- "下行风险有多大？" → 看红色段最低点（注意区间外可能更低）。
- "结构是 calendar / vertical / butterfly / iron condor 的哪种？" → 看曲线形态（双峰/单峰/平顶/V 字）。
- "改变 simulation date 后形状怎么变？" → 把 simulation date 推到接近到期，曲线会越来越接近"折线段"，0DTE 时是分段折线。

**该疑的地方**：BSM 估值假设常数 IV / r、欧式行权，结构性偏差对深 ITM 美式期权、高分红标的会偏。

### 6.2 Chart 2 价格概率密度

**它说明的问题**：到 simulation date 时，标的价大致会落在哪——以及"如果不考虑肥尾"会偏离多少。

**怎么看**：
- **t 实线/填充** → 工具实际使用的"肥尾"分布。
- **lognormal 虚线** → 严格 BSM 世界（几何布朗运动 + 常数波动率）的对照。
- **两条线分歧大的位置 = 肥尾修正大的位置**，通常出现在远离当前价的两端。
- 任意一段曲线下的面积 ≈ S 落在该区间的概率。

**典型问题与读法**：
- "S 跌到 X 的概率多大？" → 看 X 左侧曲线下面积（粗略可眼估）。
- "尾部风险被低估了吗？" → 实线在远端高于虚线 → 极端情况比 BSM 暗示的更可能发生。
- "Random Walk 开关切换" → 把历史漂移置 0，曲线整体回归到"以当前价为中心"，可看出漂移项贡献了多少偏移。

**该疑的地方**：
- df / loc 是 10 年历史拟合，不一定反映当前 regime。
- 长 horizon (>1 年) 下 i.i.d. 累加偏差累积。

### 6.3 Chart 3 P&L × 概率密度

**它说明的问题**：每个价格区间对总期望 P&L 贡献了多少（"风险贡献定位器"）。

**怎么看**：
- **曲线高于 0**：该价格区间是 E[P&L] 的**正贡献者**——P&L>0 且概率不低。
- **曲线低于 0**：**负贡献者**——P&L<0 而且概率较大，是真正"拖累期望"的地方。
- **曲线下面积总和 ≈ E[P&L]**，与 badge 互为校验。
- 曲线在 0 线附近的位置一般有两种：要么 P&L≈0，要么概率≈0（远尾或 P&L=0 区域）。

**典型问题与读法**：
- "我的卖跨结构哪里最痛？" → 看负峰所在价格段。
- "调整之后期望提升了，靠的是哪段？" → 比较调整前后正峰的位置和高度。
- "尾部到底贡献多少？" → 看远端面积是否非平凡。

**该疑的地方**：曲线本身是"P&L 解析 × 抽样密度"的乘积，远端密度噪声会被 P&L 放大；极端尾部解读要谨慎。

### 6.4 Expected P&L Badge

**它说明的问题**：在所有假设成立时，组合的预期 P&L 是多少（一个数）。

**怎么看**：
- 直接读 $ 数字。它由 1M 路径**全部**逐路径多腿 BSM 求和取平均得到，不限于显示区间。
- 与 Chart 3 曲线下面积一致（差异来自离散化）。
- 切换 Random Walk → 看漂移假设贡献了多少；切换 IV → 看 IV 假设贡献了多少。

**该疑的地方**：
- E[P&L] 不等于"未来真实期望"。
- 同一组合在不同 simulation date / IV / random walk 设定下数值会显著变化，**比较只在保持假设一致时才有意义**。

### 6.5 三图联看的标准流程

1. **先看 Chart 1**：理解结构本身——是看涨/看跌/中性，关键 BE 在哪，最大风险方向。
2. **再看 Chart 2**：理解市场预期分布——肥尾差距大不大，当前价偏向左偏右。
3. **回到 Chart 3**：理解贡献来源——E[P&L] 主要来自哪些价位段，负贡献集中在哪。
4. **看 Badge**：看一个数字总览。
5. **改 simulation date / IV offset / Random Walk** 反复 1–4，做 What-If 分析。

---

## 7. 名词解释：BSM、Black-76、GBM

### 7.1 BSM（Black-Scholes-Merton）

期权定价的经典闭式模型，1973 年提出。**标的为现货 / 股票 / ETF** 时使用。

欧式 call 价格：

```
C = S · N(d1) − K · e^(−rT) · N(d2)
d1 = [ ln(S/K) + (r + ½σ²)·T ] / (σ·√T)
d2 = d1 − σ·√T
```

put 由 put-call parity 推出。`N(·)` 为标准正态 CDF。

输入：标的价 `S`、行权价 `K`、剩余期限 `T`、无风险利率 `r`、波动率 `σ`。
本工具中：`S = legCurrentUnderlying`（或路径价）、`σ = simIV`、`r = legInterestRate`、`T` 由日历差/年化得到。

### 7.2 Black-76

BSM 的变体，1976 年 Fischer Black 提出。**标的为期货 / 远期 / 现金交收指数**（无持仓成本）时使用。

欧式 call 价格：

```
C = e^(−rT) · [ F · N(d1) − K · N(d2) ]
d1 = [ ln(F/K) + ½σ²·T ] / (σ·√T)
d2 = d1 − σ·√T
```

与 BSM 的关键区别：用 **远期/期货价 `F`** 替换现货 `S`，且**不再带 `r·T` 的持仓成本项**（因为期货已经把利率内含到价差里了）。

本工具中：`SPX / NDX`（现金交收指数）和 `ES / NQ / MES / MNQ / CL / GC / SI / HG`（FOP 期货期权）走 Black-76；`SPY / QQQ / GLD / SLV / USO / TLT / AAPL` 等股票/ETF 走 BSM。产品分流由 `js/product_registry.js` 决定。

### 7.3 GBM（Geometric Brownian Motion，几何布朗运动）

BSM/Black-76 背后的**标的价格演化模型**。

标的价格 `S_t` 满足如下随机微分方程（SDE）：

```
dS_t = μ · S_t · dt + σ · S_t · dW_t
```

- `μ` —— 漂移率（在 risk-neutral 测度下取无风险利率 `r`）
- `σ` —— 波动率（BSM 中是常数）
- `dW_t` —— 标准布朗运动（维纳过程）

解出来：

```
S_T = S_0 · exp( (μ − ½σ²) · T + σ · W_T )
```

即 `log(S_T / S_0)` 服从正态分布——这就是为什么 BSM 世界里价格服从**对数正态分布（lognormal）**，也是 Chart 2 那条 lognormal 虚线的由来。

#### "几何"的意思

- **算术布朗运动**：`dS = μ·dt + σ·dW` —— 价格变化是绝对量，价格可能变成负数。
- **几何布朗运动**：`dS = μ·S·dt + σ·S·dW` —— 价格变化按当前价格的**比例**，价格永远 > 0。

"几何"就是"乘性 / 比例性"。日 1% 涨跌相当于乘 1.01 / 0.99，多日累乘 → 取对数后变成累加 → 对数收益服从正态。

#### GBM 隐含的关键假设

1. **价格永远为正**（lognormal 的天然属性）
2. **对数收益服从正态分布** —— 没有肥尾、没有跳跃
3. **波动率 σ 是常数** —— 不随时间、价格、到期变化（即没有 smile / skew / vol clustering）
4. **路径连续** —— 不允许跳空 / gap
5. **增量独立同分布** —— 各时段对数收益 i.i.d.

### 7.4 三者关系一句话

> **GBM** 是关于"标的怎么走"的随机过程假设；**BSM** 是在 GBM 假设下、对**现货**期权的闭式解；**Black-76** 是 BSM 套到**期货 / 现金交收指数**上的等价形式。三者绑定使用：用了 BSM/Black-76，就接受了 GBM 假设。

### 7.5 GBM 在本工具的边界

| 出现位置 | GBM 是否成立 |
| --- | --- |
| Chart 1 中间日 BSM/Black-76 估值 | **是** —— 直接套闭式 |
| Chart 2 lognormal 对照虚线 | **是** —— 对照线就是 GBM 世界的密度 |
| Chart 2 t 实线 / Chart 3 / Badge 的**模拟层** | **故意打破** —— 日收益换成 Student-t（肥尾） |
| Chart 2 t 路径上单腿 BSM 估值 | **是** —— 估值层仍假设 GBM |

> 本工具的关键设计：**估值层保留 GBM（用 BSM/Black-76 给每条腿定价），但模拟层放弃 GBM（用 Student-t 抽样）**。
>
> 这种混搭是有意为之：估值要快要标准（沿用业界共识），模拟要更贴近实证（肥尾、可调漂移）。代价是路径上的"标的服从肥尾"与"腿估值假设标的服从 GBM"在严格意义上不自洽——这是工程取舍。

---

## 8. 事实 / 拟合 / 假设——读数指南

理解输出之前，必须区分模型里哪些是真实数据、哪些是经验估计、哪些是不可避免的简化。

### 7.1 事实（Facts）

真实世界的客观输入，工具不做模型改造。

| 项 | 来源 |
| --- | --- |
| 实盘期权 / 标的 / 期货 / 对冲股票行情 | IB `reqMktData` |
| 历史日线 OHLCV | Yahoo → `underlying_daily_prices` |
| 历史期权日报价 (`options_data`) | 外部数据集预填进 SQLite |
| 美债日度收益率曲线 | U.S. Treasury XML |
| 用户输入：成本、数量、行权价、到期日、closePrice | UI 录入 / 导入 JSON |
| 合约乘数与产品规格 | `js/product_registry.js` |
| 到期日内在价值公式 | `max(0, S−K)` / `max(0, K−S)` |
| Chart 1 采样网格（500 + strike + 微扰） | `js/chart.js:199-215` |
| 蒙卡参数：1M 路径、500 bin、nDays | 配置/日历 |

### 7.2 拟合（Fitted）

由历史数据估计出，带样本依赖。

| 参数 | 拟合方法 | 代码 |
| --- | --- | --- |
| Student-t `df` / `loc` / `scale` | 10 年日 logRet → `scipy.stats.t.fit` MLE | `scripts/fit_underlying.py:48` |
| 各腿 `simIV` | 由当前 mark 反推 BSM 隐含波动率 | `js/pricing_core.js`（经 `processLegData`） |
| `portfolioIV` | 各腿 simIV 的均值 | `computePortfolioMeanSimIV` |
| 单一无风险代理 `r` | 选 3m T-bill 一个 tenor | `import_treasury_risk_free_rate.py:411` |
| 用户的 IV offset | 对拟合 simIV 的人工修正 | `state.ivOffset` |

> 注意：`df` 与 `loc` 是**离线一次性**写入 `js/t_params_db.js`，不会随当下市场重新拟合；只有 `scale` 在运行时被 `IV/√365` 替换（保留尾部形状，丢弃历史尺度）。

### 7.3 前提假设（Assumptions）

模型为了能跑必须付出的代价；读数时心里要有数。

#### 关于价格演化（仅 Chart 2 / Chart 3 / Badge）

1. **日对数收益服从 i.i.d. Student-t(df, loc, scale)** —— 忽略 vol clustering、波动率制度切换、跳跃。
2. **多日累加近似 nDays 累计分布** —— t 之和并非 t，是 i.i.d. 假设的工程化外推。
3. **历史 `df / loc` 在未来仍成立** —— 10 年样本对未来 nDays 适用。
4. **df 与 IV 解耦** —— `_calibrateScale` 只校准 scale，不重估 df；即"市场决定广度，历史决定尾部形状"。
5. **几何随机游走** —— `finalPrice = currentPrice · exp(Σ logRet)`。

> Chart 1 不涉及上述任何随机模型假设——它是**确定性曲线**。

#### 关于期权定价（Chart 1 / Chart 2 / Chart 3 共用）

6. **未到期腿统一用 BSM/Black-76 估值** —— 假设标的 GBM、常数 IV、常数 r、无 smile/skew。
7. **每条腿沿路径 IV 不变** —— 没有 spot↘IV↗ 的 leverage 反馈。
8. **美式期权按欧式估值** —— 不计入提前行权 premium。
9. **不显式建模分红、借贷成本** —— 除非已折进 `r` 或 `underlyingScale`。
10. **`normalCDF` 用 A&S 多项式近似** —— ~1e-7 误差。

#### 关于组合与会计

11. **包含规则**：仅 `includedInGlobal !== false` 的组进入全局求和（`js/prob_charts.js:23-25, 302, 808`）。
12. **组合 P&L = 各腿 P&L 的代数和**（`js/prob_charts.js:187`）—— 无保证金、margin call、爆仓清算。
13. **多产品按 `underlyingScale` 同步移动**（`js/prob_charts.js:166, 932`）—— 假设 β=1、0 basis 噪声。
14. **costBasis 已含全部费用** —— 工具不区分佣金/滑点/监管费。
15. **已平仓腿走 `fixedPrice`** —— 视作确定性现金流，已实现。

#### 关于离散化与可视化

16. **Chart 1 在 strike 与 ±0.01 处加密采样**避免 0DTE 折角丢失。
17. **Chart 2 bin 外路径**不计入直方图但**计入 E[P&L]** —— 用户不易察觉。
18. **500 bin** 直方图，`Δ ≈ (maxS−minS)/500`。
19. **5 点滑动平均**仅用于显示。
20. **1M 路径**，标准误 ~1/1000；尾部分位估计仍可能不稳。

---

## 9. 何时该信，何时该疑

**该信**：
- Chart 1 的曲线"形状"——结构性盈亏分布。
- 价格"中段"的概率密度；t 与 lognormal 都给出相近答案的区域。
- 短到中期（几天～几十天）内的 E[P&L] 排序。
- 同一 IV 设定下不同结构（spread / strangle / butterfly）的相对比较。
- BE 点位置（仅依赖估值，不依赖概率假设）。

**该疑**：
- 极端尾部（>3σ）单点概率与单点 P&L —— 抽样噪声 + 拟合外推叠加。
- 跨制度切换、宏观事件窗口前后 —— `df/loc` 是 10 年混合样本，未必反映当下。
- 跨 underlying 组合且产品差距大时 —— `underlyingScale` 的 β=1 假设可能严重偏离。
- 长 horizon（>1 年）—— i.i.d. 累加偏差累积。
- 美式深 ITM / 高分红 / 高融券费的标的 —— BSM/Black-76 系统性偏差。
- "Max Profit/Loss (in range)" —— 仅区间内极值，无界结构真实极值未必出现在区间。

---

## 10. 想换模型 / 拟合 时的扩展点

| 想改什么 | 改哪里 |
| --- | --- |
| 换抽样分布（如跳扩散、SVI） | `tSample` 函数（`js/prob_charts.js:91-95`） |
| 重新拟合 df/loc | 跑 `python scripts/fit_underlying.py SPY QQQ ...` |
| 引入 IV 反馈（spot-vol） | 改 worker 内 BSM 估值前的 `leg.v` 计算（`js/prob_charts.js:131-138`） |
| 改路径数 / bin 数 | `js/prob_charts.js:884-885` |
| 添加新的 distribution proxy | `js/distribution_proxy_config.js` |
| 把已平仓腿的"确定性"改成"按当下 mark" | 修改 `js/prob_charts.js:915-921` 的 `fixedPrice` 选择逻辑 |
| Chart 1 想加密更多采样点 | `js/chart.js:199-215`（注意性能） |
| Chart 1 想换 P&L 估值模型 | `computeSimulatedPrice` in `js/pricing_core.js`（同时影响 Chart 3） |

---

## 11. 关键代码索引

| 模块 | 文件 |
| --- | --- |
| Chart 1 `PnLChart` 构造与坐标系 | `js/chart.js:18-67` |
| Chart 1 采样点 + strike 加密 | `js/chart.js:199-215` |
| Chart 1 单点 P&L 求和 | `js/chart.js:217-251` |
| Chart 1 当前价参考线 | `js/chart.js:276-297` |
| Chart 1 渐变曲线 + 填充 | `js/chart.js:313-369` |
| Chart 1 Break-even 求零点 | `js/chart.js:376-421` |
| Chart 1 Max profit / loss 文本 | `js/chart.js:425-437` |
| Chart 1 Tooltip / 悬停 | `js/chart.js:72-109, 458-540` |
| Chart 1 网格 + 双行 X 轴标签 | `js/chart.js:543-614` |
| `AmortizationChart`（继承） | `js/chart.js:629-1069` |
| Worker 与抽样器 | `js/prob_charts.js:48-209` |
| Scale 校准 | `js/prob_charts.js:229-232` |
| 对数正态对照 | `js/prob_charts.js:243-250` |
| 主线程 P&L 重算（Chart 3 用） | `js/prob_charts.js:284-331` |
| 主编排 `updateProbCharts` | `js/prob_charts.js:801-994` |
| 历史 t 拟合脚本 | `scripts/fit_underlying.py` |
| t 参数静态库 | `js/t_params_db.js` |
| 分布代理映射 | `js/distribution_proxy_config.js` |
| 多腿 simIV / BSM 估值 | `js/pricing_core.js`、`js/valuation.js` |
| 产品规格 | `js/product_registry.js` |
