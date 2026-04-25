"""基于 daily_quotes 与 daily_indicators 缓存数据计算因子分。"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import date, timedelta
import pandas as pd
import numpy as np
import duckdb


@contextmanager
def _universe_codes_temp(conn: duckdb.DuckDBPyConnection, codes: list[str]):
    """注册临时表 _univ_codes 用于 SQL JOIN 过滤；自动 unregister。"""
    conn.register("_univ_codes", pd.DataFrame({"code": codes}))
    try:
        yield
    finally:
        conn.unregister("_univ_codes")


def compute_factors(conn: duckdb.DuckDBPyConnection,
                    universe_df: pd.DataFrame,
                    snapshot_date: date | None = None) -> pd.DataFrame:
    """合并估值分位、股息率、ROE、动量、波动率，输出带综合分的 DataFrame。"""
    snapshot_date = snapshot_date or date.today()
    if universe_df.empty:
        return pd.DataFrame()

    codes = universe_df["code"].tolist()
    indicators = _fetch_indicator_features(conn, codes, snapshot_date)
    dividends = _fetch_dividend_yield(conn, codes, universe_df, snapshot_date)
    financials = _fetch_financial_features(conn, codes, snapshot_date)
    volatility = _fetch_volatility(conn, codes, snapshot_date)

    df = universe_df.merge(indicators, on="code", how="left")
    df = df.merge(dividends, on="code", how="left")
    df = df.merge(financials, on="code", how="left")
    df = df.merge(volatility, on="code", how="left")
    df["momentum_60d"] = df.get("pct_60d")

    df["valuation_score"] = _inverse_rank(df["pe_pct_3y"])
    df["dividend_score"] = _rank(df["dividend_yield"])
    df["quality_score"] = _rank(df["roe"])             # 用 ROE 正向排名作为质量分
    df["momentum_score"] = df["momentum_60d"].map(_momentum_curve)
    df["volatility_score"] = _inverse_rank(df["volatility_60d"])

    score_cols = ["valuation_score", "dividend_score", "quality_score",
                  "momentum_score", "volatility_score"]
    df["composite_score"] = df[score_cols].mean(axis=1, skipna=True)
    df["snapshot_date"] = snapshot_date
    return df


def _fetch_indicator_features(conn, codes: list[str], snapshot_date: date,
                              years: int = 5) -> pd.DataFrame:
    """逐股票计算过去 N 年的 PE/PB 分位（股息率不再从此处取，见 _fetch_dividend_yield）。"""
    cutoff = snapshot_date - timedelta(days=years * 365)
    with _universe_codes_temp(conn, codes):
        return conn.execute("""
            WITH history AS (
              SELECT d.code, d.trade_date, d.pe_ttm, d.pb
              FROM daily_indicators d
              JOIN _univ_codes u USING (code)
              WHERE d.trade_date >= ?
                AND d.pe_ttm IS NOT NULL AND d.pe_ttm > 0
            ),
            ranked AS (
              SELECT code, trade_date, pe_ttm, pb,
                     PERCENT_RANK() OVER (PARTITION BY code ORDER BY pe_ttm) * 100 AS pe_pct_3y,
                     PERCENT_RANK() OVER (PARTITION BY code ORDER BY pb) * 100 AS pb_pct_3y
              FROM history
            )
            SELECT code, pe_pct_3y, pb_pct_3y
            FROM ranked
            QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) = 1
        """, [cutoff]).fetch_df()


def _fetch_dividend_yield(conn, codes: list[str], universe_df: pd.DataFrame,
                          snapshot_date: date) -> pd.DataFrame:
    """股息率 TTM = 近 365 天派息合计 / 当前股价 × 100。

    价格来源优先级：universe_df.close（spot 实时价） → daily_quotes 最近一日收盘价。
    Spot API 整体失败时不至于让全市场股息分都变 NaN。

    pay_date 必须落在 [snapshot_date-365, snapshot_date] 区间，
    上限是为回测准备：避免引入未来才会派发的分红。

    不分红股票：dividend_yield = NaN（不参与排名），避免成长股被股息分压到底部。
    """
    cutoff = snapshot_date - timedelta(days=365)
    with _universe_codes_temp(conn, codes):
        ttm = conn.execute("""
            SELECT d.code, SUM(d.cash_per_share_pretax) AS cash_ttm
            FROM dividend_records d
            JOIN _univ_codes u USING (code)
            WHERE d.pay_date >= ? AND d.pay_date <= ?
            GROUP BY d.code
        """, [cutoff, snapshot_date]).fetch_df()

        # 兜底价格：从 daily_quotes 取每只股票的最近一日收盘价
        fallback = conn.execute("""
            SELECT code, close AS _fallback_price
            FROM daily_quotes
            WHERE code IN (SELECT code FROM _univ_codes)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) = 1
        """).fetch_df()

    price = universe_df[["code", "close"]].rename(columns={"close": "_price"})
    df = price.merge(fallback, on="code", how="left")
    # spot 价缺失时回退到 daily_quotes 最近收盘价
    df["_price"] = df["_price"].fillna(df["_fallback_price"])
    df = df.merge(ttm, on="code", how="left")

    # 关键设计：cash_ttm NaN（不分红） → dividend_yield NaN（不参与排名）
    # 而非压成 0%，避免成长股被股息分严重惩罚
    df["dividend_yield"] = df["cash_ttm"] / df["_price"] * 100
    df.loc[df["_price"].isna() | (df["_price"] <= 0), "dividend_yield"] = pd.NA
    return df[["code", "dividend_yield"]]


def _fetch_financial_features(conn, codes: list[str],
                              snapshot_date: date) -> pd.DataFrame:
    """取每只股票最新一期已公告的 ROE 与净利润同比增速。

    ROE 年化处理：baostock 的 roe_avg 是「累计净利润/平均净资产」，
    Q1=3 个月、Q2=6、Q3=9、Q4=12，跨季度直接比会让 Q1 公司的 ROE 被压低 4 倍。
    用 × 12 / EXTRACT(MONTH FROM stat_date) 简单年化（假设业务全年匀速），
    比「混合季度直接排序」准确得多，但季节性强的行业会偏离实际。

    pub_date <= snapshot_date 是回测守卫：避免引入未公告财报。
    """
    with _universe_codes_temp(conn, codes):
        return conn.execute("""
            SELECT code,
                   roe_avg * 12.0 / EXTRACT(MONTH FROM stat_date) AS roe,
                   yoy_ni AS profit_growth
            FROM financial_metrics
            WHERE code IN (SELECT code FROM _univ_codes)
              AND pub_date <= ?
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY code ORDER BY stat_date DESC, pub_date DESC
            ) = 1
        """, [snapshot_date]).fetch_df()


def _fetch_volatility(conn, codes: list[str], snapshot_date: date,
                     window: int = 60) -> pd.DataFrame:
    """过去 window 个交易日的日收益率年化标准差。"""
    cutoff = snapshot_date - timedelta(days=window * 2)
    with _universe_codes_temp(conn, codes):
        return conn.execute(f"""
            WITH recent AS (
              SELECT q.code, q.trade_date, q.pct_change / 100.0 AS ret
              FROM daily_quotes q
              JOIN _univ_codes u USING (code)
              WHERE q.trade_date >= ? AND q.pct_change IS NOT NULL
            ),
            ranked AS (
              SELECT code, ret,
                     ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn
              FROM recent
            )
            SELECT code, STDDEV_SAMP(ret) * SQRT(252) AS volatility_60d
            FROM ranked
            WHERE rn <= {window}
            GROUP BY code
        """, [cutoff]).fetch_df()


def _rank(series: pd.Series) -> pd.Series:
    """正向排名 0-100：值越大分越高。"""
    if series.dropna().empty:
        return pd.Series(np.nan, index=series.index)
    return series.rank(method="average", pct=True) * 100


def _inverse_rank(series: pd.Series) -> pd.Series:
    """反向排名 0-100：值越小分越高。"""
    return 100 - _rank(series)


# 钟形动量曲线参数。
# 来源：src.diagnostics.suggest_momentum_params（用最近 3 年全市场 60 日收益分布锚定）
# 峰值锚到 75 分位（市场上行尾部），σ 锚到 (90分位 - 75分位) × 1.4
# 历史值（凭直觉，2026-04 弃用）：peak=10, sigma=15
# 实测最新值（2026-04，77K 样本）：peak=12.6, sigma=26.6
# 重新校准命令：python -m src.main diag-momentum
_MOMENTUM_PEAK = 12.0
_MOMENTUM_SIGMA = 27.0


def _momentum_curve(x) -> float:
    """钟形曲线，峰值锚定到全市场 60 日收益的 75 分位。
    A 股右尾肥厚（90 分位达 +30%），σ 比直觉值偏大，避免过度惩罚强动量。
    """
    if pd.isna(x):
        return float("nan")
    return max(0.0, 100.0 * np.exp(
        -((float(x) - _MOMENTUM_PEAK) / _MOMENTUM_SIGMA) ** 2
    ))


def persist_factor_snapshot(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    """把当日因子快照写入 factor_snapshots 表，便于历史复盘。"""
    if df.empty:
        return

    out = df.copy()
    # 把 spot 字段 total_mv 对齐到表的 market_cap 列
    if "total_mv" in out.columns:
        out = out.rename(columns={"total_mv": "market_cap"})

    cols = ["code", "snapshot_date", "pe", "pb", "pe_pct_3y", "pb_pct_3y",
            "dividend_yield", "roe", "profit_growth",
            "momentum_60d", "volatility_60d", "market_cap",
            "valuation_score", "dividend_score", "quality_score",
            "momentum_score", "volatility_score", "composite_score"]
    for c in cols:
        if c not in out.columns:
            out[c] = None

    conn.register("_tmp_fs", out[cols])
    conn.execute("""
        INSERT OR REPLACE INTO factor_snapshots
        SELECT code, snapshot_date, pe, pb, pe_pct_3y, pb_pct_3y,
               dividend_yield, roe, profit_growth,
               momentum_60d, volatility_60d, market_cap,
               valuation_score, dividend_score, quality_score,
               momentum_score, volatility_score, composite_score
        FROM _tmp_fs
    """)
    conn.unregister("_tmp_fs")
