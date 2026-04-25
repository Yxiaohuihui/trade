# Stockpilot 架构与运行说明

> 这份文档目标：**任何完全不了解项目的人，看完后能独立运行、读懂日报、排查问题**。
> 配套阅读：项目根目录的 `README.md`（更短的快速上手）。

---

## 目录

1. [它是什么 / 不是什么](#1-它是什么--不是什么)
2. [整体架构图](#2-整体架构图)
3. [核心概念词典](#3-核心概念词典)
4. [完整运行流程](#4-完整运行流程)
5. [数据库表结构详解](#5-数据库表结构详解)
6. [因子打分体系详解](#6-因子打分体系详解)
7. [信号体系详解](#7-信号体系详解)
8. [配置文件说明](#8-配置文件说明)
9. [命令清单](#9-命令清单)
10. [常见问题排查](#10-常见问题排查)

---

## 1. 它是什么 / 不是什么

### ✅ 它是什么

- **本地运行**的 A 股投资决策辅助工具
- 每天收盘后跑一次，输出 **Markdown 投资日报**
- 报告告诉你：哪些持仓需要看，市场上哪些标的在估值低位 + 质量高 + 动量好
- 所有数据本地缓存（DuckDB 单文件数据库）；离线后仍能算因子

### ❌ 它不是什么

- **不会自动下单**——所有决策由你看完日报后手动执行
- **不是高频策略**——基于日线和季报，最快 T+1 调整
- **不是绝对正确**——阈值是经验值，回测尚未充分验证；它只提供**结构化提示**，不替你判断

---

## 2. 整体架构图

### 2.1 五层数据流

```
   ┌──────────────────────────────────────────────────────────────┐
   │                     ① 配置层 (YAML)                           │
   │   positions.yaml  rules.yaml  universe.yaml                  │
   └─────────────────────────┬────────────────────────────────────┘
                             │
   ┌─────────────────────────▼────────────────────────────────────┐
   │                     ② 数据采集层                              │
   │  ┌─────────────┐                  ┌────────────────┐         │
   │  │  akshare    │                  │   baostock     │         │
   │  │  (实时行情) │                  │  (财务/分红)   │         │
   │  └──────┬──────┘                  └────────┬───────┘         │
   │         │       data.py + validators.py    │                 │
   │         └─────────────┬──────────────────────┘               │
   └───────────────────────┼──────────────────────────────────────┘
                           │ INSERT OR REPLACE
   ┌───────────────────────▼──────────────────────────────────────┐
   │                ③ 持久化层 (DuckDB 单文件)                     │
   │   stock_info  daily_quotes  daily_indicators                 │
   │   financial_metrics  dividend_records  universe_membership   │
   │   factor_snapshots  radar_history  signal_log                │
   │   position_snapshots  schema_version                         │
   └───────────────────────┬──────────────────────────────────────┘
                           │ SQL 查询
   ┌───────────────────────▼──────────────────────────────────────┐
   │                  ④ 计算层 (Python)                            │
   │  factors.py     ─→ 5 因子打分 + 综合分                       │
   │  portfolio_check ─→ 持仓体检（红/黄/绿/加/减/卖）             │
   │  opportunity_radar ─→ Top-20 候选 + 连续入围天数             │
   │  diagnostics    ─→ 因子相关性 + 动量分布锚定                 │
   │  backtest       ─→ 历史回测                                  │
   └───────────────────────┬──────────────────────────────────────┘
                           │
   ┌───────────────────────▼──────────────────────────────────────┐
   │                 ⑤ 输出层                                      │
   │   reports/YYYY-MM-DD.md   ←── 每日 Markdown 投资日报         │
   └──────────────────────────────────────────────────────────────┘
```

### 2.2 模块依赖

```
                            main.py
                              │
        ┌──────────────────┬──┴──┬──────────────────┐
        ▼                  ▼     ▼                  ▼
  cmd_refresh_data    cmd_run    cmd_backtest    cmd_diag_*
        │                  │     │                  │
        ▼                  ▼     ▼                  ▼
       data           portfolio_check         diagnostics
        │                  │     │
        ▼                  ▼     ▼
       db ────────────► factors ◄─── universe
        │                  │
        ▼                  ▼
   validators         opportunity_radar
                          │
                          ▼
                       report ──► signals (写回 db)
```

---

## 3. 核心概念词典

| 概念 | 解释 | 哪里用到 |
|------|------|---------|
| **股票池 (Universe)** | 候选股的全集。当前 = 沪深 300 + 中证 500，过滤掉 ST / 小市值 / 次新股 / 排除行业后剩余约 700 只 | `universe.py` / `universe.yaml` |
| **因子 (Factor)** | 给股票打分的某个维度。本项目 5 因子：估值/股息/质量/动量/波动率 | `factors.py` |
| **综合分 (Composite Score)** | 5 个因子分（每个 0-100）的算术平均 | `factors.compute_factors` |
| **PE 分位** | 当前 PE 在该股票自身过去 5 年所有交易日 PE 分布中的位置（0=最低估，100=最高估）| `factors._fetch_indicator_features` |
| **股息率 TTM** | 过去 365 天派息合计 ÷ 当前股价 × 100% | `factors._fetch_dividend_yield` |
| **ROE 年化** | baostock 给的累计 ROE × 12 ÷ 累计月数 | `factors._fetch_financial_features` |
| **机会雷达 (Radar)** | 每日 Top-N 候选标的（按综合分排序）| `opportunity_radar.py` |
| **连续入围天数** | 一只股票从最新日开始连续出现在 Top-N 的天数；中断即归零 | `opportunity_radar._consecutive_days` |
| **持仓体检** | 对你 `positions.yaml` 中每只持仓，根据 PE 分位 + 浮盈浮亏给出信号 | `portfolio_check.py` |
| **信号 (Signal)** | 6 种持仓动作建议：green/yellow/warn/reduce/sell/add | `portfolio_check._decide_signal` |
| **基准指数 (Benchmark)** | 沪深 300（code=000300）和中证 500（code=000905），存在 daily_quotes，用于回测对照 | `data.refresh_indices_incremental` |
| **快照 (Snapshot)** | 某天的因子分 / 持仓 / Top-N 状态保存到 `*_snapshots` 表，便于事后回看与回测 | `*_snapshots` 系列表 |

---

## 4. 完整运行流程

### 4.1 一次完整使用周期

```
首次安装 (一次性)
   │
   ├─→ make install      # uv 装依赖
   ├─→ make init         # 建数据库 schema
   ├─→ make universe     # 拉指数成份股名单（约 30 秒）
   └─→ make data         # 预热数据缓存（首次 30-50 分钟）
                              │
                              ▼
                          ┌────────────────┐
                          │  日常每个交易日 │
                          └────────┬───────┘
                                   │
                  ┌────────────────┼─────────────────┐
                  │                │                  │
                  ▼                ▼                  ▼
              收盘后           编辑持仓             月度任务
                  │                │                  │
        ┌─────────▼─────┐    ┌─────▼──────┐   ┌──────▼──────┐
        │ make run      │    │ 改          │   │ 重跑        │
        │   ↓           │    │ positions.  │   │ make data   │
        │ 生成日报       │    │ yaml        │   │ 同步财报    │
        └─────────┬─────┘    └─────────────┘   └─────────────┘
                  │
                  ▼
        阅读 reports/YYYY-MM-DD.md
                  │
                  ▼
        手动决策、券商 App 下单
                  │
                  ▼
        更新 positions.yaml 反映成交
```

### 4.2 `make run` 内部流程图

```
make run  →  python -m src.main run
              │
              ▼
   ┌──────────────────────────────────────────┐
   │  ① freshness 自检（daily_quotes / indicators）
   │     如最新日期超 7 天前 → 打印告警       │
   └──────────────┬───────────────────────────┘
                  │
   ┌──────────────▼───────────────────────────┐
   │  ② 拉取实时快照 (akshare)                 │
   │     全 A 股 spot + 持仓 ETF              │
   │     从 daily_indicators 补 PE/PB/总市值  │
   │     从 daily_quotes 补 60 日动量         │
   └──────────────┬───────────────────────────┘
                  │
                  ├──────────────────────────┐
                  ▼                          ▼
   ┌─────────────────────────┐  ┌──────────────────────────┐
   │ ③ 持仓体检               │  │ ④ 股票池构造              │
   │  - 每只持仓的 PE 分位   │  │  - 沪深 300 + 中证 500   │
   │  - 浮盈浮亏              │  │  - 市值/ST/PE>0/上市年限 │
   │  - 决定信号 6 类         │  │  - 行业排除              │
   │  - 计算持仓权重          │  │  - 输出约 700 只         │
   └─────────────┬───────────┘  └──────────────┬───────────┘
                 │                              │
                 │                              ▼
                 │                ┌──────────────────────────┐
                 │                │ ⑤ 因子计算                │
                 │                │   5 因子打分 + 综合分    │
                 │                │   写入 factor_snapshots  │
                 │                └──────────────┬───────────┘
                 │                               │
                 │                               ▼
                 │                ┌──────────────────────────┐
                 │                │ ⑥ 机会雷达                │
                 │                │   Top 20 + 连续入围天数  │
                 │                │   ⭐ 标记 ≥10 天的       │
                 │                │   写入 radar_history     │
                 │                └──────────────┬───────────┘
                 │                               │
                 ▼                               ▼
            ┌─────────────────────────────────────────────┐
            │ ⑦ 渲染 Markdown 日报                         │
            │   - 账户概况                                 │
            │   - 持仓体检表（含信号 + 占比）              │
            │   - 现金部署建议                             │
            │   - 雷达 Top 20 表                           │
            └────────────────┬────────────────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────────────────┐
            │ ⑧ 信号 + 持仓快照持久化                      │
            │   signal_log / position_snapshots           │
            │   （事后复盘 + 回测用）                     │
            └─────────────────────────────────────────────┘
```

### 4.3 `make data` 内部 7 步

```
[1/7] 准备股票名单
       从 universe_membership 取沪深 300 + 中证 500（约 800 只）
       并合并你的持仓个股

[2/7] 拉取股票名称（akshare 兜底）
       全 A 股 spot 含 code + name → upsert stock_info

[3/7] 刷新行情 + 估值（最耗时）
       逐只股票调 akshare：
         - stock_zh_a_hist_tx (腾讯) → daily_quotes
         - stock_value_em (东财)     → daily_indicators

[4/7] 行业分类 + 上市日期 (baostock)
       一次拉全市场：query_stock_basic + query_stock_industry
       写入 stock_info.industry / list_date / is_st

[5/7] 基准指数行情 (baostock)
       沪深 300 + 中证 500 日线 → daily_quotes (与个股共用)

[6/7] 财务指标 (baostock，最耗时)
       逐只股票拉最近 4 个季度的 ROE / 净利率 / 同比增速
       fresh_days=60，已有近 60 天财报的跳过

[7/7] 分红记录 (baostock)
       逐只股票拉最近 2 年的现金派息明细
       fresh_days=90 跳过最近 90 天派息已落库的

最后：freshness 自检告警
```

---

## 5. 数据库表结构详解

### 5.1 表关系图

```
                  ┌────────────────────┐
                  │   stock_info       │
                  │  (主信息：名称、   │
                  │   行业、上市日期、 │
                  │   is_st)           │
                  └─────────┬──────────┘
                            │ code
              ┌─────────────┼──────────────┐
              │             │              │
              ▼             ▼              ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │ daily_quotes │ │daily_indica- │ │ universe_    │
    │ (OHLCV +     │ │  tors        │ │ membership   │
    │  指数日线)   │ │ (PE/PB/MV)   │ │ (时点成份)   │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           │                │                │
           │                │                │
           └────────────────┼────────────────┘
                            │
                            │ JOIN by code
                            ▼
              ┌──────────────────────────┐
              │     factors.py           │
              │   (因子计算引擎)         │
              └──────────┬───────────────┘
                         │
                         ▼
              ┌──────────────────────────┐
              │  factor_snapshots        │
              │  (每日 5 因子分 + 综合) │
              └──────────┬───────────────┘
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
┌──────────────┐ ┌──────────────┐ ┌─────────────┐
│radar_history │ │ signal_log   │ │ position_   │
│ (每日 Top-N) │ │ (信号留痕)   │ │ snapshots   │
└──────────────┘ └──────────────┘ └─────────────┘

           ┌──────────────────────┐
           │ financial_metrics    │ ──┐
           │ (季度 ROE/增速)      │   │
           └──────────────────────┘   │ JOIN by code
                                       │ in factors
           ┌──────────────────────┐   │
           │ dividend_records     │ ──┘
           │ (现金派息明细)       │
           └──────────────────────┘

           ┌──────────────────────┐
           │ schema_version       │  迁移历史，运维用
           └──────────────────────┘
```

### 5.2 详细字段说明

#### `stock_info` — 股票主信息（一行一只股票）

| 字段 | 类型 | 含义 | 来源 |
|------|------|------|------|
| code | VARCHAR PK | 6 位股票代码（如 "600036"） | 拉取时填充 |
| name | VARCHAR | 股票简称 | akshare spot |
| industry | VARCHAR | 国标行业（如 "J66货币金融服务"） | baostock query_stock_industry |
| list_date | DATE | 上市日期 | baostock query_stock_basic |
| is_st | BOOLEAN | 是否 ST 标识 | 名称含 "ST" |
| updated_at | TIMESTAMP | 最近一次更新时间 | 自动 |

#### `daily_quotes` — 日线行情（含个股 + 基准指数）

| 字段 | 类型 | 含义 |
|------|------|------|
| code | VARCHAR | 个股 6 位 / 指数代码（"000300"=沪深300, "000905"=中证500）|
| trade_date | DATE | 交易日 |
| open / high / low / close | DOUBLE | 开高低收，**前复权** |
| volume | BIGINT | 成交量（个股可能为空，腾讯源不提供）|
| amount | DOUBLE | 成交额（元）|
| pct_change | DOUBLE | 当日涨跌幅 (%) |
| **PK** | (code, trade_date) | |

#### `daily_indicators` — 估值指标历史

| 字段 | 类型 | 含义 |
|------|------|------|
| code, trade_date | VARCHAR/DATE | 主键 |
| pe | DOUBLE | 静态市盈率 |
| pe_ttm | DOUBLE | 滚动 12 个月 PE（**因子计算用这个**）|
| pb | DOUBLE | 市净率 |
| ps_ttm | DOUBLE | 市销率 TTM |
| dv_ratio | DOUBLE | 股息率（**当前为空，未使用**） |
| dv_ttm | DOUBLE | 股息率 TTM（**当前为空，未使用**，由 dividend_records 单独算）|
| total_mv | DOUBLE | 总市值（元，注意单位）|

#### `financial_metrics` — 季度财务指标（baostock）

| 字段 | 类型 | 含义 |
|------|------|------|
| code | VARCHAR | 股票代码 |
| stat_date | DATE | 报告期截止日（如 2025-12-31）|
| pub_date | DATE | 财报公告日（用于回测前视守卫）|
| roe_avg | DOUBLE | 累计期 ROE（**注意是累计而非年化**）|
| np_margin | DOUBLE | 销售净利率 % |
| gp_margin | DOUBLE | 销售毛利率 % |
| yoy_ni | DOUBLE | 净利润同比增长率 % |
| yoy_equity | DOUBLE | 净资产同比增长率 % |
| **PK** | (code, stat_date) | |

> ⚠️ **roe_avg 是累计值**：Q1 财报 = 1 季度，Q4 = 全年。`factors._fetch_financial_features` 用 `× 12 / EXTRACT(MONTH FROM stat_date)` 简单年化。

#### `dividend_records` — 现金分红明细

| 字段 | 类型 | 含义 |
|------|------|------|
| code | VARCHAR | 股票代码 |
| report_year | INTEGER | 分红所属财年 |
| pay_date | DATE | 实际派息日 |
| cash_per_share_pretax | DOUBLE | 每股税前派息（元）|
| **PK** | (code, report_year, pay_date) | |

#### `universe_membership` — 指数成份股（带时点）

| 字段 | 类型 | 含义 |
|------|------|------|
| code | VARCHAR | 股票代码 |
| index_code | VARCHAR | 指数代码（"000300" / "000905"） |
| effective_from | DATE | 进入指数当日 |
| effective_to | DATE | 退出指数当日；NULL = 当前仍在 |
| **PK** | (code, index_code, effective_from) | |

> 回测某历史日 D 的成份股：`effective_from <= D AND (effective_to IS NULL OR effective_to > D)`

#### `factor_snapshots` — 每日因子打分快照

| 字段 | 类型 | 含义 |
|------|------|------|
| code, snapshot_date | | 主键 |
| pe, pb | DOUBLE | 当日 PE / PB |
| pe_pct_3y, pb_pct_3y | DOUBLE | PE/PB 分位（**字段名遗留命名**，实际窗口 5 年）|
| dividend_yield | DOUBLE | TTM 股息率 % |
| roe | DOUBLE | 年化 ROE % |
| profit_growth | DOUBLE | 净利润同比 % |
| momentum_60d | DOUBLE | 60 日累计涨跌幅 % |
| volatility_60d | DOUBLE | 60 日年化波动率 |
| market_cap | DOUBLE | 总市值 |
| valuation_score | DOUBLE | 估值分 (0-100) |
| dividend_score | DOUBLE | 股息分 (0-100，NaN = 不分红股) |
| quality_score | DOUBLE | 质量分 (0-100) |
| momentum_score | DOUBLE | 动量分 (0-100) |
| volatility_score | DOUBLE | 波动率分 (0-100) |
| composite_score | DOUBLE | 5 因子均值 (0-100) |

#### `radar_history` — 每日雷达 Top-N

| 字段 | 类型 | 含义 |
|------|------|------|
| snapshot_date, code | | 主键 |
| rank | INTEGER | 当日排名 1-N |
| composite_score | DOUBLE | 综合分 |

#### `signal_log` — 信号留痕

| 字段 | 类型 | 含义 |
|------|------|------|
| id | BIGINT PK | 自增 ID |
| signal_date | DATE | 信号生成日 |
| code, name | | 标的 |
| signal_type | VARCHAR | hold/watch_yellow/warn_red/trim_reduce/stop_loss/buy_add/opportunity |
| reason | VARCHAR | 文字解释 |
| user_action | VARCHAR | 你实际怎么做（手填） |
| user_note | VARCHAR | 你的备注（手填） |
| created_at | TIMESTAMP | 写入时间 |

> `user_action` / `user_note` 留给你后续手动填，做"信号 → 实际操作 → 收益"的事后归因。

#### `position_snapshots` — 持仓每日快照

| 字段 | 类型 | 含义 |
|------|------|------|
| snapshot_date, code | | 主键 |
| name, shares, cost_price, current_price, market_value, pnl, pnl_pct | | |
| weight | DOUBLE | 持仓内权重 (0-100，市值 ÷ 总持仓市值) |

#### `schema_version` — 迁移版本表

| 字段 | 类型 | 含义 |
|------|------|------|
| version | INTEGER PK | 迁移版本号 |
| description | VARCHAR | 迁移说明 |
| applied_at | TIMESTAMP | 应用时间 |

---

## 6. 因子打分体系详解

### 6.1 5 因子定义

| 因子 | 算法 | 含义 |
|------|------|------|
| **估值分** | `100 - PERCENT_RANK(pe_ttm)` 在自身过去 5 年的分位 | PE 越低分越高 |
| **股息分** | `RANK(dividend_yield)` 横截面 | 股息率越高分越高；不分红 → NaN（退出排名） |
| **质量分** | `RANK(roe_annualized)` 横截面 | ROE 越高分越高 |
| **动量分** | `100 × exp(-((x - 12) / 27)²)`，x = 60 日累计涨跌幅 % | 钟形曲线，峰值 +12%，σ=27 由 3 年市场分布锚定 |
| **波动率分** | `100 - PERCENT_RANK(volatility_60d)` | 波动率越低分越高 |

### 6.2 综合分

```python
composite_score = mean(5 因子分, skipna=True)
```

> NaN 跳过：例如不分红股的股息分 = NaN，综合分按其他 4 因子均值算。

### 6.3 5 因子打分流水（以一只股票为例）

```
原始数据:
   pe_ttm = 12.0  (在自身 5 年分布的 30 分位)
   dividend_yield = 5.6%  (横截面 95 分位)
   roe = 15.2%  (横截面 80 分位)
   60日涨跌 = +8%  (距钟形峰值 12 偏 4)
   60日波动率 = 0.20  (横截面 30 分位)

打分:
   valuation_score = 100 - 30 = 70
   dividend_score  = 95
   quality_score   = 80
   momentum_score  = 100 × exp(-((8-12)/27)²) ≈ 97.8
   volatility_score = 100 - 30 = 70

综合分 = (70 + 95 + 80 + 97.8 + 70) / 5 = 82.6
```

---

## 7. 信号体系详解

### 7.1 持仓体检 6 类信号决策树

```
持仓信号判定 (portfolio_check._decide_signal)
                │
                ▼
        ┌──── ETF? ──── yes → 🟢 green: "ETF 按现金部署计划持有"
        │ no
        ▼
   PE 分位 > 90? ─── yes ─┬─ 浮盈 > 30% → 🔻 reduce: "部分止盈"
        │                  └─ 否           → 🔴 warn:   "高估警示"
        │ no
        ▼
   PE 分位 > 75? ─── yes → 🟡 yellow: "偏高观察"
        │ no
        ▼
   浮亏 < -15%?  ─── yes → ⛔ sell:   "触发止损线"
        │ no
        ▼
   浮亏 < -8%? ─┬─ PE < 25 → 🟢➕ add:    "浮亏低估可加仓"
        │       └─ 否       → 🟡 yellow: "浮亏观察基本面"
        │ no
        ▼
   PE < 25?    ── yes → 🟢➕ add:  "低估可加仓"
        │ no
        ▼
       🟢 green: "正常持有"
```

### 7.2 信号写入数据库

| Python 信号名 | DB signal_type | 日报图标 |
|---|---|---|
| green | hold | 🟢 持有 |
| yellow | watch_yellow | 🟡 观察 |
| warn | warn_red | 🔴 警示 |
| reduce | trim_reduce | 🔻 减仓 |
| sell | stop_loss | ⛔ 止损 |
| add | buy_add | 🟢➕ 加仓 |
| (雷达 ⭐ 高亮) | opportunity | ⭐ 重点研究 |

---

## 8. 配置文件说明

### 8.1 `config/positions.yaml` — 你的持仓

```yaml
positions:
  - code: "601988"           # 6 位代码
    name: "中国银行"
    shares: 1000             # 持仓股数
    cost_price: 4.477        # 成本均价
    asset_type: "stock"      # 或 "etf"
cash: 70305.67               # 可用现金（元）
total_budget: 100000         # 总预算（用于现金占比建议）
```

> **每次买卖后必须更新这个文件**，否则体检和占比都不对。

### 8.2 `config/rules.yaml` — 决策规则

| 字段 | 默认 | 含义 |
|------|------|------|
| `pe_pct_red` | 90 | PE 分位 > 此值 = 高估红灯 |
| `pe_pct_yellow` | 75 | 黄灯阈值 |
| `pe_pct_buy` | 25 | 加仓阈值 |
| `profit_trim_pct` | 30 | 浮盈 > 此值 + 高估 → 止盈 |
| `loss_review_pct` | -8 | 浮亏 < 此值 → 黄灯 |
| `loss_stop_pct` | -15 | 浮亏 < 此值 → 止损 |
| `monthly_dca_budget` | 6000 | 月度定投预算（元）|
| `radar.top_n` | 20 | 雷达每日候选数 |
| `radar.consecutive_highlight_days` | 10 | 连续入围 ≥ 此天数标 ⭐ |

### 8.3 `config/universe.yaml` — 股票池构造

| 字段 | 含义 |
|------|------|
| `index_membership` | 候选指数列表（默认沪深 300 + 中证 500）|
| `min_market_cap_yi` | 总市值下限（亿元）|
| `exclude_st` | 是否排除 ST |
| `require_positive_pe` | 是否要求 PE > 0（盈利公司） |
| `min_list_years` | 上市最少年限（次新股过滤） |
| `exclude_industries` | 行业排除列表（用国标全名）|

---

## 9. 命令清单

| 命令 | 用途 | 频率 |
|------|------|------|
| `make install` | 装依赖 | 一次性 |
| `make init` | 建数据库 | 一次性 |
| `make universe` | 刷新指数成份股 | 季度 |
| `make data` | 刷新所有历史数据 | 周/月 |
| `make run` | 生成今日日报 | 每个交易日收盘后 |
| `python -m src.main diag-factors` | 5 因子相关性诊断 | 偶尔 |
| `python -m src.main diag-momentum` | 动量曲线参数诊断 | 偶尔 |
| `python -m src.main backtest --start YYYY-MM-DD --top-n 20` | 历史回测 | 积累 ≥ 2 个月数据后 |
| `uv run pytest -q` | 跑单元测试 | 改完代码后 |

---

## 10. 常见问题排查

### Q1. 跑 `make run` 报告里所有股息率都是 "-" 或 NaN？
- **原因**：dividend_records 表为空，没跑过 `make data` 的 [7/7] 步
- **修复**：跑一次 `make data`

### Q2. 雷达 Top-20 全是同一类股票（如全是银行）？
- **原因**：5 因子相关性高（用 `diag-factors` 看），或股票池过滤太松
- **修复**：在 `universe.yaml` 加 `exclude_industries`，或扫描调整阈值

### Q3. 持仓 PE 分位为空（"-"）？
- **原因**：daily_indicators 表里没这只股票（不在沪深 300 / 中证 500 内 + 持仓里）
- **修复**：检查 `make data` 是否把持仓个股也纳入了；看 `cmd_refresh_data` 第 [1/7] 步

### Q4. ROE 数值看着不对（如某股 100%）？
- **原因**：累计 ROE × 12 / 月数年化，杠杆股或季节性股会偏离实际
- **理解**：参考 `factors._fetch_financial_features` 注释；跨季度可比性更重要，绝对值仅供参考

### Q5. 为什么有时 spot 拉不全？
- **原因**：akshare 的新浪/东财端点偶尔断流
- **修复**：data.py 已带退避重试 + 多源降级；如果还失败，改天再跑

### Q6. 我改了 `positions.yaml` 但日报没反映？
- **原因**：`load_positions` 用了 `lru_cache`，进程内缓存
- **修复**：每次 `make run` 是新进程，不受影响。问题更可能是改完没保存

### Q7. 回测显示"结果为空"？
- **原因**：`factor_snapshots` 在指定区间没数据
- **修复**：要么 `make run` 累积一段时间，要么传一个有数据的 `--start`

### Q8. 数据库太大想清理？
```bash
make clean            # 清空数据库 + 报告（破坏性）
make all              # 重建一切
```

---

## 附录：项目文件清单

```
trade/
├── config/                   YAML 配置（你需要改的）
│   ├── positions.yaml        持仓清单
│   ├── rules.yaml            信号阈值
│   └── universe.yaml         股票池过滤规则
├── src/                      Python 源码
│   ├── main.py               CLI 入口
│   ├── db.py                 schema + 迁移
│   ├── data.py               数据采集（akshare + baostock）
│   ├── validators.py         数据校验
│   ├── universe.py           股票池构造
│   ├── factors.py            5 因子计算引擎
│   ├── portfolio_check.py    持仓体检
│   ├── opportunity_radar.py  Top-N 雷达
│   ├── signals.py            信号 + 快照写入
│   ├── report.py             Markdown 渲染
│   ├── diagnostics.py        因子/动量诊断
│   └── backtest.py           回测框架
├── tests/                    pytest 测试（49 个 case）
├── data/                     DuckDB 数据库（gitignored）
│   └── stockpilot.duckdb
├── reports/                  每日日报（gitignored）
│   └── YYYY-MM-DD.md
├── docs/
│   └── ARCHITECTURE.md       本文档
├── pyproject.toml
├── Makefile
└── README.md                 快速上手
```
