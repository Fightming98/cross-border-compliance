"""独立核对 Markdown、HTML、CSV 三个导出入口的业务字段。

只读取已经落盘的文件，不调用业务模块；用于第五阶段跨入口验收。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKDOWN = PROJECT_ROOT / "reports" / "compliance_report.md"
DEFAULT_HTML = PROJECT_ROOT / "reports" / "compliance_report.html"
DEFAULT_CSV = PROJECT_ROOT / "reports" / "compliance_details.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "reports" / "stage5_cross_entry_check.txt"

EXPECTED = {
    "n_input": 120,
    "n_matched": 115,
    "n_deleted": 5,
    "n_missing": 5,
    "n_evaluated": 110,
    "compliant": 47,
    "pending": 56,
    "violation": 7,
}

PLACEHOLDER = "—"
NOT_APPLICABLE = "—（不适用）"


class CrossEntryError(RuntimeError):
    """跨入口核对失败。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _split_markdown_row(line: str) -> list[str]:
    """按未转义的 | 拆分 Markdown 表格行，并还原转义字符。"""

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.strip().strip("|"):
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append(_normalize("".join(current)))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append(_normalize("".join(current)))
    return cells


def _parse_markdown(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    in_detail = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 一、明细区"):
            in_detail = True
            continue
        if in_detail and line.startswith("## "):
            break
        if not in_detail or not line.startswith("|"):
            continue
        if re.match(r"^\|\s*:?-{3,}", line) or "sku_ref" in line:
            continue
        cells = _split_markdown_row(line)
        if cells and cells[0].isdigit():
            rows.append(cells)
    return rows


def _parse_csv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for item in reader:
            original = (
                f"{item['原价']} {item['币种']}"
                if item["原价"] != PLACEHOLDER
                else PLACEHOLDER
            )
            rows.append(
                [
                    _normalize(item["序号"]),
                    _normalize(item["sku_ref"]),
                    _normalize(item["batch_no"]),
                    _normalize(item["处置结果"]),
                    _normalize(item["商品名称"]),
                    _normalize(item["类目"]),
                    _normalize(item["材质"]),
                    _normalize(item["产地"]),
                    _normalize(original),
                    _normalize(item["人民币价格"]),
                    _normalize(item["风险标记"]),
                    _normalize(item["命中规则编号"]),
                ]
            )
    return rows


def _parse_html(path: Path) -> list[list[str]]:
    document = path.read_text(encoding="utf-8")
    rows: list[list[str]] = []
    for match in re.finditer(
        r'<tr class="detail-row"[^>]*>(.*?)</tr>', document, re.DOTALL
    ):
        body = match.group(1)
        cells = [
            _normalize(html.unescape(re.sub(r"<[^>]+>", "", cell)))
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", body, re.DOTALL)
        ]
        if len(cells) != 12:
            raise CrossEntryError(f"HTML 明细列数异常：{len(cells)}")
        rows.append(cells)
    lowered = document.lower()
    for forbidden in ("<script", "http://", "https://", "<link rel=\"stylesheet\""):
        if forbidden in lowered:
            raise CrossEntryError(f"HTML 报告含外部资源或脚本：{forbidden}")
    return rows


def _assert_equal(markdown, html_rows, csv_rows) -> None:
    if not (len(markdown) == len(html_rows) == len(csv_rows) == EXPECTED["n_input"]):
        raise CrossEntryError(
            "明细行数不一致："
            f"Markdown={len(markdown)} HTML={len(html_rows)} CSV={len(csv_rows)}"
        )
    for index, (md_row, html_row, csv_row) in enumerate(
        zip(markdown, html_rows, csv_rows), start=1
    ):
        if not (md_row == html_row == csv_row):
            raise CrossEntryError(
                f"第 {index} 条明细不一致：Markdown={md_row} "
                f"HTML={html_row} CSV={csv_row}"
            )


def _summary_counts(csv_rows: list[list[str]]) -> dict[str, int]:
    dispositions = {"正常评估": 0, "已下架": 0, "库中缺失": 0}
    risks = {"合规": 0, "待确认": 0, "违规": 0}
    for row in csv_rows:
        disposition = row[3]
        if disposition not in dispositions:
            raise CrossEntryError(f"未知处置结果：{disposition}")
        dispositions[disposition] += 1
        risk = row[10]
        if risk in risks:
            risks[risk] += 1
        elif disposition == "正常评估":
            raise CrossEntryError(f"正常评估商品缺少风险：{row[1]}")

    actual = {
        "n_input": len(csv_rows),
        "n_matched": len(csv_rows) - dispositions["库中缺失"],
        "n_deleted": dispositions["已下架"],
        "n_missing": dispositions["库中缺失"],
        "n_evaluated": dispositions["正常评估"],
        "compliant": risks["合规"],
        "pending": risks["待确认"],
        "violation": risks["违规"],
    }
    if actual != EXPECTED:
        raise CrossEntryError(f"汇总与基线不一致：{actual} != {EXPECTED}")
    if actual["n_evaluated"] != sum(risks.values()):
        raise CrossEntryError("风险数量之和与正常评估数不一致")
    if actual["n_input"] != actual["n_matched"] + actual["n_missing"]:
        raise CrossEntryError("清单数 != 在库数 + 缺失数")
    if actual["n_matched"] != actual["n_deleted"] + actual["n_evaluated"]:
        raise CrossEntryError("在库数 != 已下架数 + 实际评估数")
    return actual


def run(markdown_path: Path, html_path: Path, csv_path: Path) -> list[str]:
    markdown_rows = _parse_markdown(markdown_path)
    html_rows = _parse_html(html_path)
    csv_rows = _parse_csv(csv_path)
    _assert_equal(markdown_rows, html_rows, csv_rows)
    counts = _summary_counts(csv_rows)

    lines = [
        "跨入口核对通过：Markdown / HTML / CSV 各 120 条明细逐字段一致",
        (
            "风险汇总一致：合规 {compliant} / 待确认 {pending} / 违规 {violation}；"
            "{compliant} + {pending} + {violation} = {n_evaluated}"
        ).format(**counts),
        (
            "完整性等式：120 = 115 + 5；115 = 5 + 110；120 = 5 + 5 + 110"
        ),
        "HTML 离线检查：无 <script>、无 http(s) 外部引用",
    ]
    for path in (markdown_path, html_path, csv_path):
        lines.append(f"SHA-256 {path.name}: {_sha256(path)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对 Stage 5 三个导出入口的一致性")
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        lines = run(args.markdown, args.html, args.csv)
    except (OSError, CrossEntryError) as exc:
        print(f"跨入口核对失败：{exc}", file=sys.stderr)
        return 1

    text = "\n".join(lines) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
