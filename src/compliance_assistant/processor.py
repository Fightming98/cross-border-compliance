"""业务流程编排：映射、状态判定、规则评估与价格换算。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal

from .errors import ComplianceError, InputDataError, ProcessingError
from .models import (
    ComplianceRule,
    Disposition,
    ProcessingResult,
    Product,
    SkuInput,
)
from .pricing import convert_to_cny
from .rules_engine import evaluate_product, validate_rules

SKU_REF_PATTERN = re.compile(r"\d{5}")


def map_sku_ref(sku_ref: str) -> str:
    """把 CSV 中的五位编号转换为数据库查询键。

    只接受 `strip` 后恰好五位数字的输入；不补零、不截断、不猜测。
    """

    if not isinstance(sku_ref, str):
        raise InputDataError(f"sku_ref 不是文本: {sku_ref!r}")
    normalized = sku_ref.strip()
    if SKU_REF_PATTERN.fullmatch(normalized) is None:
        raise InputDataError(
            f"sku_ref 格式非法，期望五位数字，实际为 {sku_ref!r}"
        )
    return "SKU-" + normalized


def process_sku_inputs(
    sku_inputs: Sequence[SkuInput],
    products: Mapping[str, Product],
    rules: Sequence[ComplianceRule],
    exchange_rates: Mapping[str, Decimal],
) -> list[ProcessingResult]:
    """按输入顺序处理全部 SKU，每条输入都必须产出一个结果。

    状态优先级：库中缺失 -> 已下架 -> 正常评估。
    已下架与缺失商品不会调用规则引擎或价格模块。
    """

    # 即使本次输入中没有正常评估商品，空规则表也必须被视为配置错误。
    validate_rules(rules)

    results: list[ProcessingResult] = []
    for sku_input in sku_inputs:
        mapped_sku_id = map_sku_ref(sku_input.sku_ref)
        product = products.get(mapped_sku_id)

        if product is None:
            results.append(
                ProcessingResult(
                    sku_input=sku_input,
                    mapped_sku_id=mapped_sku_id,
                    disposition=Disposition.MISSING,
                    product=None,
                    cny_price=None,
                    risk=None,
                    matched_rule_ids=(),
                )
            )
            continue

        if product.is_deleted == 1:
            results.append(
                ProcessingResult(
                    sku_input=sku_input,
                    mapped_sku_id=mapped_sku_id,
                    disposition=Disposition.DELETED,
                    product=product,
                    cny_price=None,
                    risk=None,
                    matched_rule_ids=(),
                )
            )
            continue

        if product.is_deleted != 0:
            raise ProcessingError(
                f"SKU {sku_input.sku_ref} 的 is_deleted 非法: "
                f"{product.is_deleted!r}"
            )

        try:
            evaluation = evaluate_product(product, rules)
            cny_price = convert_to_cny(
                product.price, product.currency, exchange_rates
            )
        except ComplianceError as exc:
            raise ProcessingError(
                f"SKU {sku_input.sku_ref}（{mapped_sku_id}）正常评估失败: {exc}"
            ) from exc

        results.append(
            ProcessingResult(
                sku_input=sku_input,
                mapped_sku_id=mapped_sku_id,
                disposition=Disposition.NORMAL,
                product=product,
                cny_price=cny_price,
                risk=evaluation.risk,
                matched_rule_ids=evaluation.matched_rule_ids,
            )
        )

    return results
