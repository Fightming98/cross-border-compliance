# compliance.db 数据字典

数据库共 3 张表：`sku_info`（商品主数据）、`compliance_rules`（合规规则库）、`exchange_rates`（汇率表）。

## 1. sku_info（商品主数据）

| 列名 | 类型 | 说明 |
|------|------|------|
| sku_id | TEXT | SKU 唯一标识，格式 `SKU-xxxxx` |
| name | TEXT | 商品名称 |
| category | TEXT | 商品类目（上衣 / 裤装 / 裙装 / 针织衫 / 鞋靴 / 箱包 / 配饰 / 内衣） |
| material | TEXT | 主要材质 |
| origin | TEXT | 产地 |
| price | REAL | 产地标价 |
| currency | TEXT | 标价币种（CNY / USD / EUR / JPY / KRW） |

## 2. compliance_rules（合规规则库）

| 列名 | 类型 | 说明 |
|------|------|------|
| rule_id | TEXT | 规则编号，如 `CR-001` |
| title | TEXT | 规则说明 |
| applies_to_category | TEXT | 适用类目，逗号分隔 |
| material_keyword | TEXT | 材质关键词，竖线分隔多个，空为不限 |
| origin_keyword | TEXT | 产地关键词，空为不限 |
| risk_level | TEXT | 风险等级（违规 / 待确认） |

## 3. exchange_rates（汇率表）

| 列名 | 类型 | 说明 |
|------|------|------|
| currency | TEXT | 货币代码，人民币统一使用 `RMB` |
| rate_to_cny | REAL | 1 单位该币种兑换人民币的数量 |

## 4. 与商品清单的对应关系

`sku_list.csv` 中的 `sku_ref` 即商品库（`sku_info.sku_id`）中的 SKU 标识，按此关联即可。
