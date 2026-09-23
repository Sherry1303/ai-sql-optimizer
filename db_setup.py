#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""演示数据库构建脚本 / 数据访问层。

为「AI SQL 优化助手」生成一个 SQLite 演示库（data/demo.db）：

    suppliers  100 行     供应商维度表
    customers  5,000 行   客户维度表
    products   2,000 行   商品维度表
    orders     50,000 行  订单事实表  <- 题目要求的「5 万条数据」核心表

设计要点（很重要）：
    建库时 **只** 为主键与 UNIQUE 列创建索引（customers.email / products.sku），
    orders.customer_id、orders.product_id、orders.order_date、orders.status、
    products.category 等常用过滤/连接列故意保持「无索引」状态。
    这样 EXPLAIN QUERY PLAN 会真实地出现 `SCAN orders` / `USE TEMP B-TREE`，
    DeepSeek 提出的「加索引」建议才可被验证（应用索引前后可对比耗时）。

用法：
    python db_setup.py                  # 生成 data/demo.db（已存在则报错退出）
    python db_setup.py --force          # 覆盖重建
    python db_setup.py --orders 200000  # 自定义 orders 行数（压测用）
    python db_setup.py --print-schema   # 只打印供 LLM 使用的 schema 文本
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

# --------------------------------------------------------------------------- #
# 常量与配置
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "demo.db"

#: 各表目标行数；orders 即「5 万条数据」核心事实表
TABLE_ROWS: dict[str, int] = {
    "suppliers": 100,
    "customers": 5_000,
    "products": 2_000,
    "orders": 50_000,
}

#: 默认随机种子，保证任何人重建出来的库内容一致（便于复现演示）
DEFAULT_SEED = 20240101

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE suppliers (
        id      INTEGER PRIMARY KEY,
        name    TEXT    NOT NULL,
        country TEXT    NOT NULL,
        rating  REAL    NOT NULL
    )
    """,
    """
    CREATE TABLE customers (
        id          INTEGER PRIMARY KEY,
        name        TEXT    NOT NULL,
        email       TEXT    NOT NULL UNIQUE,   -- UNIQUE -> 自动创建索引
        city        TEXT    NOT NULL,
        country     TEXT    NOT NULL,
        vip_level   INTEGER NOT NULL,
        signup_date TEXT    NOT NULL           -- 'YYYY-MM-DD'
    )
    """,
    """
    CREATE TABLE products (
        id          INTEGER PRIMARY KEY,
        sku         TEXT    NOT NULL UNIQUE,   -- UNIQUE -> 自动创建索引
        name        TEXT    NOT NULL,
        category    TEXT    NOT NULL,          -- 故意无索引（高频过滤列）
        price       REAL    NOT NULL,
        stock       INTEGER NOT NULL,
        supplier_id INTEGER NOT NULL REFERENCES suppliers(id)
    )
    """,
    """
    CREATE TABLE orders (
        id           INTEGER PRIMARY KEY,
        customer_id  INTEGER NOT NULL REFERENCES customers(id),  -- 故意无索引
        product_id   INTEGER NOT NULL REFERENCES products(id),   -- 故意无索引
        quantity     INTEGER NOT NULL,
        unit_price   REAL    NOT NULL,
        total_amount REAL    NOT NULL,
        status       TEXT    NOT NULL,   -- 故意无索引（高偏斜度，适合建部分索引演示）
        channel      TEXT    NOT NULL,
        order_date   TEXT    NOT NULL,   -- 'YYYY-MM-DD'，故意无索引（范围查询演示）
        ship_date    TEXT,
        remark       TEXT
    )
    """,
)

#: 数据字典：让结果集有真实的业务味道，同时保证取值偏斜（索引选择性更真实）
COUNTRIES_CITIES: dict[str, tuple[str, ...]] = {
    "中国": ("北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安"),
    "美国": ("New York", "San Francisco", "Seattle", "Austin", "Chicago"),
    "德国": ("Berlin", "Munich", "Hamburg", "Cologne"),
    "日本": ("Tokyo", "Osaka", "Nagoya", "Fukuoka"),
    "巴西": ("Sao Paulo", "Rio de Janeiro", "Brasilia"),
    "印度": ("Mumbai", "Delhi", "Bangalore", "Pune"),
    "英国": ("London", "Manchester", "Bristol"),
    "法国": ("Paris", "Lyon", "Toulouse"),
}

CATEGORIES: tuple[str, ...] = (
    "手机数码", "家用电器", "服饰鞋包", "食品生鲜", "图书文娱", "美妆个护",
    "运动户外", "母婴玩具", "家居家装", "汽车用品", "办公用品", "宠物用品",
)

#: (取值, 权重) —— 权重刻意不均匀，模拟真实业务分布
STATUS_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("completed", 0.62),
    ("shipped", 0.14),
    ("pending", 0.09),
    ("cancelled", 0.08),
    ("refunded", 0.07),
)

CHANNEL_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("app", 0.45),
    ("web", 0.30),
    ("miniprogram", 0.17),
    ("offline", 0.08),
)

EMAIL_DOMAINS: tuple[str, ...] = ("example.com", "mail.example.com", "test.example.org")

SURNAMES: tuple[str, ...] = (
    "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
    "徐", "孙", "马", "朱", "胡", "郭", "何", "林", "罗", "郑",
)
GIVEN_NAMES: tuple[str, ...] = (
    "伟", "芳", "娜", "敏", "静", "磊", "强", "洋", "艳", "勇",
    "军", "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英",
)

SUPPLIER_BRANDS: tuple[str, ...] = (
    "Northwind", "Globex", "Initech", "Umbrella", "Soylent", "Acme", "Wayne",
    "Stark", "Cyberdyne", "Tyrell", "Massive", "Vandelay", "Pied Piper",
)

REMARKS: tuple[str, ...] = (
    "客户要求尽快发货",
    "改地址：请以电话确认为准",
    "发票已单独邮寄",
    "包装破损，已补发",
    "VIP 客户，优先处理",
)

# --------------------------------------------------------------------------- #
# 造数工具
# --------------------------------------------------------------------------- #
def _weighted_choice(rnd: random.Random, pairs: tuple[tuple[str, float], ...]) -> str:
    """按权重随机取值（用于制造真实业务的偏斜分布）。"""
    values = [value for value, _ in pairs]
    weights = [weight for _, weight in pairs]
    return rnd.choices(values, weights=weights, k=1)[0]


def _batched_executemany(conn: sqlite3.Connection, sql: str, rows, batch_size: int = 5_000) -> int:
    """分批 executemany，避免把几十万行一次性塞进内存。返回写入行数。"""
    buffer: list[tuple] = []
    total = 0
    for row in rows:
        buffer.append(row)
        if len(buffer) >= batch_size:
            conn.executemany(sql, buffer)
            total += len(buffer)
            buffer.clear()
    if buffer:
        conn.executemany(sql, buffer)
        total += len(buffer)
    return total


def _random_date(rnd: random.Random, days_back: int, today: date | None = None) -> str:
    """返回 today 之前 days_back 天内均匀分布的日期字符串 'YYYY-MM-DD'。"""
    today = today or date.today()
    return (today - timedelta(days=rnd.randint(0, days_back))).isoformat()


def _skewed_order_date(rnd: random.Random, today: date) -> str:
    """订单日期刻意偏斜：60% 落在近半年，便于演示「时间范围 + 高选择性」查询。"""
    roll = rnd.random()
    if roll < 0.60:
        offset = rnd.randint(0, 180)
    elif roll < 0.85:
        offset = rnd.randint(181, 540)
    else:
        offset = rnd.randint(541, 1080)
    return (today - timedelta(days=offset)).isoformat()


def _gen_suppliers(rnd: random.Random, count: int):
    countries = tuple(COUNTRIES_CITIES)
    for sid in range(1, count + 1):
        yield (
            sid,
            f"{rnd.choice(SUPPLIER_BRANDS)}-{sid:03d} 供应链",
            rnd.choice(countries),
            round(rnd.uniform(2.5, 5.0), 1),
        )


def _gen_customers(rnd: random.Random, count: int):
    countries = tuple(COUNTRIES_CITIES)
    for cid in range(1, count + 1):
        country = rnd.choice(countries)
        yield (
            cid,
            f"{rnd.choice(SURNAMES)}{rnd.choice(GIVEN_NAMES)}",
            f"user{cid:06d}@{rnd.choice(EMAIL_DOMAINS)}",
            rnd.choice(COUNTRIES_CITIES[country]),
            country,
            rnd.choices((0, 1, 2, 3), weights=(0.55, 0.25, 0.15, 0.05))[0],
            _random_date(rnd, days_back=1460),
        )


def _gen_products(rnd: random.Random, count: int, supplier_ids: list[int]):
    for pid in range(1, count + 1):
        category = rnd.choice(CATEGORIES)
        price = round(rnd.uniform(9.9, 8999.0), 2)
        yield (
            pid,
            f"SKU-{pid:06d}",
            f"{category}-{pid:05d}",
            category,
            price,
            rnd.randint(0, 2000),
            rnd.choice(supplier_ids),
        )


def _gen_orders(
    rnd: random.Random,
    count: int,
    product_pool: list[tuple[int, float]],
    customer_ids: list[int],
    today: date,
):
    quantities = (1, 2, 3, 5, 10)
    quantity_weights = (0.55, 0.22, 0.12, 0.08, 0.03)
    for oid in range(1, count + 1):
        product_id, unit_price = rnd.choice(product_pool)
        quantity = rnd.choices(quantities, weights=quantity_weights)[0]
        status = _weighted_choice(rnd, STATUS_WEIGHTS)
        order_date = _skewed_order_date(rnd, today)
        if status in ("shipped", "completed"):
            ship_date = (
                date.fromisoformat(order_date) + timedelta(days=rnd.randint(1, 5))
            ).isoformat()
        else:
            ship_date = None
        yield (
            oid,
            rnd.choice(customer_ids),
            product_id,
            quantity,
            unit_price,
            round(unit_price * quantity, 2),
            status,
            _weighted_choice(rnd, CHANNEL_WEIGHTS),
            order_date,
            ship_date,
            rnd.choice(REMARKS) if rnd.random() < 0.03 else None,
        )


# --------------------------------------------------------------------------- #
# 建库主流程
# --------------------------------------------------------------------------- #
def build_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    orders_count: int | None = None,
    seed: int = DEFAULT_SEED,
    overwrite: bool = False,
    verbose: bool = True,
) -> dict:
    """创建（或覆盖重建）演示库，返回统计信息字典。"""
    db_path = Path(db_path)
    if db_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"{db_path} 已存在。如需重建请在命令行加 --force，或调用 build_database(..., overwrite=True)"
            )
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    rows = dict(TABLE_ROWS)
    if orders_count is not None:
        rows["orders"] = int(orders_count)

    rnd = random.Random(seed)
    today = date.today()
    started = time.perf_counter()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA synchronous = OFF")
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)

        supplier_ids = list(range(1, rows["suppliers"] + 1))
        customer_ids = list(range(1, rows["customers"] + 1))
        # orders 需要按商品真实价格取值，因此 products 先物化（仅 2000 行，内存无忧）
        products = list(_gen_products(rnd, rows["products"], supplier_ids))
        product_pool = [(pid, price) for pid, _, _, _, price, _, _ in products]

        _batched_executemany(
            conn,
            "INSERT INTO suppliers VALUES (?, ?, ?, ?)",
            _gen_suppliers(rnd, rows["suppliers"]),
        )
        _batched_executemany(
            conn,
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
            _gen_customers(rnd, rows["customers"]),
        )
        _batched_executemany(
            conn,
            "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
            products,
        )
        _batched_executemany(
            conn,
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _gen_orders(rnd, rows["orders"], product_pool, customer_ids, today),
        )
        conn.commit()
        # 生成 sqlite_stat1：让 EXPLAIN QUERY PLAN 更贴近真实执行，优化建议才有意义
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    stats: dict = {
        "db_path": str(db_path),
        "size_mb": round(db_path.stat().st_size / (1024 * 1024), 2),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "seed": seed,
        "rows": {},
        "indexes": {},
    }
    with connect_ro(db_path) as ro:
        for table in rows:
            stats["rows"][table] = ro.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        stats["indexes"] = index_map(ro)
    stats["total_rows"] = sum(stats["rows"].values())

    if verbose:
        print(f"[OK] 演示库已生成：{stats['db_path']}")
        print(
            f"[OK] 文件 {stats['size_mb']} MB，总计 {stats['total_rows']:,} 行，"
            f"耗时 {stats['elapsed_s']} s（随机种子 {seed}）"
        )
        for table, count in stats["rows"].items():
            print(f"     - {table:<10} {count:>8,} 行")

    return stats


# --------------------------------------------------------------------------- #
# 数据访问层（app.py 复用本节函数）
# --------------------------------------------------------------------------- #
def read_only_uri(db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """构造 SQLite 只读 URI（对空格/中文路径做 URL 编码，Windows 已实测可用）。"""
    return "file:" + quote(Path(db_path).resolve().as_posix(), safe="/:+-._") + "?mode=ro"


@contextmanager
def connect_ro(db_path: Path | str = DEFAULT_DB_PATH):
    """只读连接：URI `mode=ro` 阻止写入，再加 `PRAGMA query_only` 双保险。"""
    conn = sqlite3.connect(read_only_uri(db_path), uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_rw(db_path: Path | str = DEFAULT_DB_PATH):
    """可写连接：只用于 DDL（CREATE / DROP INDEX），不做数据变更。"""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in table_names(conn)
    }


def column_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """{表名: [列名, ...]}，用于校验（包括校验 LLM 给出的索引列是否真实存在）。"""
    return {
        table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        for table in table_names(conn)
    }


def _index_columns(conn: sqlite3.Connection, index_name: str) -> list[str]:
    escaped = index_name.replace("'", "''")
    return [row[2] for row in conn.execute(f"PRAGMA index_info('{escaped}')") if row[2]]


def index_map(conn: sqlite3.Connection, table: str | None = None) -> dict[str, list[dict]]:
    """返回 {表名: [索引信息...]}；索引信息含 name / unique / origin / columns。"""
    result: dict[str, list[dict]] = {}
    for name in ([table] if table else table_names(conn)):
        items: list[dict] = []
        for row in conn.execute(f'PRAGMA index_list("{name}")'):
            index_name, unique, origin = row[1], bool(row[2]), row[3]
            items.append(
                {
                    "table": name,
                    "name": index_name,
                    "unique": unique,
                    "origin": origin,  # c=CREATE INDEX, u=UNIQUE 约束, pk=主键
                    "columns": _index_columns(conn, index_name),
                }
            )
        result[name] = items
    return result


def describe_schema(conn: sqlite3.Connection) -> str:
    """生成给 LLM 的 schema 文本：建表 DDL + 行数 + 现有索引（含「无索引」提示）。"""
    blocks: list[str] = []
    for table in table_names(conn):
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        lines = [f"### 表 {table}（{count:,} 行）", (ddl_row[0] or "").strip() + ";"]
        indexes = index_map(conn, table)[table]
        if indexes:
            lines.append("现有索引：")
            for item in indexes:
                kind = "UNIQUE " if item["unique"] else ""
                columns = ", ".join(item["columns"]) or "rowid"
                lines.append(f"  - {kind}{item['name']} ({columns})")
        else:
            lines.append(f"现有索引：无 —— 该表只能全表扫描（SCAN {table}）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 / 查看「AI SQL 优化助手」的 SQLite 演示库")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="数据库文件路径")
    parser.add_argument("--orders", type=int, default=None, help="orders 表行数（默认 50000）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（保证数据可复现）")
    parser.add_argument("--force", action="store_true", help="已存在时覆盖重建")
    parser.add_argument("--print-schema", action="store_true", help="只打印 schema 文本后退出")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if args.print_schema:
        if not db_path.exists():
            print(f"[ERR] 数据库不存在：{db_path}，请先执行 python db_setup.py", file=sys.stderr)
            return 1
        with connect_ro(db_path) as conn:
            print(describe_schema(conn))
        return 0

    try:
        build_database(db_path, args.orders, args.seed, args.force)
    except FileExistsError as exc:
        print(f"[SKIP] {exc}", file=sys.stderr)
        return 1

    print("\n下一步：python -m streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
