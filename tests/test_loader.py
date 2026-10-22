import hashlib
import sqlite3
import unittest
from decimal import Decimal

from src.compliance_assistant.errors import (
    CsvLoadError,
    DatabaseDataError,
    DatabaseLoadError,
    DatabaseSchemaError,
)
from src.compliance_assistant.loader import (
    connect_readonly,
    load_exchange_rates,
    load_products,
    load_rules,
    load_sku_inputs,
)
from tests.support import (
    ORIGINAL_CSV,
    ORIGINAL_DB,
    TempDirTestCase,
    create_db,
    write_csv,
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SkuInputLoaderTests(TempDirTestCase):
    def test_reads_bom_csv_and_preserves_values(self):
        path = write_csv(
            self.temp_path("sku.csv"),
            [
                {"sku_ref": "00001", "batch_no": "B1"},
                {"sku_ref": "00002", "batch_no": "B2"},
            ],
        )
        records = load_sku_inputs(path)
        self.assertEqual(2, len(records))
        self.assertEqual("00001", records[0].sku_ref)
        self.assertEqual("B1", records[0].batch_no)
        self.assertEqual(2, records[0].line_no)
        self.assertEqual(3, records[1].line_no)

    def test_duplicate_batch_is_not_deduplicated(self):
        path = write_csv(
            self.temp_path("sku.csv"),
            [
                {"sku_ref": "00001", "batch_no": "SAME"},
                {"sku_ref": "00002", "batch_no": "SAME"},
            ],
        )
        self.assertEqual(2, len(load_sku_inputs(path)))

    def test_missing_file(self):
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(self.temp_path("nope.csv"))

    def test_missing_required_column(self):
        path = self.temp_path("sku.csv")
        path.write_text("sku_ref\n00001\n", encoding="utf-8")
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(path)

    def test_empty_field(self):
        path = self.temp_path("sku.csv")
        path.write_text("sku_ref,batch_no\n00001,\n", encoding="utf-8-sig")
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(path)

    def test_empty_row(self):
        path = self.temp_path("sku.csv")
        path.write_text("sku_ref,batch_no\n,\n", encoding="utf-8-sig")
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(path)

    def test_extra_value_is_malformed(self):
        path = self.temp_path("sku.csv")
        path.write_text(
            "sku_ref,batch_no\n00001,B1,EXTRA\n", encoding="utf-8-sig"
        )
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(path)

    def test_invalid_sku_ref(self):
        for value in ("1234", "0001A", "000001", ""):
            with self.subTest(value=value):
                path = write_csv(
                    self.temp_path(f"{value or 'empty'}.csv"),
                    [{"sku_ref": value, "batch_no": "B1"}],
                )
                with self.assertRaises(CsvLoadError):
                    load_sku_inputs(path)

    def test_header_only_is_error(self):
        path = self.temp_path("sku.csv")
        path.write_text("sku_ref,batch_no\n", encoding="utf-8-sig")
        with self.assertRaises(CsvLoadError):
            load_sku_inputs(path)


class DatabaseLoaderTests(TempDirTestCase):
    def _valid_db(self):
        return create_db(
            self.temp_path("test.db"),
            products=[
                {
                    "sku_id": "SKU-00001",
                    "name": "测试商品",
                    "category": "上衣",
                    "material": "棉",
                    "origin": "中国",
                    "price": 100.5,
                    "currency": "CNY",
                    "is_deleted": 0,
                },
                {
                    "sku_id": "SKU-00002",
                    "name": "下架商品",
                    "category": "鞋靴",
                    "material": "PU革",
                    "origin": "越南",
                    "price": 20,
                    "currency": "USD",
                    "is_deleted": 1,
                },
            ],
            rules=[
                {
                    "rule_id": "CR-001",
                    "title": "测试规则",
                    "applies_to_category": "鞋靴",
                    "material_keyword": None,
                    "origin_keyword": "",
                    "severity": "待确认",
                }
            ],
            rates=[
                {"currency": "CNY", "rate_to_cny": 1.0},
                {"currency": "USD", "rate_to_cny": 7.12},
            ],
        )

    def test_load_products(self):
        products = load_products(self._valid_db())
        self.assertEqual(2, len(products))
        self.assertEqual(Decimal("100.5"), products["SKU-00001"].price)
        self.assertEqual(1, products["SKU-00002"].is_deleted)

    def test_load_rules_allows_null_and_empty(self):
        rules = load_rules(self._valid_db())
        self.assertEqual(1, len(rules))
        self.assertIsNone(rules[0].material_keyword)
        self.assertEqual("", rules[0].origin_keyword)

    def test_load_rates(self):
        rates = load_exchange_rates(self._valid_db())
        self.assertEqual(Decimal("7.12"), rates["USD"])

    def test_missing_database_file(self):
        with self.assertRaises(DatabaseLoadError):
            load_products(self.temp_path("missing.db"))

    def test_missing_table(self):
        path = self.temp_path("empty.db")
        sqlite3.connect(path).close()
        with self.assertRaises(DatabaseSchemaError):
            load_products(path)

    def test_missing_column(self):
        path = self.temp_path("bad.db")
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE sku_info (sku_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        with self.assertRaises(DatabaseSchemaError):
            load_products(path)

    def test_invalid_is_deleted(self):
        path = create_db(
            self.temp_path("bad_status.db"),
            products=[{"sku_id": "SKU-00001", "is_deleted": 2}],
        )
        with self.assertRaises(DatabaseDataError):
            load_products(path)

    def test_zero_price(self):
        path = create_db(
            self.temp_path("zero_price.db"),
            products=[{"sku_id": "SKU-00001", "price": 0}],
        )
        with self.assertRaises(DatabaseDataError):
            load_products(path)

    def test_empty_rates_table(self):
        path = create_db(self.temp_path("empty_rates.db"))
        with self.assertRaises(DatabaseDataError):
            load_exchange_rates(path)

    def test_connection_is_read_only(self):
        path = self._valid_db()
        before = sha256(path)
        connection = connect_readonly(path)
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO exchange_rates VALUES ('EUR', 7.78)"
            )
        connection.close()
        self.assertEqual(before, sha256(path))

    def test_original_inputs_unchanged(self):
        csv_before = sha256(ORIGINAL_CSV)
        db_before = sha256(ORIGINAL_DB)
        inputs = load_sku_inputs(ORIGINAL_CSV)
        products = load_products(ORIGINAL_DB)
        rules = load_rules(ORIGINAL_DB)
        rates = load_exchange_rates(ORIGINAL_DB)
        self.assertEqual(120, len(inputs))
        self.assertEqual(115, len(products))
        self.assertEqual(10, len(rules))
        self.assertEqual(5, len(rates))
        self.assertEqual(csv_before, sha256(ORIGINAL_CSV))
        self.assertEqual(db_before, sha256(ORIGINAL_DB))


if __name__ == "__main__":
    unittest.main()
