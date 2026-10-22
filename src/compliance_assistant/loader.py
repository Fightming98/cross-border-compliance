"""原始数据加载与结构校验。

本模块只负责把 CSV 和 SQLite 中的原始数据读成模型对象，并进行必要的
格式/取值校验；不执行规则匹配、不计算人民币价格、不生成报告。

数据库始终通过 SQLite 只读 URI（``?mode=ro``）打开，且不会创建文件。
"""

from __future__ import annotations

import csv
import re
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import (
    CsvLoadError,
    DatabaseDataError,
    DatabaseLoadError,
    DatabaseSchemaError,
)
from .models import ComplianceRule, Product, SkuInput

CSV_REQUIRED_COLUMNS: tuple[str, ...] = ("sku_ref", "batch_no")
SKU_REF_PATTERN = re.compile(r"\d{5}")

PRODUCT_COLUMNS: tuple[str, ...] = (
    "sku_id",
    "name",
    "category",
    "material",
    "origin",
    "price",
    "currency",
    "is_deleted",
)
RULE_COLUMNS: tuple[str, ...] = (
    "rule_id",
    "title",
    "applies_to_category",
    "material_keyword",
    "origin_keyword",
    "severity",
)
RATE_COLUMNS: tuple[str, ...] = ("currency", "rate_to_cny")


def _path(value: str | Path, label: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise DatabaseLoadError(f"{label}不存在或不是文件: {path}")
    return path


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """以只读方式打开 SQLite 数据库。

    先显式检查文件存在，避免 ``sqlite3.connect`` 在路径错误时自动创建空库。
    """

    path = _path(db_path, "数据库文件")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DatabaseLoadError(f"无法只读打开数据库 {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        connection.close()
        raise DatabaseLoadError(f"无法设置只读查询模式: {exc}") from exc
    return connection


def _require_table(
    connection: sqlite3.Connection,
    table: str,
    required_columns: tuple[str, ...],
) -> None:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if table not in tables:
        raise DatabaseSchemaError(
            f"数据库缺少必需的表 {table!r}；实际表: {sorted(tables)}"
        )

    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    missing = [name for name in required_columns if name not in columns]
    if missing:
        raise DatabaseSchemaError(
            f"表 {table!r} 缺少必需字段 {missing}；实际字段: {sorted(columns)}"
        )


def _text(
    value: object,
    *,
    field: str,
    record: str,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DatabaseDataError(
            f"{record} 的 {field} 不是文本，实际为 {type(value).__name__}"
        )
    if not value.strip():
        if allow_empty:
            return value
        raise DatabaseDataError(f"{record} 的 {field} 为空")
    if value != value.strip():
        raise DatabaseDataError(f"{record} 的 {field} 含前后空格: {value!r}")
    return value


def _decimal(
    value: object,
    *,
    field: str,
    record: str,
    positive: bool = True,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise DatabaseDataError(
            f"{record} 的 {field} 不是数值，实际为 {type(value).__name__}"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DatabaseDataError(
            f"{record} 的 {field} 无法转换为 Decimal: {value!r}"
        ) from exc
    if not number.is_finite():
        raise DatabaseDataError(f"{record} 的 {field} 不是有限数值: {value!r}")
    if positive and number <= 0:
        raise DatabaseDataError(f"{record} 的 {field} 必须大于 0，实际为 {value!r}")
    return number


def load_sku_inputs(csv_path: str | Path) -> list[SkuInput]:
    """读取并校验 `sku_list.csv`，保留全部行和原始值。"""

    path = Path(csv_path)
    if not path.is_file():
        raise CsvLoadError(f"CSV 文件不存在或不是文件: {path}")

    records: list[SkuInput] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, restkey="__extra__", restval=None)
            if reader.fieldnames is None:
                raise CsvLoadError(f"CSV 没有表头: {path}")

            missing = [
                column
                for column in CSV_REQUIRED_COLUMNS
                if column not in reader.fieldnames
            ]
            if missing:
                raise CsvLoadError(
                    f"CSV 缺少必需列 {missing}；实际表头: {reader.fieldnames}"
                )

            for row in reader:
                line_no = reader.line_num
                if "__extra__" in row:
                    raise CsvLoadError(
                        f"CSV 第 {line_no} 行存在多余字段: {row['__extra__']!r}"
                    )

                raw_sku_ref = row.get("sku_ref")
                raw_batch_no = row.get("batch_no")
                if raw_sku_ref is None or raw_batch_no is None:
                    raise CsvLoadError(
                        f"CSV 第 {line_no} 行缺少字段（必需列 {CSV_REQUIRED_COLUMNS}）"
                    )
                if not raw_sku_ref.strip() and not raw_batch_no.strip():
                    raise CsvLoadError(f"CSV 第 {line_no} 行为空行")
                if not raw_sku_ref.strip():
                    raise CsvLoadError(f"CSV 第 {line_no} 行的 sku_ref 为空")
                if not raw_batch_no.strip():
                    raise CsvLoadError(f"CSV 第 {line_no} 行的 batch_no 为空")

                normalized = raw_sku_ref.strip()
                if SKU_REF_PATTERN.fullmatch(normalized) is None:
                    raise CsvLoadError(
                        f"CSV 第 {line_no} 行的 sku_ref 不是五位数字: "
                        f"{raw_sku_ref!r}"
                    )

                records.append(
                    SkuInput(
                        sku_ref=raw_sku_ref,
                        batch_no=raw_batch_no,
                        line_no=line_no,
                    )
                )
    except CsvLoadError:
        raise
    except UnicodeDecodeError as exc:
        raise CsvLoadError(f"CSV 不是有效的 UTF-8 编码: {exc}") from exc
    except csv.Error as exc:
        raise CsvLoadError(f"CSV 解析失败: {exc}") from exc
    except OSError as exc:
        raise CsvLoadError(f"无法读取 CSV {path}: {exc}") from exc

    if not records:
        raise CsvLoadError(f"CSV 不含任何数据行: {path}")
    return records


def load_products(db_path: str | Path) -> dict[str, Product]:
    """读取 `sku_info`，按 `sku_id` 建立索引。"""

    products: dict[str, Product] = {}
    with closing(connect_readonly(db_path)) as connection:
        try:
            _require_table(connection, "sku_info", PRODUCT_COLUMNS)
            rows = connection.execute(
                "SELECT sku_id, name, category, material, origin, price, "
                "currency, is_deleted FROM sku_info ORDER BY sku_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseLoadError(f"读取 sku_info 失败: {exc}") from exc

    if not rows:
        raise DatabaseDataError("sku_info 表为空")

    for row in rows:
        sku_id = _text(row["sku_id"], field="sku_id", record="sku_info")
        record = f"sku_info[{sku_id}]"
        name = _text(row["name"], field="name", record=record)
        category = _text(row["category"], field="category", record=record)
        material = _text(row["material"], field="material", record=record)
        origin = _text(row["origin"], field="origin", record=record)
        currency = _text(row["currency"], field="currency", record=record)
        price = _decimal(row["price"], field="price", record=record)

        is_deleted = row["is_deleted"]
        if type(is_deleted) is not int or is_deleted not in (0, 1):
            raise DatabaseDataError(
                f"{record} 的 is_deleted 必须为整数 0 或 1，实际为 {is_deleted!r}"
            )
        if sku_id in products:
            raise DatabaseDataError(f"sku_info 主键重复: {sku_id}")

        products[sku_id] = Product(
            sku_id=sku_id,
            name=name,
            category=category,
            material=material,
            origin=origin,
            price=price,
            currency=currency,
            is_deleted=is_deleted,
        )

    return products


def load_rules(db_path: str | Path) -> list[ComplianceRule]:
    """读取全部合规规则。

    规则 token 与 severity 的合法性由规则引擎负责，这样规则引擎也可以对
    构造对象做纯单元测试。
    """

    with closing(connect_readonly(db_path)) as connection:
        try:
            _require_table(connection, "compliance_rules", RULE_COLUMNS)
            rows = connection.execute(
                "SELECT rule_id, title, applies_to_category, material_keyword, "
                "origin_keyword, severity FROM compliance_rules ORDER BY rule_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseLoadError(f"读取 compliance_rules 失败: {exc}") from exc

    rules: list[ComplianceRule] = []
    seen: set[str] = set()
    for row in rows:
        rule_id = _text(row["rule_id"], field="rule_id", record="compliance_rules")
        record = f"compliance_rules[{rule_id}]"
        title = _text(row["title"], field="title", record=record)
        categories = _text(
            row["applies_to_category"],
            field="applies_to_category",
            record=record,
        )
        severity = _text(row["severity"], field="severity", record=record)

        material = row["material_keyword"]
        if material is not None:
            material = _text(
                material,
                field="material_keyword",
                record=record,
                allow_empty=True,
            )
        origin = row["origin_keyword"]
        if origin is not None:
            origin = _text(
                origin,
                field="origin_keyword",
                record=record,
                allow_empty=True,
            )

        if rule_id in seen:
            raise DatabaseDataError(f"compliance_rules 主键重复: {rule_id}")
        seen.add(rule_id)

        rules.append(
            ComplianceRule(
                rule_id=rule_id,
                title=title,
                applies_to_category=categories,
                material_keyword=material,
                origin_keyword=origin,
                severity=severity,
            )
        )

    return rules


def load_exchange_rates(db_path: str | Path) -> dict[str, Decimal]:
    """读取 `exchange_rates`，返回“币种 -> 人民币汇率”的映射。"""

    with closing(connect_readonly(db_path)) as connection:
        try:
            _require_table(connection, "exchange_rates", RATE_COLUMNS)
            rows = connection.execute(
                "SELECT currency, rate_to_cny FROM exchange_rates ORDER BY currency"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseLoadError(f"读取 exchange_rates 失败: {exc}") from exc

    if not rows:
        raise DatabaseDataError("exchange_rates 表为空")

    rates: dict[str, Decimal] = {}
    for row in rows:
        currency = _text(
            row["currency"], field="currency", record="exchange_rates"
        )
        record = f"exchange_rates[{currency}]"
        if currency in rates:
            raise DatabaseDataError(f"exchange_rates 币种重复: {currency}")
        rates[currency] = _decimal(
            row["rate_to_cny"], field="rate_to_cny", record=record
        )

    return rates
