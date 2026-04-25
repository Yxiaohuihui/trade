"""回测框架核心计算的单元测试（用合成 NAV 序列）。"""
import math
import pandas as pd
import pytest
from src.backtest import _compute_metrics


def _make_nav(daily_returns: list[float]) -> pd.Series:
    """从日收益率列表构造 NAV 序列。"""
    nav = [1.0]
    for r in daily_returns:
        nav.append(nav[-1] * (1 + r))
    dates = pd.date_range("2025-01-01", periods=len(nav))
    return pd.Series(nav, index=dates)


def test_metrics_steady_growth():
    """每日 +0.5% ± 0.1% 涨 100 天，累计 ~65%，回撤极小，sharpe 大正值。"""
    rng = [0.005 + (0.001 if i % 2 == 0 else -0.001) for i in range(100)]
    nav = _make_nav(rng)
    bench = _make_nav([0] * 100)
    m = _compute_metrics(nav, bench)
    assert m["cum_return"] > 0.5
    assert m["max_drawdown"] > -0.01    # 接近 0（小幅波动）
    assert m["sharpe"] > 5              # 持续正收益，夏普应该很高
    assert math.isfinite(m["sharpe"])


def test_metrics_with_drawdown():
    """先涨 20% 再跌 25%，回撤接近 -25%。"""
    nav = _make_nav([0.20] + [-0.25 / 10] * 10)
    bench = _make_nav([0] * 11)
    m = _compute_metrics(nav, bench)
    assert m["max_drawdown"] < -0.20


def test_metrics_excess_return():
    """组合涨 10%，基准涨 5%，超额收益 5%。"""
    nav = _make_nav([0.10 / 50] * 50)
    bench = _make_nav([0.05 / 50] * 50)
    m = _compute_metrics(nav, bench)
    assert m["excess_return"] == pytest.approx(
        m["cum_return"] - m["benchmark_cum_return"], abs=1e-6
    )


def test_metrics_empty_series():
    """空序列返回空 dict。"""
    m = _compute_metrics(pd.Series(dtype=float), pd.Series(dtype=float))
    assert m == {}


def test_metrics_single_point_series():
    """只有 1 个点也返回空。"""
    nav = pd.Series([1.0], index=pd.to_datetime(["2025-01-01"]))
    m = _compute_metrics(nav, pd.Series(dtype=float))
    assert m == {}
