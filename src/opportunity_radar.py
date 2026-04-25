"""模块 B：扫描股票池，输出 Top-N 候选 + 连续入围天数。"""
from __future__ import annotations
from datetime import date
from pathlib import Path
import yaml
import pandas as pd
import duckdb

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_radar_rules() -> dict:
    """读取 rules.yaml 中的 radar 段。"""
    with open(CONFIG_DIR / "rules.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["radar"]


def run_radar(conn: duckdb.DuckDBPyConnection,
              factors_df: pd.DataFrame) -> pd.DataFrame:
    """从因子表里取 Top-N，写入 radar_history，附加连续入围天数。"""
    rules = load_radar_rules()
    if factors_df.empty:
        return pd.DataFrame()

    df = factors_df.dropna(subset=["composite_score"]).copy()
    df = df.sort_values("composite_score", ascending=False).head(rules["top_n"])
    df = df.reset_index(drop=True)
    df["rank"] = df.index + 1
    df["snapshot_date"] = date.today()

    _persist(conn, df)
    df["consecutive_days"] = df["code"].map(
        lambda c: _consecutive_days(conn, c)
    )
    df["highlight"] = df["consecutive_days"] >= rules["consecutive_highlight_days"]
    return df


def _persist(conn, df: pd.DataFrame) -> None:
    """把今日 Top-N 写入 radar_history（覆盖同日数据，便于重跑）。"""
    cols = ["snapshot_date", "rank", "code", "composite_score"]
    conn.register("_tmp_radar", df[cols])
    conn.execute("""
        DELETE FROM radar_history
        WHERE snapshot_date IN (SELECT DISTINCT snapshot_date FROM _tmp_radar)
    """)
    conn.execute("""
        INSERT INTO radar_history (snapshot_date, rank, code, composite_score)
        SELECT snapshot_date, rank, code, composite_score FROM _tmp_radar
    """)
    conn.unregister("_tmp_radar")


def _consecutive_days(conn, code: str, max_lookback: int = 120) -> int:
    """从最新日期往前数，连续出现在 Top-N 的天数。中断即停。"""
    all_dates = conn.execute("""
        SELECT DISTINCT snapshot_date FROM radar_history
        ORDER BY snapshot_date DESC LIMIT ?
    """, [max_lookback]).fetchall()

    if not all_dates:
        return 0

    presence = conn.execute("""
        SELECT snapshot_date FROM radar_history
        WHERE code = ?
        ORDER BY snapshot_date DESC LIMIT ?
    """, [code, max_lookback]).fetchall()

    present = {r[0] for r in presence}
    count = 0
    for (d,) in all_dates:
        if d in present:
            count += 1
        else:
            break
    return count
