"""可复用的合规检查应用服务。

CLI、Web、HTML 报告和 CSV 导出都从这里取得同一批 :class:`ProcessingResult`，
不在此模块之外重新实现 SKU 映射、规则匹配或价格换算。
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .errors import ReportError
from .loader import (
    load_exchange_rates,
    load_products,
    load_rules,
    load_sku_inputs,
)
from .models import (
    ComplianceRule,
    Disposition,
    IntegrityStats,
    ProcessingResult,
    Product,
    RiskLevel,
    SkuInput,
)
from .processor import process_sku_inputs
from .report import CategoryRiskStat, top_risk_categories
from .validation import (
    validate_database_snapshot,
    validate_processing_results,
    validate_sku_inputs,
)

REPORT_VERSION = "1.0.0"
MISSING_PLACEHOLDER = "—"
NOT_APPLICABLE_PLACEHOLDER = "—（不适用）"


@dataclass(frozen=True)
class ComplianceRun:
    """一次完整检查的统一结果快照。"""

    results: tuple[ProcessingResult, ...]
    sku_inputs: tuple[SkuInput, ...]
    stats: IntegrityStats
    products: Mapping[str, Product]
    rules: tuple[ComplianceRule, ...]
    exchange_rates: Mapping[str, Decimal]
    source_filename: str
    source_sha256: str
    source_size_bytes: int
    generated_at: datetime
    risk_counts: Mapping[RiskLevel, int]
    disposition_counts: Mapping[Disposition, int]
    top_categories: tuple[CategoryRiskStat, ...]

    @property
    def risk_ratio(self) -> float:
        """待确认 + 违规占实际评估商品的比例；没有评估商品时为 0。"""

        if self.stats.n_evaluated == 0:
            return 0.0
        risky = (
            self.risk_counts[RiskLevel.PENDING]
            + self.risk_counts[RiskLevel.VIOLATION]
        )
        return risky / self.stats.n_evaluated

    @property
    def rule_map(self) -> dict[str, ComplianceRule]:
        return {rule.rule_id: rule for rule in self.rules}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_compliance_check(
    csv_path: str | Path,
    db_path: str | Path,
    *,
    expected_input_count: int | None = None,
    source_filename: str | None = None,
    generated_at: datetime | None = None,
) -> ComplianceRun:
    """执行一次完整检查并返回统一结果，不生成任何报告文件。

    所有业务校验仍在原有 loader/processor/validation 中完成。Web 调用时
    ``expected_input_count`` 为 ``None``，允许上传任意非空、符合契约的清单；
    CLI 继续默认要求 120 条。
    """

    path = Path(csv_path)
    sku_inputs = load_sku_inputs(path)
    validate_sku_inputs(sku_inputs, expected_input_count=expected_input_count)

    products = load_products(db_path)
    rules = load_rules(db_path)
    exchange_rates = load_exchange_rates(db_path)
    validate_database_snapshot(products, rules, exchange_rates)

    results = process_sku_inputs(sku_inputs, products, rules, exchange_rates)
    stats = validate_processing_results(
        results,
        expected_input_count=expected_input_count,
        exchange_rates=exchange_rates,
        rules=rules,
    )

    risk_counts = {level: 0 for level in RiskLevel}
    disposition_counts = {disposition: 0 for disposition in Disposition}
    for result in results:
        disposition_counts[result.disposition] += 1
        if result.disposition == Disposition.NORMAL and isinstance(
            result.risk, RiskLevel
        ):
            risk_counts[result.risk] += 1

    timestamp = generated_at or datetime.now().astimezone()
    return ComplianceRun(
        results=tuple(results),
        sku_inputs=tuple(sku_inputs),
        stats=stats,
        products=dict(products),
        rules=tuple(rules),
        exchange_rates=dict(exchange_rates),
        source_filename=source_filename or path.name,
        source_sha256=_sha256_file(path),
        source_size_bytes=path.stat().st_size,
        generated_at=timestamp,
        risk_counts=risk_counts,
        disposition_counts=disposition_counts,
        top_categories=tuple(top_risk_categories(results)),
    )


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _detail_record(result: ProcessingResult, index: int) -> dict[str, object]:
    product = result.product
    is_missing = result.disposition == Disposition.MISSING
    return {
        "index": index,
        "sku_ref": result.sku_input.sku_ref,
        "batch_no": result.sku_input.batch_no,
        "line_no": result.sku_input.line_no,
        "mapped_sku_id": result.mapped_sku_id,
        "disposition": result.disposition.value,
        "name": None if is_missing or product is None else product.name,
        "category": None if is_missing or product is None else product.category,
        "material": None if is_missing or product is None else product.material,
        "origin": None if is_missing or product is None else product.origin,
        "original_price": (
            None if is_missing or product is None else _money(product.price)
        ),
        "currency": None if is_missing or product is None else product.currency,
        "cny_price": (
            _money(result.cny_price) if result.cny_price is not None else None
        ),
        "risk": result.risk.value if result.risk is not None else None,
        "matched_rule_ids": list(result.matched_rule_ids),
    }


def _integrity_payload(run: ComplianceRun) -> dict[str, object]:
    stats = run.stats
    equations = [
        {
            "name": name,
            "left": left,
            "terms": list(terms),
            "holds": holds,
            "expression": f"{left} = " + " + ".join(str(term) for term in terms),
        }
        for name, left, terms, holds in stats.equation_rows()
    ]
    risk_total = sum(run.risk_counts.values())
    risk_holds = risk_total == stats.n_evaluated
    return {
        "stats": {
            "n_input": stats.n_input,
            "n_matched": stats.n_matched,
            "n_deleted": stats.n_deleted,
            "n_missing": stats.n_missing,
            "n_evaluated": stats.n_evaluated,
        },
        "equations": equations,
        "equations_hold": stats.equations_hold,
        "risk_equation": {
            "left": risk_total,
            "terms": [
                run.risk_counts[RiskLevel.COMPLIANT],
                run.risk_counts[RiskLevel.PENDING],
                run.risk_counts[RiskLevel.VIOLATION],
            ],
            "right": stats.n_evaluated,
            "holds": risk_holds,
            "expression": (
                f"{run.risk_counts[RiskLevel.COMPLIANT]} + "
                f"{run.risk_counts[RiskLevel.PENDING]} + "
                f"{run.risk_counts[RiskLevel.VIOLATION]} = {risk_total}"
            ),
        },
        "all_hold": stats.equations_hold and risk_holds,
    }


def build_result_payload(
    run: ComplianceRun,
    job_id: str,
    *,
    links: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """构造 API/任务快照共用的 JSON 结构。

    这里只做对象到 JSON 的呈现转换，不重新判断风险或换算价格。
    """

    return {
        "job_id": job_id,
        "status": "completed",
        "version": REPORT_VERSION,
        "generated_at": run.generated_at.isoformat(timespec="seconds"),
        "source": {
            "filename": run.source_filename,
            "sha256": run.source_sha256,
            "size_bytes": run.source_size_bytes,
        },
        "summary": {
            "n_input": run.stats.n_input,
            "n_matched": run.stats.n_matched,
            "n_deleted": run.stats.n_deleted,
            "n_missing": run.stats.n_missing,
            "n_evaluated": run.stats.n_evaluated,
            "risk_counts": {
                level.value: run.risk_counts[level] for level in RiskLevel
            },
            "disposition_counts": {
                disposition.value: run.disposition_counts[disposition]
                for disposition in Disposition
            },
            "risk_ratio": round(run.risk_ratio, 4),
        },
        "integrity": _integrity_payload(run),
        "top_categories": [
            {
                "category": item.category,
                "violations": item.violations,
                "pending": item.pending,
                "total": item.total,
            }
            for item in run.top_categories
        ],
        "rules": [
            {
                "rule_id": rule.rule_id,
                "title": rule.title,
                "severity": rule.severity,
                "applies_to_category": rule.applies_to_category,
                "material_keyword": rule.material_keyword,
                "origin_keyword": rule.origin_keyword,
            }
            for rule in run.rules
        ],
        "details": [
            _detail_record(result, index)
            for index, result in enumerate(run.results, start=1)
        ],
        "links": dict(links or {}),
    }


def build_details_csv(run: ComplianceRun) -> str:
    """把统一结果渲染为 UTF-8 BOM CSV；不重新计算业务字段。"""

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "序号",
            "sku_ref",
            "batch_no",
            "行号",
            "映射SKU",
            "处置结果",
            "商品名称",
            "类目",
            "材质",
            "产地",
            "原价",
            "币种",
            "人民币价格",
            "风险标记",
            "命中规则编号",
        ]
    )
    for index, result in enumerate(run.results, start=1):
        row = _detail_record(result, index)
        writer.writerow(
            [
                row["index"],
                row["sku_ref"],
                row["batch_no"],
                row["line_no"],
                row["mapped_sku_id"],
                row["disposition"],
                row["name"] if row["name"] is not None else MISSING_PLACEHOLDER,
                row["category"]
                if row["category"] is not None
                else MISSING_PLACEHOLDER,
                row["material"]
                if row["material"] is not None
                else MISSING_PLACEHOLDER,
                row["origin"]
                if row["origin"] is not None
                else MISSING_PLACEHOLDER,
                row["original_price"]
                if row["original_price"] is not None
                else MISSING_PLACEHOLDER,
                row["currency"] if row["currency"] is not None else MISSING_PLACEHOLDER,
                row["cny_price"]
                if row["cny_price"] is not None
                else (
                    NOT_APPLICABLE_PLACEHOLDER
                    if result.disposition == Disposition.DELETED
                    else MISSING_PLACEHOLDER
                ),
                row["risk"]
                if row["risk"] is not None
                else (
                    NOT_APPLICABLE_PLACEHOLDER
                    if result.disposition == Disposition.DELETED
                    else MISSING_PLACEHOLDER
                ),
                ";".join(row["matched_rule_ids"]) or (
                    NOT_APPLICABLE_PLACEHOLDER
                    if result.disposition == Disposition.DELETED
                    else MISSING_PLACEHOLDER
                ),
            ]
        )
    return "﻿" + output.getvalue()


def _verify_csv_file(path: Path, expected_rows: int) -> None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ReportError(f"CSV 文件复核失败: {exc}") from exc
    if len(rows) != expected_rows + 1:
        raise ReportError(
            f"CSV 明细行数应为 {expected_rows}，实际为 {max(len(rows) - 1, 0)}"
        )


def write_details_csv(run: ComplianceRun, output_path: str | Path) -> Path:
    """原子写入 CSV 明细并复核行数。"""

    path = Path(output_path)
    tmp_path = path.with_name(path.name + ".tmp")
    content = build_details_csv(run)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise ReportError(f"写入 CSV 明细失败: {path}: {exc}") from exc
    try:
        _verify_csv_file(path, len(run.results))
    except ReportError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path
