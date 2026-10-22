#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段一：数据审计与需求确认（只读）。

约束：
- 仅使用 Python 标准库；
- SQLite 使用 mode=ro 只读连接；
- 不修改任何原始输入文件；
- 不实现逐 SKU 合规判定，仅审查数据、结构与映射关系。

输出：
- audit/database_schema.json
- audit/异常数据清单.csv
- audit/07_数据审计汇总.json

运行：
    python -I audit/run_audit.py
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "audit"
DB_PATH = ROOT / "compliance.db"
CSV_PATH = ROOT / "sku_list.csv"

SOURCE_FILES = [
    "instructions.md",
    "data-dictionary.md",
    "seed.py",
    "sku_list.csv",
    "compliance.db",
]

# 题目明确要求的输入记录数
EXPECTED_RECORDS = 120

# 规则匹配口径常量（来自 instructions.md 2.1）
RISK_ORDER = {"待确认": 1, "违规": 2}
VALID_RISK_LEVELS = {"待确认", "违规"}
DEFAULT_RISK = "合规"


def now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_meta(path: Path) -> dict:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def pattern_of(value: str) -> str:
    """把数字替换为 9、字母替换为 A，用于展示格式分布。"""
    return re.sub(r"[0-9]", "9", re.sub(r"[A-Za-z]", "A", value))


def is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def quantize_cny(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# CSV 审计
# ---------------------------------------------------------------------------

def audit_csv() -> dict:
    raw = CSV_PATH.read_bytes()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    text = raw.decode("utf-8-sig")

    crlf_count = raw.count(b"\r\n")
    lf_total = raw.count(b"\n")
    lf_only = lf_total - crlf_count

    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = list(reader.fieldnames or [])
    records = []
    malformed = []
    for row in reader:
        line_no = reader.line_num
        if None in row or any(v is None for v in row.values()):
            malformed.append(
                {
                    "line": line_no,
                    "row": {str(k): v for k, v in row.items()},
                }
            )
        records.append({"line": line_no, "row": row})

    fields = {}
    for name in header:
        values = [r["row"].get(name) for r in records]
        non_null = [v for v in values if v is not None]
        strings = [v for v in non_null if isinstance(v, str)]
        counter = Counter(strings)
        duplicates = [
            {"value": v, "count": c, "lines": [r["line"] for r in records if r["row"].get(name) == v]}
            for v, c in counter.items()
            if c > 1
        ]
        fields[name] = {
            "count": len(values),
            "null_count": len(values) - len(non_null),
            "empty_count": sum(1 for v in strings if v == ""),
            "blank_or_whitespace_count": sum(1 for v in strings if v.strip() == ""),
            "leading_or_trailing_whitespace_count": sum(1 for v in strings if v != v.strip()),
            "min_length": min((len(v) for v in strings), default=None),
            "max_length": max((len(v) for v in strings), default=None),
            "distinct_count": len(set(strings)),
            "format_distribution": dict(sorted(Counter(pattern_of(v) for v in strings).items())),
            "duplicate_groups": duplicates,
        }

    refs = [r["row"].get("sku_ref") for r in records]
    batches = [r["row"].get("batch_no") for r in records]

    ref_counter = Counter(refs)
    batch_counter = Counter(batches)

    ref_duplicate_groups = [
        {"value": v, "count": c, "lines": [r["line"] for r in records if r["row"].get("sku_ref") == v]}
        for v, c in ref_counter.items()
        if c > 1
    ]
    batch_duplicate_groups = [
        {
            "value": v,
            "count": c,
            "lines": [r["line"] for r in records if r["row"].get("batch_no") == v],
            "sku_refs": [r["row"].get("sku_ref") for r in records if r["row"].get("batch_no") == v],
        }
        for v, c in batch_counter.items()
        if c > 1
    ]

    ref_blank = [
        {"line": r["line"], "value": r["row"].get("sku_ref")}
        for r in records
        if is_blank(r["row"].get("sku_ref"))
    ]
    batch_blank = [
        {"line": r["line"], "value": r["row"].get("batch_no")}
        for r in records
        if is_blank(r["row"].get("batch_no"))
    ]

    ref_numeric = [v for v in refs if isinstance(v, str) and re.fullmatch(r"\d+", v)]
    ref_numbers = [int(v) for v in ref_numeric]
    out_of_range = [
        {"line": r["line"], "value": r["row"].get("sku_ref")}
        for r in records
        if isinstance(r["row"].get("sku_ref"), str)
        and re.fullmatch(r"\d+", r["row"]["sku_ref"])
        and not (1 <= int(r["row"]["sku_ref"]) <= EXPECTED_RECORDS)
    ]

    return {
        "path": CSV_PATH.relative_to(ROOT).as_posix(),
        "size": len(raw),
        "encoding": "UTF-8 with BOM (utf-8-sig)" if has_bom else "UTF-8 without BOM",
        "has_bom": has_bom,
        "line_endings": {"crlf_lines": crlf_count, "lf_only_lines": lf_only, "ends_with_newline": raw.endswith(b"\n")},
        "header": header,
        "record_count": len(records),
        "expected_record_count": EXPECTED_RECORDS,
        "record_count_matches_expected": len(records) == EXPECTED_RECORDS,
        "malformed_rows": malformed,
        "fields": fields,
        "sku_ref": {
            "null_or_blank": ref_blank,
            "duplicate_group_count": len(ref_duplicate_groups),
            "duplicate_value_count": sum(g["count"] for g in ref_duplicate_groups),
            "duplicate_groups": ref_duplicate_groups,
            "distinct_count": len(set(refs)),
            "format_distribution": dict(sorted(Counter(pattern_of(v) for v in refs if isinstance(v, str)).items())),
            "all_match_5_digits": all(isinstance(v, str) and re.fullmatch(r"\d{5}", v) for v in refs),
            "numeric_min": min(ref_numbers) if ref_numbers else None,
            "numeric_max": max(ref_numbers) if ref_numbers else None,
            "out_of_expected_range": out_of_range,
            "leading_or_trailing_whitespace": [
                {"line": r["line"], "value": r["row"].get("sku_ref")}
                for r in records
                if isinstance(r["row"].get("sku_ref"), str)
                and r["row"]["sku_ref"] != r["row"]["sku_ref"].strip()
            ],
        },
        "batch_no": {
            "null_or_blank": batch_blank,
            "duplicate_group_count": len(batch_duplicate_groups),
            "duplicate_record_count": sum(g["count"] for g in batch_duplicate_groups),
            "duplicate_groups": batch_duplicate_groups,
            "distinct_count": len(set(batches)),
            "format_distribution": dict(sorted(Counter(pattern_of(v) for v in batches if isinstance(v, str)).items())),
            "leading_or_trailing_whitespace": [
                {"line": r["line"], "value": r["row"].get("batch_no")}
                for r in records
                if isinstance(r["row"].get("batch_no"), str)
                and r["row"]["batch_no"] != r["row"]["batch_no"].strip()
            ],
        },
        "_records": records,  # 内部使用，写 JSON 前移除
    }


# ---------------------------------------------------------------------------
# 数据库结构与数据质量
# ---------------------------------------------------------------------------

def audit_database(con: sqlite3.Connection) -> dict:
    table_objs = rows_to_dicts(
        con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        )
    )
    tables = [o for o in table_objs if o["type"] == "table"]
    views = [o for o in table_objs if o["type"] == "view"]

    table_details = []
    for t in tables:
        name = t["name"]
        qname = quote_ident(name)
        columns = rows_to_dicts(con.execute(f"PRAGMA table_info({qname})"))
        row_count = con.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]

        indexes = []
        for idx in con.execute(f"PRAGMA index_list({qname})"):
            idx_name = idx[1]
            idx_cols = [r[2] for r in con.execute(f"PRAGMA index_info({quote_ident(idx_name)})")]
            indexes.append(
                {
                    "name": idx_name,
                    "unique": bool(idx[2]),
                    "origin": idx[3],
                    "partial": bool(idx[4]),
                    "columns": idx_cols,
                }
            )

        fks = rows_to_dicts(con.execute(f"PRAGMA foreign_key_list({qname})"))
        pk_cols = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"] > 0]

        pk_duplicate_groups = 0
        if pk_cols:
            group_expr = ",".join(quote_ident(c) for c in pk_cols)
            pk_duplicate_groups = con.execute(
                f"SELECT COUNT(*) FROM (SELECT {group_expr}, COUNT(*) c "
                f"FROM {qname} GROUP BY {group_expr} HAVING c > 1)"
            ).fetchone()[0]

        samples = rows_to_dicts(
            con.execute(
                f"SELECT * FROM {qname} ORDER BY "
                + (",".join(quote_ident(c) for c in pk_cols) if pk_cols else "rowid")
                + " LIMIT 2"
            )
        )

        column_stats = {}
        for col in columns:
            cname = col["name"]
            ctype = (col["type"] or "").upper()
            qcol = quote_ident(cname)
            base = con.execute(
                f"SELECT COUNT(*) total, COUNT({qcol}) non_null, COUNT(DISTINCT {qcol}) distinct_count "
                f"FROM {qname}"
            ).fetchone()
            stat = {
                "cid": col["cid"],
                "name": cname,
                "declared_type": col["type"],
                "not_null": bool(col["notnull"]),
                "default": col["dflt_value"],
                "primary_key_position": col["pk"],
                "total": base[0],
                "non_null": base[1],
                "null_count": base[0] - base[1],
                "distinct_count": base[2],
            }
            if "TEXT" in ctype or ctype == "":
                r = con.execute(
                    f"SELECT COALESCE(SUM(CASE WHEN {qcol} IS NOT NULL AND TRIM({qcol})='' THEN 1 ELSE 0 END),0), "
                    f"COALESCE(SUM(CASE WHEN {qcol} IS NOT NULL AND {qcol}<>TRIM({qcol}) THEN 1 ELSE 0 END),0), "
                    f"MIN(LENGTH({qcol})), MAX(LENGTH({qcol})) FROM {qname}"
                ).fetchone()
                stat.update(
                    blank_or_whitespace_count=r[0],
                    leading_or_trailing_whitespace_count=r[1],
                    min_length=r[2],
                    max_length=r[3],
                )
            if any(k in ctype for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")):
                values = [r[0] for r in con.execute(f"SELECT {qcol} FROM {qname} WHERE {qcol} IS NOT NULL")]
                numeric = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
                typeof_dist = {
                    r[0]: r[1]
                    for r in con.execute(
                        f"SELECT typeof({qcol}), COUNT(*) FROM {qname} GROUP BY typeof({qcol})"
                    )
                }
                stat.update(
                    min=min(numeric) if numeric else None,
                    max=max(numeric) if numeric else None,
                    zero_count=sum(1 for v in numeric if v == 0),
                    negative_count=sum(1 for v in numeric if v < 0),
                    non_finite_count=sum(1 for v in numeric if isinstance(v, float) and not math.isfinite(v)),
                    typeof_distribution=typeof_dist,
                )
            column_stats[cname] = stat

        table_details.append(
            {
                "name": name,
                "row_count": row_count,
                "create_sql": t["sql"],
                "columns": columns,
                "primary_key": pk_cols,
                "pk_duplicate_groups": pk_duplicate_groups,
                "indexes": indexes,
                "foreign_keys": fks,
                "sample_rows": samples,
                "column_stats": column_stats,
            }
        )

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    encoding = con.execute("PRAGMA encoding").fetchone()[0]
    foreign_key_check = rows_to_dicts(con.execute("PRAGMA foreign_key_check"))

    return {
        "path": DB_PATH.relative_to(ROOT).as_posix(),
        "size": DB_PATH.stat().st_size,
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_check": integrity,
        "encoding": encoding,
        "foreign_key_check": foreign_key_check,
        "views": views,
        "tables": table_details,
    }


def make_schema_json(db: dict) -> dict:
    tables = []
    for t in db["tables"]:
        tables.append(
            {
                "name": t["name"],
                "row_count": t["row_count"],
                "create_sql": t["create_sql"],
                "columns": [
                    {
                        "cid": c["cid"],
                        "name": c["name"],
                        "type": c["type"],
                        "not_null": bool(c["notnull"]),
                        "default": c["dflt_value"],
                        "primary_key_position": c["pk"],
                    }
                    for c in t["columns"]
                ],
                "primary_key": t["primary_key"],
                "indexes": t["indexes"],
                "foreign_keys": t["foreign_keys"],
                "sample_rows": t["sample_rows"],
            }
        )
    return {
        "generated_at": now_iso(),
        "database": db["path"],
        "size_bytes": db["size"],
        "sqlite_version": db["sqlite_version"],
        "encoding": db["encoding"],
        "integrity_check": db["integrity_check"],
        "views": db["views"],
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# 业务表专项检查
# ---------------------------------------------------------------------------

def get_sku_rows(con: sqlite3.Connection) -> list[dict]:
    return rows_to_dicts(con.execute("SELECT * FROM sku_info ORDER BY sku_id"))


def audit_sku_info(sku_rows: list[dict]) -> dict:
    def distribution(key: str) -> list[dict]:
        c = Counter(str(r[key]) for r in sku_rows)
        return [{"value": k, "count": v} for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "row_count": len(sku_rows),
        "sku_id_format_distribution": dict(sorted(Counter(pattern_of(r["sku_id"]) for r in sku_rows).items())),
        "category_distribution": distribution("category"),
        "material_distribution": distribution("material"),
        "origin_distribution": distribution("origin"),
        "currency_distribution": distribution("currency"),
        "is_deleted_distribution": distribution("is_deleted"),
        "is_deleted_values": sorted({r["is_deleted"] for r in sku_rows}),
        "price_range": {
            "min": min(r["price"] for r in sku_rows),
            "max": max(r["price"] for r in sku_rows),
        },
        "deleted_skus": [
            {
                "sku_id": r["sku_id"],
                "name": r["name"],
                "category": r["category"],
                "material": r["material"],
                "origin": r["origin"],
                "price": r["price"],
                "currency": r["currency"],
            }
            for r in sku_rows
            if int(r["is_deleted"]) == 1
        ],
    }


def audit_rules(con: sqlite3.Connection, sku_rows: list[dict]) -> dict:
    rules = rows_to_dicts(con.execute("SELECT * FROM compliance_rules ORDER BY rule_id"))
    sku_categories = {r["category"] for r in sku_rows}
    sku_origins = {r["origin"] for r in sku_rows}
    sku_materials = sorted({r["material"] for r in sku_rows})

    rule_details = []
    category_tokens_all = set()
    material_tokens_all = set()
    origin_tokens = set()
    separator_issues = []
    for r in rules:
        cats = str(r["applies_to_category"]).split(",")
        mats = [] if r["material_keyword"] in (None, "") else str(r["material_keyword"]).split("|")
        category_tokens_all.update(cats)
        material_tokens_all.update(mats)
        if r["origin_keyword"] not in (None, ""):
            origin_tokens.add(r["origin_keyword"])

        cats_empty = [c for c in cats if c.strip() == ""]
        mats_empty = [m for m in mats if m.strip() == ""]
        cats_spaced = [c for c in cats if c != c.strip()]
        mats_spaced = [m for m in mats if m != m.strip()]
        if cats_empty or mats_empty or cats_spaced or mats_spaced:
            separator_issues.append(
                {
                    "rule_id": r["rule_id"],
                    "category_tokens": cats,
                    "material_tokens": mats,
                    "empty_category_tokens": cats_empty,
                    "empty_material_tokens": mats_empty,
                    "category_tokens_with_spaces": cats_spaced,
                    "material_tokens_with_spaces": mats_spaced,
                }
            )

        rule_details.append(
            {
                "rule_id": r["rule_id"],
                "title": r["title"],
                "applies_to_category": r["applies_to_category"],
                "category_tokens": cats,
                "material_keyword": r["material_keyword"],
                "material_tokens": mats,
                "origin_keyword": r["origin_keyword"],
                "severity": r["severity"],
            }
        )

    severity_dist = Counter(r["severity"] for r in rules)
    category_union = sorted(category_tokens_all)
    material_union = sorted(material_tokens_all)
    origin_union = sorted(origin_tokens)

    keyword_material_matches = {
        kw: [m for m in sku_materials if kw in m] for kw in material_union
    }
    material_dangling = [kw for kw, matches in keyword_material_matches.items() if not matches]
    category_dangling = sorted(category_tokens_all - sku_categories)
    origin_dangling = sorted(origin_tokens - sku_origins)

    return {
        "rule_count": len(rules),
        "rules": rule_details,
        "severity_distribution": dict(sorted(severity_dist.items(), key=lambda kv: (-kv[1], kv[0]))),
        "severity_values": sorted(severity_dist),
        "unknown_severity_values": sorted(set(severity_dist) - VALID_RISK_LEVELS),
        "rule_id_duplicate_groups": [
            {"value": k, "count": v}
            for k, v in Counter(r["rule_id"] for r in rules).items()
            if v > 1
        ],
        "null_or_blank_fields": {
            field: [
                r["rule_id"]
                for r in rules
                if is_blank(r.get(field))
            ]
            for field in ("rule_id", "title", "applies_to_category", "material_keyword", "origin_keyword", "severity")
        },
        "separator_issues": separator_issues,
        "category_union": category_union,
        "material_union": material_union,
        "origin_union": origin_union,
        "category_dangling": category_dangling,
        "material_dangling": material_dangling,
        "origin_dangling": origin_dangling,
        "keyword_material_matches": keyword_material_matches,
        "title_duplicate_groups": [
            {"value": k, "count": v}
            for k, v in Counter(r["title"] for r in rules).items()
            if v > 1
        ],
        "matching_spec": {
            "category": "applies_to_category 按英文逗号分列后，sku.category 与其中任一项完全相等",
            "material": "material_keyword 为空表示不限；否则按竖线分词，sku.material 包含任一关键词（子串包含）",
            "origin": "origin_keyword 为空表示不限；否则 sku.origin 与规则值完全相等",
            "combination": "类目、材质、产地三个条件同时满足，规则才生效",
            "precedence": "多条命中取最严：违规 > 待确认；无命中为合规",
            "normalization": "比较前建议对规则与商品字段做 strip；本数据无前后空格，规范化不改变结果",
        },
    }


def audit_rates(con: sqlite3.Connection, sku_rows: list[dict]) -> dict:
    rates = rows_to_dicts(con.execute("SELECT * FROM exchange_rates ORDER BY currency"))
    rate_map = {r["currency"]: r["rate_to_cny"] for r in rates}
    sku_currencies = sorted({r["currency"] for r in sku_rows})
    missing_rates = sorted(set(sku_currencies) - set(rate_map))
    unused_rates = sorted(set(rate_map) - set(sku_currencies))

    per_currency = []
    for cur in sku_currencies:
        subset = [r for r in sku_rows if r["currency"] == cur]
        if cur in rate_map:
            converted = [
                quantize_cny(Decimal(str(r["price"])) * Decimal(str(rate_map[cur])))
                for r in subset
            ]
            converted_range = {"min": str(min(converted)), "max": str(max(converted))}
        else:
            converted_range = None
        per_currency.append(
            {
                "currency": cur,
                "product_count": len(subset),
                "price_min": min(r["price"] for r in subset),
                "price_max": max(r["price"] for r in subset),
                "rate_to_cny": rate_map.get(cur),
                "converted_price_range_cny": converted_range,
            }
        )

    return {
        "row_count": len(rates),
        "rates": rates,
        "duplicate_currencies": [
            {"value": k, "count": v}
            for k, v in Counter(r["currency"] for r in rates).items()
            if v > 1
        ],
        "null_or_blank": [
            r for r in rates if is_blank(r["currency"]) or r["rate_to_cny"] is None
        ],
        "non_positive_rates": [r for r in rates if r["rate_to_cny"] is not None and r["rate_to_cny"] <= 0],
        "non_finite_rates": [
            r for r in rates if isinstance(r["rate_to_cny"], float) and not math.isfinite(r["rate_to_cny"])
        ],
        "currency_format_issues": [
            r["currency"]
            for r in rates
            if r["currency"] != r["currency"].strip() or r["currency"] != r["currency"].upper()
        ],
        "cny_rate": rate_map.get("CNY"),
        "has_rmb_row": "RMB" in rate_map,
        "product_currencies": sku_currencies,
        "missing_rates_for_product_currencies": missing_rates,
        "unused_rates": unused_rates,
        "per_currency": per_currency,
        "formula": "人民币价格 = 商品原价 × 对应币种的 rate_to_cny，结果保留两位小数",
    }


# ---------------------------------------------------------------------------
# SKU 映射
# ---------------------------------------------------------------------------

def evaluate_mapping(records: list[dict], transform, db_by_id: dict) -> dict:
    mapped = []
    for rec in records:
        ref = rec["row"].get("sku_ref")
        target = transform(ref) if isinstance(ref, str) else None
        matched = target in db_by_id
        mapped.append(
            {
                "line": rec["line"],
                "sku_ref": ref,
                "batch_no": rec["row"].get("batch_no"),
                "mapped_id": target,
                "matched": matched,
            }
        )

    matched_records = [m for m in mapped if m["matched"]]
    unmatched_records = [m for m in mapped if not m["matched"]]
    target_counts = Counter(m["mapped_id"] for m in matched_records)
    duplicate_targets = [
        {"mapped_id": k, "count": v, "sku_refs": [m["sku_ref"] for m in matched_records if m["mapped_id"] == k]}
        for k, v in target_counts.items()
        if v > 1
    ]

    return {
        "matched_count": len(matched_records),
        "unmatched_count": len(unmatched_records),
        "matched_sku_refs": [m["sku_ref"] for m in matched_records],
        "unmatched_sku_refs": [m["sku_ref"] for m in unmatched_records],
        "unmatched_records": [
            {"line": m["line"], "sku_ref": m["sku_ref"], "batch_no": m["batch_no"], "mapped_id": m["mapped_id"]}
            for m in unmatched_records
        ],
        "matched_db_id_count": len(set(m["mapped_id"] for m in matched_records)),
        "duplicate_target_groups": duplicate_targets,
        "has_many_to_one": bool(duplicate_targets),
        "has_one_to_many": False,  # 单值函数，一个输入只产生一个目标
    }


def audit_mapping(records: list[dict], sku_rows: list[dict]) -> dict:
    db_by_id = {r["sku_id"]: r for r in sku_rows}
    db_ids = set(db_by_id)

    candidates = [
        {"id": "A", "name": "sku_ref 直接等于 sku_id", "transform": lambda s: s},
        {"id": "B", "name": "sku_ref 去除前后空格后加 \"SKU-\" 前缀", "transform": lambda s: "SKU-" + s.strip()},
        {
            "id": "C",
            "name": "sku_ref 去除前后空格、数字补足 5 位后加 \"SKU-\" 前缀",
            "transform": lambda s: "SKU-" + (s.strip().zfill(5) if s.strip().isdigit() else s.strip()),
        },
        {"id": "D", "name": "sku_ref 去除前后空格并转大写后直接等于 sku_id", "transform": lambda s: s.strip().upper()},
    ]

    candidate_results = []
    for cand in candidates:
        result = evaluate_mapping(records, cand["transform"], db_by_id)
        candidate_results.append({"id": cand["id"], "name": cand["name"], **result})

    chosen_cand = next(c for c in candidates if c["id"] == "B")
    chosen = evaluate_mapping(records, chosen_cand["transform"], db_by_id)

    status_records = []
    for rec in records:
        ref = rec["row"].get("sku_ref")
        mapped_id = "SKU-" + ref.strip() if isinstance(ref, str) else None
        sku = db_by_id.get(mapped_id)
        if sku is None:
            status = "库中缺失"
        elif int(sku["is_deleted"]) == 1:
            status = "已下架"
        else:
            status = "正常评估"
        status_records.append(
            {
                "line": rec["line"],
                "sku_ref": ref,
                "batch_no": rec["row"].get("batch_no"),
                "mapped_id": mapped_id,
                "status": status,
                "sku": sku,
            }
        )

    missing_records = [r for r in status_records if r["status"] == "库中缺失"]
    deleted_records = [r for r in status_records if r["status"] == "已下架"]
    evaluated_records = [r for r in status_records if r["status"] == "正常评估"]

    mapped_ids = {r["mapped_id"] for r in status_records if r["mapped_id"] in db_ids}
    db_ids_not_in_list = sorted(db_ids - mapped_ids)

    counts = {
        "N_input": len(records),
        "N_matched": len(mapped_ids),
        "N_deleted": len(deleted_records),
        "N_missing": len(missing_records),
        "N_evaluated": len(evaluated_records),
        "db_total_rows": len(sku_rows),
        "matched_incl_deleted": len([r for r in status_records if r["status"] in ("已下架", "正常评估")]),
    }

    equations = [
        {
            "name": "N_input = N_matched + N_missing",
            "left": counts["N_input"],
            "right": counts["N_matched"] + counts["N_missing"],
            "holds": counts["N_input"] == counts["N_matched"] + counts["N_missing"],
        },
        {
            "name": "N_matched = N_deleted + N_evaluated",
            "left": counts["N_matched"],
            "right": counts["N_deleted"] + counts["N_evaluated"],
            "holds": counts["N_matched"] == counts["N_deleted"] + counts["N_evaluated"],
        },
        {
            "name": "N_input = N_missing + N_deleted + N_evaluated",
            "left": counts["N_input"],
            "right": counts["N_missing"] + counts["N_deleted"] + counts["N_evaluated"],
            "holds": counts["N_input"] == counts["N_missing"] + counts["N_deleted"] + counts["N_evaluated"],
        },
    ]

    return {
        "candidates": candidate_results,
        "chosen": {
            "candidate_id": "B",
            "method": chosen_cand["name"],
            "matched_count": chosen["matched_count"],
            "unmatched_count": chosen["unmatched_count"],
            "matched_sku_refs": chosen["matched_sku_refs"],
            "unmatched_records": chosen["unmatched_records"],
            "duplicate_target_groups": chosen["duplicate_target_groups"],
        },
        "chosen_method_evidence": {
            "db_id_format_distribution": dict(sorted(Counter(pattern_of(r["sku_id"]) for r in sku_rows).items())),
            "csv_ref_format_distribution": dict(
                sorted(Counter(pattern_of(rec["row"].get("sku_ref")) for rec in records).items())
            ),
            "direct_mapping_matches": candidate_results[0]["matched_count"],
            "prefix_mapping_matches": candidate_results[1]["matched_count"],
            "unmatched_after_prefix": chosen["unmatched_records"],
            "all_db_ids_covered_by_list": len(db_ids_not_in_list) == 0,
            "db_ids_not_in_list": db_ids_not_in_list,
            "duplicate_target_groups": chosen["duplicate_target_groups"],
            "one_to_one": not chosen["duplicate_target_groups"] and len(status_records) == len(records),
            "missing_numeric_ids": sorted(
                f"{i:05d}" for i in range(1, EXPECTED_RECORDS + 1) if f"SKU-{i:05d}" not in db_ids
            ),
        },
        "status_records": status_records,
        "missing_records": [
            {"line": r["line"], "sku_ref": r["sku_ref"], "batch_no": r["batch_no"]} for r in missing_records
        ],
        "deleted_records": [
            {
                "line": r["line"],
                "sku_ref": r["sku_ref"],
                "batch_no": r["batch_no"],
                "sku_id": r["mapped_id"],
                "name": r["sku"]["name"],
                "category": r["sku"]["category"],
                "material": r["sku"]["material"],
                "origin": r["sku"]["origin"],
                "price": r["sku"]["price"],
                "currency": r["sku"]["currency"],
            }
            for r in deleted_records
        ],
        "evaluated_sku_refs": [r["sku_ref"] for r in evaluated_records],
        "counts": counts,
        "equations": equations,
    }


# ---------------------------------------------------------------------------
# 异常清单与结论
# ---------------------------------------------------------------------------

def build_anomalies(csv_audit: dict, mapping: dict, rules: dict, rates: dict, sku_info: dict) -> list[dict]:
    items = []

    def add(atype, severity, source, identifier, field, value, description, suggestion, line=""):
        items.append(
            {
                "异常类型": atype,
                "严重级别": severity,
                "来源文件或数据表": source,
                "行号/主键": line,
                "原始标识": identifier,
                "字段名": field,
                "原始值": value,
                "问题描述": description,
                "处理建议": suggestion,
            }
        )

    add(
        "编号格式差异（系统性）",
        "P0",
        "sku_list.csv ↔ sku_info",
        "全部 120 条清单记录",
        "sku_ref / sku_id",
        "清单示例：00001；数据库示例：SKU-00001",
        "清单 sku_ref 为 5 位数字，数据库 sku_id 为 SKU- + 5 位数字；直接相等仅匹配 0/120，加前缀后匹配 115/120。",
        "第二阶段统一采用 strip + \"SKU-\" 前缀映射，并校验格式；保留原始 sku_ref。",
        "全表",
    )

    for m in mapping["missing_records"]:
        add(
            "库中缺失",
            "P1",
            "sku_list.csv",
            m["sku_ref"],
            "sku_ref",
            m["sku_ref"],
            f"CSV 第 {m['line']} 行；按 SKU- 前缀映射后在 sku_info 中不存在，应标注“库中缺失”，不得静默丢弃。",
            "报告中保留该行，处置结果=库中缺失，商品字段填“—”，不参与规则评估与价格换算，并计入完整性自检。",
            m["line"],
        )

    for d in mapping["deleted_records"]:
        add(
            "状态记录-已下架",
            "信息",
            "sku_info",
            d["sku_id"],
            "is_deleted",
            "1",
            f"CSV 第 {d['line']} 行；商品 {d['name']} 的 is_deleted=1，应跳过规则评估并标注“已下架”。",
            "报告中处置结果=已下架；不参与风险统计与实际评估数；保留商品基本信息及原价币种。",
            d["line"],
        )

    for g in csv_audit["batch_no"]["duplicate_groups"]:
        add(
            "批次号重复（非 SKU 重复）",
            "提示",
            "sku_list.csv",
            g["value"],
            "batch_no",
            g["value"],
            f"批次号重复 {g['count']} 次，出现在第 {', '.join(map(str, g['lines']))} 行，对应不同 sku_ref：{', '.join(g['sku_refs'])}。",
            "batch_no 仅记录用；不得按 batch_no 去重，也不得据此判定 SKU 重复；按 sku_ref 逐行处理。",
            ", ".join(map(str, g["lines"])),
        )

    # 数据字典差异
    add(
        "数据字典差异",
        "P0",
        "data-dictionary.md ↔ compliance_rules",
        "compliance_rules.risk_level",
        "risk_level / severity",
        "字典：risk_level；实际：severity",
        "规则表实际风险字段名为 severity，字典写成 risk_level；按字典写 SQL 会因列不存在而失败。",
        "第二阶段一律使用 severity；同时在 README 中记录该差异。",
        "规则表全表",
    )
    add(
        "数据字典差异",
        "P0",
        "data-dictionary.md ↔ sku_info",
        "sku_info.is_deleted",
        "is_deleted",
        "字典未列出该字段",
        "实际 sku_info 存在 is_deleted INTEGER NOT NULL DEFAULT 0，110 条为 0、5 条为 1；字典完全遗漏。",
        "第二阶段使用 is_deleted=1 判定已下架，并在 README/踩坑说明中说明该字段来源与取值。",
        "商品表全表",
    )
    add(
        "数据字典差异",
        "P1",
        "data-dictionary.md ↔ exchange_rates",
        "exchange_rates.currency",
        "currency",
        "字典：人民币使用 RMB；实际：CNY",
        "汇率表实际人民币代码为 CNY（rate_to_cny=1.0），无 RMB 行；按字典查 RMB 会查不到。",
        "第二阶段使用数据库实际币种代码 CNY；不要硬编码 RMB。",
        "汇率表全表",
    )
    add(
        "数据字典差异",
        "P0",
        "data-dictionary.md ↔ sku_list.csv",
        "sku_ref = sku_id 直接关联",
        "sku_ref / sku_id",
        "字典：按此关联即可；实际直接匹配 0/120",
        "字典第 4 节声称 sku_ref 即 sku_id 可直接关联，与实际格式不一致；直接相等匹配 0 条，加 SKU- 前缀匹配 115 条。",
        "第二阶段实现并测试前缀映射；将字典该条标记为错误。",
        "120 条清单",
    )
    add(
        "数据字典差异",
        "P2",
        "data-dictionary.md",
        "约束与记录数",
        "PK / NOT NULL / 默认值 / 行数",
        "字典未说明",
        f"字典未记录主键、NOT NULL、默认值及行数；实际 sku_info {sku_info['row_count']} 行、rules {rules['rule_count']} 行、rates {rates['row_count']} 行。",
        "以 database_schema.json 为准确认约束；文档中补充真实行数与约束。",
        "全库",
    )

    # 价格量级观察
    if rates["per_currency"]:
        low = [
            p
            for p in rates["per_currency"]
            if p["converted_price_range_cny"]
            and Decimal(p["converted_price_range_cny"]["max"]) < Decimal("50")
        ]
        if low:
            desc = "；".join(
                f"{p['currency']}：原价 {p['price_min']}–{p['price_max']}，换算后 {p['converted_price_range_cny']['min']}–{p['converted_price_range_cny']['max']} CNY"
                for p in low
            )
            add(
                "价格量级待确认",
                "P2",
                "sku_info + exchange_rates",
                "低价币种商品",
                "price / currency",
                desc,
                "这些币种商品换算后金额极低，疑似合成数据或价格单位与币种不匹配；不影响按题目公式换算的正确性，但影响报告金额的业务合理性。",
                "按题目公式照常换算；向出题方确认价格单位，不得自行改价或改汇率。",
                "按币种汇总",
            )

    # 种子代码缺陷（结构化留档）
    add(
        "种子代码缺陷",
        "P0",
        "seed.py",
        "第 23 行",
        "sku_ref 查询条件",
        'WHERE sku_id = row["sku_ref"]',
        "直接拿 5 位数字 sku_ref 查 SKU- 前缀主键，120 条全部查不到；后续会被当作“(未找到)”而非规范处置。",
        "改为前缀映射，并对映射失败单独标记“库中缺失”。",
        "23",
    )
    add(
        "种子代码缺陷",
        "P0",
        "seed.py",
        "第 26–28 行",
        "TODO",
        "规则匹配 / 汇率换算 / 缺失处理均未实现",
        "核心合规逻辑全部缺失，风险固定输出“待补充”，不属于题目允许的三档风险。",
        "第二阶段按模块实现规则引擎、价格模块、报告模块，并加入测试。",
        "26-28",
    )
    add(
        "种子代码缺陷",
        "P0",
        "seed.py",
        "第 37–38 行",
        "异常处理",
        "except Exception: pass",
        "任何单条异常都被静默吞掉，记录不会进入 report，违反“不允许静默丢失”；数据库/表缺失时也会输出 0 条而不报错。",
        "移除裸 except；逐条捕获并记录到异常清单，最终校验读取数=输入数。",
        "37-38",
    )

    for i, item in enumerate(items, 1):
        item["序号"] = i
    return items


def build_dictionary_differences(csv_audit: dict, mapping: dict, rules: dict, rates: dict, sku_info: dict) -> list[dict]:
    return [
        {
            "表名": "sku_info",
            "字段或定义": "is_deleted",
            "数据字典描述": "未列出该字段",
            "数据库实际情况": "存在 is_deleted INTEGER NOT NULL DEFAULT 0；110 条=0，5 条=1",
            "差异类型": "遗漏",
            "开发影响": "无法从字典识别已下架商品，可能把 5 条已下架商品当作正常商品评估",
            "建议处理方式": "以 PRAGMA table_info 为准，使用 is_deleted=1 判定已下架",
        },
        {
            "表名": "sku_info",
            "字段或定义": "主键 / NOT NULL / 默认值 / 行数",
            "数据字典描述": "仅列字段与类型，未说明约束和行数",
            "数据库实际情况": f"sku_id 为主键；7 个原字段均 NOT NULL；共 {sku_info['row_count']} 行",
            "差异类型": "遗漏",
            "开发影响": "低；但会影响空值处理与数据量预期",
            "建议处理方式": "以 database_schema.json 为准，代码中显式校验行数与空值",
        },
        {
            "表名": "compliance_rules",
            "字段或定义": "risk_level / severity",
            "数据字典描述": "risk_level TEXT，风险等级（违规 / 待确认）",
            "数据库实际情况": "实际列名为 severity TEXT NOT NULL；10 条规则中 8 条待确认、2 条违规",
            "差异类型": "错误",
            "开发影响": "按字典写 SELECT risk_level 会直接报错，规则引擎无法运行",
            "建议处理方式": "统一使用 severity，并在代码中校验取值属于 {待确认, 违规}",
        },
        {
            "表名": "compliance_rules",
            "字段或定义": "主键 / NOT NULL / 空值表示",
            "数据字典描述": "未说明主键与 NOT NULL；空为不限",
            "数据库实际情况": "rule_id 为主键；title/applies_to_category/severity NOT NULL；material_keyword、origin_keyword 可空；实际 origin_keyword 有 8 条为空字符串（非 NULL）",
            "差异类型": "遗漏",
            "开发影响": "若只判断 IS NULL 会漏掉空字符串，导致“不限”条件判断错误",
            "建议处理方式": "用 strip 后为空判断“不限”，同时兼容 NULL 与空字符串",
        },
        {
            "表名": "exchange_rates",
            "字段或定义": "currency（人民币代码）",
            "数据字典描述": "人民币统一使用 RMB",
            "数据库实际情况": "实际为 CNY，rate_to_cny=1.0；无 RMB 行",
            "差异类型": "错误",
            "开发影响": "按字典查 RMB 会缺失人民币汇率，导致 CNY 商品无法换算或报错",
            "建议处理方式": "使用数据库实际值 CNY；汇率读取后校验商品币种全覆盖",
        },
        {
            "表名": "exchange_rates",
            "字段或定义": "主键 / NOT NULL / 行数",
            "数据字典描述": "仅列字段与类型",
            "数据库实际情况": f"currency 为主键、rate_to_cny NOT NULL；共 {rates['row_count']} 行（CNY/EUR/JPY/KRW/USD）",
            "差异类型": "遗漏",
            "开发影响": "低；但缺少汇率完整性校验依据",
            "建议处理方式": "以 schema JSON 为准，运行时检查商品币种是否全部有汇率",
        },
        {
            "表名": "sku_list.csv ↔ sku_info",
            "字段或定义": "sku_ref 与 sku_id 的关联方式",
            "数据字典描述": "sku_ref 即 sku_info.sku_id，按此关联即可",
            "数据库实际情况": f"直接相等匹配 0/{csv_audit['record_count']}；strip + SKU- 前缀匹配 {mapping['counts']['N_matched']}/{csv_audit['record_count']}，5 条缺失",
            "差异类型": "错误",
            "开发影响": "按字典实现会导致全部清单记录匹配失败，输出 120 条“未找到”",
            "建议处理方式": "实现前缀映射并保留原始 sku_ref；对 5 条缺失单独标注",
        },
        {
            "表名": "compliance_rules",
            "字段或定义": "风险等级取值分布",
            "数据字典描述": "风险等级（违规 / 待确认），未给出数量",
            "数据库实际情况": "8 条待确认、2 条违规；无其他取值",
            "差异类型": "一致（已核实）",
            "开发影响": "无；可作为风险优先级的合法输入集合",
            "建议处理方式": "代码中对未知 severity 直接报错，避免静默降级",
        },
        {
            "表名": "sku_info",
            "字段或定义": "category / currency 枚举",
            "数据字典描述": "8 个类目；CNY/USD/EUR/JPY/KRW",
            "数据库实际情况": f"8 个类目均出现；5 种币种均出现，共 {sku_info['row_count']} 行",
            "差异类型": "一致（已核实）",
            "开发影响": "无",
            "建议处理方式": "无需特殊处理；保留枚举校验",
        },
        {
            "表名": "全库",
            "字段或定义": "表数量与表名",
            "数据字典描述": "3 张表：sku_info、compliance_rules、exchange_rates",
            "数据库实际情况": "3 张表，名称一致；无视图",
            "差异类型": "一致（已核实）",
            "开发影响": "无",
            "建议处理方式": "无需处理",
        },
    ]


def build_findings(csv_audit: dict, mapping: dict, rules: dict, rates: dict, sku_info: dict) -> list[dict]:
    c = mapping["counts"]
    return [
        {
            "id": "P0-01",
            "severity": "P0",
            "title": "sku_ref 与 sku_id 格式不一致，直接相等匹配 0/120",
            "evidence": "CSV 全部为 5 位数字；数据库全部为 SKU- + 5 位数字；直接匹配 0，加前缀匹配 115。",
            "impact": "按字典/种子代码直接关联会导致所有商品匹配失败，报告全部变成“未找到”。",
        },
        {
            "id": "P0-02",
            "severity": "P0",
            "title": "数据字典遗漏 is_deleted，已下架状态无法从字典得知",
            "evidence": "sku_info 实际有 is_deleted INTEGER NOT NULL DEFAULT 0；110 条=0、5 条=1；字典 7 列中无此列。",
            "impact": "不处理该字段会把 5 条已下架商品当作正常在售并错误评估风险/价格。",
        },
        {
            "id": "P0-03",
            "severity": "P0",
            "title": "规则风险字段实际为 severity，字典写成 risk_level",
            "evidence": "PRAGMA table_info(compliance_rules) 显示 severity TEXT NOT NULL；字典写 risk_level。",
            "impact": "按字典编码会在查询时报 no such column，规则引擎无法运行。",
        },
        {
            "id": "P0-04",
            "severity": "P0",
            "title": "seed.py 核心逻辑未实现，且输出不符合三档风险要求",
            "evidence": "第 26–28 行三个 TODO；风险固定为“待补充”；无报告文件、无汇总、无完整性自检。",
            "impact": "原样运行无法满足任何一条验收标准，不能作为交付基础。",
        },
        {
            "id": "P0-05",
            "severity": "P0",
            "title": "seed.py 裸 except 静默吞掉单条失败",
            "evidence": "第 37–38 行 except Exception: pass；异常记录不会进入 report，最终只打印数量。",
            "impact": "输入记录可能静默丢失，违反“不允许静默丢失”的硬性验收条件。",
        },
        {
            "id": "P1-01",
            "severity": "P1",
            "title": "人民币币种代码字典写 RMB、实际为 CNY",
            "evidence": "exchange_rates 实际行：CNY 1.0、EUR 7.78、JPY 0.0476、KRW 0.0052、USD 7.12；无 RMB。",
            "impact": "按字典查 RMB 会缺少人民币汇率；CNY 商品可能无法换算。",
        },
        {
            "id": "P1-02",
            "severity": "P1",
            "title": "“在库数”口径存在歧义：115（可关联，含下架）还是 110（在售）",
            "evidence": f"清单 {c['N_input']} 条；可关联商品库 {c['N_matched']} 条（含已下架 {c['N_deleted']} 条）；实际评估 {c['N_evaluated']} 条。",
            "impact": "完整性自检五个数字若口径不一致，会被判定为对不上账；必须在报告中写清定义并同时展示等式。",
        },
        {
            "id": "P1-03",
            "severity": "P1",
            "title": "缺失/已下架商品在明细区的价格、风险、规则字段取值未定义",
            "evidence": "题目只规定缺失商品名称等填“—”，未规定原价/人民币价/风险标记/依据规则如何填；已下架商品是否展示价格也未说明。",
            "impact": "不同实现会给出不同表格，影响验收一致性；需在 README 中明确口径。",
        },
        {
            "id": "P1-04",
            "severity": "P1",
            "title": "金额舍入方式未明确",
            "evidence": "题目仅要求保留 2 位小数，未说明四舍五入或银行家舍入；浮点直接 round 还可能引入二进制误差。",
            "impact": "边界金额可能出现 0.01 差异；建议 Decimal + ROUND_HALF_UP 并写入 README。",
        },
        {
            "id": "P1-05",
            "severity": "P1",
            "title": "批次号重复容易被误判为 SKU 重复",
            "evidence": f"120 条记录中 sku_ref 无重复，但 9 个批次号重复、涉及 {csv_audit['batch_no']['duplicate_record_count']} 条记录。",
            "impact": "若按 batch_no 去重会丢失记录，破坏 120 条完整性；batch_no 仅记录用。",
        },
        {
            "id": "P2-01",
            "severity": "P2",
            "title": "数据字典未记录约束、默认值与真实行数",
            "evidence": "字典只列字段名和类型；实际有 PK、NOT NULL、DEFAULT 0，sku_info 115 行。",
            "impact": "文档可维护性差，空值与数据量假设容易出错。",
        },
        {
            "id": "P2-02",
            "severity": "P2",
            "title": "JPY/KRW 商品价格量级明显偏低，疑似合成数据或单位不匹配",
            "evidence": "KRW 原价 86.36–409.85，换算后约 0.45–2.13 CNY；JPY 原价 59.16–378.51，换算后约 2.82–18.02 CNY。",
            "impact": "不影响按公式换算，但报告金额的业务合理性存疑；需业务确认，不得自行改价。",
        },
        {
            "id": "P2-03",
            "severity": "P2",
            "title": "Top 风险类目的统计口径与展示数量未定义",
            "evidence": "题目只要求汇总区包含“Top 风险类目”，未说明按违规数、待确认数还是加权，也未说明 Top N。",
            "impact": "不同实现结果不可比；建议在 README 中固定口径（如按违规数降序、同分按待确认数、取前 5）。",
        },
    ]


OPEN_QUESTIONS = [
    {
        "id": "Q1",
        "question": "is_deleted=1 是否就是题目所说的“已下架”？",
        "why": "数据库无 CHECK 约束，数据字典未记录该字段；只能依据字段名、取值分布和题目语境推断。",
        "current_assumption": "高置信度按 1=已下架处理：110 条 0、5 条 1，且 5 条均在清单中。",
        "impact": "若语义不同，N_deleted/N_evaluated 会改变。",
    },
    {
        "id": "Q2",
        "question": "完整性自检中的“在库数”指可关联商品库的记录数（含已下架，115），还是在售可评估数（110）？",
        "why": "题目只给五个数字名称，未给公式；两种常见口径都能自洽，但数值不同。",
        "current_assumption": "采用审计定义：在库数=可关联记录数（含已下架）=115；另展示 N_evaluated=110 与两条等式。",
        "impact": "口径写错会被认为五个数字对不上账。",
    },
    {
        "id": "Q3",
        "question": "人民币价格保留两位小数时采用哪种舍入方式？",
        "why": "题目未说明；Python 内置 round 为银行家舍入，且浮点乘法可能有二进制误差。",
        "current_assumption": "建议 Decimal(str(price)) × Decimal(str(rate)) 后 ROUND_HALF_UP。",
        "impact": "仅影响边界分值，可能产生 0.01 差异。",
    },
    {
        "id": "Q4",
        "question": "缺失/已下架商品的原价、人民币价格、风险标记、依据规则应填什么？",
        "why": "题目只规定缺失商品的名称/类目/材质/产地填“—”，未规定其余列；已下架商品是否展示原价也未说明。",
        "current_assumption": "建议缺失商品全部商品字段与金额/风险/规则填“—”；已下架商品保留商品信息与原价币种，人民币价格/风险/规则填“—（不适用）”。",
        "impact": "影响明细表可读性与验收一致性。",
    },
    {
        "id": "Q5",
        "question": "Top 风险类目按什么指标排序、取前几名？",
        "why": "题目未定义排序指标与数量。",
        "current_assumption": "建议按违规数降序、违规数相同按待确认数降序、再按类目名排序，取前 5，并在报告注明口径。",
        "impact": "影响汇总区展示，不影响逐 SKU 判定。",
    },
    {
        "id": "Q6",
        "question": "JPY/KRW 商品价格量级是否为单位问题或合成数据？",
        "why": "换算后金额远低于同类商品，但题目只要求按数据库汇率换算，未给价格合理性标准。",
        "current_assumption": "按数据库原价与汇率照常换算，不修改数据；在踩坑说明中记录观察。",
        "impact": "不影响公式正确性，但影响报告金额的业务可信度。",
    },
]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    before = {name: file_meta(ROOT / name) for name in SOURCE_FILES}
    sidecars_before = sorted(p.name for p in ROOT.glob("compliance.db*") if p.name != "compliance.db")

    csv_audit = audit_csv()
    records = csv_audit.pop("_records")

    con = connect_ro(DB_PATH)
    try:
        db_audit = audit_database(con)
        sku_rows = get_sku_rows(con)
        sku_info = audit_sku_info(sku_rows)
        rules = audit_rules(con, sku_rows)
        rates = audit_rates(con, sku_rows)
    finally:
        con.close()

    mapping = audit_mapping(records, sku_rows)
    anomalies = build_anomalies(csv_audit, mapping, rules, rates, sku_info)
    dict_diffs = build_dictionary_differences(csv_audit, mapping, rules, rates, sku_info)
    findings = build_findings(csv_audit, mapping, rules, rates, sku_info)

    after = {name: file_meta(ROOT / name) for name in SOURCE_FILES}
    sidecars_after = sorted(p.name for p in ROOT.glob("compliance.db*") if p.name != "compliance.db")

    source_integrity = {
        "files": [
            {
                "path": name,
                "size": before[name]["size"],
                "sha256_before": before[name]["sha256"],
                "sha256_after": after[name]["sha256"],
                "unchanged": before[name]["sha256"] == after[name]["sha256"],
            }
            for name in SOURCE_FILES
        ],
        "sidecar_files_before": sidecars_before,
        "sidecar_files_after": sidecars_after,
        "all_unchanged": all(before[n]["sha256"] == after[n]["sha256"] for n in SOURCE_FILES),
        "database_opened_read_only": True,
    }

    anomaly_by_type = dict(sorted(Counter(a["异常类型"] for a in anomalies).items(), key=lambda kv: (-kv[1], kv[0])))
    anomaly_by_severity = dict(sorted(Counter(a["严重级别"] for a in anomalies).items()))

    counts = mapping["counts"]
    equations_hold = all(e["holds"] for e in mapping["equations"])

    summary = {
        "meta": {
            "audit_stage": "阶段一：数据审计与需求确认",
            "generated_at": now_iso(),
            "project_root": ROOT.as_posix(),
            "python_version": sys.version.split()[0],
            "sqlite_version": sqlite3.sqlite_version,
            "db_connection_mode": "read-only (URI ?mode=ro)",
            "third_party_dependencies": [],
        },
        "source_integrity": source_integrity,
        "verdict": {
            "csv_record_count_ok": csv_audit["record_count_matches_expected"],
            "csv_header_ok": csv_audit["header"] == ["sku_ref", "batch_no"],
            "sku_ref_unique": csv_audit["sku_ref"]["duplicate_group_count"] == 0,
            "db_integrity_ok": db_audit["integrity_check"] == "ok",
            "mapping_confirmed": mapping["chosen"]["matched_count"] == 115 and not mapping["chosen"]["duplicate_target_groups"],
            "completeness_equations_hold": equations_hold,
            "all_source_files_unchanged": source_integrity["all_unchanged"],
            "full_business_program_implemented": False,
        },
        "csv": csv_audit,
        "database": {
            "path": db_audit["path"],
            "size": db_audit["size"],
            "integrity_check": db_audit["integrity_check"],
            "encoding": db_audit["encoding"],
            "tables": [
                {
                    "name": t["name"],
                    "row_count": t["row_count"],
                    "primary_key": t["primary_key"],
                    "column_names": [c["name"] for c in t["columns"]],
                    "column_count": len(t["columns"]),
                    "pk_duplicate_groups": t["pk_duplicate_groups"],
                }
                for t in db_audit["tables"]
            ],
            "views": db_audit["views"],
        },
        "sku_info": sku_info,
        "sku_mapping": {
            "candidates": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "matched_count": c["matched_count"],
                    "unmatched_count": c["unmatched_count"],
                    "matched_sku_refs": c["matched_sku_refs"],
                    "unmatched_sku_refs": c["unmatched_sku_refs"],
                    "unmatched_records": c["unmatched_records"],
                    "duplicate_target_groups": c["duplicate_target_groups"],
                    "has_many_to_one": c["has_many_to_one"],
                }
                for c in mapping["candidates"]
            ],
            "chosen": mapping["chosen"],
            "chosen_method_evidence": mapping["chosen_method_evidence"],
            "missing_records": mapping["missing_records"],
            "deleted_records": mapping["deleted_records"],
            "evaluated_sku_refs": mapping["evaluated_sku_refs"],
        },
        "status_counts": {
            **counts,
            "definitions": {
                "N_input": "CSV 实际读取记录数（不含表头）",
                "N_matched": "清单中能够关联到商品库的记录数，包含已下架商品",
                "N_deleted": "清单中 is_deleted=1 的已下架商品数",
                "N_missing": "清单中未能关联到商品库的商品数",
                "N_evaluated": "清单中正常在售、可进入规则评估的商品数",
            },
        },
        "equations": mapping["equations"],
        "rules": rules,
        "rates": rates,
        "dictionary_differences": dict_diffs,
        "anomalies": {
            "total": len(anomalies),
            "by_type": anomaly_by_type,
            "by_severity": anomaly_by_severity,
            "items": anomalies,
        },
        "findings": findings,
        "open_questions": OPEN_QUESTIONS,
        "conclusion": {
            "summary": (
                f"清单 {counts['N_input']} 条；按 SKU- 前缀映射可关联 {counts['N_matched']} 条，"
                f"其中已下架 {counts['N_deleted']} 条、正常评估 {counts['N_evaluated']} 条，"
                f"库中缺失 {counts['N_missing']} 条；三条完整性等式全部成立。"
            ),
            "blocking_issues": [f["id"] for f in findings if f["severity"] == "P0"],
            "next_stage_ready": equations_hold and source_integrity["all_unchanged"],
        },
    }

    # 写入 database_schema.json
    schema_json = make_schema_json(db_audit)
    (AUDIT_DIR / "database_schema.json").write_text(
        json.dumps(schema_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 写入异常数据清单.csv（UTF-8 with BOM，便于 Excel 打开）
    anomaly_fields = [
        "序号",
        "异常类型",
        "严重级别",
        "来源文件或数据表",
        "行号/主键",
        "原始标识",
        "字段名",
        "原始值",
        "问题描述",
        "处理建议",
    ]
    with (AUDIT_DIR / "异常数据清单.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=anomaly_fields)
        writer.writeheader()
        for item in anomalies:
            writer.writerow({k: item.get(k, "") for k in anomaly_fields})

    # 写入逐行 SKU 映射/状态台账（审计用，不做规则判定）
    mapping_fields = [
        "行号",
        "sku_ref",
        "batch_no",
        "映射后sku_id",
        "是否匹配",
        "处置结果",
        "商品名",
        "类目",
        "材质",
        "产地",
        "原价",
        "币种",
        "is_deleted",
    ]
    with (AUDIT_DIR / "sku_mapping.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=mapping_fields)
        writer.writeheader()
        for rec in mapping["status_records"]:
            sku = rec["sku"]
            writer.writerow(
                {
                    "行号": rec["line"],
                    "sku_ref": rec["sku_ref"],
                    "batch_no": rec["batch_no"],
                    "映射后sku_id": rec["mapped_id"],
                    "是否匹配": "是" if sku is not None else "否",
                    "处置结果": rec["status"],
                    "商品名": sku["name"] if sku else "",
                    "类目": sku["category"] if sku else "",
                    "材质": sku["material"] if sku else "",
                    "产地": sku["origin"] if sku else "",
                    "原价": sku["price"] if sku else "",
                    "币种": sku["currency"] if sku else "",
                    "is_deleted": sku["is_deleted"] if sku else "",
                }
            )

    # 写入 07_数据审计汇总.json
    (AUDIT_DIR / "07_数据审计汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("审计完成")
    print(f"  CSV: {counts['N_input']} 条，字段 {csv_audit['header']}，BOM={csv_audit['has_bom']}")
    print(
        "  数据库: "
        + ", ".join(f"{t['name']}={t['row_count']}" for t in summary["database"]["tables"])
    )
    print(
        f"  映射: A 直接={mapping['candidates'][0]['matched_count']}，"
        f"B 前缀={mapping['candidates'][1]['matched_count']}，"
        f"缺失={counts['N_missing']}"
    )
    print(
        f"  状态: matched={counts['N_matched']}, deleted={counts['N_deleted']}, "
        f"missing={counts['N_missing']}, evaluated={counts['N_evaluated']}"
    )
    print(f"  等式: {'全部成立' if equations_hold else '存在不成立'}")
    print(f"  异常记录: {len(anomalies)} 条")
    print(f"  原始文件未修改: {source_integrity['all_unchanged']}")
    print("  输出: audit/database_schema.json, audit/异常数据清单.csv, audit/sku_mapping.csv, audit/07_数据审计汇总.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
