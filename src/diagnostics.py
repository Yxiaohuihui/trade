"""因子健康度诊断 + 动量曲线经验分布锚定。

用法（通过 src.main 调用）：
  python -m src.main diag-factors    # 5 因子相关性矩阵
  python -m src.main diag-momentum   # 60 日收益分布与建议参数
"""
from __future__ import annotations
from datetime import date, timedelta
import duckdb
import numpy as np
import pandas as pd

from src.factors import _MOMENTUM_PEAK, _MOMENTUM_SIGMA


# ------------------------------------------------------------------
# 因子相关性
# ------------------------------------------------------------------

_FACTOR_COLS = ["valuation_score", "dividend_score", "quality_score",
                "momentum_score", "volatility_score"]


def factor_correlation(conn: duckdb.DuckDBPyConnection,
                      snapshot_date: date | None = None,
                      method: str = "spearman") -> pd.DataFrame:
    """计算 5 因子分的相关矩阵。
    snapshot_date=None 默认取最新一日；method 默认 spearman（rank-based 鲁棒）。
    """
    if snapshot_date is None:
        row = conn.execute(
            "SELECT MAX(snapshot_date) FROM factor_snapshots"
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("factor_snapshots 表为空，先跑 make run。")
        snapshot_date = row[0]

    cols = ", ".join(_FACTOR_COLS)
    df = conn.execute(
        f"SELECT {cols} FROM factor_snapshots WHERE snapshot_date = ?",
        [snapshot_date],
    ).fetch_df()
    return df.corr(method=method)  # type: ignore[arg-type]


def print_factor_correlation(conn: duckdb.DuckDBPyConnection,
                             snapshot_date: date | None = None) -> None:
    """打印相关矩阵 + 高相关警示。"""
    corr = factor_correlation(conn, snapshot_date)
    print("=== 5 因子 Spearman 相关矩阵 ===")
    print(corr.round(3).to_string())
    print()
    print("⚠️  高相关警示（|ρ| > 0.5，提示因子重复计数）：")
    flagged = []
    for i, a in enumerate(_FACTOR_COLS):
        for b in _FACTOR_COLS[i + 1:]:
            rho = corr.loc[a, b]
            if abs(float(rho)) > 0.5:  # type: ignore[arg-type]
                flagged.append(f"  {a} ↔ {b}: ρ={rho:+.3f}")
    if flagged:
        print("\n".join(flagged))
        print("\n建议：考虑去掉一个、或做横截面正交化。")
    else:
        print("  （无）— 5 因子目前线性独立性可接受")


# ------------------------------------------------------------------
# 动量曲线经验分布锚定
# ------------------------------------------------------------------

def momentum_distribution(conn: duckdb.DuckDBPyConnection,
                         lookback_years: int = 3,
                         window: int = 60) -> pd.Series:
    """计算全市场 60 日收益率的分布，输出关键分位。

    返回 Series：keys 是 ["10%", "25%", "50%", "75%", "90%", "mean", "std"]。
    """
    cutoff = date.today() - timedelta(days=lookback_years * 365)
    df = conn.execute(f"""
        WITH ranked AS (
          SELECT code, trade_date, close,
                 ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date) AS rn
          FROM daily_quotes
          WHERE trade_date >= ? AND code NOT IN ('000300', '000905')
        ),
        joined AS (
          SELECT a.code, a.trade_date,
                 100.0 * (a.close / b.close - 1) AS ret_60d
          FROM ranked a
          JOIN ranked b ON a.code = b.code AND a.rn = b.rn + {window}
        )
        SELECT ret_60d FROM joined WHERE ret_60d IS NOT NULL
    """, [cutoff]).fetch_df()

    if df.empty:
        raise RuntimeError("daily_quotes 数据不足，无法计算 60 日收益分布。")

    s = df["ret_60d"]
    return pd.Series({
        "n_samples": len(s),
        "mean": s.mean(),
        "std": s.std(),
        "10%": s.quantile(0.10),
        "25%": s.quantile(0.25),
        "50%": s.quantile(0.50),
        "75%": s.quantile(0.75),
        "90%": s.quantile(0.90),
    })


def suggest_momentum_params(conn: duckdb.DuckDBPyConnection,
                           lookback_years: int = 3) -> dict:
    """基于经验分布建议钟形曲线参数。
    峰值锚定到 75 分位，半宽 σ 锚定到 (90分位 - 75分位) × 1.4。
    """
    dist = momentum_distribution(conn, lookback_years=lookback_years)
    peak = float(dist["75%"])
    sigma = float(dist["90%"] - dist["75%"]) * 1.4
    return {"peak": round(peak, 2), "sigma": round(sigma, 2),
            "n_samples": int(dist["n_samples"])}


def print_momentum_diagnosis(conn: duckdb.DuckDBPyConnection,
                            lookback_years: int = 3) -> None:
    """打印分布 + 当前参数 vs 建议参数对照。"""
    dist = momentum_distribution(conn, lookback_years=lookback_years)
    print(f"=== 全市场 60 日收益率分布（最近 {lookback_years} 年）===")
    print(f"  样本数: {int(dist['n_samples']):,}")
    print(f"  均值: {dist['mean']:+.2f}%  标准差: {dist['std']:.2f}%")
    print(f"  分位 10/25/50/75/90 = "
          f"{dist['10%']:+.1f}% / {dist['25%']:+.1f}% / "
          f"{dist['50%']:+.1f}% / {dist['75%']:+.1f}% / {dist['90%']:+.1f}%")
    print()

    suggestion = suggest_momentum_params(conn, lookback_years)
    print("=== 钟形曲线参数对照 ===")
    print(f"  当前 factors.py:  peak={_MOMENTUM_PEAK}, sigma={_MOMENTUM_SIGMA}")
    print(f"  经验分布建议  : peak={suggestion['peak']}, sigma={suggestion['sigma']}  "
          f"（峰值=75分位，半宽=(90分位-75分位)×1.4）")
    print()
    if abs(suggestion['peak'] - _MOMENTUM_PEAK) > 5 or abs(suggestion['sigma'] - _MOMENTUM_SIGMA) > 5:
        print("⚠️  当前参数偏离经验分布，建议同步 factors._momentum_curve 参数。")
    else:
        print("✅ 当前参数与经验分布基本一致。")
