"""统一数据模型。

模型只表达业务对象和已计算的最终结果，不包含数据库访问、规则匹配或
金额换算逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Disposition(str, Enum):
    """输入清单中每条记录的最终处置结果。"""

    NORMAL = "正常评估"
    DELETED = "已下架"
    MISSING = "库中缺失"

    def __str__(self) -> str:
        return self.value


class RiskLevel(str, Enum):
    """正常评估商品的风险标记。"""

    COMPLIANT = "合规"
    PENDING = "待确认"
    VIOLATION = "违规"

    def __str__(self) -> str:
        return self.value


# 规则库中允许出现的风险等级；“合规”只作为无命中时的默认结果。
RULE_SEVERITIES: frozenset[str] = frozenset(
    {RiskLevel.PENDING.value, RiskLevel.VIOLATION.value}
)

# 数值越大风险越严重，用于多规则命中时取最严。
RISK_PRIORITY: dict[RiskLevel, int] = {
    RiskLevel.COMPLIANT: 0,
    RiskLevel.PENDING: 1,
    RiskLevel.VIOLATION: 2,
}


@dataclass(frozen=True)
class SkuInput:
    """CSV 中的一条原始输入记录。"""

    sku_ref: str
    batch_no: str
    line_no: int


@dataclass(frozen=True)
class Product:
    """`sku_info` 中的一条商品记录。"""

    sku_id: str
    name: str
    category: str
    material: str
    origin: str
    price: Decimal
    currency: str
    is_deleted: int


@dataclass(frozen=True)
class ComplianceRule:
    """`compliance_rules` 中的一条规则。"""

    rule_id: str
    title: str
    applies_to_category: str
    material_keyword: str | None
    origin_keyword: str | None
    severity: str


@dataclass(frozen=True)
class RuleEvaluation:
    """规则引擎对单个商品的计算结果。"""

    risk: RiskLevel
    matched_rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProcessingResult:
    """单个输入 SKU 的最终处理结果。

    对于 `已下架` 和 `库中缺失`，价格、风险与规则字段为 ``None``/空元组，
    由报告层统一渲染为“—（不适用）”或“—”，不能伪装成“合规”。
    """

    sku_input: SkuInput
    mapped_sku_id: str
    disposition: Disposition
    product: Product | None
    cny_price: Decimal | None
    risk: RiskLevel | None
    matched_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrityStats:
    """完整性自检的五个数字。"""

    n_input: int
    n_matched: int
    n_deleted: int
    n_missing: int
    n_evaluated: int

    def equation_rows(self) -> list[tuple[str, int, tuple[int, ...], bool]]:
        """返回三条等式的 (名称, 左值, 右侧各项, 是否成立)。

        右侧保留各项原始数值，报告层才能完整展示
        ``120 = 5 + 5 + 110``，而不是把缺失数和已下架数预先合并。
        """

        return [
            (
                "清单读取数 = 在库数 + 缺失数",
                self.n_input,
                (self.n_matched, self.n_missing),
                self.n_input == self.n_matched + self.n_missing,
            ),
            (
                "在库数 = 已下架数 + 实际评估数",
                self.n_matched,
                (self.n_deleted, self.n_evaluated),
                self.n_matched == self.n_deleted + self.n_evaluated,
            ),
            (
                "清单读取数 = 缺失数 + 已下架数 + 实际评估数",
                self.n_input,
                (self.n_missing, self.n_deleted, self.n_evaluated),
                self.n_input == self.n_missing + self.n_deleted + self.n_evaluated,
            ),
        ]

    @property
    def equations_hold(self) -> bool:
        return all(row[3] for row in self.equation_rows())
