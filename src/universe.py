"""股票池构造：基于指数成份 + 配置化过滤规则。"""
from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
import yaml
import pandas as pd
import duckdb

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_universe_config() -> dict:
    """读取 universe.yaml 配置。"""
    with open(CONFIG_DIR / "universe.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["universe"]


def build_universe(conn: duckdb.DuckDBPyConnection,
                   spot_df: pd.DataFrame) -> pd.DataFrame:
    """应用过滤规则并返回可交易股票池（带当日 spot 字段）。"""
    cfg = load_universe_config()
    members = _load_index_members(conn, cfg["index_membership"])

    if not members:
        print("  ⚠️  股票池成份为空。请先执行 `make universe`。")
        return pd.DataFrame()

    # 先过滤到 stock 类型，避免把 ETF 卷进来
    df = spot_df[spot_df.get("asset_type", "stock") == "stock"].copy()
    df = df[df["code"].isin(members)]

    df = _attach_stock_meta(conn, df)
    df = _apply_filters(df, cfg)
    return df.reset_index(drop=True)


def _load_index_members(conn, index_codes: list[str],
                        as_of: date | None = None) -> set[str]:
    """从 universe_membership 取出指定指数在某时点的成份股代码。

    as_of=None 表示当前活跃成份股（effective_to IS NULL）；
    传入具体日期则按时点回看（用于回测）。
    """
    placeholders = ",".join(f"'{c}'" for c in index_codes)
    if as_of is None:
        rows = conn.execute(f"""
            SELECT DISTINCT code FROM universe_membership
            WHERE index_code IN ({placeholders})
              AND effective_to IS NULL
        """).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT DISTINCT code FROM universe_membership
            WHERE index_code IN ({placeholders})
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
        """, [as_of, as_of]).fetchall()
    return {r[0] for r in rows}


def _attach_stock_meta(conn, df: pd.DataFrame) -> pd.DataFrame:
    """从 stock_info 表合并 industry / list_date / is_st 到行情 df。"""
    if df.empty:
        return df
    meta = conn.execute(
        "SELECT code, industry, list_date, is_st FROM stock_info"
    ).fetch_df()
    if meta.empty:
        return df
    # 防止重名列冲突
    df = df.drop(columns=[c for c in ["industry", "list_date", "is_st"]
                          if c in df.columns], errors="ignore")
    return df.merge(meta, on="code", how="left")


def _apply_filters(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """逐条应用 universe.yaml 中的硬过滤。"""
    # 市值下限：单位是"亿元"，1 亿 = 1e8
    min_mv = cfg.get("min_market_cap_yi", 100) * 1e8
    if "total_mv" in df.columns:
        df = df[df["total_mv"].fillna(0) >= min_mv]

    if cfg.get("exclude_st", True):
        # 双保险：名称含 ST 或 stock_info.is_st=True
        st_by_name = df["name"].str.contains("ST", na=False)
        st_by_meta = df["is_st"].fillna(False) if "is_st" in df.columns else False
        df = df[~(st_by_name | st_by_meta)]

    if cfg.get("require_positive_pe", True):
        df = df[df["pe"].notna() & (df["pe"] > 0)]

    # 上市年限：剔除上市未满 N 年的次新股；list_date 缺失视为未知不过滤
    # DuckDB 把 NULL DATE 列读为 datetime64，统一用 pd.Timestamp 做比较
    min_years = cfg.get("min_list_years", 0)
    if min_years and "list_date" in df.columns:
        cutoff = pd.Timestamp(date.today() - timedelta(days=int(min_years * 365)))
        list_ts = pd.to_datetime(df["list_date"], errors="coerce")
        too_new = list_ts.notna() & (list_ts > cutoff)
        df = df[~too_new]

    # 行业排除
    excl = cfg.get("exclude_industries") or []
    if excl and "industry" in df.columns:
        df = df[~df["industry"].isin(excl)]

    return df
