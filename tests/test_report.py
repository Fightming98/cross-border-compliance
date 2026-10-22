import re
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from src.compliance_assistant.errors import ReportError
from src.compliance_assistant.models import (
    Disposition,
    ProcessingResult,
    RiskLevel,
)
from src.compliance_assistant.report import (
    build_detail_section,
    generate_report,
    top_risk_categories,
    verify_report_file,
)
from tests.support import TempDirTestCase, build_small_results, make_input, make_product


class ReportGenerationTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        (
            self.results,
            self.inputs,
            self.products,
            self.rules,
            self.rates,
        ) = build_small_results()

    def test_report_contains_required_sections_and_rows(self):
        path = self.temp_path("nested") / "report.md"
        generate_report(self.results, path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 一、明细区", text)
        self.assertIn("## 二、汇总区", text)
        self.assertIn("## 三、完整性自检区", text)
        detail = text.split("## 一、明细区", 1)[1].split("## 二、汇总区", 1)[0]
        rows = re.findall(r"^\|\s*\d+\s*\|", detail, re.MULTILINE)
        self.assertEqual(3, len(rows))
        verify_report_file(path, expected_detail_rows=3)

    def test_missing_and_deleted_placeholders(self):
        path = self.temp_path("report.md")
        generate_report(self.results, path)
        text = path.read_text(encoding="utf-8")
        rows = {
            line.split("|")[2].strip(): line
            for line in text.splitlines()
            if line.startswith("| ") and line.split("|")[2].strip() in {"00001", "00002", "00003"}
        }
        self.assertIn("已下架", rows["00002"])
        self.assertIn("—（不适用）", rows["00002"])
        self.assertIn("库中缺失", rows["00003"])
        self.assertNotIn("合规", rows["00002"])
        self.assertIn("100.00", rows["00001"])

    def test_multiple_rule_ids_use_semicolon(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition == Disposition.NORMAL)
        index = results.index(normal)
        results[index] = replace(
            normal,
            risk=RiskLevel.VIOLATION,
            matched_rule_ids=("CR-001", "CR-004"),
        )
        path = self.temp_path("rules.md")
        generate_report(results, path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("CR-001;CR-004", text)

    def test_markdown_cell_escapes_pipes_and_newlines(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition == Disposition.NORMAL)
        self.assertIsNotNone(normal.product)
        damaged_product = replace(
            normal.product,
            name="测试|商品\n第二行\r第三行",
        )
        results[results.index(normal)] = replace(
            normal, product=damaged_product
        )
        detail = build_detail_section(results)
        row = next(line for line in detail.splitlines() if "00001" in line)
        self.assertIn("测试\\|商品 第二行 第三行", row)
        self.assertNotIn("\r", row)

    def test_currency_and_rule_ids_are_escaped(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition == Disposition.NORMAL)
        self.assertIsNotNone(normal.product)
        results[results.index(normal)] = replace(
            normal,
            product=replace(normal.product, currency="USD|X"),
            matched_rule_ids=("CR|001",),
        )
        detail = build_detail_section(results)
        row = next(line for line in detail.splitlines() if "00001" in line)
        self.assertIn("USD\\|X", row)
        self.assertIn("CR\\|001", row)

    def test_old_report_is_unchanged_when_new_validation_fails(self):
        path = self.temp_path("report.md")
        generate_report(self.results, path)
        old_text = path.read_text(encoding="utf-8")

        invalid = list(self.results)
        normal = next(r for r in invalid if r.disposition == Disposition.NORMAL)
        invalid[invalid.index(normal)] = replace(normal, risk=None)
        with self.assertRaises(ReportError):
            generate_report(invalid, path)

        self.assertEqual(old_text, path.read_text(encoding="utf-8"))

    def test_temp_file_is_removed_when_replace_fails(self):
        path = self.temp_path("report.md")
        tmp_path = path.with_name(path.name + ".tmp")
        with patch(
            "src.compliance_assistant.report.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(ReportError):
                generate_report(self.results, path)
        self.assertFalse(path.exists())
        self.assertFalse(tmp_path.exists())

    def test_temp_file_is_removed_when_write_fails(self):
        path = self.temp_path("report.md")
        tmp_path = path.with_name(path.name + ".tmp")
        with patch("pathlib.Path.write_text", side_effect=OSError("write failed")):
            with self.assertRaises(ReportError):
                generate_report(self.results, path)
        self.assertFalse(path.exists())
        self.assertFalse(tmp_path.exists())

    def test_post_write_verification_failure_removes_report(self):
        path = self.temp_path("report.md")
        with patch(
            "src.compliance_assistant.report.verify_report_file",
            side_effect=ReportError("post-write verify failed"),
        ):
            with self.assertRaises(ReportError):
                generate_report(self.results, path)
        self.assertFalse(path.exists())

    def test_missing_goods_are_not_dropped(self):
        detail = build_detail_section(self.results)
        self.assertEqual(3, sum(1 for line in detail.splitlines() if line.startswith("| ") and line.split("|")[1].strip().isdigit()))
        self.assertIn("00003", detail)

    def test_invalid_results_do_not_create_file(self):
        invalid = list(self.results)
        normal = next(r for r in invalid if r.disposition == Disposition.NORMAL)
        invalid[invalid.index(normal)] = replace(normal, risk=None)
        path = self.temp_path("invalid.md")
        with self.assertRaises(ReportError):
            generate_report(invalid, path)
        self.assertFalse(path.exists())

    def test_parent_directory_is_created(self):
        path = self.temp_path("a") / "b" / "c" / "report.md"
        generate_report(self.results, path)
        self.assertTrue(path.is_file())

    def test_verify_detects_wrong_row_count(self):
        path = self.temp_path("report.md")
        generate_report(self.results, path)
        with self.assertRaises(ReportError):
            verify_report_file(path, expected_detail_rows=120)

    def test_verify_detects_wrong_sku_order(self):
        path = self.temp_path("report.md")
        generate_report(self.results, path)
        with self.assertRaises(ReportError):
            verify_report_file(path, expected_sku_refs=["00003", "00002", "00001"])

    def test_no_risk_categories_message(self):
        path = self.temp_path("report.md")
        generate_report(self.results, path)
        self.assertIn("没有风险类目", path.read_text(encoding="utf-8"))


class TopRiskCategoryTests(unittest.TestCase):
    @staticmethod
    def _result(category, risk, index):
        product = make_product(sku_id=f"SKU-{index:05d}", category=category)
        return ProcessingResult(
            sku_input=make_input(f"{index:05d}", line_no=index + 1),
            mapped_sku_id=f"SKU-{index:05d}",
            disposition=Disposition.NORMAL,
            product=product,
            cny_price=Decimal("1.00"),
            risk=risk,
            matched_rule_ids=("CR-001",) if risk != RiskLevel.COMPLIANT else (),
        )

    def test_sorting_and_limit(self):
        results = [
            self._result("甲", RiskLevel.VIOLATION, 1),
            self._result("甲", RiskLevel.PENDING, 2),
            self._result("乙", RiskLevel.VIOLATION, 3),
            self._result("丙", RiskLevel.PENDING, 4),
            self._result("丙", RiskLevel.PENDING, 5),
            self._result("丁", RiskLevel.PENDING, 6),
            self._result("戊", RiskLevel.PENDING, 7),
            self._result("己", RiskLevel.COMPLIANT, 8),
        ]
        stats = top_risk_categories(results)
        self.assertEqual(["甲", "乙", "丙", "丁", "戊"], [s.category for s in stats])
        self.assertEqual((1, 1, 2), (stats[0].violations, stats[0].pending, stats[0].total))
        self.assertEqual(1, stats[1].violations)
        self.assertNotIn("己", [s.category for s in stats])

    def test_no_risk_categories(self):
        results = [self._result("甲", RiskLevel.COMPLIANT, 1)]
        self.assertEqual([], top_risk_categories(results))


if __name__ == "__main__":
    unittest.main()
