import unittest

from src.compliance_assistant.errors import RuleConfigurationError
from src.compliance_assistant.loader import load_rules
from src.compliance_assistant.models import RiskLevel
from src.compliance_assistant.rules_engine import (
    category_matches,
    evaluate_product,
    evaluate_risk,
    match_rules,
    material_matches,
    origin_matches,
)
from tests.support import ORIGINAL_DB, make_product, make_rule


class RuleConditionTests(unittest.TestCase):
    def test_category_exact_member(self):
        rule = make_rule(categories="鞋靴,箱包,配饰")
        self.assertTrue(category_matches(make_product(category="箱包"), rule))

    def test_category_does_not_use_substring(self):
        rule = make_rule(categories="鞋靴")
        self.assertFalse(category_matches(make_product(category="靴"), rule))
        self.assertFalse(category_matches(make_product(category="鞋"), rule))

    def test_material_substring_and_any_keyword(self):
        rule = make_rule(material="羊毛|羊绒")
        self.assertTrue(material_matches(make_product(material="羊绒"), rule))
        self.assertTrue(material_matches(make_product(material="羊毛混纺"), rule))
        self.assertFalse(material_matches(make_product(material="棉"), rule))

    def test_material_empty_means_unrestricted(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                rule = make_rule(material=value)
                self.assertTrue(material_matches(make_product(material="任意"), rule))

    def test_material_does_not_confuse_similar_names(self):
        rule = make_rule(material="棉")
        self.assertFalse(material_matches(make_product(material="锦纶"), rule))
        self.assertFalse(material_matches(make_product(material="涤纶"), rule))

    def test_origin_exact(self):
        rule = make_rule(origin="意大利")
        self.assertTrue(origin_matches(make_product(origin="意大利"), rule))
        self.assertFalse(origin_matches(make_product(origin="意大"), rule))
        self.assertFalse(origin_matches(make_product(origin="越南"), rule))

    def test_origin_empty_means_unrestricted(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                rule = make_rule(origin=value)
                self.assertTrue(origin_matches(make_product(origin="任意"), rule))


class RuleEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_rules = load_rules(ORIGINAL_DB)

    def test_business_combinations_from_real_rules(self):
        cases = [
            ("箱包", "真皮", "意大利", RiskLevel.VIOLATION, ("CR-001", "CR-004")),
            ("箱包", "真皮", "越南", RiskLevel.PENDING, ("CR-001",)),
            ("鞋靴", "PU革", "越南", RiskLevel.PENDING, ("CR-002", "CR-008")),
            ("鞋靴", "PU革", "意大利", RiskLevel.PENDING, ("CR-002",)),
            ("鞋靴", "牛皮", "中国", RiskLevel.PENDING, ("CR-001", "CR-010")),
            ("内衣", "再生涤纶", "中国", RiskLevel.VIOLATION, ("CR-007",)),
            ("内衣", "棉", "中国", RiskLevel.PENDING, ("CR-006",)),
            ("上衣", "涤纶", "中国", RiskLevel.COMPLIANT, ()),
        ]
        for category, material, origin, risk, rule_ids in cases:
            with self.subTest(category=category, material=material, origin=origin):
                product = make_product(
                    category=category, material=material, origin=origin
                )
                result = evaluate_product(product, self.real_rules)
                self.assertEqual(risk, result.risk)
                self.assertEqual(rule_ids, result.matched_rule_ids)

    def test_multiple_hits_keep_all_ids_sorted(self):
        product = make_product(category="配饰", material="合金", origin="中国")
        result = evaluate_product(product, self.real_rules)
        self.assertEqual(RiskLevel.PENDING, result.risk)
        self.assertEqual(("CR-005", "CR-009"), result.matched_rule_ids)
        self.assertEqual(tuple(sorted(result.matched_rule_ids)), result.matched_rule_ids)

    def test_violation_wins_over_pending(self):
        product = make_product(category="箱包", material="真皮", origin="意大利")
        result = evaluate_product(product, self.real_rules)
        self.assertEqual(RiskLevel.VIOLATION, result.risk)
        self.assertIn("CR-001", result.matched_rule_ids)
        self.assertIn("CR-004", result.matched_rule_ids)

    def test_no_hit_is_compliant(self):
        product = make_product(category="上衣", material="涤纶", origin="中国")
        result = evaluate_product(product, self.real_rules)
        self.assertEqual(RiskLevel.COMPLIANT, result.risk)
        self.assertEqual((), result.matched_rule_ids)

    def test_match_rules_returns_all_matches(self):
        rules = [
            make_rule(rule_id="CR-002", categories="箱包", material="PU革"),
            make_rule(rule_id="CR-001", categories="箱包", material="PU革"),
        ]
        matched = match_rules(make_product(category="箱包", material="PU革"), rules)
        self.assertEqual(("CR-001", "CR-002"), tuple(r.rule_id for r in matched))

    def test_evaluate_risk_empty_is_compliant(self):
        self.assertEqual(RiskLevel.COMPLIANT, evaluate_risk([]))


class RuleConfigurationErrorTests(unittest.TestCase):
    def test_unknown_severity(self):
        product = make_product(category="鞋靴", material="PU革")
        rule = make_rule(severity="高风险")
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(product, [rule])

    def test_unknown_severity_on_non_matching_rule_still_fails(self):
        product = make_product(category="上衣", material="涤纶")
        rule = make_rule(categories="鞋靴", severity="高风险")
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(product, [rule])

    def test_rule_severity_cannot_be_compliant(self):
        product = make_product(category="鞋靴", material="PU革")
        rule = make_rule(severity="合规")
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(product, [rule])

    def test_empty_rule_table(self):
        product = make_product()
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(product, [])

    def test_empty_category(self):
        product = make_product()
        for categories in ("", "   "):
            with self.subTest(categories=categories):
                with self.assertRaises(RuleConfigurationError):
                    evaluate_product(product, [make_rule(categories=categories)])

    def test_empty_category_token(self):
        product = make_product()
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(product, [make_rule(categories="鞋靴,,箱包")])

    def test_material_without_valid_token(self):
        product = make_product()
        for material in ("|", "||", "真皮||牛皮"):
            with self.subTest(material=material):
                with self.assertRaises(RuleConfigurationError):
                    evaluate_product(product, [make_rule(material=material)])

    def test_duplicate_rule_id(self):
        product = make_product()
        with self.assertRaises(RuleConfigurationError):
            evaluate_product(
                product,
                [make_rule(rule_id="CR-001"), make_rule(rule_id="CR-001")],
            )


if __name__ == "__main__":
    unittest.main()
