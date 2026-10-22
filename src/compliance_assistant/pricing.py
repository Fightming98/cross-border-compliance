"""人民币价格换算模块。

汇率只来自调用方传入的数据库快照；本模块不访问数据库、不硬编码汇率，
也不修改商品对象。返回 ``Decimal``，展示格式由报告层负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .errors import PricingError

CENT = Decimal("0.01")


def _to_decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, Decimal, str)
    ):
        raise PricingError(
            f"{label}不是有效数值，实际类型为 {type(value).__name__}"
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"{label}无法转换为 Decimal: {value!r}") from exc
    if not number.is_finite():
        raise PricingError(f"{label}不是有限数值: {value!r}")
    return number


def convert_to_cny(
    price: object,
    currency: str,
    exchange_rates: Mapping[str, Decimal],
) -> Decimal:
    """按数据库汇率把原价换算为人民币，保留两位小数（HALF_UP）。

    参数:
        price: 商品原价，支持 Decimal/整数/浮点/十进制字符串。
        currency: 商品币种代码，必须存在于 ``exchange_rates``。
        exchange_rates: 币种到 ``rate_to_cny`` 的映射，必须来自数据库。

    异常:
        PricingError: 价格、币种或汇率缺失/非法。
    """

    if not isinstance(currency, str) or not currency.strip():
        raise PricingError(f"币种为空或不是文本: {currency!r}")
    currency_code = currency.strip()

    amount = _to_decimal(price, "商品价格")
    if amount <= 0:
        raise PricingError(f"商品价格必须大于 0，实际为 {price!r}")

    if currency_code not in exchange_rates:
        available = sorted(exchange_rates)
        raise PricingError(
            f"币种 {currency_code!r} 在汇率表中不存在；可用币种: {available}"
        )

    rate = _to_decimal(exchange_rates[currency_code], f"币种 {currency_code} 的汇率")
    if rate <= 0:
        raise PricingError(
            f"币种 {currency_code} 的汇率必须大于 0，实际为 "
            f"{exchange_rates[currency_code]!r}"
        )

    try:
        return (amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise PricingError(
            f"金额换算失败: price={price!r}, currency={currency_code!r}"
        ) from exc
