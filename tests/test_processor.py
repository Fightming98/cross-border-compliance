import unittest
from decimal import Decimal
from unittest.mock import patch

from src.compliance_assistant.errors import (
    InputDataError,
    ProcessingError,
    RuleConfigurationError,
)
from src.compliance_assistant.models import Disposition, RiskLevel, RuleEvaluation
from src.compliance_assistant.processor import map_sku_ref, process_sku_inputs
from tests.support import DEFAULT_RATES, make_input, make_product, make_rule


class MappingTests(unittest.TestCase):
    def test_valid_mapping(self):
        self.assertEqual("SKU-00001", map_sku_ref("00001"))
        self.assertEqual("SKU-00001", map_sku_ref(" 00001 "))

    def test_invalid_mapping_raises(self):
        for value in ("1", "0001", "000001", "ABCDE", "", "0000A"):
            with self.subTest(value=value):
                with self.assertRaises(InputDataError):
                    map_sku_ref(value)

    def test_non_string_mapping_raises(self):
        with self.assertRaises(InputDataError):
            map_sku_ref(None)  # type: ignore[arg-type]


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.rule = make_rule(categories="鞋靴", material="PU革")
        self.rules = [self.rule]
        self.rates = {"CNY": Decimal("1.0")}

    def test_missing_records_are_kept_and_do_not_call_evaluators(self):
        inputs = [make_input("00031", line_no=2), make_input("00057", line_no=3)]
        with patch(
            "src.compliance_assistant.processor.evaluate_product"
        ) as mock_evaluate, patch(
            "src.compliance_assistant.processor.convert_to_cny"
        ) as mock_price:
            results = process_sku_inputs(inputs, {}, self.rules, self.rates)

        self.assertEqual(2, len(results))
        self.assertTrue(all(r.disposition == Disposition.MISSING for r in results))
        self.assertTrue(all(r.product is None for r in results))
        self.assertTrue(all(r.cny_price is None for r in results))
        self.assertTrue(all(r.risk is None for r in results))
        self.assertTrue(all(r.matched_rule_ids == () for r in results))
        mock_evaluate.assert_not_called()
        mock_price.assert_not_called()

    def test_deleted_product_skips_rules_and_pricing(self):
        inputs = [make_input("00001")]
        products = {
            "SKU-00001": make_product(sku_id="SKU-00001", is_deleted=1)
        }
        with patch(
            "src.compliance_assistant.processor.evaluate_product"
        ) as mock_evaluate, patch(
            "src.compliance_assistant.processor.convert_to_cny"
        ) as mock_price:
            results = process_sku_inputs(inputs, products, self.rules, self.rates)

        result = results[0]
        self.assertEqual(Disposition.DELETED, result.disposition)
        self.assertIsNotNone(result.product)
        self.assertIsNone(result.cny_price)
        self.assertIsNone(result.risk)
        self.assertEqual((), result.matched_rule_ids)
        mock_evaluate.assert_not_called()
        mock_price.assert_not_called()

    def test_normal_product_calls_rules_and_pricing(self):
        inputs = [make_input("00001")]
        products = {
            "SKU-00001": make_product(sku_id="SKU-00001", is_deleted=0)
        }
        evaluation = RuleEvaluation(
            risk=RiskLevel.PENDING, matched_rule_ids=("CR-001",)
        )
        with patch(
            "src.compliance_assistant.processor.evaluate_product",
            return_value=evaluation,
        ) as mock_evaluate, patch(
            "src.compliance_assistant.processor.convert_to_cny",
            return_value=Decimal("100.00"),
        ) as mock_price:
            results = process_sku_inputs(inputs, products, self.rules, self.rates)

        result = results[0]
        self.assertEqual(Disposition.NORMAL, result.disposition)
        self.assertEqual(RiskLevel.PENDING, result.risk)
        self.assertEqual(("CR-001",), result.matched_rule_ids)
        self.assertEqual(Decimal("100.00"), result.cny_price)
        mock_evaluate.assert_called_once()
        mock_price.assert_called_once()

    def test_duplicate_batch_does_not_merge_inputs(self):
        inputs = [
            make_input("00002", "SAME", 2),
            make_input("00001", "SAME", 3),
        ]
        products = {
            "SKU-00001": make_product(sku_id="SKU-00001"),
            "SKU-00002": make_product(sku_id="SKU-00002"),
        }
        with patch(
            "src.compliance_assistant.processor.evaluate_product",
            return_value=RuleEvaluation(RiskLevel.COMPLIANT, ()),
        ), patch(
            "src.compliance_assistant.processor.convert_to_cny",
            return_value=Decimal("100.00"),
        ):
            results = process_sku_inputs(inputs, products, self.rules, self.rates)

        self.assertEqual(["00002", "00001"], [r.sku_input.sku_ref for r in results])
        self.assertEqual([2, 3], [r.sku_input.line_no for r in results])

    def test_preserves_input_order(self):
        inputs = [
            make_input("00003", line_no=2),
            make_input("00001", line_no=3),
            make_input("00002", line_no=4),
        ]
        products = {
            "SKU-00001": make_product(sku_id="SKU-00001"),
            "SKU-00002": make_product(sku_id="SKU-00002"),
        }
        with patch(
            "src.compliance_assistant.processor.evaluate_product",
            return_value=RuleEvaluation(RiskLevel.COMPLIANT, ()),
        ), patch(
            "src.compliance_assistant.processor.convert_to_cny",
            return_value=Decimal("100.00"),
        ):
            results = process_sku_inputs(inputs, products, self.rules, self.rates)
        self.assertEqual(
            ["00003", "00001", "00002"],
            [r.sku_input.sku_ref for r in results],
        )
        self.assertEqual(
            [Disposition.MISSING, Disposition.NORMAL, Disposition.NORMAL],
            [r.disposition for r in results],
        )

    def test_invalid_is_deleted_raises(self):
        inputs = [make_input("00001")]
        products = {"SKU-00001": make_product(is_deleted=2)}
        with self.assertRaises(ProcessingError):
            process_sku_inputs(inputs, products, self.rules, self.rates)

    def test_pricing_error_contains_sku_context(self):
        inputs = [make_input("00001")]
        products = {"SKU-00001": make_product(currency="RMB")}
        with self.assertRaises(ProcessingError) as context:
            process_sku_inputs(inputs, products, self.rules, self.rates)
        self.assertIn("00001", str(context.exception))
        self.assertIn("RMB", str(context.exception))

    def test_empty_rules_fail_even_without_normal_products(self):
        inputs = [make_input("00031")]
        with self.assertRaises(RuleConfigurationError):
            process_sku_inputs(inputs, {}, [], self.rates)


if __name__ == "__main__":
    unittest.main()
