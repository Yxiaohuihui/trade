"""验证 signal → signal_type → emoji 三层映射对齐。"""
from src import signals, report, portfolio_check


_ALL_SIGNALS = ("green", "yellow", "warn", "reduce", "sell", "add")


def test_all_signals_have_db_type():
    for s in _ALL_SIGNALS:
        assert s in signals._SIGNAL_TYPE


def test_all_signals_have_emoji():
    for s in _ALL_SIGNALS:
        assert s in report._SIGNAL_EMOJI


def test_db_types_are_unique():
    """signal_type 列必须互不重叠（DELETE 时按 type 清理需要这个保证）。"""
    types = list(signals._SIGNAL_TYPE.values())
    assert len(types) == len(set(types))


def test_decide_signal_only_returns_known_signals():
    """所有可能的输出都在已知映射表里。"""
    rules = portfolio_check._DEFAULT_RULES
    cases = [
        (50, 95, "stock"), (10, 95, "stock"), (5, 80, "stock"),
        (-20, 50, "stock"), (-10, 50, "stock"), (-10, 20, "stock"),
        (5, 20, "stock"), (5, 50, "stock"), (0, None, "etf"),
        (5, None, "stock"),
    ]
    for pnl, pe, atype in cases:
        sig, _ = portfolio_check._decide_signal(pnl, pe, rules, atype)
        assert sig in _ALL_SIGNALS, f"unknown signal: {sig}"
