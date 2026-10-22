"""第三阶段独立对账测试。

期望结果由测试侧直接读取原始 CSV / SQLite 后按题目契约重新实现，
不调用生产模块的规则引擎或价格模块；生产结果只作为被核对对象。
"""

import contextlib
import csv
import hashlib
import io
import json
import re
import sqlite3
import unittest
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from main import run
from src.compliance_assistant.loader import (
    load_exchange_rates,
    load_products,
    load_rules,
    load_sku_inputs,
)
from src.compliance_assistant.processor import process_sku_inputs
from src.compliance_assistant.report import top_risk_categories
from tests.support import (
    ORIGINAL_CSV,
    ORIGINAL_DB,
    PROJECT_ROOT,
    TempDirTestCase,
)

DISPOSITION_MISSING = "库中缺失"
DISPOSITION_DELETED = "已下架"
DISPOSITION_NORMAL = "正常评估"
RISK_COMPLIANT = "合规"
RISK_PENDING = "待确认"
RISK_VIOLATION = "违规"
SEVERITY_PRIORITY = {RISK_PENDING: 1, RISK_VIOLATION: 2}
AUDIT_SUMMARY = PROJECT_ROOT / "audit" / "07_数据审计汇总.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_reference():
    """直接读取原始材料，返回与生产模块无关的期望数据。"""

    with ORIGINAL_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append(
                {
                    "sku_ref": row["sku_ref"],
                    "batch_no": row["batch_no"],
                    "line_no": reader.line_num,
                }
            )

    connection = sqlite3.connect(
        ORIGINAL_DB.resolve().as_uri() + "?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        products = {
            row["sku_id"]: dict(row)
            for row in connection.execute(
                "SELECT sku_id, name, category, material, origin, price, "
                "currency, is_deleted FROM sku_info"
            )
        }
        rules = [
            dict(row)
            for row in connection.execute(
                "SELECT rule_id, title, applies_to_category, material_keyword, "
                "origin_keyword, severity FROM compliance_rules"
            )
        ]
        rates = {
            row["currency"]: row["rate_to_cny"]
            for row in connection.execute(
                "SELECT currency, rate_to_cny FROM exchange_rates"
            )
        }
    finally:
        connection.close()
    return rows, products, rules, rates


def _reference_matches(product, rule) -> bool:
    categories = [
        token.strip() for token in rule["applies_to_category"].split(",")
    ]
    if product["category"].strip() not in categories:
        return False

    material = rule["material_keyword"]
    if material is not None and material.strip():
        keywords = [token.strip() for token in material.split("|")]
        if not any(keyword in product["material"].strip() for keyword in keywords):
            return False

    origin = rule["origin_keyword"]
    if (
        origin is not None
        and origin.strip()
        and product["origin"].strip() != origin.strip()
    ):
        return False
    return True


def _reference_results(rows, products, rules, rates):
    expected = []
    for row in rows:
        ref = row["sku_ref"]
        mapped = "SKU-" + ref.strip()
        product = products.get(mapped)
        item = {
            "sku_ref": ref,
            "batch_no": row["batch_no"],
            "line_no": row["line_no"],
            "mapped_sku_id": mapped,
            "product": product,
            "disposition": None,
            "risk": None,
            "rule_ids": (),
            "cny_price": None,
        }

        if product is None:
            item["disposition"] = DISPOSITION_MISSING
        elif product["is_deleted"] == 1:
            item["disposition"] = DISPOSITION_DELETED
        elif product["is_deleted"] == 0:
            item["disposition"] = DISPOSITION_NORMAL
            hits = [
                rule for rule in rules if _reference_matches(product, rule)
            ]
            hits.sort(key=lambda rule: rule["rule_id"])
            item["rule_ids"] = tuple(rule["rule_id"] for rule in hits)
            if hits:
                item["risk"] = max(
                    (rule["severity"] for rule in hits),
                    key=lambda value: SEVERITY_PRIORITY[value],
                )
            else:
                item["risk"] = RISK_COMPLIANT
            amount = Decimal(str(product["price"]))
            rate = Decimal(str(rates[product["currency"]]))
            item["cny_price"] = (amount * rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            raise AssertionError(
                f"{mapped} 的 is_deleted 非 0/1: {product['is_deleted']!r}"
            )
        expected.append(item)
    return expected


def _reference_top(expected):
    counts = {}
    for item in expected:
        if item["disposition"] != DISPOSITION_NORMAL:
            continue
        if item["risk"] not in (RISK_VIOLATION, RISK_PENDING):
            continue
        category = item["product"]["category"]
        bucket = counts.setdefault(category, [0, 0])
        if item["risk"] == RISK_VIOLATION:
            bucket[0] += 1
        else:
            bucket[1] += 1
    ordered = sorted(
        counts.items(), key=lambda pair: (-pair[1][0], -pair[1][1], pair[0])
    )
    return [
        (category, values[0], values[1], values[0] + values[1])
        for category, values in ordered[:5]
    ]


def _parse_detail_rows(report_text):
    detail = report_text.split("## 一、明细区", 1)[1].split(
        "## 二、汇总区", 1
    )[0]
    rows = []
    for line in detail.splitlines():
        if re.match(r"^\|\s*\d+\s*\|", line):
            rows.append(
                [cell.strip() for cell in line.strip().strip("|").split("|")]
            )
    return rows


def _parse_top_rows(report_text):
    summary = report_text.split("### 2.2 Top 风险类目", 1)[1].split(
        "## 三、", 1
    )[0]
    rows = []
    for line in summary.splitlines():
        if re.match(r"^\|\s*[1-5]\s*\|", line):
            rows.append(
                [cell.strip() for cell in line.strip().strip("|").split("|")]
            )
    return rows


class IndependentReconciliationTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.rows, self.reference_products, self.reference_rules, self.reference_rates = (
            _load_reference()
        )
        self.expected = _reference_results(
            self.rows,
            self.reference_products,
            self.reference_rules,
            self.reference_rates,
        )
        self.inputs = load_sku_inputs(ORIGINAL_CSV)
        self.products = load_products(ORIGINAL_DB)
        self.rules = load_rules(ORIGINAL_DB)
        self.rates = load_exchange_rates(ORIGINAL_DB)
        self.actual = process_sku_inputs(
            self.inputs, self.products, self.rules, self.rates
        )

    def test_independent_per_sku_reconciliation(self):
        self.assertEqual(120, len(self.expected))
        self.assertEqual(120, len(self.actual))
        self.assertEqual(
            [item["sku_ref"] for item in self.expected],
            [result.sku_input.sku_ref for result in self.actual],
        )

        for expected, result in zip(self.expected, self.actual):
            with self.subTest(sku_ref=expected["sku_ref"]):
                self.assertEqual(expected["line_no"], result.sku_input.line_no)
                self.assertEqual(expected["batch_no"], result.sku_input.batch_no)
                self.assertEqual(expected["mapped_sku_id"], result.mapped_sku_id)
                self.assertEqual(expected["disposition"], result.disposition.value)
                self.assertEqual(
                    expected["rule_ids"], result.matched_rule_ids
                )
                self.assertEqual(
                    expected["risk"],
                    result.risk.value if result.risk is not None else None,
                )
                self.assertEqual(expected["cny_price"], result.cny_price)

                if expected["product"] is None:
                    self.assertIsNone(result.product)
                    continue

                self.assertIsNotNone(result.product)
                for field in (
                    "sku_id",
                    "name",
                    "category",
                    "material",
                    "origin",
                    "currency",
                    "is_deleted",
                ):
                    self.assertEqual(
                        expected["product"][field],
                        getattr(result.product, field),
                    )
                self.assertEqual(
                    Decimal(str(expected["product"]["price"])),
                    result.product.price,
                )

        dispositions = Counter(item["disposition"] for item in self.expected)
        self.assertEqual(
            {
                DISPOSITION_NORMAL: 110,
                DISPOSITION_DELETED: 5,
                DISPOSITION_MISSING: 5,
            },
            dispositions,
        )
        risks = Counter(
            item["risk"]
            for item in self.expected
            if item["disposition"] == DISPOSITION_NORMAL
        )
        self.assertEqual(
            {RISK_COMPLIANT: 47, RISK_PENDING: 56, RISK_VIOLATION: 7},
            risks,
        )

    def test_independent_top_categories(self):
        expected_top = _reference_top(self.expected)
        actual_top = [
            (item.category, item.violations, item.pending, item.total)
            for item in top_risk_categories(self.actual)
        ]
        self.assertEqual(
            [
                ("内衣", 4, 6, 10),
                ("箱包", 3, 18, 21),
                ("配饰", 0, 13, 13),
                ("鞋靴", 0, 11, 11),
                ("上衣", 0, 5, 5),
            ],
            expected_top,
        )
        self.assertEqual(expected_top, actual_top)

    def test_formal_report_matches_independent_expectation(self):
        report_path = self.temp_path("independent_report.md")
        with contextlib.redirect_stdout(io.StringIO()):
            run(ORIGINAL_CSV, ORIGINAL_DB, report_path, 120)

        text = report_path.read_text(encoding="utf-8")
        detail_rows = _parse_detail_rows(text)
        self.assertEqual(120, len(detail_rows))

        for expected, row in zip(self.expected, detail_rows):
            with self.subTest(sku_ref=expected["sku_ref"]):
                self.assertEqual(expected["sku_ref"], row[1])
                self.assertEqual(expected["batch_no"], row[2])
                self.assertEqual(expected["disposition"], row[3])
                product = expected["product"]
                if product is None:
                    self.assertEqual(["—"] * 8, row[4:12])
                    continue
                self.assertEqual(product["name"], row[4])
                self.assertEqual(product["category"], row[5])
                self.assertEqual(product["material"], row[6])
                self.assertEqual(product["origin"], row[7])
                self.assertEqual(
                    f"{Decimal(str(product['price'])):.2f} "
                    f"{product['currency']}",
                    row[8],
                )
                if expected["disposition"] == DISPOSITION_DELETED:
                    self.assertEqual(["—（不适用）"] * 3, row[9:12])
                else:
                    self.assertEqual(
                        f"{expected['cny_price']:.2f}", row[9]
                    )
                    self.assertEqual(expected["risk"], row[10])
                    self.assertEqual(
                        ";".join(expected["rule_ids"]) or "—", row[11]
                    )

        expected_top = _reference_top(self.expected)
        self.assertEqual(
            [
                [str(rank), category, str(violations), str(pending), str(total)]
                for rank, (category, violations, pending, total) in enumerate(
                    expected_top, start=1
                )
            ],
            _parse_top_rows(text),
        )
        self.assertIn("| 合规 | 47 |", text)
        self.assertIn("| 待确认 | 56 |", text)
        self.assertIn("| 违规 | 7 |", text)
        self.assertIn("120 = 115 + 5，成立", text)
        self.assertIn("115 = 5 + 110，成立", text)
        self.assertIn("120 = 5 + 5 + 110，成立", text)
        self.assertIn("47 + 56 + 7 = 110", text)


class OriginalIntegrityTests(unittest.TestCase):
    def test_original_files_match_audit_hashes(self):
        baseline = json.loads(AUDIT_SUMMARY.read_text(encoding="utf-8"))
        for item in baseline["source_integrity"]["files"]:
            path = PROJECT_ROOT / item["path"]
            with self.subTest(path=item["path"]):
                self.assertTrue(path.is_file())
                self.assertEqual(item["sha256_after"], _sha256(path))
        self.assertFalse(
            any(
                (PROJECT_ROOT / f"compliance.db-{suffix}").exists()
                for suffix in ("wal", "shm", "journal")
            )
        )


if __name__ == "__main__":
    unittest.main()
