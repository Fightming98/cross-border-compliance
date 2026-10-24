"""应用服务层：统一结果、基线与 CSV 导出的一致性测试。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.compliance_assistant.application_service import (
    MISSING_PLACEHOLDER,
    NOT_APPLICABLE_PLACEHOLDER,
    build_details_csv,
    build_result_payload,
    run_compliance_check,
    write_details_csv,
)
from src.compliance_assistant.errors import ComplianceError
from src.compliance_assistant.models import Disposition, RiskLevel
from tests.support import ORIGINAL_CSV, ORIGINAL_DB, TempDirTestCase, write_csv

JOB_ID = "0123456789abcdef0123456789abcdef"


class ApplicationServiceBaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.outcome = run_compliance_check(
            ORIGINAL_CSV,
            ORIGINAL_DB,
            expected_input_count=120,
        )
        cls.payload = build_result_payload(cls.outcome, JOB_ID)

    def test_baseline_counts_and_equations(self) -> None:
        stats = self.outcome.stats
        self.assertEqual(
            (
                stats.n_input,
                stats.n_matched,
                stats.n_deleted,
                stats.n_missing,
                stats.n_evaluated,
            ),
            (120, 115, 5, 5, 110),
        )
        self.assertEqual(
            (
                self.outcome.risk_counts[RiskLevel.COMPLIANT],
                self.outcome.risk_counts[RiskLevel.PENDING],
                self.outcome.risk_counts[RiskLevel.VIOLATION],
            ),
            (47, 56, 7),
        )
        self.assertTrue(stats.equations_hold)
        self.assertEqual(sum(self.outcome.risk_counts.values()), stats.n_evaluated)
        self.assertEqual(len(self.outcome.results), 120)

    def test_disposition_counts_and_risk_scope(self) -> None:
        self.assertEqual(
            {
                disposition: self.outcome.disposition_counts[disposition]
                for disposition in Disposition
            },
            {
                Disposition.NORMAL: 110,
                Disposition.DELETED: 5,
                Disposition.MISSING: 5,
            },
        )
        for result in self.outcome.results:
            if result.disposition != Disposition.NORMAL:
                self.assertIsNone(result.risk)
        self.assertEqual(
            sum(
                1
                for result in self.outcome.results
                if result.disposition == Disposition.NORMAL
            ),
            self.outcome.stats.n_evaluated,
        )

    def test_source_identity_and_timestamp(self) -> None:
        source = self.payload["source"]
        self.assertEqual(source["filename"], ORIGINAL_CSV.name)
        self.assertEqual(source["size_bytes"], ORIGINAL_CSV.stat().st_size)
        self.assertEqual(
            source["sha256"],
            hashlib.sha256(ORIGINAL_CSV.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.payload["job_id"], JOB_ID)
        self.assertEqual(self.payload["status"], "completed")
        self.assertIsInstance(self.payload["generated_at"], str)

    def test_payload_details_match_results_in_order(self) -> None:
        details = self.payload["details"]
        self.assertEqual(len(details), len(self.outcome.results))
        for detail, result in zip(details, self.outcome.results, strict=True):
            self.assertEqual(detail["sku_ref"], result.sku_input.sku_ref)
            self.assertEqual(detail["batch_no"], result.sku_input.batch_no)
            self.assertEqual(detail["line_no"], result.sku_input.line_no)
            self.assertEqual(detail["mapped_sku_id"], result.mapped_sku_id)
            self.assertEqual(detail["disposition"], result.disposition.value)
            self.assertEqual(detail["risk"], result.risk.value if result.risk else None)
            self.assertEqual(detail["matched_rule_ids"], list(result.matched_rule_ids))
            if result.cny_price is None:
                self.assertIsNone(detail["cny_price"])
            else:
                self.assertEqual(detail["cny_price"], f"{result.cny_price:.2f}")
            if result.product is None or result.disposition == Disposition.MISSING:
                self.assertIsNone(detail["name"])
                self.assertIsNone(detail["original_price"])
            else:
                self.assertEqual(detail["name"], result.product.name)
                self.assertEqual(
                    detail["original_price"], f"{result.product.price:.2f}"
                )

    def test_payload_integrity_and_top_categories(self) -> None:
        integrity = self.payload["integrity"]
        self.assertTrue(integrity["all_hold"])
        self.assertEqual(len(integrity["equations"]), 3)
        self.assertTrue(all(row["holds"] for row in integrity["equations"]))
        self.assertTrue(integrity["risk_equation"]["holds"])
        self.assertEqual(
            self.payload["top_categories"],
            [
                {
                    "category": item.category,
                    "violations": item.violations,
                    "pending": item.pending,
                    "total": item.total,
                }
                for item in self.outcome.top_categories
            ],
        )
        categories = self.payload["top_categories"]
        self.assertLessEqual(len(categories), 5)
        self.assertEqual(
            categories,
            sorted(
                categories,
                key=lambda item: (
                    -item["violations"],
                    -item["pending"],
                    item["category"],
                ),
            ),
        )

    def test_payload_is_json_serializable(self) -> None:
        encoded = json.dumps(self.payload, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(len(decoded["details"]), 120)
        self.assertEqual(decoded["summary"]["risk_counts"]["违规"], 7)

    def test_csv_export_has_bom_and_all_rows(self) -> None:
        text = build_details_csv(self.outcome)
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(len(rows), 121)
        self.assertEqual(len(rows[0]), 15)
        self.assertEqual(rows[0][0], "序号")
        self.assertEqual(rows[0][-1], "命中规则编号")
        details = self.payload["details"]
        for index, (row, detail, result) in enumerate(
            zip(rows[1:], details, self.outcome.results, strict=True),
            start=1,
        ):
            self.assertEqual(row[0], str(index))
            self.assertEqual(row[1], detail["sku_ref"])
            self.assertEqual(row[2], detail["batch_no"])
            self.assertEqual(row[5], detail["disposition"])
            self.assertEqual(
                row[6], detail["name"] if detail["name"] is not None else MISSING_PLACEHOLDER
            )
            if detail["cny_price"] is not None:
                self.assertEqual(row[12], detail["cny_price"])
            else:
                expected = (
                    NOT_APPLICABLE_PLACEHOLDER
                    if result.disposition == Disposition.DELETED
                    else MISSING_PLACEHOLDER
                )
                self.assertEqual(row[12], expected)
            expected_rules = (
                ";".join(detail["matched_rule_ids"])
                if detail["matched_rule_ids"]
                else (
                    NOT_APPLICABLE_PLACEHOLDER
                    if result.disposition == Disposition.DELETED
                    else MISSING_PLACEHOLDER
                )
            )
            self.assertEqual(row[14], expected_rules)

    def test_write_csv_is_atomic_and_has_no_temp_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "details.csv"
            path = write_details_csv(self.outcome, output)
            self.assertEqual(path, output)
            self.assertTrue(path.is_file())
            self.assertFalse(output.with_name(output.name + ".tmp").exists())
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows), 121)


class ApplicationServiceInputTest(TempDirTestCase):
    def _subset_csv(self, count: int = 2) -> Path:
        with ORIGINAL_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for _index, row in zip(range(count), csv.DictReader(handle))]
        return write_csv(self.temp_path("subset.csv"), rows)

    def test_web_mode_accepts_subset_without_expected_count(self) -> None:
        subset = self._subset_csv(2)
        outcome = run_compliance_check(
            subset,
            ORIGINAL_DB,
            expected_input_count=None,
            source_filename="子集.csv",
        )
        self.assertEqual(outcome.stats.n_input, 2)
        self.assertEqual(outcome.source_filename, "子集.csv")
        self.assertEqual(
            outcome.source_sha256,
            hashlib.sha256(subset.read_bytes()).hexdigest(),
        )

    def test_cli_expected_count_still_enforced(self) -> None:
        subset = self._subset_csv(2)
        with self.assertRaises(ComplianceError):
            run_compliance_check(subset, ORIGINAL_DB, expected_input_count=120)

    def test_generated_at_override_is_preserved(self) -> None:
        subset = self._subset_csv(1)
        timestamp = datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc)
        outcome = run_compliance_check(
            subset,
            ORIGINAL_DB,
            expected_input_count=None,
            generated_at=timestamp,
        )
        self.assertEqual(outcome.generated_at, timestamp)
        self.assertEqual(
            build_result_payload(outcome, JOB_ID)["generated_at"],
            "2026-10-24T09:30:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
