"""命令行入口。运行方式：python -m src.main <command>"""
from __future__ import annotations
import argparse

from datetime import date, datetime
from src import (db, data, universe, factors, portfolio_check, opportunity_radar,
                 signals, report, validators, diagnostics, backtest)


def cmd_init_db(_args) -> None:
    """初始化 DuckDB schema。"""
    db.ensure_db()
    print(f"✅ 数据库已初始化：{db.DB_PATH}")


def cmd_refresh_universe(_args) -> None:
    """刷新沪深300 / 中证500 等指数成份股名单。"""
    conn = db.ensure_db()
    cfg = universe.load_universe_config()
    for idx in cfg["index_membership"]:
        codes = data.fetch_index_constituents(idx)
        print(f"  [{idx}] 成份股 {len(codes)} 只")
        data.upsert_universe(conn, idx, codes)
    print("✅ 指数成份股已更新")


def cmd_refresh_data(args) -> None:
    """刷新行情 + 估值 + 财务 + 分红历史。基于指数成份股迭代。"""
    conn = db.ensure_db()

    print("[1/7] 准备股票名单 ...")
    cfg = universe.load_universe_config()
    members = sorted(universe._load_index_members(conn, cfg["index_membership"]))
    if not members:
        print("  ❌ 股票池成份为空。请先执行 `make universe`。")
        return

    codes = list(members)
    # 把持仓中的个股也纳入历史拉取
    for p in portfolio_check.load_positions()["positions"]:
        c = str(p["code"]).zfill(6)
        if c not in codes and p.get("asset_type", "stock") == "stock":
            codes.append(c)

    if args.limit:
        codes = codes[: args.limit]
        print(f"      [测试模式] 仅处理前 {len(codes)} 只")

    print(f"[2/7] 拉取股票名称表（akshare 兜底）...")
    names = data.fetch_stock_names()
    if not names.empty:
        names = names[names["code"].isin(codes)]
        data.upsert_stock_info(conn, names)
        print(f"      写入 {len(names)} 行")

    print(f"[3/7] 刷新 {len(codes)} 只标的的行情 + 估值 ...")
    print("      （首次约需 10-20 分钟，后续增量更新约 1-2 分钟）")
    data.refresh_history_incremental(conn, list(codes), min_days=120,
                                     sleep_sec=args.sleep)
    data.refresh_indicator_incremental(conn, list(codes), sleep_sec=args.sleep)

    with data.baostock_session():
        print("[4/7] 刷新行业分类 + 上市日期（baostock 全市场一次拉）...")
        n_meta = data.refresh_stock_meta(conn, codes=list(codes))
        print(f"      写入 {n_meta} 行")

        print("[5/7] 刷新基准指数行情（沪深300 / 中证500）...")
        idx_stats = data.refresh_indices_incremental(conn, cfg["index_membership"])
        print(f"      {idx_stats}")

        print(f"[6/7] 刷新财务指标（ROE / 净利润增速）...")
        print("      （首次约 20-30 分钟；之后只在有新财报披露时拉取）")
        data.refresh_financial_incremental(conn, codes)

        print(f"[7/7] 刷新分红记录 ...")
        data.refresh_dividend_incremental(conn, codes)
    print("✅ 历史数据已刷新")

    # 新鲜度自检（API 静默失效时给出告警）
    for label, sql in [
        ("daily_quotes", "SELECT MAX(trade_date) FROM daily_quotes"),
        ("daily_indicators", "SELECT MAX(trade_date) FROM daily_indicators"),
    ]:
        row = conn.execute(sql).fetchone()
        if row and row[0]:
            validators.check_freshness(row[0], label)
        else:
            validators.check_freshness(None, label)


def cmd_run(_args) -> None:
    """主流程：体检持仓 + 跑雷达 + 生成日报。"""
    conn = db.ensure_db()

    # 数据新鲜度自检（提示用户是否需要先 make data）
    for label, sql in [
        ("daily_quotes", "SELECT MAX(trade_date) FROM daily_quotes"),
        ("daily_indicators", "SELECT MAX(trade_date) FROM daily_indicators"),
    ]:
        row = conn.execute(sql).fetchone()
        if row and row[0]:
            validators.check_freshness(row[0], label)
        else:
            validators.check_freshness(None, label)

    print("[1/5] 拉取实时快照 ...")
    # 持仓里的 ETF 走腾讯单只拉（东财 ETF 端点不稳）
    etf_codes = [str(p["code"]).zfill(6)
                 for p in portfolio_check.load_positions()["positions"]
                 if p.get("asset_type") == "etf"]
    spot = data.fetch_combined_spot(conn=conn, etf_codes=etf_codes)

    print("[2/5] 持仓体检 ...")
    positions_df = portfolio_check.check_portfolio(conn, spot)

    print("[3/5] 计算股票池因子 ...")
    univ = universe.build_universe(conn, spot)
    factors_df = factors.compute_factors(conn, univ)
    factors.persist_factor_snapshot(conn, factors_df)

    print("[4/5] 运行机会雷达 ...")
    radar_df = opportunity_radar.run_radar(conn, factors_df)

    print("[5/5] 生成日报 ...")
    account = _build_account_summary(positions_df)
    content = report.render_report(positions_df, radar_df, account)
    path = report.save_report(content)
    print(f"✅ 日报已生成：{path}")

    # 持久化信号 + 持仓快照，便于之后回看
    signals.persist_position_signals(conn, positions_df)
    signals.persist_radar_signals(conn, radar_df)
    signals.persist_position_snapshots(conn, positions_df)


def _build_account_summary(positions_df) -> dict:
    """汇总账户总值、现金占比，附带部署节奏建议。"""
    pos_cfg = portfolio_check.load_positions()
    rules = portfolio_check.load_rules()
    cash = pos_cfg.get("cash", 0)
    holdings = float(positions_df["market_value"].sum()) if not positions_df.empty else 0.0
    total = cash + holdings
    cash_pct = (cash / total * 100) if total else 0.0

    note = "正常持仓节奏"
    if cash_pct > 60:
        note = "现金占比偏高，建议按月定投部署"
    elif cash_pct < 20:
        note = "现金占比偏低，新机会需先腾挪仓位"

    return {
        "total_asset": total,
        "holdings_value": holdings,
        "cash": cash,
        "deployment": {
            "cash_pct": cash_pct,
            "monthly_budget": rules.get("cash_deployment", {}).get("monthly_dca_budget", 6000),
            "note": note,
        },
    }


def cmd_diag_factors(_args) -> None:
    """打印 5 因子相关性矩阵，识别冗余。"""
    conn = db.ensure_db()
    diagnostics.print_factor_correlation(conn)


def cmd_diag_momentum(_args) -> None:
    """打印 60 日收益率分布与动量曲线建议参数。"""
    conn = db.ensure_db()
    diagnostics.print_momentum_diagnosis(conn)


def cmd_backtest(args) -> None:
    """跑机会雷达 Top-N 策略的回测。"""
    conn = db.ensure_db()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
    result = backtest.run_backtest(
        conn, start=start, end=end,
        top_n=args.top_n, rebalance_days=args.rebalance_days,
    )
    backtest.print_backtest_report(result)


def main() -> None:
    p = argparse.ArgumentParser(description="Stockpilot：A 股持仓监控 + 机会雷达")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="初始化数据库 schema")
    sub.add_parser("refresh-universe", help="刷新沪深300 / 中证500 成份股")

    p_data = sub.add_parser("refresh-data", help="刷新行情 + 估值历史")
    p_data.add_argument("--limit", type=int, default=0,
                       help="仅处理前 N 只（调试用）")
    p_data.add_argument("--sleep", type=float, default=0.3,
                       help="每次 API 调用间隔秒数")

    sub.add_parser("run", help="完整流程 → 输出 markdown 日报")
    sub.add_parser("diag-factors", help="5 因子相关性诊断")
    sub.add_parser("diag-momentum", help="动量曲线经验分布诊断")

    p_bt = sub.add_parser("backtest", help="Top-N 策略回测")
    p_bt.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p_bt.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认今天）")
    p_bt.add_argument("--top-n", type=int, default=20, help="持仓数量")
    p_bt.add_argument("--rebalance-days", type=int, default=5,
                     help="再平衡频率（每 N 个 factor_snapshot 日）")

    args = p.parse_args()
    {
        "init-db": cmd_init_db,
        "refresh-universe": cmd_refresh_universe,
        "refresh-data": cmd_refresh_data,
        "run": cmd_run,
        "diag-factors": cmd_diag_factors,
        "diag-momentum": cmd_diag_momentum,
        "backtest": cmd_backtest,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
