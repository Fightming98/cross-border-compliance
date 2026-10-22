import unittest
from dataclasses import replace
from decimal import Decimal

from src.compliance_assistant.errors import ValidationError
from src.compliance_assistant.models import RiskLevel
from src.compliance_assistant.validation import (
    compute_stats,
    validate_database_snapshot,
    validate_processing_results,
    validate_sku_inputs,
)
from tests.support import (
    build_small_results,
    make_input,
    make_product,
)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        (
            self.results,
            self.inputs,
            self.products,
            self.rules,
            self.rates,
        ) = build_small_results()

    def _validate(self, results=None, **kwargs):
        return validate_processing_results(
            self.results if results is None else results,
            exchange_rates=self.rates,
            rules=self.rules,
            **kwargs,
        )

    def test_valid_results_pass_and_stats_are_dynamic(self):
        stats = self._validate(expected_input_count=3)
        self.assertEqual(3, stats.n_input)
        self.assertEqual(2, stats.n_matched)
        self.assertEqual(1, stats.n_deleted)
        self.assertEqual(1, stats.n_missing)
        self.assertEqual(1, stats.n_evaluated)
        self.assertTrue(stats.equations_hold)

    def test_expected_count_mismatch(self):
        with self.assertRaises(ValidationError):
            self._validate(expected_input_count=120)

    def test_empty_results(self):
        with self.assertRaises(ValidationError):
            self._validate([])

    def test_reversed_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._validate(list(reversed(self.results)))

    def test_missing_result_cannot_have_product(self):
        results = list(self.results)
        missing = next(r for r in results if r.disposition.value == "库中缺失")
        results[results.index(missing)] = replace(
            missing, product=make_product()
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_deleted_result_cannot_have_risk(self):
        results = list(self.results)
        deleted = next(r for r in results if r.disposition.value == "已下架")
        results[results.index(deleted)] = replace(
            deleted, risk=RiskLevel.PENDING
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_non_compliant_requires_rule_ids(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal, risk=RiskLevel.PENDING, matched_rule_ids=()
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_compliant_cannot_have_rule_ids(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal, matched_rule_ids=("CR-001",)
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_normal_requires_rule_evaluation(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(normal, risk=None)
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_normal_requires_two_decimal_price(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal, cny_price=Decimal("100.0")
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_price_is_recomputed_from_rates(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal, cny_price=Decimal("999.00")
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_rule_ids_are_recomputed(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal,
            risk=RiskLevel.PENDING,
            matched_rule_ids=("CR-999",),
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_rule_ids_must_be_sorted_and_unique(self):
        results = list(self.results)
        normal = next(r for r in results if r.disposition.value == "正常评估")
        results[results.index(normal)] = replace(
            normal,
            risk=RiskLevel.PENDING,
            matched_rule_ids=("CR-002", "CR-001"),
        )
        with self.assertRaises(ValidationError):
            self._validate(results)

    def test_expected_120_against_small_results_fails(self):
        with self.assertRaises(ValidationError):
            validate_processing_results(self.results, expected_input_count=120)


class InputValidationTests(unittest.TestCase):
    def test_valid_inputs(self):
        validate_sku_inputs(
            [
                make_input("00001", line_no=2),
                make_input("00002", line_no=3),
            ],
            expected_input_count=2,
        )

    def test_count_mismatch(self):
        with self.assertRaises(ValidationError):
            validate_sku_inputs(
                [make_input("00001", line_no=2)], expected_input_count=120
            )

    def test_invalid_sku_ref(self):
        with self.assertRaises(ValidationError):
            validate_sku_inputs([make_input("ABC", line_no=2)])

    def test_empty_batch(self):
        with self.assertRaises(ValidationError):
            validate_sku_inputs([make_input("00001", batch_no="", line_no=2)])

    def test_line_numbers_must_increase(self):
        with self.assertRaises(ValidationError):
            validate_sku_inputs(
                [
                    make_input("00002", line_no=3),
                    make_input("00001", line_no=2),
                ]
            )


class StatsAndSnapshotTests(unittest.TestCase):
    def test_compute_stats(self):
        results, *_ = build_small_results()
        stats = compute_stats(results)
        self.assertEqual((3, 2, 1, 1, 1), (
            stats.n_input,
            stats.n_matched,
            stats.n_deleted,
            stats.n_missing,
            stats.n_evaluated,
        ))

    def test_empty_database_snapshot(self):
        with self.assertRaises(ValidationError):
            validate_database_snapshot({}, [], {})


if __name__ == "__main__":
    unittest.main()
