"""数据层：akshare 接口封装 + DuckDB 持久化。"""
from __future__ import annotations
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import pandas as pd
import akshare as ak
import baostock as bs
import duckdb

from src import validators


# ------------------------------------------------------------------
# 通用：带退避的重试包装
# ------------------------------------------------------------------

def _retry(fn, name: str, max_retries: int = 3, base_wait: float = 3.0):
    """给 akshare 偶发断流场景用的简单指数退避重试。"""
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = base_wait * (attempt + 1)
                print(f"  ⚠️  {name} 第 {attempt+1}/{max_retries} 次失败：{type(e).__name__}，"
                      f"等待 {wait}s 后重试")
                time.sleep(wait)
    print(f"  ❌ {name} 重试 {max_retries} 次均失败：{last_err}")
    return None


# ------------------------------------------------------------------
# 实时快照（新浪源，东财端点 stock_zh_a_spot_em 服务端断流不稳）
# ------------------------------------------------------------------

def fetch_spot_snapshot() -> pd.DataFrame:
    """全 A 股票当前快照（新浪源）。返回 code/name/close/pct_change。
    新浪不提供 PE/PB/总市值，这些字段由 enrich_spot_from_cache 从缓存补齐。
    """
    df = _retry(ak.stock_zh_a_spot, "全 A 快照（新浪）")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={
        "代码": "code_raw", "名称": "name",
        "最新价": "close", "涨跌幅": "pct_change",
    })
    # 新浪格式 sh601988 / sz000651，剥离前缀
    df["code"] = df["code_raw"].astype(str).str[-6:]
    df = df[["code", "name", "close", "pct_change"]].copy()
    df["asset_type"] = "stock"
    return df


def fetch_etf_spot_snapshot(codes: list[str] | None = None) -> pd.DataFrame:
    """ETF 当前快照。东财端点常断流，因此当指定 codes 时用腾讯逐只拉最新一日。
    不指定 codes 时尝试东财全量；失败返回空。
    """
    if codes:
        return _fetch_etf_spot_via_tx(codes)
    df = _retry(ak.fund_etf_spot_em, "ETF 快照（东财）", max_retries=2)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={
        "代码": "code", "名称": "name", "最新价": "close",
        "涨跌幅": "pct_change",
    })
    df = df[[c for c in ["code", "name", "close", "pct_change"] if c in df.columns]].copy()
    if "code" not in df.columns:
        return pd.DataFrame()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["asset_type"] = "etf"
    return df


def _fetch_etf_spot_via_tx(codes: list[str]) -> pd.DataFrame:
    """腾讯历史源逐只拉 ETF 最近 5 日，取最后一行作为当前价。"""
    today = datetime.now()
    start = (today - timedelta(days=10)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    rows = []
    for code in codes:
        symbol = f"{_exchange_prefix(code)}{code}"
        try:
            df = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start,
                                       end_date=end, adjust="qfq")
        except Exception as e:
            print(f"  ⚠️  ETF {code} 现价拉取失败：{type(e).__name__}")
            continue
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        pct = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else 0
        rows.append({"code": code, "name": code, "close": float(last["close"]),
                    "pct_change": pct, "asset_type": "etf"})
    return pd.DataFrame(rows)


def fetch_combined_spot(conn: duckdb.DuckDBPyConnection | None = None,
                        etf_codes: list[str] | None = None) -> pd.DataFrame:
    """股票 + ETF 合并快照。
    etf_codes 为持仓 ETF 列表（用腾讯逐只拉，回避东财端点不稳）。
    传入 conn 时会从缓存补齐 PE/PB/总市值/60日动量。
    """
    stocks = fetch_spot_snapshot()
    etfs = fetch_etf_spot_snapshot(etf_codes)
    if stocks.empty and etfs.empty:
        return pd.DataFrame()
    spot = pd.concat([stocks, etfs], ignore_index=True, sort=False)
    if conn is not None and not spot.empty:
        spot = enrich_spot_from_cache(conn, spot)
    return spot


def enrich_spot_from_cache(conn: duckdb.DuckDBPyConnection,
                           spot_df: pd.DataFrame) -> pd.DataFrame:
    """从 daily_indicators / daily_quotes 缓存补齐估值与动量字段。"""
    if spot_df.empty:
        return spot_df

    codes = spot_df["code"].tolist()
    conn.register("_codes", pd.DataFrame({"code": codes}))
    try:
        # 最新一行估值
        ind = conn.execute("""
            SELECT code, pe_ttm AS pe, pb, total_mv
            FROM daily_indicators
            WHERE code IN (SELECT code FROM _codes)
            QUALIFY ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) = 1
        """).fetch_df()

        # 60 日累计涨跌幅：取最新与第 60 个交易日的复权收盘比值
        cutoff = date.today() - timedelta(days=120)
        mom = conn.execute("""
            WITH win AS (
              SELECT q.code, q.close,
                     ROW_NUMBER() OVER (PARTITION BY q.code ORDER BY q.trade_date DESC) AS rn
              FROM daily_quotes q
              WHERE q.code IN (SELECT code FROM _codes) AND q.trade_date >= ?
            )
            SELECT code,
                   100.0 * (MAX(CASE WHEN rn = 1 THEN close END) /
                            NULLIF(MAX(CASE WHEN rn = 60 THEN close END), 0) - 1) AS pct_60d
            FROM win
            WHERE rn IN (1, 60)
            GROUP BY code
        """, [cutoff]).fetch_df()
    finally:
        conn.unregister("_codes")

    # 防止重复列冲突
    spot_df = spot_df.drop(columns=[c for c in ["pe", "pb", "total_mv", "pct_60d"]
                                    if c in spot_df.columns], errors="ignore")
    spot_df = spot_df.merge(ind, on="code", how="left")
    spot_df = spot_df.merge(mom, on="code", how="left")
    return spot_df


# ------------------------------------------------------------------
# 股票主信息（名称表）
# ------------------------------------------------------------------

def fetch_stock_names() -> pd.DataFrame:
    """全 A 股票代码 + 名称对照表。优先复用新浪 spot（同时拿到名称），
    上交所 stock_info_a_code_name 端点不稳定不再使用。
    """
    spot = fetch_spot_snapshot()
    if spot.empty:
        return pd.DataFrame()
    return spot[["code", "name"]].copy()


# ------------------------------------------------------------------
# 指数成份股
# ------------------------------------------------------------------

def fetch_index_constituents(index_code: str) -> list[str]:
    """获取指数成份股代码列表，多个数据源依次尝试。"""
    for fetcher in (_fetch_cons_csindex, _fetch_cons_sina):
        try:
            codes = fetcher(index_code)
            if codes:
                return codes
        except Exception as e:
            print(f"  ⚠️  {fetcher.__name__}({index_code}) 失败：{e}")
    return []


def _fetch_cons_csindex(index_code: str) -> list[str]:
    """从中证指数官方接口获取成份股。"""
    df = ak.index_stock_cons_csindex(symbol=index_code)
    col = "成分券代码" if "成分券代码" in df.columns else df.columns[0]
    return df[col].astype(str).str.zfill(6).tolist()


def _fetch_cons_sina(index_code: str) -> list[str]:
    """从新浪接口获取成份股，作为备用源。"""
    df = ak.index_stock_cons_sina(symbol=index_code)
    col = "code" if "code" in df.columns else df.columns[0]
    return df[col].astype(str).str.zfill(6).tolist()


# ------------------------------------------------------------------
# 历史数据（按个股拉取）
# ------------------------------------------------------------------

def _exchange_prefix(code: str) -> str:
    """根据 6 位代码推断交易所前缀（腾讯源需要）。"""
    # 沪市：60xxxx 主板，68xxxx 科创板，9xxxxx B股，58xxxx 科创板 ETF，51xxxx 沪市 ETF
    if code.startswith(("60", "68", "9", "58", "51", "56")):
        return "sh"
    if code.startswith(("4", "8")):          # 北交所
        return "bj"
    return "sz"                              # 其余归深市（含 159xxx ETF）


def fetch_daily_quotes(code: str, start_date: str,
                      end_date: str | None = None) -> pd.DataFrame:
    """拉取个股日线 OHLC（腾讯源，比东财稳定）。日期格式 YYYYMMDD。
    注意：腾讯源不返回 volume 和涨跌幅，所以 volume 留空，pct_change 自行计算。
    """
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    symbol = f"{_exchange_prefix(code)}{code}"
    try:
        df = ak.stock_zh_a_hist_tx(symbol=symbol,
                                   start_date=start_date, end_date=end_date,
                                   adjust="qfq")
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"date": "trade_date"})
    df["code"] = code
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["pct_change"] = df["close"].pct_change() * 100
    df["volume"] = None
    cols = ["code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "pct_change"]
    return df[[c for c in cols if c in df.columns]]


def fetch_indicator_history(code: str) -> pd.DataFrame:
    """拉取个股估值历史（PE/PB/总市值），来自东财 stock_value_em。
    注意：该接口不提供股息率，因此 dv_ttm/dv_ratio 为空。
    """
    try:
        df = ak.stock_value_em(symbol=code)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={
        "数据日期": "trade_date",
        "PE(TTM)": "pe_ttm",
        "PE(静)": "pe",
        "市净率": "pb",
        "市销率": "ps_ttm",
        "总市值": "total_mv",
    })
    df["code"] = code
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["dv_ratio"] = None
    df["dv_ttm"] = None
    cols = ["code", "trade_date", "pe", "pe_ttm", "pb",
            "ps_ttm", "dv_ratio", "dv_ttm", "total_mv"]
    return df[[c for c in cols if c in df.columns]]


# ------------------------------------------------------------------
# DuckDB 写入
# ------------------------------------------------------------------

def upsert_stock_info(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    """更新股票基础信息（code + name + 可选 industry / list_date / is_st）。
    缺失列自动填空，已有库行被整体替换。
    """
    if df.empty:
        return
    out = df.copy()
    for col, default in [("industry", None), ("list_date", None), ("is_st", False)]:
        if col not in out.columns:
            out[col] = default
    out = out[["code", "name", "industry", "list_date", "is_st"]]
    conn.register("_tmp_si", out)
    conn.execute("""
        INSERT OR REPLACE INTO stock_info (code, name, industry, list_date, is_st)
        SELECT code, name, industry, list_date, is_st FROM _tmp_si
    """)
    conn.unregister("_tmp_si")


def upsert_universe(conn: duckdb.DuckDBPyConnection,
                   index_code: str, codes: list[str]) -> None:
    """更新某指数的成份股名单（保留时点历史）。

    逻辑：
      - 当前在指数里的（effective_to IS NULL）但新名单中没有的 → 标记 effective_to = today
      - 新名单中有但数据库中没活跃记录的 → INSERT 一条 (code, index_code, today, NULL)
      - 已在指数中且仍在新名单的 → 不动

    这样回测时可用 `effective_from <= D AND (effective_to IS NULL OR effective_to > D)`
    还原任意 D 时点的成份股。
    """
    if not codes:
        return
    today = date.today()
    df = pd.DataFrame({"code": codes, "index_code": index_code})
    conn.register("_tmp_u", df)
    # 1. 退出指数：当前活跃但新名单中没有的，标记 effective_to=today
    conn.execute("""
        UPDATE universe_membership
        SET effective_to = ?
        WHERE index_code = ? AND effective_to IS NULL
          AND code NOT IN (SELECT code FROM _tmp_u)
    """, [today, index_code])
    # 2. 新进指数：新名单中有但数据库中没有活跃记录的
    conn.execute("""
        INSERT INTO universe_membership (code, index_code, effective_from, effective_to)
        SELECT t.code, t.index_code, ?, NULL
        FROM _tmp_u t
        WHERE NOT EXISTS (
            SELECT 1 FROM universe_membership u
            WHERE u.code = t.code AND u.index_code = t.index_code
              AND u.effective_to IS NULL
        )
    """, [today])
    conn.unregister("_tmp_u")


def upsert_daily_quotes(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """写入日线行情，返回写入行数。"""
    if df.empty:
        return 0
    df = validators.validate_quotes(df)
    if df.empty:
        return 0
    conn.register("_tmp_q", df)
    conn.execute("""
        INSERT OR REPLACE INTO daily_quotes
        SELECT code, trade_date, open, high, low, close, volume, amount, pct_change
        FROM _tmp_q
    """)
    n = len(df)
    conn.unregister("_tmp_q")
    return n


def upsert_daily_indicators(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """写入估值历史，返回写入行数。"""
    if df.empty:
        return 0
    df = validators.validate_indicators(df)
    cols = ["code", "trade_date", "pe", "pe_ttm", "pb",
            "ps_ttm", "dv_ratio", "dv_ttm", "total_mv"]
    df = df.copy()
    for c in cols:
        if c not in df.columns:
            df[c] = None
    conn.register("_tmp_i", df[cols])
    conn.execute("""
        INSERT OR REPLACE INTO daily_indicators
        SELECT code, trade_date, pe, pe_ttm, pb, ps_ttm, dv_ratio, dv_ttm, total_mv
        FROM _tmp_i
    """)
    n = len(df)
    conn.unregister("_tmp_i")
    return n


# ------------------------------------------------------------------
# 增量刷新
# ------------------------------------------------------------------

def _last_quote_date(conn, code: str) -> date | None:
    """查询某代码的最后一条行情日期。"""
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_quotes WHERE code = ?", [code]
    ).fetchone()
    return row[0] if row and row[0] else None


def _last_indicator_date(conn, code: str) -> date | None:
    """查询某代码的最后一条估值指标日期。"""
    row = conn.execute(
        "SELECT MAX(trade_date) FROM daily_indicators WHERE code = ?", [code]
    ).fetchone()
    return row[0] if row and row[0] else None


def _for_each_code(conn: duckdb.DuckDBPyConnection, codes: list[str], *,
                  label: str, last_date_fn, fetcher, upsert,
                  fresh_days: int = 1, sleep_sec: float = 0.0) -> dict:
    """通用增量刷新模板：每只 code 检查 last_date → 拉数据 → 写入。

    last_date_fn(conn, code) -> date | None 返回数据库中已落库的最新日期；
    fetcher(code, last_date) -> DataFrame 拉新数据；upsert(conn, df) 写入。
    fresh_days 内的不重复拉。每 50 只打一次进度。
    """
    stats = {"ok": 0, "fail": 0, "skip": 0}
    today = date.today()
    for i, code in enumerate(codes):
        last = last_date_fn(conn, code)
        if last and (today - last).days <= fresh_days:
            stats["skip"] += 1
        else:
            df = fetcher(code, last)
            if df.empty:
                stats["fail"] += 1
            else:
                upsert(conn, df)
                stats["ok"] += 1
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        if (i + 1) % 50 == 0:
            print(f"  [{label}] {i+1}/{len(codes)} | "
                  f"ok={stats['ok']} fail={stats['fail']} skip={stats['skip']}")
    return stats


def refresh_history_incremental(conn: duckdb.DuckDBPyConnection, codes: list[str],
                               min_days: int = 120, sleep_sec: float = 0.3) -> dict:
    """增量更新行情：从上次缓存日期之后开始拉。"""
    today = date.today()

    def fetch(code, last):
        start = (last + timedelta(days=1)) if last else (today - timedelta(days=min_days * 2))
        return fetch_daily_quotes(code, start.strftime("%Y%m%d"))

    return _for_each_code(
        conn, codes, label="行情",
        last_date_fn=_last_quote_date, fetcher=fetch,
        upsert=upsert_daily_quotes, sleep_sec=sleep_sec,
    )


def refresh_indicator_incremental(conn: duckdb.DuckDBPyConnection,
                                  codes: list[str], sleep_sec: float = 0.3) -> dict:
    """增量更新估值：stock_value_em 不支持日期参数，全量重拉 + INSERT OR REPLACE 去重。"""
    return _for_each_code(
        conn, codes, label="估值",
        last_date_fn=_last_indicator_date,
        fetcher=lambda code, _last: fetch_indicator_history(code),
        upsert=upsert_daily_indicators, sleep_sec=sleep_sec,
    )


# ------------------------------------------------------------------
# Baostock：财务指标 + 分红记录
# Baostock 完全免费、稳定，但每次查询 ~200ms，因此只对股票池范围拉
# ------------------------------------------------------------------

def _to_baostock_code(code: str) -> str:
    """6 位代码 → baostock 格式（sh.600000 / sz.000001）。"""
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "9", "58", "51", "56")):
        return f"sh.{code}"
    if code.startswith(("4", "8")):
        return f"bj.{code}"
    return f"sz.{code}"


@contextmanager
def baostock_session():
    """登录/注销 baostock 的上下文管理器。整段刷新过程共用一个 session。"""
    rs = bs.login()
    if rs.error_code != "0":
        raise RuntimeError(f"baostock 登录失败：{rs.error_code} {rs.error_msg}")
    try:
        yield
    finally:
        bs.logout()


def _bs_query_to_df(rs) -> pd.DataFrame:
    """把 baostock 查询结果展开为 DataFrame。错误时返回空 DataFrame。"""
    if rs is None or rs.error_code != "0":
        return pd.DataFrame()
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=rs.fields)


def _recent_quarters(n: int = 4) -> list[tuple[int, int]]:
    """返回最近 n 个已结束季度 (year, quarter) 列表，从近到远。
    财报披露通常滞后 1-2 个月，因此取上一个完整季度作为起点。
    """
    today = date.today()
    cur_q = (today.month - 1) // 3 + 1   # 当前所在季度（1-4）
    # 拉过去 n 个季度的财报
    quarters = []
    y, q = today.year, cur_q
    for _ in range(n):
        q -= 1
        if q == 0:
            q = 4
            y -= 1
        quarters.append((y, q))
    return quarters


def fetch_financial_metrics(code: str, n_quarters: int = 4) -> pd.DataFrame:
    """从 baostock 拉单只股票最近 n 个季度的 ROE / 增速等财务数据。
    每季度需要 query_profit_data + query_growth_data 两次调用。
    """
    bs_code = _to_baostock_code(code)
    rows = []
    for y, q in _recent_quarters(n_quarters):
        prof = _bs_query_to_df(bs.query_profit_data(code=bs_code, year=y, quarter=q))
        grow = _bs_query_to_df(bs.query_growth_data(code=bs_code, year=y, quarter=q))
        if prof.empty:
            continue
        merged = prof.iloc[0].to_dict()
        if not grow.empty:
            merged.update({k: v for k, v in grow.iloc[0].to_dict().items()
                          if k not in merged})
        rows.append(merged)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "code": code,
        "stat_date": pd.to_datetime(df["statDate"], errors="coerce").dt.date,
        "pub_date": pd.to_datetime(df["pubDate"], errors="coerce").dt.date,
        "roe_avg": pd.to_numeric(df.get("roeAvg"), errors="coerce") * 100,
        "np_margin": pd.to_numeric(df.get("npMargin"), errors="coerce") * 100,
        "gp_margin": pd.to_numeric(df.get("gpMargin"), errors="coerce") * 100,
        "yoy_ni": pd.to_numeric(df.get("YOYNI"), errors="coerce") * 100,
        "yoy_equity": pd.to_numeric(df.get("YOYEquity"), errors="coerce") * 100,
    })
    return out.dropna(subset=["stat_date"])


def fetch_dividend_records(code: str, years: int = 2) -> pd.DataFrame:
    """拉最近 N 年的现金分红明细（按报告期分组）。"""
    bs_code = _to_baostock_code(code)
    cur_year = date.today().year
    rows = []
    for y in range(cur_year - years, cur_year + 1):
        df = _bs_query_to_df(bs.query_dividend_data(code=bs_code, year=str(y),
                                                   yearType="report"))
        if df.empty:
            continue
        df = df.assign(report_year=y)
        rows.append(df)
    if not rows:
        return pd.DataFrame()

    raw = pd.concat(rows, ignore_index=True)
    out = pd.DataFrame({
        "code": code,
        "report_year": raw["report_year"],
        "pay_date": pd.to_datetime(raw.get("dividPayDate"), errors="coerce").dt.date,
        "cash_per_share_pretax": pd.to_numeric(raw.get("dividCashPsBeforeTax"),
                                                errors="coerce"),
    })
    out = out.dropna(subset=["pay_date", "cash_per_share_pretax"])
    out = out[out["cash_per_share_pretax"] > 0]
    return out


def upsert_financial_metrics(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """写入财务指标，返回写入行数。"""
    if df.empty:
        return 0
    df = validators.validate_financials(df)
    if df.empty:
        return 0
    cols = ["code", "stat_date", "pub_date", "roe_avg", "np_margin",
            "gp_margin", "yoy_ni", "yoy_equity"]
    conn.register("_tmp_fm", df[cols])
    conn.execute("""
        INSERT OR REPLACE INTO financial_metrics
        SELECT code, stat_date, pub_date, roe_avg, np_margin,
               gp_margin, yoy_ni, yoy_equity
        FROM _tmp_fm
    """)
    n = len(df)
    conn.unregister("_tmp_fm")
    return n


def upsert_dividend_records(conn: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """写入分红明细，返回写入行数。"""
    if df.empty:
        return 0
    df = validators.validate_dividends(df)
    if df.empty:
        return 0
    cols = ["code", "report_year", "pay_date", "cash_per_share_pretax"]
    conn.register("_tmp_dv", df[cols])
    conn.execute("""
        INSERT OR REPLACE INTO dividend_records
        SELECT code, report_year, pay_date, cash_per_share_pretax
        FROM _tmp_dv
    """)
    n = len(df)
    conn.unregister("_tmp_dv")
    return n


def _last_financial_pub_date(conn, code: str) -> date | None:
    """查询某代码已落库的最新财报公告日。"""
    row = conn.execute(
        "SELECT MAX(pub_date) FROM financial_metrics WHERE code = ?", [code]
    ).fetchone()
    return row[0] if row and row[0] else None


def _last_dividend_pay_date(conn, code: str) -> date | None:
    """查询某代码已落库的最新分红派息日。"""
    row = conn.execute(
        "SELECT MAX(pay_date) FROM dividend_records WHERE code = ?", [code]
    ).fetchone()
    return row[0] if row and row[0] else None


def refresh_financial_incremental(conn: duckdb.DuckDBPyConnection,
                                  codes: list[str], sleep_sec: float = 0.0,
                                  fresh_days: int = 60) -> dict:
    """增量刷新财务指标。已有近 fresh_days 天内公告的不重复拉。
    调用前需在 baostock_session 上下文里。
    """
    return _for_each_code(
        conn, codes, label="财务",
        last_date_fn=_last_financial_pub_date,
        fetcher=lambda code, _last: fetch_financial_metrics(code),
        upsert=upsert_financial_metrics,
        fresh_days=fresh_days, sleep_sec=sleep_sec,
    )


def _to_baostock_index_code(code: str) -> str:
    """指数代码 → baostock 格式。沪市指数 000xxx → sh.000xxx；深市指数 399xxx → sz.399xxx。"""
    code = str(code).zfill(6)
    if code.startswith("000"):
        return f"sh.{code}"
    if code.startswith("399"):
        return f"sz.{code}"
    raise ValueError(f"未识别的指数代码: {code}")


def fetch_index_daily(index_code: str, start_date: str | None = None,
                     end_date: str | None = None) -> pd.DataFrame:
    """拉指数日线行情。index_code 为 6 位代码（如 '000300'）。
    默认拉过去 3 年；返回字段对齐 daily_quotes 表结构。
    """
    bs_code = _to_baostock_index_code(index_code)
    end_date = end_date or date.today().strftime("%Y-%m-%d")
    start_date = start_date or (date.today() - timedelta(days=3 * 365)).strftime("%Y-%m-%d")
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,amount,pctChg",
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="3",
    )
    raw = _bs_query_to_df(rs)
    if raw.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "code": index_code,
        "trade_date": pd.to_datetime(raw["date"], errors="coerce").dt.date,
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw["volume"], errors="coerce").astype("Int64"),
        "amount": pd.to_numeric(raw["amount"], errors="coerce"),
        "pct_change": pd.to_numeric(raw["pctChg"], errors="coerce"),
    })
    return out.dropna(subset=["trade_date"])


def refresh_indices_incremental(conn: duckdb.DuckDBPyConnection,
                               index_codes: list[str]) -> dict:
    """增量刷新基准指数日线，写入 daily_quotes（与个股共用一张表）。"""
    stats = {"ok": 0, "fail": 0, "skip": 0}
    today = date.today()
    for code in index_codes:
        last = _last_quote_date(conn, code)
        if last and (today - last).days <= 1:
            stats["skip"] += 1
            continue
        start = (last + timedelta(days=1)).strftime("%Y-%m-%d") if last else None
        df = fetch_index_daily(code, start_date=start)
        if df.empty:
            stats["fail"] += 1
        else:
            upsert_daily_quotes(conn, df)
            stats["ok"] += 1
    return stats


def fetch_basic_info() -> pd.DataFrame:
    """全市场基础信息：code / name / list_date / is_st。
    baostock query_stock_basic 不带参数会返回全市场，比逐只调用快得多。
    """
    df = _bs_query_to_df(bs.query_stock_basic())
    if df.empty:
        return pd.DataFrame()
    # 仅保留 type=1 的股票（type 2=指数, 3=其它, 4=可转债, 5=ETF）
    df = df[df["type"] == "1"].copy()
    out = pd.DataFrame({
        "code": df["code"].str.split(".").str[1],
        "name": df["code_name"],
        "list_date": pd.to_datetime(df["ipoDate"], errors="coerce").dt.date,
        "is_st": df["code_name"].str.contains("ST", na=False),
    })
    return out.dropna(subset=["code"])


def fetch_industry_info() -> pd.DataFrame:
    """全市场行业分类（baostock 用国标 GB/T 4754，如 'C39计算机...' / 'J66货币金融服务'）。"""
    df = _bs_query_to_df(bs.query_stock_industry())
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "code": df["code"].str.split(".").str[1],
        "industry": df["industry"],
    })
    out = out[out["industry"].astype(str).str.len() > 0]
    return out.drop_duplicates(subset=["code"])


def refresh_stock_meta(conn: duckdb.DuckDBPyConnection,
                      codes: list[str] | None = None) -> int:
    """刷新 stock_info 的 industry 和 list_date 字段。
    需要在 baostock_session 上下文里调用。codes 为 None 则更新全市场。
    返回写入行数。
    """
    basic = fetch_basic_info()
    if basic.empty:
        print("  ⚠️  baostock 基础信息拉取为空")
        return 0
    industry = fetch_industry_info()
    df = basic.merge(industry, on="code", how="left")
    if codes is not None:
        df = df[df["code"].isin(codes)]
    upsert_stock_info(conn, df)
    return len(df)


def refresh_dividend_incremental(conn: duckdb.DuckDBPyConnection,
                                 codes: list[str], sleep_sec: float = 0.0,
                                 fresh_days: int = 90) -> dict:
    """增量刷新分红记录。已有近 fresh_days 天内派息的不重复拉。"""
    return _for_each_code(
        conn, codes, label="分红",
        last_date_fn=_last_dividend_pay_date,
        fetcher=lambda code, _last: fetch_dividend_records(code),
        upsert=upsert_dividend_records,
        fresh_days=fresh_days, sleep_sec=sleep_sec,
    )
