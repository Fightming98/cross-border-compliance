#!/usr/bin/env python
"""第四阶段正式验收编排脚本。

脚本只读取原始 CSV/SQLite，调用生产程序生成正式报告，并用测试侧独立
参考实现逐 SKU 对账。所有证据写入新的 ``evidence/stage4/<timestamp>/``
目录，绝不覆盖已有目录。

退出码约定：
    0：所有验收检查通过；
    1：验收执行完成，但存在命令失败或业务核对失败；
    2：脚本自身无法完成验收（例如无法创建证据目录）。

本脚本不修改原始数据库、原始 CSV 或历史审计文件。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.compliance_assistant.loader import (  # noqa: E402
    load_exchange_rates,
    load_products,
    load_rules,
    load_sku_inputs,
)
from src.compliance_assistant.processor import process_sku_inputs  # noqa: E402
from src.compliance_assistant.report import top_risk_categories  # noqa: E402
from tests.test_independent_reconciliation import (  # noqa: E402
    _load_reference,
    _reference_results,
    _reference_top,
)

CSV_PATH = PROJECT_ROOT / "sku_list.csv"
DB_PATH = PROJECT_ROOT / "compliance.db"
REPORT_PATH = PROJECT_ROOT / "reports" / "compliance_report.md"
AUDIT_SUMMARY = PROJECT_ROOT / "audit" / "07_数据审计汇总.json"
EVIDENCE_ROOT = PROJECT_ROOT / "evidence" / "stage4"
ACCEPTANCE_SCRIPT = PROJECT_ROOT / "scripts" / "run_stage4_acceptance.py"

DETAIL_HEADING = "## 一、明细区"
SUMMARY_HEADING = "## 二、汇总区"
INTEGRITY_HEADING = "## 三、完整性自检区"

DISPOSITION_MISSING = "库中缺失"
DISPOSITION_DELETED = "已下架"
DISPOSITION_NORMAL = "正常评估"
RISK_COMPLIANT = "合规"
RISK_PENDING = "待确认"
RISK_VIOLATION = "违规"

# 题目给出的验收基线。仅用于确认本次原始数据仍符合验收前提；生产代码
# 不读取这些常量，业务结果全部由独立参考实现重新计算。
EXPECTED_BASELINE = {
    "input_count": 120,
    "in_stock_count": 115,
    "deleted_count": 5,
    "missing_count": 5,
    "evaluated_count": 110,
    "risk_counts": {RISK_COMPLIANT: 47, RISK_PENDING: 56, RISK_VIOLATION: 7},
    "deleted_refs": ["00001", "00002", "00003", "00004", "00005"],
    "missing_refs": ["00031", "00057", "00084", "00099", "00113"],
    "top_categories": [
        ("内衣", 4, 6, 10),
        ("箱包", 3, 18, 21),
        ("配饰", 0, 13, 13),
        ("鞋靴", 0, 11, 11),
        ("上衣", 0, 5, 5),
    ],
}

REQUIRED_PATHS = [
    "instructions.md",
    "data-dictionary.md",
    "seed.py",
    "sku_list.csv",
    "compliance.db",
    "audit",
    "main.py",
    "src/compliance_assistant",
    "tests",
    "README.md",
    "requirements.txt",
    "docs/AI_USAGE.md",
    "docs/踩坑与验收说明.md",
    "docs/第三阶段代码审查与修复报告.md",
    "reports/compliance_report.md",
    "reports/test_results.txt",
    "reports/run_log.txt",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def unique_run_dir() -> Path:
    base = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    candidate = EVIDENCE_ROOT / base
    suffix = 1
    while candidate.exists():
        candidate = EVIDENCE_ROOT / f"{base}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def hash_tree(root: Path) -> list[dict[str, object]]:
    records = []
    if not root.is_dir():
        return records
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        records.append(
            {
                "path": path.relative_to(PROJECT_ROOT).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def git_info() -> dict[str, object]:
    def run(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    commit = run(["rev-parse", "HEAD"])
    status = run(["status", "--short"])
    return {
        "branch": branch,
        "commit": commit,
        "status_short": status.splitlines() if status else [],
    }


def run_command(
    index: int,
    name: str,
    args: list[str],
    log_dir: Path,
) -> dict[str, object]:
    stdout_path = log_dir / f"{index:02d}_{name}.stdout.log"
    stderr_path = log_dir / f"{index:02d}_{name}.stderr.log"
    # 任务书推荐 01_compile.log 等直接位于证据目录根部；原始 stdout/stderr
    # 仍保存在 logs/ 子目录，便于逐项核查。
    combined_path = log_dir.parent / f"{index:02d}_{name}.log"
    started_at = now_iso()
    started = time.time()

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"

    error = None
    try:
        completed = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            timeout=900,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = (exc.stderr or b"") + b"\nCOMMAND TIMEOUT AFTER 900s\n"
        exit_code = None
        error = "timeout"
    except Exception as exc:  # noqa: BLE001 - 验收脚本必须记录真实失败
        stdout = b""
        stderr = f"{type(exc).__name__}: {exc}\n".encode("utf-8")
        exit_code = None
        error = f"{type(exc).__name__}: {exc}"

    finished_at = now_iso()
    duration = time.time() - started
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    display = " ".join(args)
    combined = (
        f"COMMAND: {display}\n"
        f"STARTED: {started_at}\n"
        f"FINISHED: {finished_at}\n"
        f"EXIT CODE: {exit_code}\n"
        f"STDOUT:\n{stdout_text}\n"
        f"STDERR:\n{stderr_text}\n"
    )
    combined_path.write_text(combined, encoding="utf-8")

    return {
        "index": index,
        "name": name,
        "argv": args,
        "command": display,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration, 3),
        "exit_code": exit_code,
        "expected_exit_code": 0,
        "passed": exit_code == 0,
        "error": error,
        "stdout_path": stdout_path.relative_to(PROJECT_ROOT).as_posix(),
        "stderr_path": stderr_path.relative_to(PROJECT_ROOT).as_posix(),
        "log_path": combined_path.relative_to(PROJECT_ROOT).as_posix(),
        "stdout_text": stdout_text,
        "stderr_text": stderr_text,
    }


def parse_unittest_counts(text: str) -> dict[str, object]:
    total = None
    duration = None
    failures = 0
    errors = 0
    match = re.search(r"Ran (\d+) tests? in ([\d.]+)s", text)
    if match:
        total = int(match.group(1))
        duration = float(match.group(2))
    failed = re.search(r"FAILED \((.*?)\)", text)
    if failed:
        for key in ("failures", "errors"):
            item = re.search(rf"{key}=(\d+)", failed.group(1))
            if item:
                if key == "failures":
                    failures = int(item.group(1))
                else:
                    errors = int(item.group(1))
    return {
        "total": total,
        "duration_seconds": duration,
        "failures": failures,
        "errors": errors,
        "ok": "OK" in text and failures == 0 and errors == 0,
    }


def split_markdown_row(line: str) -> list[str]:
    """按未转义的 ``|`` 拆分表格行，并还原 ``\\|``。"""

    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]

    cells: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "|":
            buffer.append("|")
            index += 2
            continue
        if char == "|":
            cells.append("".join(buffer).strip())
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    cells.append("".join(buffer).strip())
    return cells


def section(text: str, start: str, end: str | None = None) -> str:
    if start not in text:
        return ""
    body = text.split(start, 1)[1]
    if end is not None:
        body = body.split(end, 1)[0]
    return body


def parse_detail_rows(text: str) -> tuple[list[list[str]], list[str], list[str]]:
    detail = section(text, DETAIL_HEADING, SUMMARY_HEADING)
    header: list[str] = []
    rows: list[list[str]] = []
    raw_rows: list[str] = []
    for line in detail.splitlines():
        if not line.startswith("|"):
            continue
        cells = split_markdown_row(line)
        if cells and cells[0] == "序号":
            header = cells
            continue
        if cells and re.fullmatch(r"\d+", cells[0]):
            rows.append(cells)
            raw_rows.append(line)
    return header, rows, raw_rows


def product_price_text(product: dict[str, object] | None) -> str:
    if product is None:
        return ""
    return f"{Decimal(str(product['price'])):.2f} {product['currency']}"


def expected_row_values(item: dict[str, object]) -> dict[str, str]:
    disposition = item["disposition"]
    product = item["product"]
    if disposition == DISPOSITION_MISSING:
        return {
            "name": "—",
            "category": "—",
            "material": "—",
            "origin": "—",
            "original_price": "—",
            "cny_price": "—",
            "risk": "—",
            "rule_ids": "—",
        }
    assert product is not None
    values = {
        "name": product["name"],
        "category": product["category"],
        "material": product["material"],
        "origin": product["origin"],
        "original_price": product_price_text(product),
    }
    if disposition == DISPOSITION_DELETED:
        values.update(
            {
                "cny_price": "—（不适用）",
                "risk": "—（不适用）",
                "rule_ids": "—（不适用）",
            }
        )
    else:
        cny = item["cny_price"]
        assert isinstance(cny, Decimal)
        values.update(
            {
                "cny_price": f"{cny:.2f}",
                "risk": item["risk"],
                "rule_ids": ";".join(item["rule_ids"]) or "—",
            }
        )
    return values


def report_row_values(row: list[str]) -> dict[str, str]:
    if len(row) < 12:
        return {key: "" for key in (
            "name", "category", "material", "origin", "original_price",
            "cny_price", "risk", "rule_ids",
        )}
    return {
        "name": row[4],
        "category": row[5],
        "material": row[6],
        "origin": row[7],
        "original_price": row[8],
        "cny_price": row[9],
        "risk": row[10],
        "rule_ids": row[11],
    }


def compare_report_row(expected: dict[str, str], row: list[str]) -> list[str]:
    actual = report_row_values(row)
    issues = []
    for key, expected_value in expected.items():
        actual_value = actual[key]
        if actual_value != expected_value:
            issues.append(f"{key}: 期望 {expected_value!r}，实际 {actual_value!r}")
    return issues


def main() -> int:
    started_at = now_iso()
    run_dir = unique_run_dir()
    log_dir = run_dir / "logs"
    log_dir.mkdir()

    # 验收开始时的哈希基线；用于证明验收过程未修改原始/审计文件。
    originals_before = {
        path.name: sha256_file(path)
        for path in [
            PROJECT_ROOT / "instructions.md",
            PROJECT_ROOT / "data-dictionary.md",
            PROJECT_ROOT / "seed.py",
            CSV_PATH,
            DB_PATH,
        ]
    }
    audit_before = hash_tree(PROJECT_ROOT / "audit")
    code_paths = [PROJECT_ROOT / "main.py"]
    code_paths.extend(sorted((PROJECT_ROOT / "src").rglob("*.py")))
    test_paths = sorted((PROJECT_ROOT / "tests").rglob("*.py"))
    code_paths.extend(test_paths)
    code_paths.append(ACCEPTANCE_SCRIPT)
    code_before = {
        path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path)
        for path in code_paths
        if path.is_file()
    }

    commands = [
        run_command(
            1,
            "compile",
            [sys.executable, "-m", "compileall", "-q", "main.py", "src", "tests"],
            log_dir,
        ),
        run_command(
            2,
            "unit_tests",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            log_dir,
        ),
        run_command(
            3,
            "independent_reconciliation",
            [
                sys.executable,
                "-m",
                "unittest",
                "-v",
                "tests.test_independent_reconciliation",
            ],
            log_dir,
        ),
        run_command(
            4,
            "main_run",
            [sys.executable, "main.py"],
            log_dir,
        ),
    ]

    commands_passed = all(bool(item["passed"]) for item in commands)
    unit_counts = parse_unittest_counts(
        commands[1]["stdout_text"] + "\n" + commands[1]["stderr_text"]
    )
    independent_counts = parse_unittest_counts(
        commands[2]["stdout_text"] + "\n" + commands[2]["stderr_text"]
    )
    main_passed = bool(commands[3]["passed"])
    report_exists = REPORT_PATH.is_file()
    report_mtime_ns = REPORT_PATH.stat().st_mtime_ns if report_exists else None
    main_started_epoch = commands[3].get("started_at")
    # 报告文件必须在本次 main 运行期间写入；用 ns 时间戳与命令开始时间
    # 做粗略比较，避免把历史报告误认为本次产物。
    report_written_during_run = False
    if report_exists:
        try:
            main_started_ns = int(
                datetime.fromisoformat(main_started_epoch).timestamp() * 1_000_000_000
            )
            report_written_during_run = report_mtime_ns >= main_started_ns - 2_000_000_000
        except (TypeError, ValueError):
            report_written_during_run = False

    checks: dict[str, bool] = {}
    integrity_lines: list[str] = []
    summary: dict[str, object] = {
        "run_id": run_dir.name,
        "generated_at": None,
        "input_count": None,
        "in_stock_count": None,
        "deleted_count": None,
        "missing_count": None,
        "evaluated_count": None,
        "risk_counts": None,
        "top_categories": None,
        "deleted_skus": None,
        "missing_skus": None,
        "report_detail_rows": None,
        "test_total": unit_counts["total"],
        "test_duration_seconds": unit_counts["duration_seconds"],
        "test_failures": unit_counts["failures"],
        "test_errors": unit_counts["errors"],
        "independent_test_total": independent_counts["total"],
        "independent_test_failures": independent_counts["failures"],
        "independent_test_errors": independent_counts["errors"],
        "integrity_equations_passed": False,
        "sku_reconciliation_passed": False,
        "price_reconciliation_passed": False,
        "top_categories_passed": False,
        "markdown_structure_passed": False,
        "source_hashes_unchanged": False,
        "audit_hashes_unchanged": False,
        "overall_passed": False,
    }
    mismatch_records: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = bool(ok)
        integrity_lines.append(
            f"[{'PASS' if ok else 'FAIL'}] {name}"
            + (f": {detail}" if detail else "")
        )

    try:
        missing_paths = [
            rel for rel in REQUIRED_PATHS if not (PROJECT_ROOT / rel).exists()
        ]
        check("验收所需文件/目录齐全", not missing_paths, f"缺失={missing_paths}")

        check(
            "编译检查退出码为 0",
            commands[0]["exit_code"] == 0,
            f"实际退出码={commands[0]['exit_code']}",
        )
        check(
            "全量测试退出码为 0 且无失败/错误",
            bool(unit_counts["ok"])
            and commands[1]["exit_code"] == 0
            and unit_counts["total"] is not None,
            f"总数={unit_counts['total']}, 失败={unit_counts['failures']}, "
            f"错误={unit_counts['errors']}, 退出码={commands[1]['exit_code']}",
        )
        check(
            "独立对账测试退出码为 0 且无失败/错误",
            bool(independent_counts["ok"])
            and commands[2]["exit_code"] == 0
            and independent_counts["total"] is not None,
            f"总数={independent_counts['total']}, "
            f"失败={independent_counts['failures']}, "
            f"错误={independent_counts['errors']}, "
            f"退出码={commands[2]['exit_code']}",
        )
        check(
            "正式程序退出码为 0",
            main_passed,
            f"实际退出码={commands[3]['exit_code']}",
        )
        check("正式报告存在且可读", report_exists and report_written_during_run,
              f"存在={report_exists}, 本次运行写入={report_written_during_run}")

        reference_rows, reference_products, reference_rules, reference_rates = (
            _load_reference()
        )
        expected = _reference_results(
            reference_rows,
            reference_products,
            reference_rules,
            reference_rates,
        )
        expected_dispositions = Counter(
            item["disposition"] for item in expected
        )
        expected_risk_counts = Counter(
            item["risk"]
            for item in expected
            if item["disposition"] == DISPOSITION_NORMAL
        )
        expected_input_count = len(expected)
        expected_deleted_count = expected_dispositions[DISPOSITION_DELETED]
        expected_missing_count = expected_dispositions[DISPOSITION_MISSING]
        expected_normal_count = expected_dispositions[DISPOSITION_NORMAL]
        expected_in_stock_count = expected_deleted_count + expected_normal_count
        expected_deleted_refs = sorted(
            item["sku_ref"]
            for item in expected
            if item["disposition"] == DISPOSITION_DELETED
        )
        expected_missing_refs = sorted(
            item["sku_ref"]
            for item in expected
            if item["disposition"] == DISPOSITION_MISSING
        )
        expected_top = _reference_top(expected)

        summary.update(
            {
                "input_count": expected_input_count,
                "in_stock_count": expected_in_stock_count,
                "deleted_count": expected_deleted_count,
                "missing_count": expected_missing_count,
                "evaluated_count": expected_normal_count,
                "risk_counts": {
                    RISK_COMPLIANT: expected_risk_counts[RISK_COMPLIANT],
                    RISK_PENDING: expected_risk_counts[RISK_PENDING],
                    RISK_VIOLATION: expected_risk_counts[RISK_VIOLATION],
                },
                "top_categories": [
                    {
                        "rank": index,
                        "category": category,
                        "violations": violations,
                        "pending": pending,
                        "total": total,
                    }
                    for index, (category, violations, pending, total) in enumerate(
                        expected_top, start=1
                    )
                ],
                "deleted_skus": expected_deleted_refs,
                "missing_skus": expected_missing_refs,
            }
        )

        check(
            "独立参考实现处置数量符合验收基线",
            (
                expected_input_count,
                expected_in_stock_count,
                expected_deleted_count,
                expected_missing_count,
                expected_normal_count,
            )
            == (
                EXPECTED_BASELINE["input_count"],
                EXPECTED_BASELINE["in_stock_count"],
                EXPECTED_BASELINE["deleted_count"],
                EXPECTED_BASELINE["missing_count"],
                EXPECTED_BASELINE["evaluated_count"],
            ),
            f"实际={expected_input_count}/{expected_in_stock_count}/"
            f"{expected_deleted_count}/{expected_missing_count}/"
            f"{expected_normal_count}",
        )
        equations_hold = (
            expected_input_count == expected_in_stock_count + expected_missing_count
            and expected_in_stock_count
            == expected_deleted_count + expected_normal_count
            and expected_input_count
            == expected_missing_count + expected_deleted_count + expected_normal_count
        )
        summary["integrity_equations_passed"] = equations_hold
        check(
            "处置完整性等式成立",
            equations_hold,
            f"{expected_input_count} = {expected_in_stock_count} + "
            f"{expected_missing_count}; {expected_in_stock_count} = "
            f"{expected_deleted_count} + {expected_normal_count}; "
            f"{expected_input_count} = {expected_missing_count} + "
            f"{expected_deleted_count} + {expected_normal_count}",
        )
        check(
            "独立参考实现风险数量符合验收基线",
            dict(expected_risk_counts) == EXPECTED_BASELINE["risk_counts"],
            f"实际={dict(expected_risk_counts)}",
        )
        check(
            "已下架/缺失 SKU 集合符合验收基线",
            expected_deleted_refs == EXPECTED_BASELINE["deleted_refs"]
            and expected_missing_refs == EXPECTED_BASELINE["missing_refs"],
            f"已下架={expected_deleted_refs}, 缺失={expected_missing_refs}",
        )
        check(
            "独立参考实现 Top 类目符合验收基线",
            expected_top == EXPECTED_BASELINE["top_categories"],
            f"实际={expected_top}",
        )

        # 生产结果：直接调用生产加载/处理模块，仅作为被核对对象。
        inputs = load_sku_inputs(CSV_PATH)
        products = load_products(DB_PATH)
        rules = load_rules(DB_PATH)
        rates = load_exchange_rates(DB_PATH)
        actual_results = process_sku_inputs(inputs, products, rules, rates)

        actual_mismatches: list[str] = []
        if len(actual_results) != expected_input_count:
            actual_mismatches.append(
                f"生产结果数 {len(actual_results)} != {expected_input_count}"
            )
        for index, (item, result) in enumerate(
            zip(expected, actual_results), start=1
        ):
            if item["sku_ref"] != result.sku_input.sku_ref:
                actual_mismatches.append(
                    f"#{index} sku_ref: {item['sku_ref']!r} != "
                    f"{result.sku_input.sku_ref!r}"
                )
            if item["batch_no"] != result.sku_input.batch_no:
                actual_mismatches.append(
                    f"#{index} batch_no: {item['batch_no']!r} != "
                    f"{result.sku_input.batch_no!r}"
                )
            if item["line_no"] != result.sku_input.line_no:
                actual_mismatches.append(
                    f"#{index} line_no: {item['line_no']} != "
                    f"{result.sku_input.line_no}"
                )
            actual_risk = result.risk.value if result.risk is not None else None
            if item["disposition"] != result.disposition.value:
                actual_mismatches.append(
                    f"#{index} disposition: {item['disposition']!r} != "
                    f"{result.disposition.value!r}"
                )
            if item["risk"] != actual_risk:
                actual_mismatches.append(
                    f"#{index} risk: {item['risk']!r} != {actual_risk!r}"
                )
            if item["rule_ids"] != result.matched_rule_ids:
                actual_mismatches.append(
                    f"#{index} rule_ids: {item['rule_ids']!r} != "
                    f"{result.matched_rule_ids!r}"
                )
            if item["cny_price"] != result.cny_price:
                actual_mismatches.append(
                    f"#{index} cny: {item['cny_price']!r} != "
                    f"{result.cny_price!r}"
                )
            if item["product"] is None:
                if result.product is not None:
                    actual_mismatches.append(
                        f"#{index} 缺失商品却返回商品对象"
                    )
            elif result.product is None:
                actual_mismatches.append(f"#{index} 生产结果缺少商品对象")
            else:
                for field in ("sku_id", "name", "category", "material", "origin", "currency", "is_deleted"):
                    if item["product"][field] != getattr(result.product, field):
                        actual_mismatches.append(
                            f"#{index} {field}: {item['product'][field]!r} != "
                            f"{getattr(result.product, field)!r}"
                        )
                if Decimal(str(item["product"]["price"])) != result.product.price:
                    actual_mismatches.append(f"#{index} price 不一致")
        check(
            "生产结果与独立参考逐 SKU 一致",
            not actual_mismatches,
            f"差异={len(actual_mismatches)}",
        )
        if actual_mismatches:
            integrity_lines.extend(
                f"  - 生产差异: {line}" for line in actual_mismatches[:20]
            )

        normal_expected = [
            (item, result)
            for item, result in zip(expected, actual_results)
            if item["disposition"] == DISPOSITION_NORMAL
        ]
        price_mismatches = [
            (item["sku_ref"], item["cny_price"], result.cny_price)
            for item, result in normal_expected
            if item["cny_price"] != result.cny_price
            or result.cny_price is None
        ]
        price_ok = (
            len(normal_expected) == expected_normal_count
            and not price_mismatches
        )
        summary["price_reconciliation_passed"] = price_ok
        check(
            "人民币价格逐 SKU 独立复算一致",
            price_ok,
            f"正常评估={len(normal_expected)}, 差异={len(price_mismatches)}",
        )
        if price_mismatches:
            integrity_lines.extend(
                f"  - 价格差异: {item}" for item in price_mismatches[:20]
            )

        actual_top = [
            (stat.category, stat.violations, stat.pending, stat.total)
            for stat in top_risk_categories(actual_results)
        ]
        top_production_ok = tuple(actual_top) == tuple(expected_top)
        check(
            "生产 Top 类目与独立聚合一致",
            top_production_ok,
            f"生产={actual_top}, 独立={expected_top}",
        )

        if report_exists:
            report_text = REPORT_PATH.read_text(encoding="utf-8")
        else:
            report_text = ""

        header, detail_rows, raw_rows = parse_detail_rows(report_text)
        report_structure_issues: list[str] = []
        if "# 跨境商品合规检查报告" not in report_text:
            report_structure_issues.append("缺少报告标题")
        for heading in (DETAIL_HEADING, SUMMARY_HEADING, INTEGRITY_HEADING):
            if report_text.count(heading) != 1:
                report_structure_issues.append(f"分节数量异常: {heading}")
        if header != [
            "序号", "sku_ref", "batch_no", "处置结果", "商品名", "类目",
            "材质", "产地", "原价", "人民币价格", "风险标记", "依据规则",
        ]:
            report_structure_issues.append(f"明细表头不符: {header}")
        if len(detail_rows) != expected_input_count:
            report_structure_issues.append(
                f"明细行数 {len(detail_rows)} != {expected_input_count}"
            )
        if [row[0] for row in detail_rows] != [
            str(index) for index in range(1, expected_input_count + 1)
        ]:
            report_structure_issues.append("明细序号不连续")
        for row in detail_rows:
            if len(row) != 12:
                report_structure_issues.append(f"明细行列数异常: {len(row)}")
                break
        if "\r" in report_text:
            report_structure_issues.append("报告含裸 CR 字符")

        report_mismatches: list[str] = []
        report_rows_by_ref: dict[str, list[str]] = {}
        for row in detail_rows:
            if len(row) >= 3:
                report_rows_by_ref.setdefault(row[1], row)
        if len(report_rows_by_ref) != len(detail_rows):
            report_mismatches.append("报告存在重复 sku_ref")
        for index, item in enumerate(expected):
            row = detail_rows[index] if index < len(detail_rows) else None
            if row is None:
                report_mismatches.append(f"#{index + 1} 缺少明细行")
                continue
            if row[1] != item["sku_ref"]:
                report_mismatches.append(
                    f"#{index + 1} sku_ref: 期望 {item['sku_ref']!r}，"
                    f"实际 {row[1]!r}"
                )
            if row[2] != item["batch_no"]:
                report_mismatches.append(
                    f"#{index + 1} batch_no: 期望 {item['batch_no']!r}，"
                    f"实际 {row[2]!r}"
                )
            expected_values = expected_row_values(item)
            issues = compare_report_row(expected_values, row)
            if issues:
                report_mismatches.append(
                    f"#{index + 1} {item['sku_ref']}: " + "; ".join(issues)
                )
            report_values = report_row_values(row)
            mismatch_detail = "; ".join(issues)
            mismatch_records.append(
                {
                    "seq": index + 1,
                    "sku_ref": item["sku_ref"],
                    "batch_no": item["batch_no"],
                    "expected_disposition": item["disposition"],
                    "actual_disposition": (
                        actual_results[index].disposition.value
                        if index < len(actual_results)
                        else ""
                    ),
                    "expected_risk": item["risk"] or "",
                    "actual_risk": (
                        actual_results[index].risk.value
                        if index < len(actual_results)
                        and actual_results[index].risk is not None
                        else ""
                    ),
                    "expected_rule_ids": ";".join(item["rule_ids"]),
                    "actual_rule_ids": (
                        ";".join(actual_results[index].matched_rule_ids)
                        if index < len(actual_results)
                        else ""
                    ),
                    "expected_cny_price": (
                        f"{item['cny_price']:.2f}"
                        if item["cny_price"] is not None
                        else ""
                    ),
                    "actual_cny_price": (
                        f"{actual_results[index].cny_price:.2f}"
                        if index < len(actual_results)
                        and actual_results[index].cny_price is not None
                        else ""
                    ),
                    "expected_original_price": product_price_text(item["product"]),
                    "actual_original_price": product_price_text(
                        None
                        if index >= len(actual_results)
                        else (
                            {
                                "price": actual_results[index].product.price,
                                "currency": actual_results[index].product.currency,
                            }
                            if actual_results[index].product is not None
                            else None
                        )
                    ),
                    "report_disposition": row[3],
                    "report_risk": report_values["risk"],
                    "report_rule_ids": report_values["rule_ids"],
                    "report_cny_price": report_values["cny_price"],
                    "report_original_price": report_values["original_price"],
                    "conclusion": "一致" if not issues else "不一致",
                    "mismatch_detail": mismatch_detail,
                }
            )

        if len(detail_rows) == len(expected):
            report_row_ok = not report_mismatches
        else:
            report_row_ok = False
        summary["report_detail_rows"] = len(detail_rows)
        summary["sku_reconciliation_passed"] = (
            not actual_mismatches
            and not report_mismatches
            and report_row_ok
        )
        check(
            "正式报告逐行与独立参考/生产结果一致",
            summary["sku_reconciliation_passed"],
            f"报告行={len(detail_rows)}, 报告差异={len(report_mismatches)}, "
            f"生产差异={len(actual_mismatches)}",
        )
        if report_mismatches:
            integrity_lines.extend(
                f"  - 报告差异: {line}" for line in report_mismatches[:20]
            )

        # 汇总区风险表与风险等式
        risk_section = section(report_text, "### 2.1", "### 2.2")
        risk_rows = re.findall(
            r"^\|\s*(合规|待确认|违规)\s*\|\s*(\d+)\s*\|", risk_section, re.M
        )
        report_risk_counts = {label: int(value) for label, value in risk_rows}
        risk_table_ok = report_risk_counts == dict(expected_risk_counts)
        check(
            "报告风险汇总表与独立复算一致",
            risk_table_ok,
            f"报告={report_risk_counts}, 独立={dict(expected_risk_counts)}",
        )
        risk_equation = (
            "三档风险数量之和："
            f"{expected_risk_counts[RISK_COMPLIANT]} + "
            f"{expected_risk_counts[RISK_PENDING]} + "
            f"{expected_risk_counts[RISK_VIOLATION]} = "
            f"{sum(expected_risk_counts.values())}；"
            f"实际评估数：{expected_normal_count}。"
        )
        risk_equation_ok = risk_equation in report_text
        check("报告风险数量等式可见且正确", risk_equation_ok, risk_equation)

        # Top 类目表
        top_section = section(report_text, "### 2.2", INTEGRITY_HEADING)
        report_top: list[tuple[str, int, int, int]] = []
        for line in top_section.splitlines():
            if not line.startswith("|"):
                continue
            cells = split_markdown_row(line)
            if cells and re.fullmatch(r"[1-5]", cells[0]):
                if len(cells) >= 5 and all(
                    re.fullmatch(r"\d+", value) for value in cells[2:5]
                ):
                    report_top.append(
                        (cells[1], int(cells[2]), int(cells[3]), int(cells[4]))
                    )
        top_report_ok = report_top == expected_top
        summary["top_categories_passed"] = top_production_ok and top_report_ok
        check(
            "Top 风险类目独立聚合、生产结果与报告一致",
            top_production_ok and top_report_ok,
            f"独立={expected_top}, 生产={actual_top}, 报告={report_top}",
        )

        # 完整性自检区
        integrity_section = section(report_text, INTEGRITY_HEADING)
        metric_rows = re.findall(
            r"^\|\s*(清单读取数|在库数（含已下架）|已下架数|缺失数|实际评估数)"
            r"\s*\|\s*(\d+)\s*\|",
            integrity_section,
            re.M,
        )
        report_metrics = {label: int(value) for label, value in metric_rows}
        expected_metrics = {
            "清单读取数": expected_input_count,
            "在库数（含已下架）": expected_in_stock_count,
            "已下架数": expected_deleted_count,
            "缺失数": expected_missing_count,
            "实际评估数": expected_normal_count,
        }
        metrics_ok = report_metrics == expected_metrics
        check(
            "报告完整性五数与独立复算一致",
            metrics_ok,
            f"报告={report_metrics}, 独立={expected_metrics}",
        )
        equation_lines = [
            f"- 清单读取数 = 在库数 + 缺失数：{expected_input_count} = "
            f"{expected_in_stock_count} + {expected_missing_count}，成立",
            f"- 在库数 = 已下架数 + 实际评估数：{expected_in_stock_count} = "
            f"{expected_deleted_count} + {expected_normal_count}，成立",
            f"- 清单读取数 = 缺失数 + 已下架数 + 实际评估数："
            f"{expected_input_count} = {expected_missing_count} + "
            f"{expected_deleted_count} + {expected_normal_count}，成立",
        ]
        equations_visible = all(line in report_text for line in equation_lines)
        conclusion_visible = "校验结论：全部成立。" in report_text
        summary["integrity_equations_passed"] = (
            summary["integrity_equations_passed"]
            and risk_table_ok
            and risk_equation_ok
            and metrics_ok
            and equations_visible
            and conclusion_visible
        )
        check(
            "报告完整性三条等式与结论可见",
            equations_visible and conclusion_visible,
            f"等式={equations_visible}, 结论={conclusion_visible}",
        )

        markdown_ok = not report_structure_issues
        summary["markdown_structure_passed"] = markdown_ok
        check(
            "Markdown 报告结构完整（标题/三节/12 列/120 行/无裸 CR）",
            markdown_ok,
            f"问题={report_structure_issues}",
        )
        if report_structure_issues:
            integrity_lines.extend(
                f"  - 结构问题: {line}" for line in report_structure_issues[:20]
            )

        # 原始文件哈希与第一阶段基线
        baseline = json.loads(AUDIT_SUMMARY.read_text(encoding="utf-8"))
        baseline_files = {
            item["path"]: item for item in baseline["source_integrity"]["files"]
        }
        original_records = []
        original_ok = True
        for path in [
            PROJECT_ROOT / "instructions.md",
            PROJECT_ROOT / "data-dictionary.md",
            PROJECT_ROOT / "seed.py",
            CSV_PATH,
            DB_PATH,
        ]:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            baseline_item = baseline_files.get(rel)
            current_sha = sha256_file(path)
            before_sha = originals_before.get(path.name)
            matches = (
                baseline_item is not None
                and current_sha == baseline_item["sha256_after"]
            )
            unchanged = before_sha == current_sha
            original_ok = original_ok and matches and unchanged
            original_records.append(
                {
                    "path": rel,
                    "size": path.stat().st_size,
                    "baseline_sha256": (
                        baseline_item["sha256_after"] if baseline_item else None
                    ),
                    "before_sha256": before_sha,
                    "after_sha256": current_sha,
                    "matches_baseline": matches,
                    "unchanged_during_run": unchanged,
                }
            )
        summary["source_hashes_unchanged"] = original_ok
        check(
            "原始文件与第一阶段 SHA-256 基线一致",
            original_ok,
            f"文件数={len(original_records)}",
        )

        audit_after = hash_tree(PROJECT_ROOT / "audit")
        audit_ok = audit_before == audit_after
        summary["audit_hashes_unchanged"] = audit_ok
        check(
            "audit/ 既有审计文件验收前后哈希一致",
            audit_ok,
            f"文件数={len(audit_before)}",
        )

        sidecars = sorted(
            path.name
            for path in PROJECT_ROOT.iterdir()
            if path.name.startswith("compliance.db-")
        )
        tmp_files = sorted(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in PROJECT_ROOT.rglob("*.tmp")
            if "evidence" not in path.parts
        )
        check("无 SQLite sidecar 文件", not sidecars, f"实际={sidecars}")
        check("无遗留 .tmp 文件", not tmp_files, f"实际={tmp_files}")

        code_after = {
            path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path)
            for path in code_paths
            if path.is_file()
        }
        check("验收期间生产/测试代码哈希未变化", code_before == code_after)

        if report_exists:
            report_sha = sha256_file(REPORT_PATH)
            snapshot = run_dir / "compliance_report.md"
            shutil.copy2(REPORT_PATH, snapshot)
            check(
                "报告快照与正式报告字节一致",
                sha256_file(snapshot) == report_sha,
                f"sha256={report_sha}",
            )
        else:
            report_sha = None

        summary["report_sha256"] = report_sha
        summary["report_path"] = str(REPORT_PATH)
        summary["evidence_dir"] = str(run_dir)
        summary["checks"] = checks
        summary["overall_passed"] = (
            commands_passed
            and bool(unit_counts["ok"])
            and bool(independent_counts["ok"])
            and all(checks.values())
        )
        summary["generated_at"] = now_iso()

        # 逐 SKU 对账 CSV；即使失败也写出，便于定位。
        reconciliation_path = run_dir / "sku_reconciliation.csv"
        with reconciliation_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fieldnames = [
                "seq", "sku_ref", "batch_no",
                "expected_disposition", "actual_disposition",
                "expected_risk", "actual_risk",
                "expected_rule_ids", "actual_rule_ids",
                "expected_cny_price", "actual_cny_price",
                "expected_original_price", "actual_original_price",
                "report_disposition", "report_risk", "report_rule_ids",
                "report_cny_price", "report_original_price",
                "conclusion", "mismatch_detail",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(mismatch_records)

        if report_exists:
            integrity_lines.append(f"[INFO] 报告快照: {snapshot.relative_to(PROJECT_ROOT)}")
        integrity_lines.append(
            f"[INFO] 逐 SKU 对账记录数: {len(mismatch_records)}"
        )
        integrity_lines.append(
            f"[{'PASS' if summary['overall_passed'] else 'FAIL'}] 综合验收结论"
        )
        (run_dir / "05_integrity_checks.log").write_text(
            "\n".join(integrity_lines) + "\n", encoding="utf-8"
        )

    except Exception as exc:  # noqa: BLE001 - 任何验收异常都必须留痕
        summary["overall_passed"] = False
        summary["generated_at"] = now_iso()
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["checks"] = checks
        integrity_lines.append(
            f"[FAIL] 验收验证异常: {type(exc).__name__}: {exc}"
        )
        integrity_lines.append(traceback.format_exc())
        (run_dir / "05_integrity_checks.log").write_text(
            "\n".join(integrity_lines) + "\n", encoding="utf-8"
        )
        # 如果异常发生在 CSV 生成之前，仍生成一个带表头的空对账文件。
        reconciliation_path = run_dir / "sku_reconciliation.csv"
        if not reconciliation_path.exists():
            with reconciliation_path.open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                csv.writer(handle).writerow(
                    [
                        "seq", "sku_ref", "batch_no", "expected_disposition",
                        "actual_disposition", "expected_risk", "actual_risk",
                        "expected_rule_ids", "actual_rule_ids",
                        "expected_cny_price", "actual_cny_price",
                        "expected_original_price", "actual_original_price",
                        "report_disposition", "report_risk", "report_rule_ids",
                        "report_cny_price", "report_original_price",
                        "conclusion", "mismatch_detail",
                    ]
                )

    # 证据元数据与环境信息。
    finished_at = now_iso()
    commands_for_log = []
    for item in commands:
        commands_for_log.append(
            {
                key: value
                for key, value in item.items()
                if key not in ("stdout_text", "stderr_text")
            }
        )
    command_log = {
        "run_id": run_dir.name,
        "started_at": started_at,
        "finished_at": finished_at,
        "commands": commands_for_log,
        "all_exit_codes_expected": commands_passed,
    }
    write_json(run_dir / "command_log.json", command_log)

    environment = {
        "stage": "stage4",
        "run_id": run_dir.name,
        "started_at": started_at,
        "finished_at": finished_at,
        "project_root": str(PROJECT_ROOT),
        "cwd_at_start": str(Path.cwd()),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "system": platform.system(),
        "release": platform.release(),
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "dependencies": "仅 Python 标准库；requirements.txt 无第三方依赖",
        "commands": [item["command"] for item in commands],
        "inputs": {
            "csv": str(CSV_PATH),
            "db": str(DB_PATH),
            "audit_summary": str(AUDIT_SUMMARY),
            "formal_report": str(REPORT_PATH),
        },
        "acceptance_script": str(ACCEPTANCE_SCRIPT),
        "git": git_info(),
        "required_paths": {
            rel: (PROJECT_ROOT / rel).exists() for rel in REQUIRED_PATHS
        },
        "code_hashes_at_start": code_before,
        "code_hashes_at_end": {
            path.relative_to(PROJECT_ROOT).as_posix(): sha256_file(path)
            for path in code_paths
            if path.is_file()
        },
    }
    write_json(run_dir / "environment.json", environment)

    # 源文件哈希清单：原始文件、审计文件、生产代码、测试和正式报告。
    source_hashes = {
        "generated_at": finished_at,
        "project_root": str(PROJECT_ROOT),
        "baseline_source": "audit/07_数据审计汇总.json",
        "original_files": (
            original_records if "original_records" in locals() else []
        ),
        "audit_before": audit_before,
        "audit_after": (
            audit_after if "audit_after" in locals() else hash_tree(PROJECT_ROOT / "audit")
        ),
        "audit_unchanged": (
            audit_ok if "audit_ok" in locals() else False
        ),
        "production_code": hash_tree(PROJECT_ROOT / "src"),
        "main": (
            {
                "path": "main.py",
                "size": (PROJECT_ROOT / "main.py").stat().st_size,
                "sha256": sha256_file(PROJECT_ROOT / "main.py"),
            }
        ),
        "tests": hash_tree(PROJECT_ROOT / "tests"),
        "scripts": (
            {
                "path": "scripts/run_stage4_acceptance.py",
                "size": ACCEPTANCE_SCRIPT.stat().st_size,
                "sha256": sha256_file(ACCEPTANCE_SCRIPT),
            }
        ),
        "formal_report": (
            {
                "path": "reports/compliance_report.md",
                "size": REPORT_PATH.stat().st_size,
                "sha256": sha256_file(REPORT_PATH),
            }
            if report_exists
            else None
        ),
    }
    write_json(run_dir / "source_hashes.json", source_hashes)
    write_json(run_dir / "acceptance_summary.json", summary)

    # 人类可读摘要。
    top_text = "\n".join(
        f"| {item['rank']} | {item['category']} | {item['violations']} | "
        f"{item['pending']} | {item['total']} |"
        for item in (summary.get("top_categories") or [])
    )
    command_rows = "\n".join(
        f"| {item['name']} | `{item['command']}` | {item['exit_code']} | "
        f"{'通过' if item['passed'] else '失败'} | {item['log_path']} |"
        for item in commands_for_log
    )
    summary_md = f"""# 第四阶段验收摘要（自动生成）

> 运行标识：`{run_dir.name}`
> 生成时间：{summary.get('generated_at')}
> 项目根目录：`{PROJECT_ROOT}`

## 命令结果

| 步骤 | 命令 | 退出码 | 结果 | 日志 |
|---|---|---:|---|---|
{command_rows}

## 独立复算

- 清单读取数：{summary.get('input_count')}
- 在库数（含已下架）：{summary.get('in_stock_count')}
- 已下架数：{summary.get('deleted_count')}
- 库中缺失数：{summary.get('missing_count')}
- 实际评估数：{summary.get('evaluated_count')}
- 风险统计：{summary.get('risk_counts')}
- 测试总数：{summary.get('test_total')}；独立对账测试数：{summary.get('independent_test_total')}

## Top 风险类目

| 排名 | 类目 | 违规 | 待确认 | 风险商品总数 |
|---:|---|---:|---:|---:|
{top_text}

## 检查结论

- 处置等式：{summary.get('integrity_equations_passed')}
- 逐 SKU 对账：{summary.get('sku_reconciliation_passed')}
- 价格复算：{summary.get('price_reconciliation_passed')}
- Top 类目：{summary.get('top_categories_passed')}
- Markdown 结构：{summary.get('markdown_structure_passed')}
- 原始文件哈希：{summary.get('source_hashes_unchanged')}
- 审计文件哈希：{summary.get('audit_hashes_unchanged')}
- **综合结论：{'通过' if summary.get('overall_passed') else '未通过'}**

详细检查见 `05_integrity_checks.log`；机器可读结果见 `acceptance_summary.json`。
"""
    (run_dir / "acceptance_summary.md").write_text(summary_md, encoding="utf-8")

    # 证据清单与校验和。manifest 不包含自身与 SHA256SUMS，避免循环哈希。
    evidence_files = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in ("evidence_manifest.json", "SHA256SUMS"):
            continue
        evidence_files.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "run_id": run_dir.name,
        "generated_at": now_iso(),
        "description": "第四阶段验收证据包；manifest 自身和 SHA256SUMS 不纳入自身内容哈希。",
        "files": evidence_files,
        "excluded_from_manifest": ["evidence_manifest.json", "SHA256SUMS"],
    }
    write_json(run_dir / "evidence_manifest.json", manifest)

    checksum_files = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name == "SHA256SUMS":
            continue
        checksum_files.append(path)
    checksum_lines = [
        f"{sha256_file(path)}  {path.relative_to(run_dir).as_posix()}"
        for path in checksum_files
    ]
    (run_dir / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print(f"第四阶段验收证据目录: {run_dir}")
    print(f"综合结论: {'PASS' if summary.get('overall_passed') else 'FAIL'}")
    print(
        "处置统计: "
        f"{summary.get('input_count')} 输入 / {summary.get('in_stock_count')} 在库 / "
        f"{summary.get('deleted_count')} 下架 / {summary.get('missing_count')} 缺失 / "
        f"{summary.get('evaluated_count')} 评估"
    )
    print(f"风险统计: {summary.get('risk_counts')}")
    print(f"测试: {summary.get('test_total')} 个，失败 {summary.get('test_failures')}，错误 {summary.get('test_errors')}")
    print(f"验收摘要: {run_dir / 'acceptance_summary.md'}")
    return 0 if summary.get("overall_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
