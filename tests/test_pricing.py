import unittest
from decimal import Decimal

from src.compliance_assistant.errors import PricingError
from src.compliance_assistant.loader import load_exchange_rates
from src.compliance_assistant.pricing import convert_to_cny
from tests.support import ORIGINAL_DB


class PricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rates = load_exchange_rates(ORIGINAL_DB)

    def test_all_five_currencies_use_database_rates(self):
        cases = {
            "CNY": Decimal("100.00"),
            "EUR": Decimal("778.00"),
            "JPY": Decimal("4.76"),
            "KRW": Decimal("0.52"),
            "USD": Decimal("712.00"),
        }
        for currency, expected in cases.items():
            with self.subTest(currency=currency):
                result = convert_to_cny(
                    Decimal("100"), currency, self.rates
                )
                self.assertEqual(expected, result)
                self.assertEqual(-2, result.as_tuple().exponent)

    def test_cny_uses_database_row_not_hardcoded(self):
        result = convert_to_cny(
            Decimal("123.45"), "CNY", {"CNY": Decimal("1.0")}
        )
        self.assertEqual(Decimal("123.45"), result)

    def test_database_rate_is_authoritative(self):
        result = convert_to_cny(
            Decimal("100"), "USD", {"USD": Decimal("2.0")}
        )
        self.assertEqual(Decimal("200.00"), result)

    def test_missing_currency(self):
        with self.assertRaises(PricingError):
            convert_to_cny(Decimal("100"), "RMB", self.rates)

    def test_empty_currency(self):
        with self.assertRaises(PricingError):
            convert_to_cny(Decimal("100"), "   ", self.rates)

    def test_invalid_price_values(self):
        for value in (0, -1, "abc", None, True, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(PricingError):
                    convert_to_cny(value, "CNY", self.rates)

    def test_invalid_rate_values(self):
        for rate in (0, -1, "abc", Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(rate=rate):
                with self.assertRaises(PricingError):
                    convert_to_cny(
                        Decimal("100"), "USD", {"USD": rate}
                    )

    def test_half_up_rounding(self):
        rates = {"CNY": Decimal("1.0")}
        self.assertEqual(
            Decimal("1.01"), convert_to_cny(Decimal("1.005"), "CNY", rates)
        )
        self.assertEqual(
            Decimal("1.00"), convert_to_cny(Decimal("1.004"), "CNY", rates)
        )
        self.assertEqual(
            Decimal("0.01"), convert_to_cny(Decimal("0.005"), "CNY", rates)
        )

    def test_input_price_is_not_mutated(self):
        price = Decimal("99.99")
        before = price
        convert_to_cny(price, "USD", self.rates)
        self.assertEqual(before, price)

    def test_string_decimal_input(self):
        self.assertEqual(
            Decimal("7.78"),
            convert_to_cny("1.00", "EUR", self.rates),
        )

    def test_extreme_amount_and_rate_raise_pricing_error(self):
        rates = {"CNY": Decimal("1.0")}
        for price, exchange_rates in (
            (Decimal("1e1000000"), rates),
            (Decimal("1"), {"CNY": Decimal("1e1000000")}),
        ):
            with self.subTest(price=price, rates=exchange_rates):
                with self.assertRaises(PricingError):
                    convert_to_cny(price, "CNY", exchange_rates)


if __name__ == "__main__":
    unittest.main()
