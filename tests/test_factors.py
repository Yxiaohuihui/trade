"""因子计算辅助函数的单元测试。"""
import math
import pandas as pd
import numpy as np
from src.factors import (_rank, _inverse_rank, _momentum_curve,
                          _MOMENTUM_PEAK, _MOMENTUM_SIGMA)


def test_rank_basic():
    """正向排名：值越大分越高。"""
    s = pd.Series([1, 2, 3, 4, 5])
    out = _rank(s)
    assert out.iloc[0] < out.iloc[-1]
    assert out.min() >= 0 and out.max() <= 100


def test_rank_all_nan():
    s = pd.Series([float("nan"), float("nan")])
    out = _rank(s)
    assert out.isna().all()


def test_inverse_rank():
    """反向排名：值越小分越高。"""
    s = pd.Series([1, 2, 3, 4, 5])
    out = _inverse_rank(s)
    assert out.iloc[0] > out.iloc[-1]


def test_rank_with_nan_skipped():
    """NaN 不参与排名，但保留在结果里。"""
    s = pd.Series([1, 2, float("nan"), 4])
    out = _rank(s)
    assert pd.isna(out.iloc[2])
    assert pd.notna(out.iloc[0])


def test_momentum_curve_peak_at_anchor():
    """钟形曲线峰值落在锚定参数 _MOMENTUM_PEAK。"""
    assert _momentum_curve(_MOMENTUM_PEAK) == 100.0


def test_momentum_curve_decreases_away_from_peak():
    peak_val = _momentum_curve(_MOMENTUM_PEAK)
    assert _momentum_curve(_MOMENTUM_PEAK - 20) < peak_val
    assert _momentum_curve(_MOMENTUM_PEAK + 20) < peak_val
    assert _momentum_curve(-30) < peak_val
    assert _momentum_curve(80) < peak_val


def test_momentum_curve_symmetric():
    """钟形曲线对称：偏离峰值同距离同分。"""
    delta = _MOMENTUM_SIGMA
    assert math.isclose(_momentum_curve(_MOMENTUM_PEAK - delta),
                        _momentum_curve(_MOMENTUM_PEAK + delta))


def test_momentum_curve_nan_returns_nan():
    assert math.isnan(_momentum_curve(float("nan")))


def test_momentum_curve_extreme_returns_zero():
    """极端动量被打到接近 0。"""
    assert _momentum_curve(200) < 1
    assert _momentum_curve(-200) < 1
