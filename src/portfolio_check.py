"""模块 A：持仓健康度体检。"""
from __future__ import annotations
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
import yaml
import pandas as pd
import duckdb

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# 缺省阈值兜底，避免 rules.yaml 漏字段直接 KeyError
_DEFAULT_RULES = {
    "pe_pct_red": 90,
    "pe_pct_yellow": 75,
    "pe_pct_buy": 25,
    "profit_trim_pct": 30,
    "loss_review_pct": -8,
    "loss_stop_pct": -15,
}


@lru_cache(maxsize=1)
def load_positions() -> dict:
    """读取 positions.yaml。"""
    with open(CONFIG_DIR / "positions.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_rules() -> dict:
    """读取 rules.yaml；portfolio_check 段缺失字段用 _DEFAULT_RULES 兜底。"""
    with open(CONFIG_DIR / "rules.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    pc = {**_DEFAULT_RULES, **(cfg.get("portfolio_check") or {})}
    cfg["portfolio_check"] = pc
    return cfg


def check_portfolio(conn: duckdb.DuckDBPyConnection,
                    spot_df: pd.DataFrame) -> pd.DataFrame:
    """对每只持仓返回带健康信号的 DataFrame。"""
    positions = load_positions()["positions"]
    rules = load_rules()["portfolio_check"]

    rows = []
    for p in positions:
        code = str(p["code"]).zfill(6)
        rows.append(_check_one(conn, spot_df, code, p, rules))
    df = pd.DataFrame(rows)
    # 持仓内权重 = market_value / 总持仓市值
    if not df.empty:
        total_mv = df["market_value"].sum()
        df["weight"] = (df["market_value"] / total_mv * 100) if total_mv else 0.0
    return df


def _check_one(conn, spot_df, code: str, pos: dict, rules: dict) -> dict:
    """单只持仓体检：当前价、盈亏、PE 分位、信号。"""
    row = spot_df[spot_df["code"] == code]
    current_price = float(row["close"].iloc[0]) if not row.empty else None
    pe = _safe_float(row["pe"].iloc[0]) if not row.empty and "pe" in row else None

    shares = pos["shares"]
    cost = pos["cost_price"]
    market_value = (current_price or 0) * shares
    pnl = market_value - cost * shares
    pnl_pct = (current_price / cost - 1) * 100 if current_price else 0.0

    asset_type = pos.get("asset_type", "stock")
    pe_pct_3y = _get_pe_pct(conn, code) if asset_type == "stock" else None
    signal, reason = _decide_signal(pnl_pct, pe_pct_3y, rules, asset_type)

    return {
        "code": code,
        "name": pos.get("name", ""),
        "asset_type": asset_type,
        "shares": shares,
        "cost_price": cost,
        "current_price": current_price,
        "market_value": market_value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "pe": pe,
        "pe_pct_3y": pe_pct_3y,
        "signal": signal,
        "reason": reason,
    }


def _safe_float(v) -> float | None:
    """转 float，遇到 None / NaN / 异常类型返回 None。"""
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_pe_pct(conn, code: str, years: int = 5) -> float | None:
    """计算当前 PE 在过去 N 年中的分位（0-100）。
    使用 PERCENT_RANK 与 factors._fetch_indicator_features 保持一致，
    避免持仓体检和雷达对同一只股票给出不同分位。
    """
    cutoff = date.today() - timedelta(days=years * 365)
    row = conn.execute("""
        WITH h AS (
          SELECT pe_ttm, trade_date,
                 PERCENT_RANK() OVER (ORDER BY pe_ttm) * 100 AS pct
          FROM daily_indicators
          WHERE code = ? AND trade_date >= ?
            AND pe_ttm IS NOT NULL AND pe_ttm > 0
        )
        SELECT pct FROM h ORDER BY trade_date DESC LIMIT 1
    """, [code, cutoff]).fetchone()
    return row[0] if row and row[0] is not None else None


def _decide_signal(pnl_pct: float, pe_pct: float | None,
                   rules: dict, asset_type: str) -> tuple[str, str]:
    """决定信号等级与说明。
    等级：green / yellow / warn / reduce / sell / add。
      - green  正常持有
      - yellow 观察（轻度偏高或轻度浮亏）
      - warn   高估警示（但未到止盈点）
      - reduce 高估止盈（高估 + 浮盈丰厚）
      - sell   止损（浮亏触线）
      - add    加仓（低估）
    """
    # ETF 规则单独走，按现金部署计划，不参与个股止盈/止损
    if asset_type == "etf":
        return ("green", "ETF：按现金部署计划持有")

    # 高估检查（优先级最高）
    if pe_pct is not None:
        if pe_pct > rules["pe_pct_red"]:
            base = f"PE 5年分位 {pe_pct:.0f}%（高估）"
            if pnl_pct > rules["profit_trim_pct"]:
                return ("reduce", f"{base}；浮盈 {pnl_pct:+.1f}% → 部分止盈")
            return ("warn", base)
        if pe_pct > rules["pe_pct_yellow"]:
            return ("yellow", f"PE 5年分位 {pe_pct:.0f}%（偏高，观察）")

    # 浮亏检查
    if pnl_pct < rules["loss_stop_pct"]:
        return ("sell", f"浮亏 {pnl_pct:+.1f}% → 触发止损线")
    if pnl_pct < rules["loss_review_pct"]:
        # 浮亏但估值低 → 转为加仓机会
        if pe_pct is not None and pe_pct < rules["pe_pct_buy"]:
            return ("add", f"浮亏 {pnl_pct:+.1f}% 但 PE 分位 {pe_pct:.0f}% → 可加仓")
        return ("yellow", f"浮亏 {pnl_pct:+.1f}% → 观察基本面")

    # 低估加仓机会
    if pe_pct is not None and pe_pct < rules["pe_pct_buy"]:
        return ("add", f"PE 5年分位 {pe_pct:.0f}%（低估）→ 可加仓")

    return ("green", "正常持有")
