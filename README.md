# Stockpilot

A 股持仓监控 + 机会雷达。本地运行，输出每日 markdown 投资日报。

> 📘 **完整架构、表结构、每个指标含义**：见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（小白友好版）

## 功能

两个模块，一份日报：

1. **持仓体检** —— 对当前每只持仓输出红/黄/绿信号，结合 PE 分位和浮盈浮亏给出加仓 / 止盈建议。
2. **机会雷达** —— 在沪深 300 + 中证 500 范围内，按估值 / 股息 / 质量 / 动量 / 波动率打分，输出 Top 20 候选；记录"连续入围天数"，过滤一日游噪音。

所有交易决策由你做出。工具只输出信号，不下单。

## 首次安装

```bash
# 1. 安装 uv（如未安装，参考 https://github.com/astral-sh/uv）
# 2. 创建虚拟环境并安装依赖
make install

# 3. 初始化 DuckDB 数据库
make init

# 4. 拉取沪深300 / 中证500 成份股名单（约 30 秒）
make universe

# 5. 预热缓存：行情 + 估值 + 财务（ROE/分红）+ 行业 + 上市日期
#    首次较慢（约 30-50 分钟），后续增量更新约 1-2 分钟
make data
```

## 日常使用

```bash
make run
```

输出文件：`reports/YYYY-MM-DD.md`。打开看完，做决策，记录操作。

## 配置

`config/` 下三个 YAML 文件：

- `positions.yaml` —— 你的持仓（每次买卖后手动维护）
- `rules.yaml` —— 信号阈值（PE 分位红黄线、止盈止损、网格步长）
- `universe.yaml` —— 股票池过滤规则（指数成员、市值下限、ST 排除）

## 数据库

单文件：`data/stockpilot.duckdb`。可用 [DBeaver](https://dbeaver.io/) 或任意 SQL 客户端查看。

表结构：
- `stock_info` —— 股票主信息（代码、名称、行业、上市日期、is_st）
- `universe_membership` —— 股票池成份股归属
- `daily_quotes` —— 日线 OHLCV（含个股 + 沪深300/中证500 基准指数）
- `daily_indicators` —— PE/PB 历史（来自东财 stock_value_em）
- `financial_metrics` —— 季度 ROE / 净利润增速等（来自 baostock）
- `dividend_records` —— 现金分红明细（来自 baostock）
- `factor_snapshots` —— 每日因子打分快照
- `radar_history` —— 每日 Top-N（用于计算连续入围天数）
- `signal_log` —— 所有信号留痕，用于回测自己的纪律性
- `position_snapshots` —— 持仓每日快照（含 weight）

## 项目结构

```
trade/
├── config/           # YAML 配置（positions / rules / universe）
├── src/              # Python 源码
├── data/             # DuckDB 文件（gitignored）
├── reports/          # 每日 markdown 日报（gitignored）
└── Makefile
```

## 诊断与回测命令

```bash
python -m src.main diag-factors        # 5 因子相关性矩阵（识别冗余）
python -m src.main diag-momentum       # 60 日收益分布 + 钟形曲线建议参数
python -m src.main backtest --start 2025-09-01 --top-n 20 --rebalance-days 5
```

## 当前 MVP 已知限制

- 质量分用 baostock 季度 ROE × 12/月数 简单年化（季节性强的行业会偏离实际）；分位窗口 5 年。
- 股息率 = 近 365 天派息合计 / 当前股价（实时价，非复权）。
- 不分红的成长股股息率为 NaN（不参与排名），不会被压到底；价值/股息策略偏好仍内嵌在另外 4 因子中。
- ETF 持仓监控只用现价，没有指数估值对齐。
- 不做自动交易。这是有意为之 —— 你决策，你下单。
- 阈值（PE 分位 90/75/25、止损 -15%）为经验值，建议跑回测后调整。
- 动量曲线 σ 已用 3 年市场分布锚定（v2026-04），但仍不考虑行业差异。
- 回测框架要求 `factor_snapshots` 有连续历史；一次性运行至少积累 2-3 个月才有意义。

## 核对 ETF 代码

`config/positions.yaml` 中的 ETF 代码请以你券商 App 显示为准。运行前确认：
- `588000` 科创50 ETF
- `159330` 沪深300 ETF（博时沪深300ETF东财）

如果你买的是别的代码，请编辑 `positions.yaml`。
