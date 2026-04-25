.PHONY: install init universe data run all clean help

help:
	@echo "Stockpilot 命令一览："
	@echo "  make install   —— 创建虚拟环境并安装依赖（首次执行一次）"
	@echo "  make init      —— 初始化 DuckDB schema"
	@echo "  make universe  —— 刷新指数成份股（约 30 秒）"
	@echo "  make data      —— 刷新行情 + 估值历史（首次约 10-20 分钟，之后增量）"
	@echo "  make run       —— 运行主流程：体检 + 雷达 + 生成日报"
	@echo "  make all       —— 顺序执行 init + universe + data + run"
	@echo "  make clean     —— 清空数据库与日报（破坏性，谨慎）"

install:
	uv venv
	uv pip install -e .

init:
	uv run python -m src.main init-db

universe:
	uv run python -m src.main refresh-universe

data:
	uv run python -m src.main refresh-data

run:
	uv run python -m src.main run

all: init universe data run

clean:
	rm -rf data/*.duckdb data/*.duckdb.wal reports/*.md
