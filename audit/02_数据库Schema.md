# 02 数据库 Schema（真实结构）

> **对象**：`compliance.db`（40,960 字节）
> **检查方式**：SQLite 只读连接（`file:...?mode=ro`），通过 `sqlite_master`、`PRAGMA table_info`、`PRAGMA index_list`、`PRAGMA foreign_key_list`、`PRAGMA integrity_check` 获取。
> **机器可读版本**：[database_schema.json](database_schema.json)
> **结论**：数据库共 **3 张表、0 个视图**；表名与字典一致，但 `sku_info` 多一列 `is_deleted`，`compliance_rules` 的风险列实际为 `severity`。

---

## 一、数据库基本情况

| 项目 | 实测值 |
|---|---|
| 文件路径 | `compliance.db` |
| 文件大小 | 40,960 字节 |
| SQLite 版本 | 3.45.3 |
| 数据库编码 | UTF-8 |
| 完整性检查 | `PRAGMA integrity_check` → **ok** |
| 表数量 | 3 |
| 视图数量 | 0 |
| 外键约束 | 无（`PRAGMA foreign_key_check` 为空） |
| 连接方式 | 只读 URI `?mode=ro`，未写入任何数据 |

### 表清单与记录数

| 表名 | 记录数 | 主键 | 字段数 |
|---|---:|---|---:|
| `sku_info` | **115** | `sku_id` | 8 |
| `compliance_rules` | **10** | `rule_id` | 6 |
| `exchange_rates` | **5** | `currency` | 2 |

> 数据库总记录 115 条，不是清单的 120 条。清单中有 5 个 SKU 在库中不存在，不能把“数据库 115 行”误当成“清单已全部覆盖”。

---

## 二、`sku_info`（商品主数据）

### 建表 SQL（来自 `sqlite_master`，原文）

```sql
CREATE TABLE sku_info (
    sku_id    TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    category  TEXT NOT NULL,
    material  TEXT NOT NULL,
    origin    TEXT NOT NULL,
    price     REAL NOT NULL,
    currency  TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0
)
```

### 字段与约束

| cid | 字段 | 声明类型 | 允许 NULL（DDL/PRAGMA） | 默认值 | 主键 | 实测说明 |
|---:|---|---|---|---|---|---|
| 0 | `sku_id` | TEXT | 未声明（`PRAGMA notnull=0`）* | — | 是（第 1 列） | 115 条全部为 `SKU-` + 5 位数字，无重复、无 NULL |
| 1 | `name` | TEXT | 否 | — | — | 无 NULL、无空白 |
| 2 | `category` | TEXT | 否 | — | — | 8 个类目，无空白 |
| 3 | `material` | TEXT | 否 | — | — | 14 种材质，无空白 |
| 4 | `origin` | TEXT | 否 | — | — | 8 个产地，无空白 |
| 5 | `price` | REAL | 否 | — | — | 全部为正的有限值，范围 14.21–415.97 |
| 6 | `currency` | TEXT | 否 | — | — | CNY/EUR/JPY/KRW/USD，无空白 |
| 7 | `is_deleted` | INTEGER | 否 | `0` | — | 仅 0/1；110 条=0、5 条=1 |

\* **SQLite 细节**：`sku_id TEXT PRIMARY KEY` 在 DDL 中没有显式写 `NOT NULL`，`PRAGMA table_info` 的 `notnull` 返回 0。SQLite 对非 `INTEGER PRIMARY KEY` 的主键列历史性地允许 NULL（除非显式 NOT NULL 或使用 `WITHOUT ROWID`）。**实际数据 115 条无 NULL**，但这是 schema 健壮性上的一个注意点；第二阶段不应依赖“主键自动非空”，应在加载时校验。

### 索引与外键

| 索引 | 唯一 | 来源 | 列 |
|---|---|---|---|
| `sqlite_autoindex_sku_info_1` | 是 | 主键自动索引 | `sku_id` |

外键：无。

### 数据画像

- 总行数：**115**
- 主键重复：**0**
- NULL：所有 8 列均为 0
- 空字符串/纯空白：所有文本列为 0
- 前后空格：所有文本列为 0
- `sku_id` 格式：`AAA-99999`（即 `SKU-` + 5 位数字）× 115
- `is_deleted` 类型：INTEGER；取值 `{0, 1}`

#### 类目分布

| 类目 | 数量 |
|---|---:|
| 箱包 | 22 |
| 内衣 | 19 |
| 裤装 | 17 |
| 上衣 | 13 |
| 裙装 | 13 |
| 配饰 | 13 |
| 鞋靴 | 11 |
| 针织衫 | 7 |
| **合计** | **115** |

#### 材质分布

| 材质 | 数量 | 材质 | 数量 |
|---|---:|---|---:|
| 棉 | 20 | 亚麻 | 7 |
| 锦纶 | 14 | 牛皮 | 7 |
| 涤纶 | 13 | 合金 | 6 |
| 真皮 | 13 | 羊绒 | 6 |
| PU革 | 12 | 不锈钢 | 4 |
| 再生涤纶 | 4 | 羊皮 | 4 |
| 羊毛 | 3 | 桑蚕丝 | 2 |
| **合计** | | | **115** |

#### 产地分布

| 产地 | 数量 |
|---|---:|
| 土耳其 | 22 |
| 越南 | 22 |
| 中国 | 16 |
| 印度 | 16 |
| 日本 | 12 |
| 韩国 | 10 |
| 孟加拉 | 9 |
| 意大利 | 8 |
| **合计** | **115** |

#### 币种分布

| 币种 | 数量 |
|---|---:|
| EUR | 39 |
| USD | 38 |
| CNY | 16 |
| JPY | 12 |
| KRW | 10 |
| **合计** | **115** |

#### 样本记录（按主键排序前 2 条）

| sku_id | name | category | material | origin | price | currency | is_deleted |
|---|---|---|---|---:|---|---:|
| `SKU-00001` | 初野家居套装经典款 | 内衣 | 棉 | 韩国 | 178.97 | KRW | 1 |
| `SKU-00002` | 木白衬衫经典款 | 上衣 | 羊绒 | 意大利 | 99.15 | EUR | 1 |

---

## 三、`compliance_rules`（合规规则库）

### 建表 SQL（来自 `sqlite_master`，原文）

```sql
CREATE TABLE compliance_rules (
    rule_id             TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    applies_to_category TEXT NOT NULL,
    material_keyword    TEXT,
    origin_keyword      TEXT,
    severity            TEXT NOT NULL
)
```

### 字段与约束

| cid | 字段 | 声明类型 | 允许 NULL | 默认值 | 主键 | 实测说明 |
|---:|---|---|---|---|---|---|
| 0 | `rule_id` | TEXT | 未声明（`PRAGMA notnull=0`）* | — | 是（第 1 列） | 10 条，`CR-001`～`CR-010`，无重复 |
| 1 | `title` | TEXT | 否 | — | — | 无 NULL、无空白、无重复 |
| 2 | `applies_to_category` | TEXT | 否 | — | — | 英文逗号分隔，无空 token、无空格 token |
| 3 | `material_keyword` | TEXT | **是** | — | — | 实际 10 条均非空；竖线分隔，无空 token |
| 4 | `origin_keyword` | TEXT | **是** | — | — | **8 条为空字符串**（“不限”），2 条为 `意大利`/`越南`；无 NULL |
| 5 | `severity` | TEXT | 否 | — | — | 只有 `待确认`（8）和 `违规`（2），无其他值 |

\* 同 `sku_info`：`rule_id TEXT PRIMARY KEY` 未显式声明 NOT NULL，实际数据无 NULL。

### 索引与外键

| 索引 | 唯一 | 来源 | 列 |
|---|---|---|---|
| `sqlite_autoindex_compliance_rules_1` | 是 | 主键自动索引 | `rule_id` |

外键：无。

### 数据画像

- 总行数：**10**
- 主键重复：**0**
- 标题重复：**0**
- NULL：所有列 0（`material_keyword`/`origin_keyword` 也没有 NULL）
- 空字符串：仅 `origin_keyword` 8 条；其余文本列 0
- 前后空格：0
- `severity` 分布：**待确认 8、违规 2**；未知取值 0
- 类目 token 共 8 个，全部能在 `sku_info.category` 找到
- 材质关键词共 10 个，全部能在至少一种 `sku_info.material` 中命中
- 产地关键词 2 个（意大利、越南），均能在 `sku_info.origin` 找到
- 分隔符问题：**0**（逗号/竖线均无空 token、无空格 token）

#### 规则清单（真实字段）

| rule_id | title | applies_to_category | material_keyword | origin_keyword | severity |
|---|---|---|---|---|---|
| CR-001 | 真皮/牛皮/羊皮制品须提供濒危物种及皮革来源证明 | 鞋靴,箱包,配饰 | 真皮\|牛皮\|羊皮 | （空） | 待确认 |
| CR-002 | PU革制品须在主图与详情页标注“人造革” | 鞋靴,箱包,配饰 | PU革 | （空） | 待确认 |
| CR-003 | 含羊毛/羊绒成分须附动物检疫证书 | 上衣,裤装,裙装,针织衫,配饰 | 羊毛\|羊绒 | （空） | 待确认 |
| CR-004 | 产地为限制国且材质为真皮的箱包禁止进口 | 箱包 | 真皮 | 意大利 | 违规 |
| CR-005 | 合金/不锈钢仿真首饰须提供镍释放检测报告 | 配饰 | 合金\|不锈钢 | （空） | 待确认 |
| CR-006 | 纯棉贴身内衣须标注成分百分比与安全类别 | 内衣 | 棉 | （空） | 待确认 |
| CR-007 | 再生涤纶内衣类商品禁售 | 内衣 | 再生涤纶 | （空） | 违规 |
| CR-008 | 产地为越南的PU革鞋靴须附环保检测报告 | 鞋靴 | PU革 | 越南 | 待确认 |
| CR-009 | 合金箱包五金件须提供铅含量检测报告 | 箱包,配饰 | 合金 | （空） | 待确认 |
| CR-010 | 牛皮鞋靴须标注头层/二层及鞣制方式 | 鞋靴 | 牛皮 | （空） | 待确认 |

#### 样本记录

| rule_id | title | applies_to_category | material_keyword | origin_keyword | severity |
|---|---|---|---|---|---|
| CR-001 | 真皮/牛皮/羊皮制品须提供濒危物种及皮革来源证明 | 鞋靴,箱包,配饰 | 真皮\|牛皮\|羊皮 | （空字符串） | 待确认 |
| CR-002 | PU革制品须在主图与详情页标注“人造革” | 鞋靴,箱包,配饰 | PU革 | （空字符串） | 待确认 |

---

## 四、`exchange_rates`（汇率表）

### 建表 SQL（来自 `sqlite_master`，原文）

```sql
CREATE TABLE exchange_rates (
    currency    TEXT PRIMARY KEY,
    rate_to_cny REAL NOT NULL
)
```

### 字段与约束

| cid | 字段 | 声明类型 | 允许 NULL | 默认值 | 主键 | 实测说明 |
|---:|---|---|---|---|---|---|
| 0 | `currency` | TEXT | 未声明（`PRAGMA notnull=0`）* | — | 是（第 1 列） | 5 种，唯一，无空格 |
| 1 | `rate_to_cny` | REAL | 否 | — | — | 全部为正的有限值 |

\* 同前：`currency TEXT PRIMARY KEY` 未显式 NOT NULL，实际无 NULL。

### 索引与外键

| 索引 | 唯一 | 来源 | 列 |
|---|---|---|---|
| `sqlite_autoindex_exchange_rates_1` | 是 | 主键自动索引 | `currency` |

外键：无。

### 全部记录

| currency | rate_to_cny |
|---|---:|
| CNY | 1.0 |
| EUR | 7.78 |
| JPY | 0.0476 |
| KRW | 0.0052 |
| USD | 7.12 |

- 行数：**5**
- 重复币种：**0**
- NULL/空值：**0**
- 零值/负值/非有限值：**0**
- 人民币代码：**CNY**（不是字典所写的 `RMB`）
- 商品使用的 5 种币种全部覆盖；汇率表无未使用币种

---

## 五、Schema 层面的注意点

1. **主键列未显式 NOT NULL**：三张表的 TEXT 主键在 DDL 中都没有 `NOT NULL`，`PRAGMA table_info` 显示 `notnull=0`。实际数据无 NULL，但 SQLite 允许这类主键列出现 NULL；第二阶段加载数据时应显式校验主键非空。
2. **无外键约束**：`sku_info`、`compliance_rules`、`exchange_rates` 之间没有数据库级外键；`applies_to_category`/`material_keyword`/`origin_keyword` 与商品字段的关联完全依赖业务逻辑，必须由规则引擎自行校验（本轮已确认无悬空引用）。
3. **无 CHECK 约束**：`is_deleted` 未限制只能为 0/1，`severity` 未限制只能为两个值；实际数据虽干净，代码仍应做取值校验并在异常时显式失败。
4. **空值表示不统一**：`origin_keyword` 的“不限”用空字符串 `''` 表示，而不是 NULL；`material_keyword` 在 DDL 中可空，实际全非空。规则引擎必须同时把 `NULL` 和 `strip()==''` 视为“不限”。
5. **真实行数与字典不同步**：字典没有记录行数；`sku_info` 只有 115 行，清单 120 条中有 5 条必然缺失，这不是数据库损坏。

以上均为 schema 健壮性问题，不影响本轮数量基线；已在 [06_材料差异与种子代码审查.md](06_材料差异与种子代码审查.md) 中与字典差异一并留档。
