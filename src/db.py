"""DuckDB 连接管理与 schema 初始化。"""
from __future__ import annotations
from pathlib import Path
import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stockpilot.duckdb"

# 全部表 + 序列定义，幂等执行
SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_info (
    code VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    industry VARCHAR,
    list_date DATE,
    is_st BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS universe_membership (
    code VARCHAR,
    index_code VARCHAR,
    effective_from DATE NOT NULL,            -- 进入指数当日
    effective_to DATE,                       -- 退出指数当日；NULL 表示当前仍在
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, index_code, effective_from)
);

CREATE TABLE IF NOT EXISTS daily_quotes (
    code VARCHAR,
    trade_date DATE,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    amount DOUBLE,
    pct_change DOUBLE,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS daily_indicators (
    code VARCHAR,
    trade_date DATE,
    pe DOUBLE,
    pe_ttm DOUBLE,
    pb DOUBLE,
    ps_ttm DOUBLE,
    dv_ratio DOUBLE,
    dv_ttm DOUBLE,
    total_mv DOUBLE,
    PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS financial_metrics (
    code VARCHAR,
    stat_date DATE,             -- 财报截止日（季末）
    pub_date DATE,              -- 财报公告日
    roe_avg DOUBLE,             -- 平均净资产收益率（季度，单位 %）
    np_margin DOUBLE,           -- 销售净利率（%）
    gp_margin DOUBLE,           -- 销售毛利率（%）
    yoy_ni DOUBLE,              -- 净利润同比增长率（%）
    yoy_equity DOUBLE,          -- 净资产同比增长率（%）
    PRIMARY KEY (code, stat_date)
);

CREATE TABLE IF NOT EXISTS dividend_records (
    code VARCHAR,
    report_year INTEGER,                    -- 分红所属年度
    pay_date DATE,                          -- 派息日
    cash_per_share_pretax DOUBLE,           -- 每股税前派息（元）
    PRIMARY KEY (code, report_year, pay_date)
);

CREATE TABLE IF NOT EXISTS factor_snapshots (
    code VARCHAR,
    snapshot_date DATE,
    pe DOUBLE,
    pb DOUBLE,
    pe_pct_3y DOUBLE,                        -- 字段名遗留命名；实际窗口 5 年（factors._fetch_indicator_features）
    pb_pct_3y DOUBLE,                        -- 同上
    dividend_yield DOUBLE,
    roe DOUBLE,
    profit_growth DOUBLE,
    momentum_60d DOUBLE,
    volatility_60d DOUBLE,
    market_cap DOUBLE,
    valuation_score DOUBLE,
    dividend_score DOUBLE,
    quality_score DOUBLE,
    momentum_score DOUBLE,
    volatility_score DOUBLE,
    composite_score DOUBLE,
    PRIMARY KEY (code, snapshot_date)
);

CREATE TABLE IF NOT EXISTS radar_history (
    snapshot_date DATE,
    rank INTEGER,
    code VARCHAR,
    composite_score DOUBLE,
    PRIMARY KEY (snapshot_date, code)
);

CREATE SEQUENCE IF NOT EXISTS signal_log_id_seq START 1;

CREATE TABLE IF NOT EXISTS signal_log (
    id BIGINT PRIMARY KEY DEFAULT nextval('signal_log_id_seq'),
    signal_date DATE,
    code VARCHAR,
    name VARCHAR,
    signal_type VARCHAR,
    reason VARCHAR,
    user_action VARCHAR,
    user_note VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_date DATE,
    code VARCHAR,
    name VARCHAR,
    shares INTEGER,
    cost_price DOUBLE,
    current_price DOUBLE,
    market_value DOUBLE,
    weight DOUBLE,              -- 持仓内权重（market_value / 总持仓市值）
    pnl DOUBLE,
    pnl_pct DOUBLE,
    PRIMARY KEY (snapshot_date, code)
);
"""


def get_connection(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """打开 DuckDB 连接，必要时创建父目录。"""
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(p))


def _migrate_v4_universe(conn: duckdb.DuckDBPyConnection) -> None:
    """universe_membership 加时点字段并改主键。已经新结构的库自动跳过。"""
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'universe_membership'"
    ).fetchall()
    if "effective_from" in {r[0] for r in cols}:
        return  # 新建库已经是新结构（SCHEMA 已升级），无需迁移
    conn.execute("""
        CREATE TABLE universe_membership_v2 (
            code VARCHAR,
            index_code VARCHAR,
            effective_from DATE NOT NULL,
            effective_to DATE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, index_code, effective_from)
        )
    """)
    # 旧数据 effective_from 用 updated_at 兜底为今日
    conn.execute("""
        INSERT INTO universe_membership_v2
            (code, index_code, effective_from, updated_at)
        SELECT code, index_code,
               COALESCE(CAST(updated_at AS DATE), CURRENT_DATE),
               updated_at
        FROM universe_membership
    """)
    conn.execute("DROP TABLE universe_membership")
    conn.execute("ALTER TABLE universe_membership_v2 RENAME TO universe_membership")


# 版本化迁移列表：(version, description, action)
# action 可以是 SQL 字符串（单条）或 callable(conn)（多条/复杂逻辑）。
# 已应用的版本会被 schema_version 表记录跳过。
MIGRATIONS = [
    (1, "factor_snapshots add roe",
     "ALTER TABLE factor_snapshots ADD COLUMN IF NOT EXISTS roe DOUBLE"),
    (2, "factor_snapshots add profit_growth",
     "ALTER TABLE factor_snapshots ADD COLUMN IF NOT EXISTS profit_growth DOUBLE"),
    (3, "position_snapshots add weight",
     "ALTER TABLE position_snapshots ADD COLUMN IF NOT EXISTS weight DOUBLE"),
    (4, "universe_membership 加 effective_from/to 时点字段",
     _migrate_v4_universe),
]

# 二级索引：DuckDB 列式 + zone map 已自带高效点查，
# 但对非主键左前缀的过滤列（如 snapshot_date、signal_date）建索引仍能加速 5-20%。
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_factor_snapshot_date ON factor_snapshots(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_signal_log_date ON signal_log(signal_date)",
    "CREATE INDEX IF NOT EXISTS idx_stock_industry ON stock_info(industry)",
    "CREATE INDEX IF NOT EXISTS idx_radar_date ON radar_history(snapshot_date)",
]

_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    description VARCHAR,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _applied_versions(conn: duckdb.DuckDBPyConnection) -> set[int]:
    """读取 schema_version 表，返回已应用的版本集合。"""
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r[0] for r in rows}


def _apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """按版本号顺序跑未应用的迁移，每条成功后记录到 schema_version。
    action 可以是 SQL 字符串或 callable(conn)。
    """
    conn.execute(_SCHEMA_VERSION_TABLE)
    applied = _applied_versions(conn)
    for version, desc, action in MIGRATIONS:
        if version in applied:
            continue
        if callable(action):
            action(conn)
        else:
            conn.execute(action)
        conn.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            [version, desc],
        )


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """创建所有表，幂等。同时按版本号补迁移、建二级索引。"""
    conn.execute(SCHEMA)
    _apply_migrations(conn)
    for stmt in INDEXES:
        conn.execute(stmt)


def ensure_db(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """打开连接并确保 schema 存在的便捷封装。"""
    conn = get_connection(path)
    init_schema(conn)
    return conn
