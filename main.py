"""跨境商品合规检查助手命令行入口。"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from src.compliance_assistant.application_service import (
    run_compliance_check,
    write_details_csv,
)
from src.compliance_assistant.errors import ComplianceError
from src.compliance_assistant.html_report import generate_html_report
from src.compliance_assistant.models import RiskLevel
from src.compliance_assistant.report import generate_report, verify_report_file

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = PROJECT_ROOT / "sku_list.csv"
DEFAULT_DB = PROJECT_ROOT / "compliance.db"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "compliance_report.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="跨境商品合规检查助手：读取 SKU 清单并生成 Markdown 合规报告。"
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"待检清单 CSV（默认: {DEFAULT_CSV.name}）",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"合规数据库（默认: {DEFAULT_DB.name}）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"报告输出路径（默认: {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--html-output",
        type=Path,
        default=None,
        help="可选：同时生成独立 HTML 报告到指定路径",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=None,
        help="可选：同时导出 CSV 明细到指定路径",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=120,
        help="期望的清单记录数（默认: 120，用于完整性校验）",
    )
    return parser


def run(
    csv_path: str | Path,
    db_path: str | Path,
    output_path: str | Path,
    expected_count: int = 120,
    *,
    html_output: str | Path | None = None,
    csv_output: str | Path | None = None,
) -> Path:
    """执行完整流程并返回 Markdown 报告路径；任何失败都抛出 ComplianceError。"""

    outcome = run_compliance_check(
        csv_path,
        db_path,
        expected_input_count=expected_count,
    )

    report_path = generate_report(
        outcome.results,
        output_path,
        expected_input_count=expected_count,
    )
    # 落盘后再次复核，确保交给用户的确实是一份完整报告。
    verify_report_file(
        report_path,
        expected_detail_rows=len(outcome.results),
        expected_sku_refs=[item.sku_ref for item in outcome.sku_inputs],
    )

    html_path = None
    if html_output is not None:
        html_path = generate_html_report(outcome, html_output)
    csv_path = None
    if csv_output is not None:
        csv_path = write_details_csv(outcome, csv_output)

    stats = outcome.stats
    risk_counts = outcome.risk_counts
    print("跨境商品合规检查完成")
    print(f"  清单读取数: {stats.n_input}")
    print(f"  在库数（含已下架）: {stats.n_matched}")
    print(f"  已下架数: {stats.n_deleted}")
    print(f"  库中缺失数: {stats.n_missing}")
    print(f"  实际评估数: {stats.n_evaluated}")
    print(
        "  风险统计: "
        f"合规 {risk_counts[RiskLevel.COMPLIANT]} / "
        f"待确认 {risk_counts[RiskLevel.PENDING]} / "
        f"违规 {risk_counts[RiskLevel.VIOLATION]}"
    )
    print(f"  报告路径: {report_path}")
    if html_path is not None:
        print(f"  HTML 报告路径: {html_path}")
    if csv_path is not None:
        print(f"  CSV 明细路径: {csv_path}")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.expected_count < 1:
        parser.error("--expected-count 必须为正整数")

    try:
        run(
            args.csv,
            args.db,
            args.output,
            args.expected_count,
            html_output=args.html_output,
            csv_output=args.csv_output,
        )
    except ComplianceError as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - 入口需对未预期异常给出非零退出码
        traceback.print_exc()
        print("处理失败: 发生未预期异常，详见上方堆栈。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
