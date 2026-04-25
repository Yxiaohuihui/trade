"""渲染每日 markdown 投资日报。"""
from __future__ import annotations
from datetime import date
from pathlib import Path
import pandas as pd

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

# 信号等级 → 报告里显示的图标 + 文字
_SIGNAL_EMOJI = {
    "green": "🟢 持有",
    "yellow": "🟡 观察",
    "warn": "🔴 警示",
    "reduce": "🔻 减仓",
    "sell": "⛔ 止损",
    "add": "🟢➕ 加仓",
}


def render_report(positions_df: pd.DataFrame, radar_df: pd.DataFrame,
                  account: dict, report_date: date | None = None) -> str:
    """拼接日报全文。"""
    report_date = report_date or date.today()
    parts = [f"# 投资日报 {report_date.strftime('%Y-%m-%d')}\n"]
    parts.append(_render_account(account))
    parts.append(_render_portfolio(positions_df))
    parts.append(_render_cash(account))
    parts.append(_render_radar(radar_df))
    return "\n".join(parts)


def _render_account(account: dict) -> str:
    """账户概况段。"""
    total = account.get("total_asset", 0)
    holdings = account.get("holdings_value", 0)
    cash = account.get("cash", 0)
    pos_pct = (holdings / total * 100) if total else 0
    lines = [
        "## 📊 账户概况\n",
        f"- 总资产: **¥{total:,.2f}**",
        f"- 持仓市值: ¥{holdings:,.2f} ({pos_pct:.1f}%)",
        f"- 可用现金: ¥{cash:,.2f} ({100 - pos_pct:.1f}%)",
        "",
    ]
    return "\n".join(lines)


def _render_portfolio(df: pd.DataFrame) -> str:
    """持仓体检表。"""
    if df.empty:
        return "## 🟢🟡🔴 持仓体检\n\n（无持仓）\n"
    lines = ["## 🟢🟡🔴 持仓体检\n",
             "| 标的 | 代码 | 持仓数 | 市值 | 占比 | 盈亏 | 信号 | 说明 |",
             "|---|---|---:|---:|---:|---:|:---:|---|"]
    for _, r in df.iterrows():
        emoji = _SIGNAL_EMOJI.get(r["signal"], "⚪")
        mv = r["market_value"] or 0
        w = r.get("weight")
        weight_str = f"{w:.1f}%" if pd.notna(w) else "-"
        lines.append(
            f"| {r['name']} | {r['code']} | {int(r['shares'])} | "
            f"¥{mv:,.0f} | {weight_str} | {r['pnl_pct']:+.2f}% | {emoji} | {r['reason']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_cash(account: dict) -> str:
    """现金部署建议段。"""
    deploy = account.get("deployment", {})
    if not deploy:
        return ""
    lines = ["## 💰 现金部署建议\n"]
    if "monthly_budget" in deploy:
        lines.append(f"- 本月定投预算: ¥{deploy['monthly_budget']:,.0f}")
    if "cash_pct" in deploy:
        lines.append(f"- 现金占比: {deploy['cash_pct']:.1f}%")
    note = deploy.get("note")
    if note:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _render_radar(df: pd.DataFrame) -> str:
    """机会雷达表。"""
    if df.empty:
        return "## 🎯 机会雷达\n\n（数据不足或暂无候选；请先跑 `make universe` 和 `make data`）\n"
    lines = ["## 🎯 机会雷达（Top 20）\n"]
    starred = df[df["highlight"]] if "highlight" in df.columns else pd.DataFrame()
    if not starred.empty:
        lines.append(f"⭐ 重点研究候选：连续入围 ≥ {int(starred['consecutive_days'].min())} 天\n")

    lines.append("| # | 代码 | 名称 | 综合分 | PE分位 | 股息率 | ROE | 净利增速 | 60日动量 | 波动率 | 连续天数 |")
    lines.append("|:---:|:---:|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for _, r in df.iterrows():
        star = " ⭐" if r.get("highlight") else ""
        pe_pct = f"{r['pe_pct_3y']:.0f}%" if pd.notna(r.get("pe_pct_3y")) else "-"
        dv = f"{r['dividend_yield']:.2f}%" if pd.notna(r.get("dividend_yield")) else "-"
        roe = f"{r['roe']:.1f}%" if pd.notna(r.get("roe")) else "-"
        pg = f"{r['profit_growth']:+.1f}%" if pd.notna(r.get("profit_growth")) else "-"
        mom = f"{r['momentum_60d']:+.1f}%" if pd.notna(r.get("momentum_60d")) else "-"
        vol = f"{r['volatility_60d']:.2f}" if pd.notna(r.get("volatility_60d")) else "-"
        cd = f"{int(r['consecutive_days'])}d{star}"
        lines.append(
            f"| {int(r['rank'])} | {r['code']} | {r['name']} | "
            f"{r['composite_score']:.1f} | {pe_pct} | {dv} | {roe} | {pg} | "
            f"{mom} | {vol} | {cd} |"
        )
    lines.append("")
    lines.append("> 提示：仅 ⭐ 标记的连续入围 10 天以上候选才值得深入研究；新进 Top-20 的标的先观察。\n")
    return "\n".join(lines)


def save_report(content: str, report_date: date | None = None) -> Path:
    """把日报写入 reports/YYYY-MM-DD.md。"""
    report_date = report_date or date.today()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{report_date.strftime('%Y-%m-%d')}.md"
    path.write_text(content, encoding="utf-8")
    return path
