"""最小回测框架：基于 factor_snapshots 历史 Top-N 在 daily_quotes 上模拟。

策略：
  - 每 rebalance_days 个交易日取一次 composite_score Top-N
  - 等权配置，下一个交易日（T+1）收盘价进/出
  - 期间用 daily_quotes 计算每日 NAV

输出：
  - 累计收益、年化收益、夏普比率、最大回撤
  - 与基准（沪深 300, code='000300'）对照

注意：
  - 当前实现不考虑交易成本（A 股双边 ~16bps，估算时心里有数）
  - 不考虑滑点；T+1 收盘价是最简单可靠的执行假设
  - factor_snapshots 数据稀疏时跳过没有数据的日期
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
import duckdb
import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    """回测结果汇总。"""
    nav: pd.Series                          # 组合每日 NAV（起始 1.0）
    benchmark_nav: pd.Series                # 基准每日 NAV（起始 1.0）
    metrics: dict                           # 关键指标
    holdings_history: pd.DataFrame = field(default_factory=pd.DataFrame)


def _load_factor_dates(conn, start: date, end: date) -> list[date]:
    """factor_snapshots 在区间内有数据的日期，升序。"""
    rows = conn.execute("""
        SELECT DISTINCT snapshot_date FROM factor_snapshots
        WHERE snapshot_date BETWEEN ? AND ?
        ORDER BY snapshot_date
    """, [start, end]).fetchall()
    return [r[0] for r in rows]


def _next_trade_date(conn, after: date) -> date | None:
    """daily_quotes 里第一个 > after 的交易日。"""
    row = conn.execute("""
        SELECT MIN(trade_date) FROM daily_quotes
        WHERE trade_date > ? AND code = '000300'
    """, [after]).fetchone()
    return row[0] if row and row[0] else None


def _top_n_codes(conn, snapshot_date: date, top_n: int) -> list[str]:
    """取某天 factor_snapshots 里 composite_score Top-N 的 code 列表。"""
    rows = conn.execute("""
        SELECT code FROM factor_snapshots
        WHERE snapshot_date = ? AND composite_score IS NOT NULL
        ORDER BY composite_score DESC LIMIT ?
    """, [snapshot_date, top_n]).fetchall()
    return [r[0] for r in rows]


def _close_price_on(conn, codes: list[str], trade_date: date) -> dict[str, float]:
    """codes 在 trade_date 的收盘价；如该日停牌则用最近一个交易日。"""
    if not codes:
        return {}
    placeholders = ",".join(f"'{c}'" for c in codes)
    rows = conn.execute(f"""
        SELECT code, close FROM daily_quotes
        WHERE code IN ({placeholders}) AND trade_date <= ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) = 1
    """, [trade_date]).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def _benchmark_nav(conn, start: date, end: date,
                   code: str = "000300") -> pd.Series:
    """基准指数从 start 到 end 的归一化 NAV。"""
    df = conn.execute("""
        SELECT trade_date, close FROM daily_quotes
        WHERE code = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
    """, [code, start, end]).fetch_df()
    if df.empty:
        return pd.Series(dtype=float)
    df["nav"] = df["close"] / df["close"].iloc[0]
    return df.set_index("trade_date")["nav"]


def _compute_metrics(nav: pd.Series, bench: pd.Series) -> dict:
    """累计收益、年化、夏普、最大回撤。"""
    if nav.empty or len(nav) < 2:
        return {}
    daily_ret = nav.pct_change().dropna()
    bench_ret = bench.pct_change().dropna() if not bench.empty else None
    n_days = len(nav)

    cum = nav.iloc[-1] - 1
    annual = (nav.iloc[-1]) ** (252.0 / n_days) - 1
    vol = daily_ret.std() * np.sqrt(252)
    sharpe = (annual / vol) if vol > 0 else float("nan")
    drawdown = (nav / nav.cummax() - 1).min()

    bench_cum = (bench.iloc[-1] - 1) if not bench.empty else float("nan")
    excess = cum - bench_cum if not bench.empty else float("nan")

    return {
        "cum_return": cum,
        "annual_return": annual,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "benchmark_cum_return": bench_cum,
        "excess_return": excess,
        "n_trade_days": n_days,
    }


def run_backtest(conn: duckdb.DuckDBPyConnection,
                 start: date, end: date,
                 top_n: int = 20,
                 rebalance_days: int = 5,
                 benchmark: str = "000300") -> BacktestResult:
    """运行回测。

    Args:
      start, end: 回测区间（包含两端）
      top_n: 持仓数量（按 composite_score 排序取前 N）
      rebalance_days: 每隔多少个 factor_snapshot 日重新选股
      benchmark: 基准指数代码（默认沪深 300）
    """
    factor_dates = _load_factor_dates(conn, start, end)
    if not factor_dates:
        raise RuntimeError(
            f"factor_snapshots 在 {start} ~ {end} 区间无数据。"
            "需要先连续跑 make run 累积历史。"
        )

    # 选股日：每 rebalance_days 个 factor_date 一次
    rebalance_dates = factor_dates[::rebalance_days]
    holdings_log = []   # [(date, [codes...])]

    nav_records = [(rebalance_dates[0], 1.0)]
    current_holdings: dict[str, float] = {}   # code → 持仓股数（归一化）
    cash = 1.0
    portfolio_value = 1.0

    for i, sig_date in enumerate(rebalance_dates):
        codes = _top_n_codes(conn, sig_date, top_n)
        if not codes:
            holdings_log.append((sig_date, []))
            continue

        # T+1 执行
        exec_date = _next_trade_date(conn, sig_date)
        if exec_date is None or exec_date > end:
            break

        # 卖出旧持仓 → 现金
        sell_prices = _close_price_on(conn, list(current_holdings), exec_date)
        proceeds = sum(current_holdings[c] * sell_prices[c]
                      for c in current_holdings if c in sell_prices)
        cash += proceeds
        current_holdings = {}

        # 等权买入新池
        buy_prices = _close_price_on(conn, codes, exec_date)
        valid_codes = [c for c in codes if c in buy_prices and buy_prices[c] > 0]
        if not valid_codes:
            holdings_log.append((sig_date, []))
            continue
        per_stock_cash = cash / len(valid_codes)
        for c in valid_codes:
            current_holdings[c] = per_stock_cash / buy_prices[c]
        cash = 0.0

        holdings_log.append((sig_date, valid_codes))

        # 在该 rebalance 区间内逐日记录 NAV
        next_sig = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else end
        next_exec = _next_trade_date(conn, next_sig) or end

        daily_close = conn.execute(f"""
            SELECT trade_date, code, close FROM daily_quotes
            WHERE code IN ({','.join(repr(c) for c in valid_codes)})
              AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
        """, [exec_date, min(next_exec, end)]).fetch_df()

        if daily_close.empty:
            continue
        for d, day_df in daily_close.groupby("trade_date"):
            value = sum(current_holdings[c] * day_df.set_index("code")["close"].get(c, 0)
                       for c in current_holdings)
            portfolio_value = value + cash
            nav_records.append((d, portfolio_value))

    nav = pd.DataFrame(nav_records, columns=["date", "nav"]).drop_duplicates("date")
    nav = nav.set_index("date")["nav"]
    bench = _benchmark_nav(conn, nav.index.min(), nav.index.max(), benchmark)

    metrics = _compute_metrics(nav, bench)
    holdings_df = pd.DataFrame(
        [{"date": d, "n_holdings": len(c), "codes": ",".join(c)}
         for d, c in holdings_log]
    )

    return BacktestResult(nav=nav, benchmark_nav=bench, metrics=metrics,
                          holdings_history=holdings_df)


def print_backtest_report(result: BacktestResult) -> None:
    """打印回测摘要到控制台。"""
    m = result.metrics
    if not m:
        print("⚠️  回测结果为空")
        return
    print("=== 回测结果 ===")
    print(f"  交易日数      : {m['n_trade_days']}")
    print(f"  累计收益      : {m['cum_return']:+.2%}")
    print(f"  年化收益      : {m['annual_return']:+.2%}")
    print(f"  年化波动      : {m['annual_vol']:.2%}")
    print(f"  夏普比率      : {m['sharpe']:.2f}")
    print(f"  最大回撤      : {m['max_drawdown']:.2%}")
    print(f"  基准累计收益  : {m['benchmark_cum_return']:+.2%}")
    print(f"  超额收益      : {m['excess_return']:+.2%}")
    print()
    print(f"  调仓次数: {len(result.holdings_history)}")
