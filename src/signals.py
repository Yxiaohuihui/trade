"""信号 + 持仓快照写入 DuckDB，便于事后复盘。"""
from __future__ import annotations
from datetime import date
import pandas as pd
import duckdb

# 内部信号等级 → signal_log.signal_type 的映射
_SIGNAL_TYPE = {
    "green": "hold",
    "yellow": "watch_yellow",
    "warn": "warn_red",
    "reduce": "trim_reduce",
    "sell": "stop_loss",
    "add": "buy_add",
}

# 持仓体检会写入的全部 signal_type，用于幂等清理同日记录
_POSITION_SIGNAL_TYPES = tuple(set(_SIGNAL_TYPE.values()))


def persist_position_signals(conn: duckdb.DuckDBPyConnection,
                             positions_df: pd.DataFrame) -> None:
    """持久化今日持仓信号（同日重复执行幂等）。"""
    if positions_df.empty:
        return
    today = date.today()
    rows = [{
        "signal_date": today,
        "code": r["code"],
        "name": r.get("name", ""),
        "signal_type": _SIGNAL_TYPE.get(r["signal"], "hold"),
        "reason": r.get("reason", ""),
    } for _, r in positions_df.iterrows()]
    df = pd.DataFrame(rows)

    conn.register("_tmp_sig", df)
    placeholders = ",".join(f"'{t}'" for t in _POSITION_SIGNAL_TYPES)
    conn.execute(f"""
        DELETE FROM signal_log
        WHERE signal_date = ?
          AND signal_type IN ({placeholders})
          AND code IN (SELECT code FROM _tmp_sig)
    """, [today])
    conn.execute("""
        INSERT INTO signal_log (signal_date, code, name, signal_type, reason)
        SELECT signal_date, code, name, signal_type, reason FROM _tmp_sig
    """)
    conn.unregister("_tmp_sig")


def persist_radar_signals(conn: duckdb.DuckDBPyConnection,
                          radar_df: pd.DataFrame) -> None:
    """持久化雷达里 ⭐ 高亮的标的（每日 Top-20 不全部入库，避免噪音）。"""
    if radar_df.empty or "highlight" not in radar_df.columns:
        return
    today = date.today()
    highlights = radar_df[radar_df["highlight"]]
    if highlights.empty:
        return
    rows = [{
        "signal_date": today,
        "code": r["code"],
        "name": r.get("name", ""),
        "signal_type": "opportunity",
        "reason": (f"Top-{int(r['rank'])} 综合分 {r['composite_score']:.1f}，"
                   f"连续入围 {int(r['consecutive_days'])} 天"),
    } for _, r in highlights.iterrows()]
    df = pd.DataFrame(rows)

    conn.register("_tmp_op", df)
    conn.execute("""
        DELETE FROM signal_log
        WHERE signal_date = ? AND signal_type = 'opportunity'
    """, [today])
    conn.execute("""
        INSERT INTO signal_log (signal_date, code, name, signal_type, reason)
        SELECT signal_date, code, name, signal_type, reason FROM _tmp_op
    """)
    conn.unregister("_tmp_op")


def persist_position_snapshots(conn: duckdb.DuckDBPyConnection,
                              positions_df: pd.DataFrame) -> None:
    """每日持仓快照 — 用于以后画持仓演变曲线。"""
    if positions_df.empty:
        return
    today = date.today()
    df = positions_df.copy()
    df["snapshot_date"] = today
    if "weight" not in df.columns:
        df["weight"] = None
    cols = ["snapshot_date", "code", "name", "shares", "cost_price",
            "current_price", "market_value", "weight", "pnl", "pnl_pct"]

    conn.register("_tmp_ps", df[cols])
    conn.execute("""
        INSERT OR REPLACE INTO position_snapshots
        SELECT snapshot_date, code, name, shares, cost_price,
               current_price, market_value, weight, pnl, pnl_pct
        FROM _tmp_ps
    """)
    conn.unregister("_tmp_ps")
