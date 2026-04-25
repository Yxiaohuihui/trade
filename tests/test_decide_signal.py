"""持仓体检信号决策的覆盖测试。"""
from src.portfolio_check import _decide_signal

RULES = {
    "pe_pct_red": 90, "pe_pct_yellow": 75, "pe_pct_buy": 25,
    "profit_trim_pct": 30, "loss_review_pct": -8, "loss_stop_pct": -15,
}


def test_etf_always_green():
    sig, _ = _decide_signal(0, None, RULES, "etf")
    assert sig == "green"


def test_high_pe_with_big_profit_triggers_reduce():
    sig, reason = _decide_signal(50, 95, RULES, "stock")
    assert sig == "reduce"
    assert "止盈" in reason


def test_high_pe_without_big_profit_is_warn():
    sig, _ = _decide_signal(10, 95, RULES, "stock")
    assert sig == "warn"


def test_yellow_zone_pe():
    sig, _ = _decide_signal(5, 80, RULES, "stock")
    assert sig == "yellow"


def test_stop_loss_triggers_sell():
    sig, reason = _decide_signal(-20, 50, RULES, "stock")
    assert sig == "sell"
    assert "止损" in reason


def test_loss_review_normal_pe_is_yellow():
    sig, _ = _decide_signal(-10, 50, RULES, "stock")
    assert sig == "yellow"


def test_loss_with_low_pe_triggers_add():
    sig, _ = _decide_signal(-10, 20, RULES, "stock")
    assert sig == "add"


def test_low_pe_normal_pnl_is_add():
    sig, _ = _decide_signal(5, 20, RULES, "stock")
    assert sig == "add"


def test_normal_state_is_green():
    sig, _ = _decide_signal(5, 50, RULES, "stock")
    assert sig == "green"


def test_no_pe_data_normal_pnl_is_green():
    """daily_indicators 没数据时 pe_pct=None，小幅浮亏小幅浮盈走 green。"""
    sig, _ = _decide_signal(5, None, RULES, "stock")
    assert sig == "green"


def test_no_pe_data_stop_loss_still_triggers():
    """没有 PE 也要能触发止损。"""
    sig, _ = _decide_signal(-20, None, RULES, "stock")
    assert sig == "sell"


def test_boundary_at_red_threshold():
    """PE 等于红线时不触发（>，不是 >=）。"""
    sig, _ = _decide_signal(5, 90, RULES, "stock")
    assert sig == "yellow"   # 90 > 75 走 yellow


def test_boundary_at_buy_threshold():
    """PE 等于加仓线时不触发（<，不是 <=）。"""
    sig, _ = _decide_signal(5, 25, RULES, "stock")
    assert sig == "green"
