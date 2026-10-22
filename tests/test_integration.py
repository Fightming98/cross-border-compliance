import contextlib
import hashlib
import io
import re
import unittest
from collections import Counter

from main import main as cli_main
from main import run
from src.compliance_assistant.loader import (
    load_exchange_rates,
    load_products,
    load_rules,
    load_sku_inputs,
)
from src.compliance_assistant.models import Disposition, RiskLevel
from src.compliance_assistant.processor import process_sku_inputs
from src.compliance_assistant.report import verify_report_file
from src.compliance_assistant.validation import validate_processing_results
from tests.support import (
    ORIGINAL_CSV,
    ORIGINAL_DB,
    PROJECT_ROOT,
    TempDirTestCase,
)

ORIGINAL_FILES = [
    PROJECT_ROOT / "instructions.md",
    PROJECT_ROOT / "data-dictionary.md",
    PROJECT_ROOT / "seed.py",
    ORIGINAL_CSV,
    ORIGINAL_DB,
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_detail_rows(report_text):
    detail = report_text.split("## 一、明细区", 1)[1].split(
        "## 二、汇总区", 1
    )[0]
    rows = []
    for line in detail.splitlines():
        if re.match(r"^\|\s*\d+\s*\|", line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows.append(cells)
    return rows


class IntegrationTests(TempDirTestCase):
    def test_full_pipeline_on_original_inputs(self):
        hashes_before = {path.name: sha256(path) for path in ORIGINAL_FILES}

        inputs = load_sku_inputs(ORIGINAL_CSV)
        products = load_products(ORIGINAL_DB)
        rules = load_rules(ORIGINAL_DB)
        rates = load_exchange_rates(ORIGINAL_DB)
        results = process_sku_inputs(inputs, products, rules, rates)
        stats = validate_processing_results(
            results,
            expected_input_count=120,
            exchange_rates=rates,
            rules=rules,
        )

        self.assertEqual((120, 115, 5, 5, 110), (
            stats.n_input,
            stats.n_matched,
            stats.n_deleted,
            stats.n_missing,
            stats.n_evaluated,
        ))
        self.assertTrue(stats.equations_hold)

        risk_counts = Counter(
            result.risk
            for result in results
            if result.disposition == Disposition.NORMAL
        )
        self.assertEqual(110, sum(risk_counts.values()))

        report_path = self.temp_path("report.md")
        with contextlib.redirect_stdout(io.StringIO()):
            generated = run(ORIGINAL_CSV, ORIGINAL_DB, report_path, 120)
        self.assertEqual(report_path, generated)
        verify_report_file(
            report_path,
            expected_detail_rows=120,
            expected_sku_refs=[item.sku_ref for item in inputs],
        )

        text = report_path.read_text(encoding="utf-8")
        rows = parse_detail_rows(text)
        self.assertEqual(120, len(rows))
        self.assertEqual(
            Counter(row[3] for row in rows),
            Counter(
                {
                    "正常评估": 110,
                    "已下架": 5,
                    "库中缺失": 5,
                }
            ),
        )
        self.assertEqual(
            {"00031", "00057", "00084", "00099", "00113"},
            {row[1] for row in rows if row[3] == "库中缺失"},
        )
        self.assertEqual(
            {"00001", "00002", "00003", "00004", "00005"},
            {row[1] for row in rows if row[3] == "已下架"},
        )

        # 报告汇总区必须与独立计算的三档数量一致。
        self.assertIn(
            f"| 合规 | {risk_counts[RiskLevel.COMPLIANT]} |", text
        )
        self.assertIn(
            f"| 待确认 | {risk_counts[RiskLevel.PENDING]} |", text
        )
        self.assertIn(
            f"| 违规 | {risk_counts[RiskLevel.VIOLATION]} |", text
        )
        self.assertIn("| 清单读取数 | 120 |", text)
        self.assertIn("| 在库数（含已下架） | 115 |", text)
        self.assertIn("| 已下架数 | 5 |", text)
        self.assertIn("| 缺失数 | 5 |", text)
        self.assertIn("| 实际评估数 | 110 |", text)
        self.assertIn("120 = 115 + 5，成立", text)
        self.assertIn("115 = 5 + 110，成立", text)
        self.assertIn("120 = 5 + 5 + 110，成立", text)

        # 原始输入必须保持不变，且数据库不能产生 sidecar 文件。
        hashes_after = {path.name: sha256(path) for path in ORIGINAL_FILES}
        self.assertEqual(hashes_before, hashes_after)
        sidecars = sorted(
            path.name
            for path in PROJECT_ROOT.iterdir()
            if path.name.startswith("compliance.db-")
        )
        self.assertEqual([], sidecars)

    def test_cli_entrypoint_creates_complete_report(self):
        path = self.temp_path("cli_report.md")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli_main(["--output", str(path)])
        self.assertEqual(0, code, msg=stderr.getvalue())
        self.assertTrue(path.is_file())
        verify_report_file(path, expected_detail_rows=120)
        self.assertIn("跨境商品合规检查完成", stdout.getvalue())
        self.assertIn("清单读取数: 120", stdout.getvalue())

    def test_report_is_deterministic(self):
        first = self.temp_path("first.md")
        second = self.temp_path("second.md")
        with contextlib.redirect_stdout(io.StringIO()):
            run(ORIGINAL_CSV, ORIGINAL_DB, first, 120)
            run(ORIGINAL_CSV, ORIGINAL_DB, second, 120)
        self.assertEqual(first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
