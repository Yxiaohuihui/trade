"""数据校验：在 upsert 之前过滤异常行，避免脏数据污染下游因子。
原则：能修就修，不能修就丢；任何丢弃都打印告警，便于事后追溯 API 端异常。
"""
from __future__ import annotations
from datetime import date
import pandas as pd


def _drop_with_log(df: pd.DataFrame, mask: pd.Series, reason: str,
                  tag: str) -> pd.DataFrame:
    """用 mask 过滤 df 并打印告警。mask=True 表示保留。"""
    n_drop = (~mask).sum()
    if n_drop > 0:
        print(f"  ⚠️  [{tag}] 丢弃 {n_drop} 行：{reason}")
    return df[mask]


def validate_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """日线 OHLC 校验：非负价格、high≥low、trade_date 不在未来。"""
    if df.empty:
        return df
    today = date.today()
    n_in = len(df)

    df = _drop_with_log(df, df["close"].notna() & (df["close"] > 0),
                       "close ≤ 0 或缺失", "quotes")
    df = _drop_with_log(df, df["close"] < 100_000,
                       "close 异常高（> 10 万）", "quotes")
    if "high" in df.columns and "low" in df.columns:
        ok = (df["high"].fillna(0) >= df["low"].fillna(0))
        df = _drop_with_log(df, ok, "high < low", "quotes")
    df = _drop_with_log(df, df["trade_date"] <= today,
                       "trade_date 在未来", "quotes")

    if len(df) != n_in:
        print(f"      → quotes 校验：{n_in} → {len(df)} 行")
    return df.reset_index(drop=True)


def validate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """估值指标校验：PE/PB 落在合理范围，total_mv 非负。
    对越界值置 NaN 而不是丢弃整行（PE 极端不影响 PB）。
    """
    if df.empty:
        return df
    out = df.copy()

    if "pe_ttm" in out.columns:
        bad = out["pe_ttm"].notna() & ~out["pe_ttm"].between(-100, 2000)
        n = bad.sum()
        if n:
            print(f"  ⚠️  [indicators] {n} 行 pe_ttm 越界 [-100, 2000]，置 NaN")
        out.loc[bad, "pe_ttm"] = pd.NA

    if "pb" in out.columns:
        bad = out["pb"].notna() & ~out["pb"].between(0.01, 100)
        n = bad.sum()
        if n:
            print(f"  ⚠️  [indicators] {n} 行 pb 越界 [0.01, 100]，置 NaN")
        out.loc[bad, "pb"] = pd.NA

    if "total_mv" in out.columns:
        bad = out["total_mv"].notna() & (out["total_mv"] <= 0)
        n = bad.sum()
        if n:
            print(f"  ⚠️  [indicators] {n} 行 total_mv ≤ 0，置 NaN")
        out.loc[bad, "total_mv"] = pd.NA

    return out


def validate_financials(df: pd.DataFrame) -> pd.DataFrame:
    """财务指标校验：ROE / 增速越界置 NaN；pub_date 不在未来才保留。"""
    if df.empty:
        return df
    today = date.today()
    n_in = len(df)

    df = _drop_with_log(df, df["pub_date"].isna() | (df["pub_date"] <= today),
                       "pub_date 在未来", "financials")

    out = df.copy()
    if "roe_avg" in out.columns:
        bad = out["roe_avg"].notna() & ~out["roe_avg"].between(-100, 200)
        n = bad.sum()
        if n:
            print(f"  ⚠️  [financials] {n} 行 roe_avg 越界 [-100, 200]，置 NaN")
        out.loc[bad, "roe_avg"] = pd.NA

    if "yoy_ni" in out.columns:
        # 净利润同比可能因低基数极端膨胀，放宽阈值
        bad = out["yoy_ni"].notna() & ~out["yoy_ni"].between(-10000, 10000)
        n = bad.sum()
        if n:
            print(f"  ⚠️  [financials] {n} 行 yoy_ni 越界，置 NaN")
        out.loc[bad, "yoy_ni"] = pd.NA

    if len(out) != n_in:
        print(f"      → financials 校验：{n_in} → {len(out)} 行")
    return out.reset_index(drop=True)


def validate_dividends(df: pd.DataFrame) -> pd.DataFrame:
    """分红校验：派息金额必须为正，pay_date 不在未来。"""
    if df.empty:
        return df
    today = date.today()
    n_in = len(df)

    df = _drop_with_log(df,
                       df["cash_per_share_pretax"].notna() &
                       (df["cash_per_share_pretax"] > 0),
                       "cash_per_share_pretax ≤ 0", "dividends")
    df = _drop_with_log(df, df["pay_date"] <= today,
                       "pay_date 在未来", "dividends")

    if len(df) != n_in:
        print(f"      → dividends 校验：{n_in} → {len(df)} 行")
    return df.reset_index(drop=True)


def check_freshness(latest_date: date | None, label: str,
                    max_lag_days: int = 7) -> None:
    """检查最新数据日期是否过期，发现端点静默失效时打印告警。"""
    if latest_date is None:
        print(f"  ⚠️  [{label}] 无数据可用")
        return
    lag = (date.today() - latest_date).days
    if lag > max_lag_days:
        print(f"  ⚠️  [{label}] 最新日期 {latest_date}，距今 {lag} 天 "
              f"（超过 {max_lag_days} 天阈值，疑似 API 静默失效）")
