"""输入与处理结果的完整性校验。

校验层不生成报告，也不替报告层做展示；它只负责确保“数量、状态、
风险依据、价格来源”彼此一致，任何对不上账的情况都必须显式失败。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

from .errors import ComplianceError, ValidationError
from .models import (
    ComplianceRule,
    Disposition,
    IntegrityStats,
    ProcessingResult,
    RiskLevel,
    SkuInput,
)
from .pricing import convert_to_cny
from .rules_engine import evaluate_product

SKU_REF_PATTERN = re.compile(r"\d{5}")


def compute_stats(results: Sequence[ProcessingResult]) -> IntegrityStats:
    """根据实际处理结果动态计算五个完整性数字。"""

    n_input = len(results)
    n_deleted = sum(
        1 for result in results if result.disposition == Disposition.DELETED
    )
    n_missing = sum(
        1 for result in results if result.disposition == Disposition.MISSING
    )
    n_evaluated = sum(
        1 for result in results if result.disposition == Disposition.NORMAL
    )
    n_matched = n_deleted + n_evaluated
    return IntegrityStats(
        n_input=n_input,
        n_matched=n_matched,
        n_deleted=n_deleted,
        n_missing=n_missing,
        n_evaluated=n_evaluated,
    )


def validate_sku_inputs(
    sku_inputs: Sequence[SkuInput],
    expected_input_count: int | None = None,
) -> None:
    """校验原始输入集合；正式数据应传入 120。"""

    errors: list[str] = []
    if expected_input_count is not None and len(sku_inputs) != expected_input_count:
        errors.append(
            f"清单读取数应为 {expected_input_count}，实际为 {len(sku_inputs)}"
        )
    if not sku_inputs:
        errors.append("清单为空")

    previous_line = 0
    for index, item in enumerate(sku_inputs, start=1):
        if not isinstance(item, SkuInput):
            errors.append(f"第 {index} 个输入对象类型错误")
            continue
        if item.line_no <= previous_line:
            errors.append(
                f"第 {index} 条输入的行号 {item.line_no} 未严格递增"
            )
        previous_line = item.line_no
        if not isinstance(item.sku_ref, str) or SKU_REF_PATTERN.fullmatch(
            item.sku_ref.strip()
        ) is None:
            errors.append(f"第 {index} 条输入的 sku_ref 格式非法: {item.sku_ref!r}")
        if not isinstance(item.batch_no, str) or not item.batch_no.strip():
            errors.append(f"第 {index} 条输入的 batch_no 为空")

    if errors:
        raise ValidationError("；".join(errors))


def _validate_result_fields(
    result: ProcessingResult,
    index: int,
    exchange_rates: Mapping[str, Decimal] | None,
    rules: Sequence[ComplianceRule] | None,
    errors: list[str],
) -> None:
    label = f"第 {index} 条结果（sku_ref={result.sku_input.sku_ref}）"

    expected_mapped = "SKU-" + result.sku_input.sku_ref.strip()
    if result.mapped_sku_id != expected_mapped:
        errors.append(
            f"{label} 映射键 {result.mapped_sku_id!r} 与期望 "
            f"{expected_mapped!r} 不一致"
        )

    if result.disposition not in (
        Disposition.MISSING,
        Disposition.DELETED,
        Disposition.NORMAL,
    ):
        errors.append(f"{label} 处置结果非法: {result.disposition!r}")
        return

    if result.disposition == Disposition.MISSING:
        if result.product is not None:
            errors.append(f"{label} 库中缺失却带有商品信息")
        if result.cny_price is not None:
            errors.append(f"{label} 库中缺失却带有人民币价格")
        if result.risk is not None:
            errors.append(f"{label} 库中缺失却带有风险标记")
        if result.matched_rule_ids:
            errors.append(f"{label} 库中缺失却带有规则编号")
        return

    product = result.product
    if product is None:
        errors.append(f"{label} 处置为 {result.disposition.value} 却缺少商品信息")
        return
    if product.sku_id != result.mapped_sku_id:
        errors.append(
            f"{label} 商品主键 {product.sku_id!r} 与映射键 "
            f"{result.mapped_sku_id!r} 不一致"
        )

    if result.disposition == Disposition.DELETED:
        if product.is_deleted != 1:
            errors.append(f"{label} 标记已下架但 is_deleted={product.is_deleted!r}")
        if result.cny_price is not None:
            errors.append(f"{label} 已下架却带有人民币价格")
        if result.risk is not None:
            errors.append(f"{label} 已下架却带有风险标记")
        if result.matched_rule_ids:
            errors.append(f"{label} 已下架却带有规则编号")
        return

    # 正常评估
    if product.is_deleted != 0:
        errors.append(f"{label} 标记正常评估但 is_deleted={product.is_deleted!r}")
    if not isinstance(result.risk, RiskLevel):
        errors.append(f"{label} 缺少合法风险标记: {result.risk!r}")
    if not isinstance(result.cny_price, Decimal):
        errors.append(f"{label} 缺少 Decimal 人民币价格")
    else:
        if not result.cny_price.is_finite() or result.cny_price <= 0:
            errors.append(f"{label} 人民币价格非法: {result.cny_price!r}")
        if result.cny_price.as_tuple().exponent != -2:
            errors.append(f"{label} 人民币价格未保留两位小数: {result.cny_price!r}")

    rule_ids = result.matched_rule_ids
    if not isinstance(rule_ids, tuple):
        errors.append(f"{label} matched_rule_ids 不是元组")
    else:
        if len(set(rule_ids)) != len(rule_ids):
            errors.append(f"{label} 规则编号重复")
        if tuple(sorted(rule_ids)) != rule_ids:
            errors.append(f"{label} 规则编号未按升序排列")
        if any(not isinstance(rule_id, str) or not rule_id for rule_id in rule_ids):
            errors.append(f"{label} 含空规则编号")

    if result.risk == RiskLevel.COMPLIANT and rule_ids:
        errors.append(f"{label} 风险为合规却引用了规则")
    if result.risk in (RiskLevel.PENDING, RiskLevel.VIOLATION) and not rule_ids:
        errors.append(f"{label} 风险为 {result.risk.value} 却没有规则依据")

    if exchange_rates is not None:
        try:
            expected_price = convert_to_cny(
                product.price, product.currency, exchange_rates
            )
        except ComplianceError as exc:
            errors.append(f"{label} 价格来源非法: {exc}")
        else:
            if result.cny_price != expected_price:
                errors.append(
                    f"{label} 人民币价格 {result.cny_price!r} 与按数据库汇率复算的 "
                    f"{expected_price!r} 不一致"
                )

    if rules is not None:
        rule_map = {rule.rule_id: rule for rule in rules}
        missing_rules = [rule_id for rule_id in rule_ids if rule_id not in rule_map]
        if missing_rules:
            errors.append(f"{label} 引用了不存在的规则: {missing_rules}")
        else:
            try:
                expected = evaluate_product(product, rules)
            except ComplianceError as exc:
                errors.append(f"{label} 规则复算失败: {exc}")
            else:
                if result.risk != expected.risk:
                    errors.append(
                        f"{label} 风险 {result.risk!r} 与规则复算 "
                        f"{expected.risk!r} 不一致"
                    )
                if rule_ids != expected.matched_rule_ids:
                    errors.append(
                        f"{label} 规则编号 {rule_ids!r} 与规则复算 "
                        f"{expected.matched_rule_ids!r} 不一致"
                    )


def validate_processing_results(
    results: Sequence[ProcessingResult],
    *,
    expected_input_count: int | None = None,
    exchange_rates: Mapping[str, Decimal] | None = None,
    rules: Sequence[ComplianceRule] | None = None,
) -> IntegrityStats:
    """校验全部处理结果并返回动态统计。

    可选传入汇率和规则时，会逐条复算人民币价格与规则命中结果，避免处理
    模块“自说自话”。
    """

    errors: list[str] = []
    if not results:
        errors.append("处理结果为空")
    if expected_input_count is not None and len(results) != expected_input_count:
        errors.append(
            f"结果数应为 {expected_input_count}，实际为 {len(results)}"
        )

    previous_line = 0
    for index, result in enumerate(results, start=1):
        if not isinstance(result, ProcessingResult):
            errors.append(f"第 {index} 个结果类型错误")
            continue
        if result.sku_input.line_no <= previous_line:
            errors.append(
                f"第 {index} 条结果的行号 {result.sku_input.line_no} 未严格递增"
            )
        previous_line = result.sku_input.line_no
        _validate_result_fields(result, index, exchange_rates, rules, errors)

    stats = compute_stats(results)
    if not stats.equations_hold:
        errors.append(
            "完整性等式不成立: "
            f"{stats.n_input} = {stats.n_matched} + {stats.n_missing}; "
            f"{stats.n_matched} = {stats.n_deleted} + {stats.n_evaluated}"
        )

    risk_counts = {level: 0 for level in RiskLevel}
    for result in results:
        if result.disposition == Disposition.NORMAL and isinstance(
            result.risk, RiskLevel
        ):
            risk_counts[result.risk] += 1
    if sum(risk_counts.values()) != stats.n_evaluated:
        errors.append(
            f"三档风险数量之和 {sum(risk_counts.values())} 不等于实际评估数 "
            f"{stats.n_evaluated}"
        )

    if errors:
        raise ValidationError("；".join(errors))
    return stats


def validate_database_snapshot(
    products: Mapping[str, object],
    rules: Sequence[object],
    exchange_rates: Mapping[str, Decimal],
) -> None:
    """校验数据库快照的基本非空条件。"""

    errors: list[str] = []
    if not products:
        errors.append("商品表为空")
    if not rules:
        errors.append("规则表为空")
    if not exchange_rates:
        errors.append("汇率表为空")
    if errors:
        raise ValidationError("；".join(errors))
