"""数据校验层的覆盖测试。"""
from datetime import date, timedelta
import pandas as pd
import pytest
from src import validators


def _quote_row(**kwargs):
    base = {
        "code": "000001", "trade_date": date.today(),
        "open": 10, "high": 11, "low": 9, "close": 10,
        "volume": 1000, "amount": 10000, "pct_change": 1,
    }
    base.update(kwargs)
    return base


def test_quotes_keep_valid_row():
    df = pd.DataFrame([_quote_row()])
    out = validators.validate_quotes(df)
    assert len(out) == 1


def test_quotes_drop_negative_close():
    df = pd.DataFrame([_quote_row(close=-1)])
    assert validators.validate_quotes(df).empty


def test_quotes_drop_zero_close():
    df = pd.DataFrame([_quote_row(close=0)])
    assert validators.validate_quotes(df).empty


def test_quotes_drop_high_lower_than_low():
    df = pd.DataFrame([_quote_row(high=5, low=10)])
    assert validators.validate_quotes(df).empty


def test_quotes_drop_future_date():
    future = date.today() + timedelta(days=30)
    df = pd.DataFrame([_quote_row(trade_date=future)])
    assert validators.validate_quotes(df).empty


def test_quotes_drop_extreme_high_close():
    """超过 10 万的价格被认为异常。"""
    df = pd.DataFrame([_quote_row(close=200_000)])
    assert validators.validate_quotes(df).empty


def test_quotes_empty_returns_empty():
    out = validators.validate_quotes(pd.DataFrame())
    assert out.empty


def test_indicators_pe_out_of_range_set_nan():
    df = pd.DataFrame([{"code": "000001", "trade_date": date.today(),
                       "pe_ttm": 5000, "pb": 5, "total_mv": 1e10}])
    out = validators.validate_indicators(df)
    assert pd.isna(out["pe_ttm"].iloc[0])
    assert out["pb"].iloc[0] == 5  # 其他字段不受影响


def test_indicators_pb_negative_set_nan():
    df = pd.DataFrame([{"code": "000001", "trade_date": date.today(),
                       "pe_ttm": 20, "pb": -1, "total_mv": 1e10}])
    out = validators.validate_indicators(df)
    assert pd.isna(out["pb"].iloc[0])


def test_indicators_total_mv_zero_set_nan():
    df = pd.DataFrame([{"code": "000001", "trade_date": date.today(),
                       "pe_ttm": 20, "pb": 5, "total_mv": 0}])
    out = validators.validate_indicators(df)
    assert pd.isna(out["total_mv"].iloc[0])


def test_financials_drop_future_pub_date():
    future = date.today() + timedelta(days=30)
    df = pd.DataFrame([{"code": "000001", "stat_date": date(2025, 12, 31),
                       "pub_date": future, "roe_avg": 10, "np_margin": 10,
                       "gp_margin": 30, "yoy_ni": 5, "yoy_equity": 3}])
    out = validators.validate_financials(df)
    assert out.empty


def test_financials_keep_valid():
    df = pd.DataFrame([{"code": "000001", "stat_date": date(2025, 12, 31),
                       "pub_date": date(2026, 3, 15), "roe_avg": 10,
                       "np_margin": 10, "gp_margin": 30, "yoy_ni": 5,
                       "yoy_equity": 3}])
    out = validators.validate_financials(df)
    assert len(out) == 1


def test_financials_extreme_roe_set_nan():
    df = pd.DataFrame([{"code": "000001", "stat_date": date(2025, 12, 31),
                       "pub_date": date(2026, 3, 15), "roe_avg": 500,
                       "np_margin": 10, "gp_margin": 30, "yoy_ni": 5,
                       "yoy_equity": 3}])
    out = validators.validate_financials(df)
    assert pd.isna(out["roe_avg"].iloc[0])


def test_dividends_drop_zero_payment():
    df = pd.DataFrame([{"code": "000001", "report_year": 2025,
                       "pay_date": date(2025, 6, 1),
                       "cash_per_share_pretax": 0}])
    assert validators.validate_dividends(df).empty


def test_dividends_drop_future_pay_date():
    future = date.today() + timedelta(days=30)
    df = pd.DataFrame([{"code": "000001", "report_year": 2025,
                       "pay_date": future,
                       "cash_per_share_pretax": 1.5}])
    assert validators.validate_dividends(df).empty


def test_check_freshness_no_warning_when_fresh(capsys):
    validators.check_freshness(date.today(), "test")
    out = capsys.readouterr().out
    assert "⚠️" not in out


def test_check_freshness_warns_when_stale(capsys):
    stale = date.today() - timedelta(days=30)
    validators.check_freshness(stale, "test")
    out = capsys.readouterr().out
    assert "⚠️" in out


def test_check_freshness_warns_when_none(capsys):
    validators.check_freshness(None, "test")
    out = capsys.readouterr().out
    assert "⚠️" in out
