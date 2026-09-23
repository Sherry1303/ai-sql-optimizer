#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI SQL 优化助手 —— Streamlit 前端（SQLite + DeepSeek）。

工作流程：
    1. 用户在页面里写一条 SQL，工具用「只读连接」执行，并取回 EXPLAIN QUERY PLAN 与实测耗时；
    2. 把「表结构 + 现有索引 + 查询计划 + 实测耗时」作为上下文交给 DeepSeek；
    3. DeepSeek 返回结构化 JSON 诊断（性能评分 / 问题清单 / 优化后 SQL / 索引建议）；
    4. 索引建议经严格校验后可一键落地到 SQLite，再点「对比耗时」查看真实收益。

安全边界（重要）：
    * 执行 SQL 使用 `file:...?mode=ro` 只读连接 + `PRAGMA query_only`，
      并且只放行「单条 SELECT / WITH」语句，写操作在语句层与连接层被双重拒绝；
    * LLM 返回的 CREATE INDEX 不会被直接执行：先解析并校验表名/列名真实存在，
      校验失败则改用 LLM 给出的结构化字段（table/columns）重新拼装，表达式索引一律拒绝。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import openai
import pandas as pd
import streamlit as st

import db_setup
from db_setup import (
    DEFAULT_DB_PATH,
    column_map,
    connect_ro,
    connect_rw,
    describe_schema,
    index_map,
    table_row_counts,
)

try:  # python-dotenv 为可选依赖：装了就能自动读取 .env 中的 DeepSeek 配置
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

# --------------------------------------------------------------------------- #
# 常量与示例
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
ENV_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

FETCH_LIMIT = 20_000  # 单次执行最多取回的行数，防止 SELECT * 打爆内存
PREVIEW_ROWS = 200  # 结果表格最多展示的行数
PROMPT_SAMPLE_ROWS = 5  # 送给 LLM 的样例数据行数

DEFAULT_SQL = """SELECT o.id, o.order_date, o.total_amount, o.status
FROM orders o
WHERE o.customer_id = 1234
  AND o.status = 'completed'
ORDER BY o.order_date DESC
LIMIT 20;"""

#: 示例 SQL —— 全部是「写得没错但没利用索引」的真实场景，方便演示优化收益
SAMPLE_QUERIES: dict[str, str] = {
    "① 单表过滤 + 排序（缺索引 -> 全表扫描 + 临时排序）": DEFAULT_SQL,
    "② 关联聚合 + 时间范围（缺索引 -> 全表扫描外表）": """SELECT c.country AS country,
       COUNT(*) AS order_cnt,
       ROUND(SUM(o.total_amount), 2) AS sales
FROM orders o
JOIN customers c ON c.id = o.customer_id
WHERE o.order_date BETWEEN date('now', '-90 day') AND date('now')
GROUP BY c.country
ORDER BY sales DESC;""",
    "③ 商品类目聚合（category 无索引）": """SELECT category, COUNT(*) AS cnt, ROUND(AVG(price), 2) AS avg_price
FROM products
WHERE stock > 100
  AND price > 500
GROUP BY category
ORDER BY avg_price DESC;""",
    "④ 列被函数包裹（经典索引失效写法）": """SELECT COUNT(*) AS cnt, ROUND(SUM(total_amount), 2) AS amount
FROM orders
WHERE strftime('%Y-%m', order_date) = strftime('%Y-%m', 'now')
  AND total_amount > 1000;""",
    "⑤ 全表分组统计（对比加索引前后的极限场景）": """SELECT status, channel, COUNT(*) AS cnt
FROM orders
GROUP BY status, channel
ORDER BY cnt DESC;""",
}

COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE|TRIGGER)\b",
    re.IGNORECASE,
)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
CREATE_INDEX_RE = re.compile(
    r"^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+ON\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"\(\s*(?P<cols>[^()]+?)\s*\)$",
    re.IGNORECASE | re.DOTALL,
)

# --------------------------------------------------------------------------- #
# SQL 安全校验与执行（只读）
# --------------------------------------------------------------------------- #
def _strip_comments(sql: str) -> str:
    return COMMENT_RE.sub(" ", sql or "")


def _strip_literals(sql: str) -> str:
    return STRING_LITERAL_RE.sub("''", sql or "")


def validate_readonly_sql(sql: str) -> tuple[bool, str]:
    """只放行单条 SELECT / WITH 查询。返回 (是否通过, 失败原因)。"""
    text = _strip_comments(sql).strip()
    if not text:
        return False, "SQL 为空，请先输入查询语句。"
    if len(text) > 20_000:
        return False, "SQL 过长（超过 20000 字符），已拒绝执行。"

    body = text.rstrip().rstrip(";").strip()
    plain = _strip_literals(body)
    if ";" in plain:
        return False, "只允许执行单条 SQL 语句（检测到分号分隔的多条语句）。"

    match = re.match(r"^([A-Za-z]+)", plain)
    keyword = match.group(1).upper() if match else ""
    if keyword not in ("SELECT", "WITH"):
        return (
            False,
            f"本工具只允许执行只读查询（SELECT / WITH），当前语句以 `{keyword or '未知'} ` 开头。",
        )
    if keyword == "WITH" and WRITE_KEYWORDS.search(plain):
        return False, "WITH 语句中检测到写操作关键字，已拒绝执行。"
    return True, ""


def run_query(sql: str, db_path: Path) -> dict:
    """执行查询，返回结果 DataFrame、实测耗时与截断标记（连接为只读）。"""
    started = time.perf_counter()
    with connect_ro(db_path) as conn:
        cursor = conn.execute(sql)
        columns = [item[0] for item in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(FETCH_LIMIT + 1)
    elapsed = time.perf_counter() - started

    truncated = len(rows) > FETCH_LIMIT
    rows = rows[:FETCH_LIMIT]
    return {
        "sql": sql,
        "dataframe": pd.DataFrame(rows, columns=columns or None),
        "columns": columns,
        "elapsed_s": elapsed,
        "row_count": len(rows),
        "truncated": truncated,
        "at": datetime.now().strftime("%H:%M:%S"),
    }


def explain_query(sql: str, db_path: Path) -> list[dict]:
    """取回 EXPLAIN QUERY PLAN（不真正执行查询）。"""
    statement = "EXPLAIN QUERY PLAN " + sql.strip().rstrip(";")
    with connect_ro(db_path) as conn:
        rows = conn.execute(statement).fetchall()
    return [{"id": r[0], "parent": r[1], "detail": r[3]} for r in rows]


def plan_warnings(plan: list[dict]) -> list[str]:
    """从查询计划里提取「全表扫描 / 临时 B 树 / 子查询扫描」等性能信号。"""
    warnings: list[str] = []
    for row in plan:
        detail = str(row["detail"])
        upper = detail.upper()
        if upper.startswith("SCAN") and "USING" not in upper:
            warnings.append(f"全表扫描 → {detail}")
        elif "USE TEMP B-TREE" in upper:
            warnings.append(f"临时 B 树（排序/分组未利用索引）→ {detail}")
        elif "SUBQUERY" in upper and "SCAN" in upper:
            warnings.append(f"子查询全表扫描 → {detail}")
    return warnings


def warm_cache(sql: str, db_path: Path, repeat: int = 1) -> None:
    """预热页缓存：先跑一遍，让「对比耗时」的基准更稳定（避免首跑冷缓存失真）。"""
    for _ in range(max(1, repeat)):
        with connect_ro(db_path) as conn:
            cursor = conn.execute(sql)
            cursor.fetchmany(FETCH_LIMIT)


# --------------------------------------------------------------------------- #
# 索引建议：校验与落地
# --------------------------------------------------------------------------- #
def default_index_name(table: str, columns: list[str], unique: bool = False) -> str:
    prefix = "uidx" if unique else "idx"
    return f"{prefix}_{table}_{'_'.join(columns)}"[:63]


def build_index_sql(
    table: str, columns: list[str], name: str | None = None, unique: bool = False
) -> str:
    index_name = name or default_index_name(table, columns, unique)
    cols = ", ".join(f'"{column}"' for column in columns)
    return (
        f'CREATE {"UNIQUE " if unique else ""}INDEX IF NOT EXISTS '
        f'"{index_name}" ON "{table}" ({cols});'
    )


def validate_index_spec(
    table: str, columns: list[str], name: str, available_columns: dict[str, list[str]]
) -> tuple[bool, str]:
    """校验索引规格：表/列必须真实存在，标识符必须合法（防注入）。"""
    if table not in available_columns:
        return False, f"表 `{table}` 在库中不存在。"
    if not columns:
        return False, "索引列不能为空。"
    for column in columns:
        if not IDENT_RE.match(column):
            return False, f"列名 `{column}` 不符合标识符规范。"
        if column not in available_columns[table]:
            return False, f"列 `{table}.{column}` 在库中不存在。"
    if name and not IDENT_RE.match(name):
        return False, f"索引名 `{name}` 不符合标识符规范。"
    return True, ""


def apply_index(
    table: str,
    columns: list[str],
    name: str,
    db_path: Path,
    unique: bool = False,
) -> tuple[bool, str]:
    """校验并创建索引（成功后刷新统计信息）。返回 (是否成功, SQL 或错误信息)。"""
    with connect_ro(db_path) as conn:
        available_columns = column_map(conn)
    ok, message = validate_index_spec(table, columns, name, available_columns)
    if not ok:
        return False, message

    sql = build_index_sql(table, columns, name, unique)
    try:
        with connect_rw(db_path) as conn:
            conn.execute(sql)
            conn.execute("ANALYZE")  # 刷新 sqlite_stat1，后续查询计划立刻反映新索引
            conn.commit()
    except sqlite3.Error as exc:
        return False, f"创建索引失败：{exc}"
    return True, sql


def drop_index(name: str, db_path: Path) -> tuple[bool, str]:
    if not IDENT_RE.match(name or ""):
        return False, f"索引名 `{name}` 不合法，已拒绝。"
    try:
        with connect_rw(db_path) as conn:
            conn.execute(f'DROP INDEX IF EXISTS "{name}"')
            conn.execute("ANALYZE")
            conn.commit()
    except sqlite3.Error as exc:
        return False, f"删除索引失败：{exc}"
    return True, name


def parse_create_index_sql(sql: str) -> dict | None:
    """解析 LLM 给出的 CREATE INDEX；只接受「单表 + 纯列名」形式。"""
    text = _strip_comments(sql).strip().rstrip(";").strip()
    match = CREATE_INDEX_RE.match(text)
    if not match:
        return None
    columns = [
        column.strip().strip('"').strip("`").strip("[]")
        for column in match.group("cols").split(",")
    ]
    if not columns or any(not IDENT_RE.match(column) for column in columns):
        return None  # 拒绝表达式索引、排序方向、COLLATE 等不可控写法
    return {
        "name": match.group("name"),
        "table": match.group("table"),
        "columns": columns,
        "unique": bool(match.group("unique")),
    }


# --------------------------------------------------------------------------- #
# DeepSeek 调用层
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是一位资深的 SQLite 数据库性能工程师，负责 SQL 评审与优化。

每次你会收到：SQLite 表结构（含行数与现有索引）、用户 SQL、EXPLAIN QUERY PLAN 结果、实测耗时。

必须遵守：
1. 只输出一个 JSON 对象，禁止输出解释性文字，禁止使用 Markdown 代码围栏。
2. 结论必须基于给定信息：表名、列名、索引名必须真实存在，禁止臆造。
3. 索引建议需考虑列选择性、联合索引的列顺序与前缀复用，并避免与现有索引重复。
4. optimized_sql 必须是可直接在 SQLite 执行的单条 SELECT / WITH 语句，不要写 DDL。
5. 若 SQL 已经足够好，issues 返回空数组，并在 summary 中说明原因。
6. severity 仅可取 high / medium / low；score 为 0-100 的整数（越高表示越高效）。

JSON 结构（键名必须完全一致）：
{
  "summary": "一句话总体结论（中文）",
  "score": 85,
  "issues": [
    {"severity": "high", "title": "问题标题", "detail": "为什么慢（结合行数/查询计划）", "evidence": "查询计划或统计信息中的证据"}
  ],
  "optimized_sql": "优化后的完整 SELECT 语句",
  "indexes": [
    {"table": "orders", "columns": ["customer_id", "order_date"], "unique": false,
     "name": "idx_orders_customer_id_order_date",
     "reason": "该索引为什么能让这条 SQL 走索引", "expected_gain": "预期收益（定性描述）",
     "sql": "CREATE INDEX IF NOT EXISTS idx_orders_customer_id_order_date ON orders(customer_id, order_date);"}
  ],
  "risks": ["新增索引带来的写入放大 / 磁盘占用等副作用"],
  "next_steps": ["可落地的后续动作"]
}"""


def make_client(api_key: str, base_url: str):
    """按 DeepSeek 的 OpenAI 兼容接口创建客户端。"""
    return openai.OpenAI(
        api_key=api_key,
        base_url=(base_url or DEFAULT_BASE_URL).rstrip("/"),
        timeout=180.0,
        max_retries=1,
    )


def build_messages(
    schema_text: str,
    sql: str,
    plan: list[dict],
    execution: dict | None,
    sample_rows: str = "",
) -> list[dict]:
    """拼装上下文：schema + SQL + 查询计划 + 实测观测（+ 结果样例）。"""
    lines = [
        "## 数据库表结构",
        schema_text,
        "",
        "## 待优化的 SQL",
        "```sql",
        sql.strip(),
        "```",
        "",
        "## EXPLAIN QUERY PLAN 结果",
    ]
    lines += [f"- {row['detail']}" for row in plan] or ["- （暂无计划信息）"]

    lines += ["", "## 本次执行的实测观测"]
    if execution:
        lines += [
            "- 是否执行成功：是",
            f"- 耗时：{execution['elapsed_s'] * 1000:.2f} ms",
            f"- 返回行数：{execution['row_count']:,}"
            + ("（结果被截断，仅取前若干行）" if execution.get("truncated") else ""),
        ]
    else:
        lines.append("- 用户尚未执行该 SQL，请基于表结构与查询计划做静态推断。")

    if sample_rows:
        lines += ["", "## 结果样例（最多 5 行，可用于判断数据分布）", "```", sample_rows, "```"]

    lines += ["", "请严格按要求输出 JSON。"]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def stream_completion(
    client,
    *,
    model: str,
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    force_json: bool,
    on_delta=None,
) -> dict:
    """流式调用 DeepSeek；on_delta(正文, 思维链) 用于节流刷新界面。"""
    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if force_json:
        kwargs["response_format"] = {"type": "json_object"}

    stream = client.chat.completions.create(**kwargs)
    content: list[str] = []
    reasoning: list[str] = []
    last_flush = 0.0
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue  # 末尾 usage 之类的分片没有 choices
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue
        piece = getattr(delta, "content", None)
        thought = getattr(delta, "reasoning_content", None)  # deepseek-reasoner 的思维链
        if piece:
            content.append(piece)
        if thought:
            reasoning.append(thought)
        now = time.perf_counter()
        if on_delta is not None and now - last_flush >= 0.25:
            last_flush = now
            on_delta("".join(content), "".join(reasoning))

    if on_delta is not None:
        on_delta("".join(content), "".join(reasoning))
    return {"content": "".join(content), "reasoning": "".join(reasoning)}


# --------------------------------------------------------------------------- #
# 诊断结果解析
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> dict | None:
    """尽力从模型返回中抽取 JSON 对象（兼容代码围栏与前后夹杂文字）。"""
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    candidates += [block.strip() for block in re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_columns(value) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_diagnosis(raw: dict, available_columns: dict[str, list[str]]) -> dict:
    """规范化 LLM 的 JSON，并对每条索引建议做「可执行性」安全校验。"""
    suggestions: list[dict] = []
    for item in _as_list(raw.get("indexes")):
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip()
        columns = _normalize_columns(item.get("columns"))
        name = str(item.get("name") or "").strip()
        unique = bool(item.get("unique"))
        raw_sql = str(item.get("sql") or "").strip()

        parsed = parse_create_index_sql(raw_sql)
        if (
            parsed
            and parsed["table"] in available_columns
            and all(col in available_columns[parsed["table"]] for col in parsed["columns"])
        ):
            table = parsed["table"]
            columns = parsed["columns"]
            name = parsed["name"]
            unique = parsed["unique"]
            executable, note = raw_sql if raw_sql.endswith(";") else raw_sql + ";", "已通过校验，直接采用 LLM 原始 DDL"
        else:
            ok, message = validate_index_spec(table, columns, name, available_columns)
            if ok:
                executable, note = build_index_sql(table, columns, name, unique), "已由结构化字段重建 DDL"
            else:
                executable, note = "", f"不可执行：{message}"

        suggestions.append(
            {
                "table": table,
                "columns": columns,
                "name": name or default_index_name(table, columns, unique),
                "unique": unique,
                "reason": str(item.get("reason") or ""),
                "expected_gain": str(item.get("expected_gain") or item.get("expectedGain") or ""),
                "raw_sql": raw_sql,
                "executable": executable,
                "note": note,
            }
        )

    issues: list[dict] = []
    for item in _as_list(raw.get("issues")):
        if isinstance(item, dict):
            issues.append(
                {
                    "severity": str(item.get("severity") or "medium").strip().lower(),
                    "title": str(item.get("title") or "未命名问题"),
                    "detail": str(item.get("detail") or ""),
                    "evidence": str(item.get("evidence") or ""),
                }
            )
        elif isinstance(item, str) and item.strip():
            issues.append({"severity": "medium", "title": item.strip()[:60], "detail": item.strip(), "evidence": ""})

    score = raw.get("score")
    try:
        score = max(0, min(100, int(float(score))))
    except (TypeError, ValueError):
        score = None

    return {
        "summary": str(raw.get("summary") or "").strip(),
        "score": score,
        "issues": issues,
        "optimized_sql": str(raw.get("optimized_sql") or "").strip(),
        "indexes": suggestions,
        "risks": [str(item) for item in _as_list(raw.get("risks"))],
        "next_steps": [str(item) for item in _as_list(raw.get("next_steps"))],
    }


# --------------------------------------------------------------------------- #
# 数据访问缓存（改索引后 version 自增即可失效）
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def cached_schema_info(db_path_str: str, version: int) -> dict:
    """缓存 schema 文本 / 行数 / 索引 / 列映射，避免每次交互都重新扫描 sqlite_master。"""
    with connect_ro(db_path_str) as conn:
        return {
            "text": describe_schema(conn),
            "rows": table_row_counts(conn),
            "indexes": index_map(conn),
            "columns": column_map(conn),
        }


# --------------------------------------------------------------------------- #
# 会话状态与回调（在回调里改 widget 值才不会触发 StreamlitAPIException）
# --------------------------------------------------------------------------- #
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
INDEX_SOURCE_LABEL = {"c": "CREATE INDEX", "u": "UNIQUE 约束", "pk": "主键"}


def init_session_state() -> None:
    st.session_state.setdefault("sql_editor", DEFAULT_SQL)
    st.session_state.setdefault("schema_version", 1)  # 自增即让 schema 缓存失效
    st.session_state.setdefault("last_run", None)
    st.session_state.setdefault("baseline", None)
    st.session_state.setdefault("comparison", None)
    st.session_state.setdefault("diagnosis", None)
    st.session_state.setdefault("history", [])
    st.session_state.setdefault("flash", None)


def use_sql(sql: str) -> None:
    """回调：把一段 SQL 载入编辑器。"""
    st.session_state["sql_editor"] = sql


def clear_diagnosis() -> None:
    st.session_state["diagnosis"] = None
    st.session_state["comparison"] = None


def create_index_callback(spec: dict, db_path_str: str) -> None:
    """回调：落地一条 AI 索引建议。"""
    if not spec.get("executable"):
        st.session_state["flash"] = ("error", spec.get("note") or "该建议缺少可执行的 DDL。")
        return
    ok, message = apply_index(
        spec["table"], spec["columns"], spec["name"], Path(db_path_str), bool(spec.get("unique"))
    )
    if ok:
        st.session_state["schema_version"] += 1
        st.session_state["flash"] = ("success", f"索引已创建：{spec['name']}（可点「🔁 对比耗时」验证真实收益）")
    else:
        st.session_state["flash"] = ("error", message)


def drop_index_callback(name: str, db_path_str: str) -> None:
    ok, message = drop_index(name, Path(db_path_str))
    if ok:
        st.session_state["schema_version"] += 1
        st.session_state["flash"] = ("success", f"索引已删除：{name}")
    else:
        st.session_state["flash"] = ("error", message)


def show_flash() -> None:
    flash = st.session_state.pop("flash", None)
    if not flash:
        return
    kind, message = flash
    if kind == "success":
        st.success(message)
    else:
        st.error(message)


# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
def render_sidebar(schema_info: dict, db_path: Path) -> dict:
    st.sidebar.header("🔑 DeepSeek 配置")
    api_key = st.sidebar.text_input(
        "API Key",
        value=ENV_API_KEY,
        type="password",
        help="也可写入项目根目录 .env 的 DEEPSEEK_API_KEY，页面会自动读取。",
    )
    base_url = st.sidebar.text_input("Base URL", value=DEFAULT_BASE_URL)
    model = st.sidebar.selectbox(
        "模型",
        ["deepseek-chat", "deepseek-reasoner"],
        index=0,
        help="deepseek-chat 速度快、支持 JSON 模式；deepseek-reasoner 会返回思维链但更慢。",
    )
    default_json = model == "deepseek-chat"
    force_json = st.sidebar.checkbox(
        "强制 JSON 输出",
        value=default_json,
        help="deepseek-chat 支持 response_format=json_object；reasoner 建议关闭。",
    )
    stream_preview = st.sidebar.checkbox("流式显示原始返回", value=True)
    temperature = st.sidebar.slider("温度", 0.0, 1.5, 0.2, 0.1)
    max_tokens = st.sidebar.slider("最大输出 tokens", 512, 8192, 4096, 512)

    st.sidebar.divider()
    st.sidebar.subheader("🗄️ 数据库")
    st.sidebar.caption(f"`{db_path}`")
    rows = schema_info["rows"]
    st.sidebar.metric("总行数", f"{sum(rows.values()):,}")
    st.sidebar.dataframe(
        pd.DataFrame({"表": list(rows), "行数": list(rows.values())}),
        hide_index=True,
        width="stretch",
    )
    index_count = sum(len(items) for items in schema_info["indexes"].values())
    st.sidebar.caption(f"索引数量：{index_count}（含主键 / UNIQUE 自动索引）")

    st.sidebar.divider()
    if st.sidebar.button("🧹 清空历史记录", width="stretch"):
        st.session_state["history"] = []
        st.rerun()

    return {
        "api_key": api_key.strip(),
        "base_url": base_url.strip(),
        "model": model,
        "force_json": force_json,
        "stream_preview": stream_preview,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


# --------------------------------------------------------------------------- #
# Tab 1：查询与诊断
# --------------------------------------------------------------------------- #
def _measure(sql: str, db_path: Path) -> tuple[dict, list[dict]]:
    """先预热一次再计时（排除冷缓存干扰），返回 (执行结果, 查询计划)。"""
    warm_cache(sql, db_path, repeat=1)
    return run_query(sql, db_path), explain_query(sql, db_path)


def handle_run(sql: str, db_path: Path) -> None:
    ok, message = validate_readonly_sql(sql)
    if not ok:
        st.session_state["flash"] = ("error", message)
        return
    try:
        result, plan = _measure(sql, db_path)
    except sqlite3.Error as exc:
        st.session_state["flash"] = ("error", f"SQL 执行失败：{exc}")
        return

    st.session_state["last_run"] = {**result, "plan": plan, "warnings": plan_warnings(plan)}
    baseline = st.session_state.get("baseline")
    if not baseline or baseline["sql"] != sql:
        st.session_state["baseline"] = {
            "sql": sql,
            "elapsed_s": result["elapsed_s"],
            "row_count": result["row_count"],
            "plan": plan,
            "at": result["at"],
        }
        st.session_state["comparison"] = None
    st.session_state["flash"] = (
        "success",
        f"执行完成：{result['elapsed_s'] * 1000:.2f} ms，返回 {result['row_count']:,} 行。",
    )
    st.rerun()


def handle_compare(sql: str, db_path: Path) -> None:
    baseline = st.session_state.get("baseline")
    if not baseline:
        st.session_state["flash"] = ("error", "请先点「▶️ 执行」建立基准，再建索引后回来对比。")
        return
    if baseline["sql"] != sql:
        st.session_state["flash"] = ("error", "SQL 已被修改，请重新点「▶️ 执行」建立新基准。")
        return
    try:
        result, plan = _measure(sql, db_path)
    except sqlite3.Error as exc:
        st.session_state["flash"] = ("error", f"SQL 执行失败：{exc}")
        return

    st.session_state["last_run"] = {**result, "plan": plan, "warnings": plan_warnings(plan)}
    st.session_state["comparison"] = {
        "baseline": baseline,
        "after": {
            "elapsed_s": result["elapsed_s"],
            "row_count": result["row_count"],
            "plan": plan,
            "at": result["at"],
        },
    }
    st.session_state["flash"] = ("success", "已重新执行，见下方「优化前后对比」。")
    st.rerun()


def render_comparison(comparison: dict) -> None:
    before, after = comparison["baseline"], comparison["after"]
    before_ms = before["elapsed_s"] * 1000
    after_ms = after["elapsed_s"] * 1000
    delta_pct = ((after_ms - before_ms) / before_ms * 100) if before_ms else 0.0

    st.subheader("⚡ 优化前后对比")
    c1, c2, c3 = st.columns(3)
    c1.metric("基准耗时", f"{before_ms:,.2f} ms")
    c2.metric("当前耗时", f"{after_ms:,.2f} ms", delta=f"{delta_pct:+.1f}%", delta_color="inverse")
    c3.metric("返回行数", f"{after['row_count']:,}", delta=f"{after['row_count'] - before['row_count']:+,}")

    before_plan = "\n".join(row["detail"] for row in before["plan"]) or "（无）"
    after_plan = "\n".join(row["detail"] for row in after["plan"]) or "（无）"
    if before_plan != after_plan:
        p1, p2 = st.columns(2)
        p1.markdown("**基准查询计划**")
        p1.code(before_plan, language="text")
        p2.markdown("**当前查询计划**")
        p2.code(after_plan, language="text")

    if delta_pct < -5:
        st.success(f"索引生效：耗时下降 {abs(delta_pct):.1f}%。")
    elif delta_pct > 5:
        st.warning(f"耗时反而上升 {delta_pct:.1f}%：可能索引选择性不足或数据量太小，可考虑在「索引管理」里删掉它。")
    else:
        st.info("耗时变化不明显（数据量小或缓存波动），建议重点看查询计划是否从 SCAN 变成 SEARCH。")


def render_result(last_run: dict) -> None:
    st.subheader("📊 执行结果")
    m1, m2, m3 = st.columns(3)
    m1.metric("耗时", f"{last_run['elapsed_s'] * 1000:,.2f} ms")
    m2.metric("返回行数", f"{last_run['row_count']:,}" + ("（已截断）" if last_run["truncated"] else ""))
    m3.metric("执行时刻", last_run["at"])

    st.dataframe(last_run["dataframe"].head(PREVIEW_ROWS), width="stretch", height=300)
    if last_run["row_count"] > PREVIEW_ROWS:
        st.caption(f"表格只展示前 {PREVIEW_ROWS} 行（本次共 {last_run['row_count']:,} 行）。")

    with st.expander(f"🗂️ EXPLAIN QUERY PLAN（{len(last_run['plan'])} 个节点）", expanded=True):
        st.dataframe(pd.DataFrame(last_run["plan"]), hide_index=True, width="stretch")

    warnings = last_run.get("warnings") or []
    if warnings:
        st.warning(
            "查询计划里出现以下性能信号，建议交给 DeepSeek 分析：\n\n"
            + "\n".join(f"- {item}" for item in warnings)
        )
    else:
        st.success("查询计划中没有出现全表扫描 / 临时排序信号。")


def render_diagnosis(diagnosis: dict, db_path: Path) -> None:
    st.subheader("🩺 DeepSeek 诊断")
    normalized = diagnosis.get("normalized")
    if not normalized:
        st.error("未能从模型返回中解析出约定的 JSON，下面保留原始返回供人工查看。")
        st.code(diagnosis.get("raw") or "", language="text")
        return

    if normalized["score"] is not None:
        st.progress(normalized["score"] / 100, text=f"性能评分：{normalized['score']} / 100（越高越好）")
    if normalized["summary"]:
        st.info(normalized["summary"])

    st.markdown("#### 问题清单")
    if normalized["issues"]:
        for issue in normalized["issues"]:
            icon = SEVERITY_ICON.get(issue["severity"], "🟠")
            with st.container(border=True):
                st.markdown(f"{icon} **{issue['title']}**　`severity = {issue['severity']}`")
                if issue["detail"]:
                    st.markdown(issue["detail"])
                if issue["evidence"]:
                    st.caption(f"证据：{issue['evidence']}")
    else:
        st.success("DeepSeek 未发现明显问题。")

    optimized = normalized["optimized_sql"]
    if optimized:
        st.markdown("#### 优化后的 SQL")
        st.code(optimized, language="sql")
        st.button("📥 载入到编辑器", key="load_optimized_sql", on_click=use_sql, args=(optimized,))

    if normalized["indexes"]:
        st.markdown("#### 索引建议（已做安全校验，可一键落地）")
        for position, spec in enumerate(normalized["indexes"]):
            with st.container(border=True):
                columns = ", ".join(spec["columns"]) or "n/a"
                suffix = " · UNIQUE" if spec["unique"] else ""
                st.markdown(f"**{spec['name']}** → `{spec['table']} ({columns})`{suffix}")
                if spec["reason"]:
                    st.markdown(f"理由：{spec['reason']}")
                if spec["expected_gain"]:
                    st.caption(f"预期收益：{spec['expected_gain']}")
                st.code(spec["executable"] or f"-- 不可执行：{spec['note']}", language="sql")
                if spec["executable"]:
                    st.button(
                        "🏗️ 创建该索引",
                        key=f"create_index_{position}",
                        type="primary",
                        on_click=create_index_callback,
                        args=(spec, str(db_path)),
                    )
                else:
                    st.warning(spec["note"])

    if normalized["risks"]:
        with st.expander("⚠️ 风险与副作用"):
            for item in normalized["risks"]:
                st.markdown(f"- {item}")
    if normalized["next_steps"]:
        with st.expander("✅ 后续动作", expanded=True):
            for item in normalized["next_steps"]:
                st.markdown(f"- {item}")

    if diagnosis.get("reasoning"):
        with st.expander("🧠 模型思维链（reasoning_content）"):
            st.markdown(diagnosis["reasoning"])
    with st.expander("🔍 原始返回 JSON"):
        st.code(diagnosis.get("raw") or "", language="json")

    st.caption(
        f"诊断时间 {diagnosis.get('at')} · 模型 {diagnosis.get('model')} · "
        f"耗时 {diagnosis.get('duration_s', 0):.1f}s · 上下文 {diagnosis.get('context_chars', 0):,} 字符"
    )


def handle_analyze(sql: str, db_path: Path, schema_info: dict, settings: dict) -> None:
    if not settings["api_key"]:
        st.error("请先在侧边栏填写 DeepSeek API Key（或写入项目根目录 .env 的 DEEPSEEK_API_KEY）。")
        return
    ok, message = validate_readonly_sql(sql)
    if not ok:
        st.error(message)
        return

    last_run = st.session_state.get("last_run")
    execution = last_run if last_run and last_run["sql"] == sql else None
    try:
        plan = explain_query(sql, db_path)
    except sqlite3.Error as exc:
        st.error(f"无法生成查询计划：{exc}")
        return

    sample_rows = (
        execution["dataframe"].head(PROMPT_SAMPLE_ROWS).to_string(index=False) if execution else ""
    )
    messages = build_messages(schema_info["text"], sql, plan, execution, sample_rows)
    context_chars = sum(len(item["content"]) for item in messages)

    status = st.status("DeepSeek 正在分析…（展开可看流式原文）", expanded=settings["stream_preview"])
    preview = status.empty()

    def on_delta(content: str, reasoning: str) -> None:
        if settings["stream_preview"]:
            preview.code(content or reasoning or "…", language="json")

    started = time.perf_counter()
    try:
        client = make_client(settings["api_key"], settings["base_url"])
        reply = stream_completion(
            client,
            model=settings["model"],
            messages=messages,
            temperature=settings["temperature"],
            max_tokens=settings["max_tokens"],
            force_json=settings["force_json"],
            on_delta=on_delta,
        )
    except openai.AuthenticationError:
        status.update(label="认证失败（401）", state="error")
        st.error("API Key 无效，请检查侧边栏或 .env 中的 DEEPSEEK_API_KEY。")
        return
    except openai.RateLimitError:
        status.update(label="触发限流（429）", state="error")
        st.error("请求过于频繁或账户额度不足，请稍后重试。")
        return
    except openai.APIConnectionError as exc:
        status.update(label="网络异常", state="error")
        st.error(f"无法连接 DeepSeek：{exc}（请检查网络与 Base URL 设置）")
        return
    except openai.APIError as exc:
        status.update(label="调用失败", state="error")
        st.error(f"DeepSeek 返回错误：{exc}")
        return
    except Exception as exc:  # 兜底：避免任何一个异常把整页打挂
        status.update(label="调用失败", state="error")
        st.error(f"调用过程中出现未预期错误：{type(exc).__name__}: {exc}")
        return

    duration = time.perf_counter() - started
    raw = reply["content"]
    parsed = extract_json(raw)
    normalized = normalize_diagnosis(parsed, schema_info["columns"]) if parsed else None
    status.update(
        label="分析完成" if normalized else "返回内容无法解析为约定的 JSON",
        state="complete" if normalized else "error",
    )

    st.session_state["diagnosis"] = {
        "sql": sql,
        "raw": raw,
        "normalized": normalized,
        "reasoning": reply["reasoning"],
        "model": settings["model"],
        "at": datetime.now().strftime("%H:%M:%S"),
        "duration_s": duration,
        "context_chars": context_chars,
    }
    st.session_state["history"].insert(
        0,
        {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sql": sql,
            "model": settings["model"],
            "elapsed_ms": execution["elapsed_s"] * 1000 if execution else None,
            "score": normalized["score"] if normalized else None,
            "summary": normalized["summary"] if normalized else "",
            "indexes": [spec["name"] for spec in normalized["indexes"]] if normalized else [],
            "raw": raw,
        },
    )


def render_query_tab(db_path: Path, schema_info: dict, settings: dict) -> None:
    sample_col, load_col = st.columns([3, 1])
    sample_label = sample_col.selectbox("示例 SQL", list(SAMPLE_QUERIES), label_visibility="collapsed")
    load_col.button(
        "📋 载入示例",
        width="stretch",
        on_click=use_sql,
        args=(SAMPLE_QUERIES[sample_label],),
    )

    st.text_area(
        "SQL 编辑器（只允许单条 SELECT / WITH，数据库以只读方式打开）",
        key="sql_editor",
        height=200,
    )

    action_cols = st.columns(4)
    run_clicked = action_cols[0].button("▶️ 执行", type="primary", width="stretch")
    analyze_clicked = action_cols[1].button("🧠 DeepSeek 分析", width="stretch")
    compare_clicked = action_cols[2].button("🔁 对比耗时", width="stretch")
    action_cols[3].button("🧹 清空诊断", width="stretch", on_click=clear_diagnosis)

    sql = st.session_state.get("sql_editor", "")
    if run_clicked:
        handle_run(sql, db_path)
    if compare_clicked:
        handle_compare(sql, db_path)
    if analyze_clicked:
        handle_analyze(sql, db_path, schema_info, settings)

    if st.session_state.get("comparison"):
        st.divider()
        render_comparison(st.session_state["comparison"])
    if st.session_state.get("last_run"):
        st.divider()
        render_result(st.session_state["last_run"])
    if st.session_state.get("diagnosis"):
        st.divider()
        render_diagnosis(st.session_state["diagnosis"], db_path)


def render_schema_tab(schema_info: dict) -> None:
    st.subheader("🧩 表结构与索引（会作为上下文送给 DeepSeek）")
    rows = schema_info["rows"]
    columns_map = schema_info["columns"]
    st.dataframe(
        pd.DataFrame(
            [
                {"表名": table, "行数": rows[table], "列数": len(columns_map[table])}
                for table in rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    for table in rows:
        indexes = schema_info["indexes"].get(table, [])
        with st.expander(f"表 {table}（{rows[table]:,} 行 · {len(indexes)} 个索引）"):
            indexed_columns = {column for item in indexes for column in item["columns"]}
            st.markdown("**列（按表定义顺序）**")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"列名": column, "已被索引覆盖": column in indexed_columns}
                        for column in columns_map[table]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.markdown("**索引**")
            if indexes:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "索引名": item["name"],
                                "列": ", ".join(item["columns"]) or "rowid",
                                "唯一": item["unique"],
                                "来源": INDEX_SOURCE_LABEL.get(item["origin"], item["origin"]),
                            }
                            for item in indexes
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.warning("该表没有任何非主键索引：任何过滤、排序都只能走全表扫描。")

    with st.expander("📄 送给 DeepSeek 的 schema 文本（原文）"):
        st.code(schema_info["text"], language="sql")


def render_index_tab(db_path: Path, schema_info: dict) -> None:
    st.subheader("🛠️ 索引管理")
    all_indexes = [
        (table, item) for table, items in schema_info["indexes"].items() for item in items
    ]

    st.markdown("#### 当前索引")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "表": table,
                    "索引名": item["name"],
                    "列": ", ".join(item["columns"]) or "rowid",
                    "唯一": item["unique"],
                    "来源": INDEX_SOURCE_LABEL.get(item["origin"], item["origin"]),
                }
                for table, item in all_indexes
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    st.divider()
    st.markdown("#### 手工创建索引")
    target_table = st.selectbox("表", list(schema_info["columns"]), key="manual_index_table")
    target_columns = st.multiselect(
        "列（勾选顺序即联合索引的列顺序）",
        schema_info["columns"][target_table],
        key="manual_index_columns",
    )
    unique = st.checkbox("UNIQUE 索引", value=False, key="manual_index_unique")
    custom_name = st.text_input("索引名（留空自动生成）", value="", key="manual_index_name")
    if st.button("🏗️ 创建索引", type="primary", key="manual_create_index"):
        ok, message = apply_index(
            target_table, target_columns, custom_name.strip(), db_path, unique
        )
        if ok:
            st.session_state["schema_version"] += 1
            st.session_state["flash"] = ("success", f"索引已创建：{message}")
        else:
            st.session_state["flash"] = ("error", message)
        st.rerun()

    st.divider()
    st.markdown("#### 删除索引")
    droppable = [(table, item) for table, item in all_indexes if item["origin"] == "c"]
    if not droppable:
        st.caption("当前没有可删除的显式索引（主键与 UNIQUE 自动索引由 SQLite 管理，无法直接删除）。")
    for table, item in droppable:
        row = st.columns([5, 1])
        row[0].markdown(f"`{item['name']}` → {table} ({', '.join(item['columns'])})")
        row[1].button(
            "🗑️ 删除",
            key=f"drop_index_{item['name']}",
            on_click=drop_index_callback,
            args=(item["name"], str(db_path)),
        )

    st.divider()
    st.markdown("#### 重置演示数据")
    st.caption("索引加多了、想从头演示时，可覆盖重建数据库（恢复到只有主键 / UNIQUE 索引的初始状态）。")
    if st.button("♻️ 覆盖重建演示库", key="rebuild_database"):
        try:
            stats = db_setup.build_database(db_path, overwrite=True, verbose=False)
        except OSError as exc:
            st.session_state["flash"] = (
                "error",
                f"重建失败：{exc}（请确认没有其他程序正在占用该文件）",
            )
        else:
            st.session_state["schema_version"] += 1
            st.session_state["baseline"] = None
            st.session_state["last_run"] = None
            st.session_state["comparison"] = None
            st.session_state["diagnosis"] = None
            st.session_state["flash"] = (
                "success",
                f"已重建：共 {stats['total_rows']:,} 行（orders {stats['rows']['orders']:,} 行）",
            )
        st.rerun()


def render_history_tab(db_path: Path) -> None:
    history = st.session_state["history"]
    st.subheader(f"📜 分析历史（{len(history)} 条，仅保留在当前会话）")
    if not history:
        st.info("还没有分析记录。去「🔍 查询与诊断」写一条 SQL，再点「🧠 DeepSeek 分析」。")
        return

    for position, record in enumerate(history):
        score = record.get("score")
        score_text = f"评分 {score}" if score is not None else "评分 —"
        summary = (record.get("summary") or "")[:40]
        with st.expander(f"{record['at']} · {record['model']} · {score_text} · {summary}"):
            st.code(record["sql"], language="sql")
            if record.get("summary"):
                st.markdown(record["summary"])
            if record.get("elapsed_ms") is not None:
                st.caption(f"当时实测耗时：{record['elapsed_ms']:,.2f} ms")
            if record.get("indexes"):
                st.markdown("索引建议：" + "、".join(f"`{name}`" for name in record["indexes"]))
            else:
                st.caption("这次没有给出索引建议。")
            st.button(
                "📥 载入该 SQL 到编辑器",
                key=f"history_load_{position}",
                on_click=use_sql,
                args=(record["sql"],),
            )
            raw = record.get("raw") or ""
            st.code(raw[:4000] + ("\n…（内容过长已截断）" if len(raw) > 4000 else ""), language="json")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="AI SQL 优化助手", page_icon="🧠", layout="wide")
    init_session_state()

    st.title("🧠 AI SQL 优化助手")
    st.caption(
        "Streamlit + SQLite（orders 5 万行）+ DeepSeek：把「查询计划 + 实测耗时」交给大模型诊断，"
        "索引建议经安全校验后可一键落地，再用「🔁 对比耗时」验证真实收益。"
    )

    db_path_text = st.sidebar.text_input("数据库文件", value=str(DEFAULT_DB_PATH), key="db_path_input")
    db_path = Path(db_path_text.strip() or str(DEFAULT_DB_PATH))
    if not db_path.exists():
        st.error(f"找不到数据库文件：{db_path}")
        st.code("python db_setup.py --force", language="powershell")
        if st.button("🚀 生成演示库（约 5.7 万行，1 秒内完成）"):
            with st.spinner("正在生成数据…"):
                stats = db_setup.build_database(db_path, overwrite=False, verbose=False)
            st.success(f"已生成：{stats['rows']}（合计 {stats['total_rows']:,} 行）")
            st.rerun()
        st.stop()

    try:
        schema_info = cached_schema_info(str(db_path), st.session_state["schema_version"])
    except sqlite3.Error as exc:
        st.error(f"无法读取数据库：{exc}")
        if st.button("🧹 清空缓存后重试"):
            st.cache_data.clear()
            st.rerun()
        st.stop()

    settings = render_sidebar(schema_info, db_path)
    show_flash()

    tabs = st.tabs(["🔍 查询与诊断", "🧩 表结构", "🛠️ 索引管理", "📜 历史记录"])
    with tabs[0]:
        render_query_tab(db_path, schema_info, settings)
    with tabs[1]:
        render_schema_tab(schema_info)
    with tabs[2]:
        render_index_tab(db_path, schema_info)
    with tabs[3]:
        render_history_tab(db_path)


if __name__ == "__main__":
    main()