"""Markdown 报告生成与落盘后复核。

报告层只消费已经计算完成的 :class:`ProcessingResult`，不会重新匹配规则
或重新计算汇率。写入前强制执行完整性校验，并采用临时文件 + 原子替换，
避免异常时留下看似成功但内容不完整的报告。
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .errors import ReportError, ValidationError
from .models import Disposition, IntegrityStats, ProcessingResult, RiskLevel
from .validation import compute_stats, validate_processing_results

DETAIL_HEADING = "## 一、明细区"
SUMMARY_HEADING = "## 二、汇总区"
INTEGRITY_HEADING = "## 三、完整性自检区"
REQUIRED_HEADINGS = (DETAIL_HEADING, SUMMARY_HEADING, INTEGRITY_HEADING)


@dataclass(frozen=True)
class CategoryRiskStat:
    """Top 风险类目的计数。"""

    category: str
    violations: int
    pending: int

    @property
    def total(self) -> int:
        return self.violations + self.pending


def _cell(value: object) -> str:
    if value is None:
        return "—"
    text = str(value)
    if not text.strip():
        return "—"
    return text.replace("|", "\\|").replace("\n", " ")


def _money(price: Decimal, currency: str) -> str:
    return f"{price:.2f} {currency}"


def _cny(price: Decimal) -> str:
    return f"{price:.2f}"


def top_risk_categories(
    results: Sequence[ProcessingResult],
    limit: int = 5,
) -> list[CategoryRiskStat]:
    """按固定口径计算 Top 风险类目。

    只统计正常评估且风险为违规/待确认的商品；每个 SKU 只计一次。
    排序：违规数降序 -> 待确认数降序 -> 类目名升序。
    """

    counts: dict[str, Counter[str]] = {}
    for result in results:
        if result.disposition != Disposition.NORMAL or result.product is None:
            continue
        if result.risk not in (RiskLevel.VIOLATION, RiskLevel.PENDING):
            continue
        category = result.product.category
        bucket = counts.setdefault(category, Counter())
        bucket[result.risk.value] += 1

    stats = [
        CategoryRiskStat(
            category=category,
            violations=bucket.get(RiskLevel.VIOLATION.value, 0),
            pending=bucket.get(RiskLevel.PENDING.value, 0),
        )
        for category, bucket in counts.items()
    ]
    stats.sort(key=lambda item: (-item.violations, -item.pending, item.category))
    return stats[:limit]


def build_detail_section(results: Sequence[ProcessingResult]) -> str:
    """构造明细区；每个输入记录恰好一行。"""

    lines = [
        DETAIL_HEADING,
        "",
        f"共 {len(results)} 条输入记录，逐条处置如下。",
        "",
        "| 序号 | sku_ref | batch_no | 处置结果 | 商品名 | 类目 | 材质 | 产地 | 原价 | 人民币价格 | 风险标记 | 依据规则 |",
        "|---:|---|---|---|---|---|---|---|---:|---:|---|---|",
    ]

    for index, result in enumerate(results, start=1):
        product = result.product
        if result.disposition == Disposition.MISSING:
            product_cells = ["—", "—", "—", "—"]
            original_price = "—"
            cny_price = "—"
            risk = "—"
            rule_ids = "—"
        elif result.disposition == Disposition.DELETED:
            assert product is not None
            product_cells = [
                _cell(product.name),
                _cell(product.category),
                _cell(product.material),
                _cell(product.origin),
            ]
            original_price = _money(product.price, product.currency)
            cny_price = "—（不适用）"
            risk = "—（不适用）"
            rule_ids = "—（不适用）"
        else:
            assert product is not None
            product_cells = [
                _cell(product.name),
                _cell(product.category),
                _cell(product.material),
                _cell(product.origin),
            ]
            original_price = _money(product.price, product.currency)
            cny_price = _cny(result.cny_price) if result.cny_price is not None else "—"
            risk = result.risk.value if result.risk is not None else "—"
            rule_ids = ";".join(result.matched_rule_ids) or "—"

        name, category, material, origin = product_cells
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    _cell(result.sku_input.sku_ref),
                    _cell(result.sku_input.batch_no),
                    result.disposition.value,
                    name,
                    category,
                    material,
                    origin,
                    original_price,
                    cny_price,
                    risk,
                    rule_ids,
                ]
            )
            + " |"
        )

    return "\n".join(lines)


def build_summary_section(results: Sequence[ProcessingResult]) -> str:
    """构造汇总区：三档风险数量与 Top 风险类目。"""

    counts = {level: 0 for level in RiskLevel}
    evaluated = 0
    for result in results:
        if result.disposition == Disposition.NORMAL:
            evaluated += 1
            if isinstance(result.risk, RiskLevel):
                counts[result.risk] += 1

    lines = [
        SUMMARY_HEADING,
        "",
        "### 2.1 风险数量（仅统计正常评估商品）",
        "",
        "| 风险标记 | 数量 |",
        "|---|---:|",
        f"| 合规 | {counts[RiskLevel.COMPLIANT]} |",
        f"| 待确认 | {counts[RiskLevel.PENDING]} |",
        f"| 违规 | {counts[RiskLevel.VIOLATION]} |",
        f"| **合计** | **{evaluated}** |",
        "",
        f"三档风险数量之和：{sum(counts.values())}；实际评估数：{evaluated}。",
        "",
        "### 2.2 Top 风险类目",
        "",
        "统计口径：仅统计至少有一件违规或待确认商品的类目；每个正常评估 SKU "
        "按最终风险只计一次；先按违规数降序，再按待确认数降序，最后按类目名升序，"
        "最多展示前 5 个。",
        "",
    ]

    top = top_risk_categories(results)
    if not top:
        lines.append("没有风险类目。")
        return "\n".join(lines)

    lines.extend(
        [
            "| 排名 | 类目 | 违规数 | 待确认数 | 风险商品总数 |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for rank, item in enumerate(top, start=1):
        lines.append(
            f"| {rank} | {_cell(item.category)} | {item.violations} | "
            f"{item.pending} | {item.total} |"
        )
    return "\n".join(lines)


def build_integrity_section(results: Sequence[ProcessingResult]) -> str:
    """构造完整性自检区；数字由实际结果动态计算。"""

    stats = compute_stats(results)
    lines = [
        INTEGRITY_HEADING,
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| 清单读取数 | {stats.n_input} |",
        f"| 在库数（含已下架） | {stats.n_matched} |",
        f"| 已下架数 | {stats.n_deleted} |",
        f"| 缺失数 | {stats.n_missing} |",
        f"| 实际评估数 | {stats.n_evaluated} |",
        "",
        "完整性等式校验：",
        "",
    ]
    for name, left, terms, holds in stats.equation_rows():
        expression = " + ".join(str(term) for term in terms)
        lines.append(
            f"- {name}：{left} = {expression}，"
            f"{'成立' if holds else '不成立'}"
        )
    lines.append("")
    lines.append(
        f"校验结论：{'全部成立。' if stats.equations_hold else '存在不成立的等式，报告不得作为正式结果。'}"
    )
    return "\n".join(lines)


def build_report_document(results: Sequence[ProcessingResult]) -> str:
    """生成完整 Markdown 文本（不落盘）。"""

    sections = [
        "# 跨境商品合规检查报告",
        "",
        "> 数据来源：`sku_list.csv`、`compliance.db`。",
        "> 处置口径：正常评估 / 已下架 / 库中缺失；风险口径：合规 / 待确认 / 违规。",
        "",
        build_detail_section(results),
        "",
        build_summary_section(results),
        "",
        build_integrity_section(results),
        "",
    ]
    return "\n".join(sections)


def _verify_document(
    text: str,
    expected_detail_rows: int | None,
    expected_sku_refs: Sequence[str] | None,
) -> None:
    for heading in REQUIRED_HEADINGS:
        if heading not in text:
            raise ReportError(f"报告缺少必需部分: {heading}")

    detail = text.split(DETAIL_HEADING, 1)[1].split(SUMMARY_HEADING, 1)[0]
    row_pattern = re.compile(r"^\|\s*\d+\s*\|", re.MULTILINE)
    rows = row_pattern.findall(detail)
    if expected_detail_rows is not None and len(rows) != expected_detail_rows:
        raise ReportError(
            f"报告明细行数应为 {expected_detail_rows}，实际为 {len(rows)}"
        )

    if expected_sku_refs is not None:
        parsed_refs = re.findall(
            r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|", detail, re.MULTILINE
        )
        expected = [ref.strip() for ref in expected_sku_refs]
        actual = [ref.strip() for ref in parsed_refs]
        if actual != expected:
            raise ReportError("报告明细中的 sku_ref 顺序或内容与输入不一致")


def verify_report_file(
    report_path: str | Path,
    expected_detail_rows: int | None = None,
    expected_sku_refs: Sequence[str] | None = None,
) -> None:
    """落盘后复核报告结构、明细行数与 sku_ref 顺序。"""

    path = Path(report_path)
    if not path.is_file():
        raise ReportError(f"报告文件不存在: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"报告文件不可读: {path}: {exc}") from exc
    _verify_document(text, expected_detail_rows, expected_sku_refs)


def generate_report(
    results: Sequence[ProcessingResult],
    output_path: str | Path,
    *,
    expected_input_count: int | None = None,
) -> Path:
    """校验结果、生成报告并复核落盘文件。"""

    try:
        validate_processing_results(
            results, expected_input_count=expected_input_count
        )
    except ValidationError as exc:
        raise ReportError(f"生成报告前校验失败: {exc}") from exc

    content = build_report_document(results)
    try:
        _verify_document(
            content,
            expected_detail_rows=len(results),
            expected_sku_refs=[result.sku_input.sku_ref for result in results],
        )
    except ReportError:
        raise

    path = Path(output_path)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise ReportError(f"写入报告失败: {path}: {exc}") from exc

    try:
        verify_report_file(
            path,
            expected_detail_rows=len(results),
            expected_sku_refs=[result.sku_input.sku_ref for result in results],
        )
    except ReportError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path
