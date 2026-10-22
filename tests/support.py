"""测试辅助函数与临时数据构造。"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_CSV = PROJECT_ROOT / "sku_list.csv"
ORIGINAL_DB = PROJECT_ROOT / "compliance.db"

DEFAULT_RATES = {"CNY": Decimal("1.0")}


def make_product(
    *,
    sku_id: str = "SKU-00001",
    name: str = "测试商品",
    category: str = "上衣",
    material: str = "涤纶",
    origin: str = "中国",
    price: Decimal | str = Decimal("100.00"),
    currency: str = "CNY",
    is_deleted: int = 0,
):
    from src.compliance_assistant.models import Product

    return Product(
        sku_id=sku_id,
        name=name,
        category=category,
        material=material,
        origin=origin,
        price=Decimal(str(price)),
        currency=currency,
        is_deleted=is_deleted,
    )


def make_rule(
    *,
    rule_id: str = "CR-001",
    title: str = "测试规则",
    categories: str = "鞋靴",
    material: str | None = "PU革",
    origin: str | None = None,
    severity: str = "待确认",
):
    from src.compliance_assistant.models import ComplianceRule

    return ComplianceRule(
        rule_id=rule_id,
        title=title,
        applies_to_category=categories,
        material_keyword=material,
        origin_keyword=origin,
        severity=severity,
    )


def make_input(
    sku_ref: str = "00001",
    batch_no: str = "B2026001",
    line_no: int = 2,
):
    from src.compliance_assistant.models import SkuInput

    return SkuInput(sku_ref=sku_ref, batch_no=batch_no, line_no=line_no)


def write_csv(
    path: Path,
    rows: list[dict[str, str]],
    *,
    fieldnames: tuple[str, ...] = ("sku_ref", "batch_no"),
    encoding: str = "utf-8-sig",
) -> Path:
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)
    return path


def create_db(
    path: Path,
    *,
    products: list[dict[str, object]] | None = None,
    rules: list[dict[str, object]] | None = None,
    rates: list[dict[str, object]] | None = None,
) -> Path:
    """创建一个结构与真实数据库一致、内容可控的临时数据库。"""

    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE sku_info (
                sku_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                material TEXT NOT NULL,
                origin TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE compliance_rules (
                rule_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                applies_to_category TEXT NOT NULL,
                material_keyword TEXT,
                origin_keyword TEXT,
                severity TEXT NOT NULL
            );
            CREATE TABLE exchange_rates (
                currency TEXT PRIMARY KEY,
                rate_to_cny REAL NOT NULL
            );
            """
        )
        for product in products or []:
            connection.execute(
                "INSERT INTO sku_info VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    product.get("sku_id", "SKU-00001"),
                    product.get("name", "测试商品"),
                    product.get("category", "上衣"),
                    product.get("material", "涤纶"),
                    product.get("origin", "中国"),
                    float(product.get("price", 100.0)),
                    product.get("currency", "CNY"),
                    int(product.get("is_deleted", 0)),
                ),
            )
        for rule in rules or []:
            connection.execute(
                "INSERT INTO compliance_rules VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rule.get("rule_id", "CR-001"),
                    rule.get("title", "测试规则"),
                    rule.get("applies_to_category", "鞋靴"),
                    rule.get("material_keyword", "PU革"),
                    rule.get("origin_keyword"),
                    rule.get("severity", "待确认"),
                ),
            )
        for rate in rates or []:
            connection.execute(
                "INSERT INTO exchange_rates VALUES (?, ?)",
                (
                    rate.get("currency", "CNY"),
                    float(rate.get("rate_to_cny", 1.0)),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


class TempDirTestCase(unittest.TestCase):
    """为每个测试提供独立的临时目录。"""

    def setUp(self) -> None:
        super().setUp()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="compliance_test_"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def temp_path(self, name: str) -> Path:
        return self.temp_dir / name


def build_small_results():
    """构造一小批覆盖正常/下架/缺失三种状态的真实处理结果。"""

    from src.compliance_assistant.processor import process_sku_inputs

    inputs = [
        make_input("00001", "B2026001", 2),
        make_input("00002", "B2026001", 3),
        make_input("00003", "B2026002", 4),
    ]
    products = {
        "SKU-00001": make_product(sku_id="SKU-00001", is_deleted=0),
        "SKU-00002": make_product(sku_id="SKU-00002", is_deleted=1),
    }
    rules = [make_rule(categories="鞋靴", material="PU革", severity="待确认")]
    return (
        process_sku_inputs(inputs, products, rules, DEFAULT_RATES),
        inputs,
        products,
        rules,
        DEFAULT_RATES,
    )
