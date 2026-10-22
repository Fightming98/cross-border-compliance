"""确定性的合规规则引擎。

规则口径来自 `instructions.md` 2.1 与第一阶段审计：

* 类目：英文逗号分隔后精确成员匹配；
* 材质：空/NULL 为不限，否则竖线分隔关键词，任一关键词为材质子串；
* 产地：空/NULL 为不限，否则精确相等；
* 三个条件 AND；
* 多规则命中取最严（违规 > 待确认），无命中为合规。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import RuleConfigurationError
from .models import (
    RISK_PRIORITY,
    RULE_SEVERITIES,
    ComplianceRule,
    Product,
    RiskLevel,
    RuleEvaluation,
)


@dataclass(frozen=True)
class _CompiledRule:
    rule: ComplianceRule
    categories: tuple[str, ...]
    material_keywords: tuple[str, ...] | None
    origin_keyword: str | None
    severity: RiskLevel


def _split_category_tokens(rule: ComplianceRule) -> tuple[str, ...]:
    raw = rule.applies_to_category
    if not isinstance(raw, str) or not raw.strip():
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的 applies_to_category 为空"
        )
    tokens = tuple(token.strip() for token in raw.split(","))
    if any(not token for token in tokens):
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的 applies_to_category 含空 token: {raw!r}"
        )
    return tokens


def _split_material_keywords(
    rule: ComplianceRule,
) -> tuple[str, ...] | None:
    raw = rule.material_keyword
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的 material_keyword 不是文本"
        )
    if not raw.strip():
        return None
    tokens = tuple(token.strip() for token in raw.split("|"))
    if any(not token for token in tokens):
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的 material_keyword 含空 token: {raw!r}"
        )
    return tokens


def _parse_origin_keyword(rule: ComplianceRule) -> str | None:
    raw = rule.origin_keyword
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的 origin_keyword 不是文本"
        )
    stripped = raw.strip()
    return stripped or None


def _parse_severity(rule: ComplianceRule) -> RiskLevel:
    try:
        severity = RiskLevel(rule.severity)
    except (ValueError, TypeError) as exc:
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 含未知风险等级: {rule.severity!r}"
        ) from exc
    if rule.severity not in RULE_SEVERITIES:
        raise RuleConfigurationError(
            f"规则 {rule.rule_id} 的风险等级不能用于规则库: {rule.severity!r}"
        )
    return severity


def _compile_rule(rule: ComplianceRule) -> _CompiledRule:
    if not isinstance(rule, ComplianceRule):
        raise RuleConfigurationError(f"规则对象类型错误: {type(rule).__name__}")
    if not isinstance(rule.rule_id, str) or not rule.rule_id.strip():
        raise RuleConfigurationError("规则缺少 rule_id")
    if not isinstance(rule.title, str) or not rule.title.strip():
        raise RuleConfigurationError(f"规则 {rule.rule_id} 缺少标题")
    return _CompiledRule(
        rule=rule,
        categories=_split_category_tokens(rule),
        material_keywords=_split_material_keywords(rule),
        origin_keyword=_parse_origin_keyword(rule),
        severity=_parse_severity(rule),
    )


def compile_rules(rules: Sequence[ComplianceRule]) -> tuple[_CompiledRule, ...]:
    """校验并编译全部规则；任何配置错误都会显式失败。"""

    if not rules:
        raise RuleConfigurationError(
            "规则表为空，不能把所有商品自动判为合规"
        )

    compiled: list[_CompiledRule] = []
    seen: set[str] = set()
    for rule in rules:
        item = _compile_rule(rule)
        if item.rule.rule_id in seen:
            raise RuleConfigurationError(f"规则编号重复: {item.rule.rule_id}")
        seen.add(item.rule.rule_id)
        compiled.append(item)
    return tuple(compiled)


def validate_rules(rules: Sequence[ComplianceRule]) -> None:
    """只做配置校验，不返回编译结果。"""

    compile_rules(rules)


def _category_matches(product: Product, item: _CompiledRule) -> bool:
    return product.category.strip() in item.categories


def _material_matches(product: Product, item: _CompiledRule) -> bool:
    if item.material_keywords is None:
        return True
    material = product.material.strip()
    return any(keyword in material for keyword in item.material_keywords)


def _origin_matches(product: Product, item: _CompiledRule) -> bool:
    if item.origin_keyword is None:
        return True
    return product.origin.strip() == item.origin_keyword


def _rule_matches(product: Product, item: _CompiledRule) -> bool:
    return (
        _category_matches(product, item)
        and _material_matches(product, item)
        and _origin_matches(product, item)
    )


def category_matches(product: Product, rule: ComplianceRule) -> bool:
    """单条件函数，便于独立测试。"""

    return _category_matches(product, _compile_rule(rule))


def material_matches(product: Product, rule: ComplianceRule) -> bool:
    """单条件函数，便于独立测试。"""

    return _material_matches(product, _compile_rule(rule))


def origin_matches(product: Product, rule: ComplianceRule) -> bool:
    """单条件函数，便于独立测试。"""

    return _origin_matches(product, _compile_rule(rule))


def match_rules(
    product: Product,
    rules: Sequence[ComplianceRule],
) -> tuple[ComplianceRule, ...]:
    """返回全部生效规则，按 `rule_id` 升序；不会命中一条就提前结束。"""

    compiled = compile_rules(rules)
    matched = [item.rule for item in compiled if _rule_matches(product, item)]
    return tuple(sorted(matched, key=lambda rule: rule.rule_id))


def evaluate_risk(matched_rules: Sequence[ComplianceRule]) -> RiskLevel:
    """按最严优先级聚合已命中规则的风险。"""

    if not matched_rules:
        return RiskLevel.COMPLIANT
    severities = [_parse_severity(rule) for rule in matched_rules]
    return max(severities, key=lambda level: RISK_PRIORITY[level])


def evaluate_product(
    product: Product,
    rules: Sequence[ComplianceRule],
) -> RuleEvaluation:
    """对正常在售商品执行完整规则评估。

    调用方必须先完成“缺失/已下架”分支；本函数不处理状态。
    """

    compiled = compile_rules(rules)
    matched = tuple(
        sorted(
            (item.rule for item in compiled if _rule_matches(product, item)),
            key=lambda rule: rule.rule_id,
        )
    )
    if not matched:
        return RuleEvaluation(risk=RiskLevel.COMPLIANT, matched_rule_ids=())

    severities = [_parse_severity(rule) for rule in matched]
    risk = max(severities, key=lambda level: RISK_PRIORITY[level])
    return RuleEvaluation(
        risk=risk,
        matched_rule_ids=tuple(rule.rule_id for rule in matched),
    )
