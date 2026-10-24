"""独立 HTML 报告：完整性、一致性、离线性与安全转义测试。"""

from __future__ import annotations

import html
import re
import tempfile
import unittest
from pathlib import Path

from src.compliance_assistant.application_service import (
    MISSING_PLACEHOLDER,
    NOT_APPLICABLE_PLACEHOLDER,
    build_result_payload,
    run_compliance_check,
)
from src.compliance_assistant.errors import ReportError
from src.compliance_assistant.html_report import (
    build_html_report,
    generate_html_report,
)
from src.compliance_assistant.models import Disposition
from tests.support import (
    ORIGINAL_CSV,
    ORIGINAL_DB,
    TempDirTestCase,
    create_db,
    write_csv,
)

DETAIL_ROW_RE = re.compile(
    r'<tr class="detail-row" data-sku-ref="([^"]*)">(.*?)</tr>', re.DOTALL
)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def cell_text(value: str) -> str:
    return html.unescape(TAG_RE.sub("", value)).strip()


def detail_rows(document: str) -> list[tuple[str, list[str]]]:
    return [
        (match.group(1), [cell_text(cell) for cell in CELL_RE.findall(match.group(2))])
        for match in DETAIL_ROW_RE.finditer(document)
    ]


class HtmlReportBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.outcome = run_compliance_check(
            ORIGINAL_CSV,
            ORIGINAL_DB,
            expected_input_count=120,
        )
        cls.document = build_html_report(cls.outcome, report_id="test-report-id")
        cls.payload = build_result_payload(cls.outcome, "a" * 32)

    def test_report_contains_header_and_all_sections(self) -> None:
        self.assertTrue(self.document.startswith("<!doctype html>"))
        for marker in (
            "跨境商品合规检查报告",
            "test-report-id",
            "已完成",
            "风险等级分布",
            "处置状态分布",
            "Top 风险类目",
            "商品明细",
            "完整性自检",
            "口径说明",
            "离线单文件报告",
        ):
            self.assertIn(marker, self.document)

    def test_all_detail_rows_in_original_order(self) -> None:
        rows = detail_rows(self.document)
        self.assertEqual(len(rows), 120)
        self.assertEqual(
            [sku_ref for sku_ref, _cells in rows],
            [result.sku_input.sku_ref for result in self.outcome.results],
        )
        self.assertEqual(self.document.count('class="detail-row"'), 120)

    def test_detail_values_match_service_results(self) -> None:
        rows = detail_rows(self.document)
        for (sku_ref, cells), detail, result in zip(
            rows,
            self.payload["details"],
            self.outcome.results,
            strict=True,
        ):
            self.assertEqual(len(cells), 12)
            self.assertEqual(sku_ref, detail["sku_ref"])
            self.assertEqual(cells[0], str(detail["index"]))
            self.assertEqual(cells[1], detail["sku_ref"])
            self.assertEqual(cells[2], detail["batch_no"])
            self.assertEqual(cells[3], detail["disposition"])
            if result.disposition == Disposition.MISSING:
                for index in range(4, 12):
                    self.assertEqual(cells[index], MISSING_PLACEHOLDER)
            elif result.disposition == Disposition.DELETED:
                assert result.product is not None
                self.assertEqual(cells[4], result.product.name)
                self.assertEqual(
                    cells[8], f"{result.product.price:.2f} {result.product.currency}"
                )
                self.assertEqual(cells[9], NOT_APPLICABLE_PLACEHOLDER)
                self.assertEqual(cells[10], NOT_APPLICABLE_PLACEHOLDER)
                self.assertEqual(cells[11], NOT_APPLICABLE_PLACEHOLDER)
            else:
                self.assertEqual(cells[9], detail["cny_price"])
                self.assertEqual(cells[10], detail["risk"])
                expected_rules = (
                    ";".join(detail["matched_rule_ids"]) or MISSING_PLACEHOLDER
                )
                self.assertEqual(cells[11], expected_rules)

    def test_risk_summary_matches_run(self) -> None:
        for label, count, percentage in (
            ("合规", 47, "42.7%"),
            ("待确认", 56, "50.9%"),
            ("违规", 7, "6.4%"),
        ):
            self.assertIn(
                f"<td>{label}</td><td>{count}</td><td>{percentage}</td>",
                self.document,
            )
        self.assertIn("风险商品占比 57.3%", self.document)
        self.assertIn("47 + 56 + 7 = 110", self.document)

    def test_top_categories_match_run(self) -> None:
        for item in self.outcome.top_categories:
            self.assertIn(
                f"<td>{html.escape(item.category, quote=True)}</td>"
                f"<td>{item.violations}</td><td>{item.pending}</td>"
                f"<td>{item.total}</td>",
                self.document,
            )

    def test_offline_single_file_has_no_external_resources(self) -> None:
        lowered = self.document.lower()
        for forbidden in (
            "<script src=",
            '<link rel="stylesheet"',
            "http://",
            "https://",
            "<iframe",
            "<img src=\"http",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("url(", lowered)
        self.assertNotIn("@import", lowered)
        self.assertIn("<style>", lowered)

    def test_no_unverified_javascript(self) -> None:
        lowered = self.document.lower()
        self.assertNotIn("<script", lowered)
        self.assertNotIn("javascript:", lowered)

    def test_build_is_deterministic_for_fixed_report_id(self) -> None:
        again = build_html_report(self.outcome, report_id="test-report-id")
        self.assertEqual(again, self.document)


class HtmlReportFileTest(TempDirTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.outcome = run_compliance_check(
            ORIGINAL_CSV,
            ORIGINAL_DB,
            expected_input_count=120,
        )

    def test_generate_re_reads_and_leaves_no_temp_file(self) -> None:
        output = self.temp_path("reports/report.html")
        path = generate_html_report(self.outcome, output, report_id="file-report")
        self.assertEqual(path, output)
        saved = path.read_text(encoding="utf-8")
        self.assertEqual(saved, build_html_report(self.outcome, report_id="file-report"))
        self.assertEqual(saved.count('class="detail-row"'), 120)
        self.assertFalse(output.with_name(output.name + ".tmp").exists())

    def test_generate_failure_is_explicit_and_cleans_temp(self) -> None:
        blocker = self.temp_path("not-a-directory")
        blocker.write_text("x", encoding="utf-8")
        output = blocker / "report.html"
        with self.assertRaises(ReportError):
            generate_html_report(self.outcome, output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name(output.name + ".tmp").exists())


class HtmlReportEscapingTest(TempDirTestCase):
    def test_external_text_is_escaped(self) -> None:
        db_path = create_db(
            self.temp_path("escape.db"),
            products=[
                {
                    "sku_id": "SKU-00001",
                    "name": "<script>alert('商品')</script>",
                    "category": "<b>服饰</b>",
                    "material": "棉\" onclick=\"alert(1)",
                    "origin": "<img src=x onerror=alert(2)>",
                    "price": 100.0,
                    "currency": "CNY",
                }
            ],
            rules=[
                {
                    "rule_id": "CR-XSS",
                    "title": "<em onmouseover=alert(3)>规则</em>",
                    "applies_to_category": "<b>服饰</b>",
                    "material_keyword": "棉",
                    "origin_keyword": None,
                    "severity": "违规",
                }
            ],
            rates=[{"currency": "CNY", "rate_to_cny": 1.0}],
        )
        csv_path = write_csv(
            self.temp_path("escape.csv"),
            [{"sku_ref": "00001", "batch_no": "<B&1>"}],
        )
        outcome = run_compliance_check(
            csv_path,
            db_path,
            expected_input_count=None,
            source_filename="<img src=x onerror=alert(9)>.csv",
        )
        document = build_html_report(outcome, report_id="<script>alert(8)</script>")
        self.assertNotIn("<script>alert('商品')</script>", document)
        self.assertNotIn("<img src=x onerror=alert(2)>", document)
        self.assertNotIn("<script>alert(8)</script>", document)
        self.assertNotIn("<img src=x onerror=alert(9)>", document)
        self.assertIn("&lt;script&gt;alert", document)
        self.assertIn("&lt;b&gt;服饰&lt;/b&gt;", document)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", document)
        self.assertIn("&lt;B&amp;1&gt;", document)
        self.assertIn("&lt;img src=x onerror=alert(9)&gt;.csv", document)
        self.assertIn("&lt;script&gt;alert(8)&lt;/script&gt;", document)
        self.assertEqual(document.count('class="detail-row"'), 1)


if __name__ == "__main__":
    unittest.main()
