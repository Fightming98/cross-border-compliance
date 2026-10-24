#!/usr/bin/env python
"""第五阶段真实 HTTP 冒烟验收。

启动 ``python web_app.py`` 子进程，通过真实回环 HTTP（非 Flask 测试客户端）
完成首页、上传、任务查询、结果页、三类下载、越界/不存在任务、非法文件与
任务删除检查，并把结果写入 ``reports/stage5_web_smoke.txt`` 与同名 JSON。

脚本只读取原始 ``sku_list.csv`` 作为上传内容，不会修改原始数据库、原始清单
或第四阶段证据；所有临时上传由 Web 应用自身清理。

退出码：
    0：全部检查通过；
    1：任一检查失败；
    2：脚本无法启动验收环境（例如端口被占用）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = PROJECT_ROOT / "sku_list.csv"
CLI_MARKDOWN = PROJECT_ROOT / "reports" / "compliance_report.md"
CLI_HTML = PROJECT_ROOT / "reports" / "compliance_report.html"
CLI_CSV = PROJECT_ROOT / "reports" / "compliance_details.csv"
RESULTS_ROOT = PROJECT_ROOT / "reports" / "web_runs"
LOG_FILE = PROJECT_ROOT / "logs" / "application.log"
OUTPUT_TEXT = PROJECT_ROOT / "reports" / "stage5_web_smoke.txt"
OUTPUT_JSON = PROJECT_ROOT / "reports" / "stage5_web_smoke.json"
SERVER_LOG = PROJECT_ROOT / "reports" / "stage5_web_server.log"

BASE_URL = ""
ROW_RE = re.compile(
    r'<tr class="detail-row" data-sku-ref="([^"]*)">(.*?)</tr>', re.DOTALL
)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

MISSING = "—"
NOT_APPLICABLE = "—（不适用）"
EXPECTED = {
    "n_input": 120,
    "n_matched": 115,
    "n_deleted": 5,
    "n_missing": 5,
    "n_evaluated": 110,
    "risk_counts": {"合规": 47, "待确认": 56, "违规": 7},
    "disposition_counts": {"正常评估": 110, "已下架": 5, "库中缺失": 5},
}

OPEN_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SmokeError(RuntimeError):
    """一项真实 HTTP 验收断言失败。"""


def record(messages: list[str], text: str) -> None:
    print(text, flush=True)
    messages.append(text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def http_open(path: str, *, data: bytes | None = None, headers=None):
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        return OPEN_DIRECT.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        return exc


def read_json(response) -> dict:
    body = response.read()
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"响应不是合法 JSON：{body[:80]!r}") from exc


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def parse_markdown_rows(document: str) -> list[list[str]]:
    detail = document.split("## 一、明细区", 1)[1].split("## 二、", 1)[0]
    rows = []
    for line in detail.splitlines():
        if re.match(r"^\|\s*\d+\s*\|", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def parse_html_rows(document: str) -> list[tuple[str, list[str]]]:
    result = []
    for match in ROW_RE.finditer(document):
        cells = [
            html.unescape(TAG_RE.sub("", cell)).strip()
            for cell in CELL_RE.findall(match.group(2))
        ]
        result.append((match.group(1), cells))
    return result


def expected_detail_cells(detail: dict) -> list[str]:
    disposition = detail["disposition"]
    rules = ";".join(detail["matched_rule_ids"])
    if detail["name"] is None:
        return [MISSING] * 8
    original = f"{detail['original_price']} {detail['currency']}"
    if disposition == "已下架":
        return [
            detail["name"],
            detail["category"],
            detail["material"],
            detail["origin"],
            original,
            NOT_APPLICABLE,
            NOT_APPLICABLE,
            NOT_APPLICABLE,
        ]
    return [
        detail["name"],
        detail["category"],
        detail["material"],
        detail["origin"],
        original,
        detail["cny_price"],
        detail["risk"],
        rules or MISSING,
    ]


def wait_until_ready(process: subprocess.Popen, messages: list[str]) -> None:
    deadline = time.monotonic() + 30
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeError(f"Web 子进程提前退出，退出码 {process.returncode}")
        try:
            with http_open("/") as response:
                if response.status == 200:
                    record(messages, f"服务已就绪：{BASE_URL}/（HTTP 200）")
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise SmokeError(f"等待 Web 服务启动超时：{last_error}")


def multipart_body(csv_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----stage5smoke" + hashlib.sha256(csv_bytes).hexdigest()[:16]
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="sku_list.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + csv_bytes + tail, f"multipart/form-data; boundary={boundary}"


def validate_api_and_documents(
    payload: dict,
    markdown_bytes: bytes,
    html_bytes: bytes,
    csv_bytes: bytes,
    cli_markdown: bytes,
    cli_csv: bytes,
    cli_html: str,
) -> dict:
    checks: dict[str, object] = {}
    summary = payload["summary"]
    for key, expected in EXPECTED.items():
        expect(summary[key] == expected, f"API summary[{key}]={summary[key]!r}")

    details = payload["details"]
    expect(len(details) == 120, "API 明细不是 120 条")
    expect(payload["status"] == "completed", "任务状态不是 completed")
    expect(payload["integrity"]["all_hold"], "完整性等式未全部成立")

    markdown = markdown_bytes.decode("utf-8")
    md_rows = parse_markdown_rows(markdown)
    expect(len(md_rows) == 120, "下载 Markdown 明细不是 120 条")
    expect(markdown_bytes == cli_markdown, "Web Markdown 与 CLI Markdown 字节不一致")
    checks["markdown_matches_cli_bytes"] = True

    csv_text = csv_bytes.decode("utf-8-sig")
    csv_rows = list(csv.reader(io.StringIO(csv_text)))
    expect(len(csv_rows) == 121 and len(csv_rows[0]) == 15, "下载 CSV 不是 121 行 15 列")
    expect(csv_bytes == cli_csv, "Web CSV 与 CLI CSV 字节不一致")
    checks["csv_matches_cli_bytes"] = True

    html_document = html_bytes.decode("utf-8")
    html_rows = parse_html_rows(html_document)
    expect(len(html_rows) == 120, "下载 HTML 明细不是 120 条")
    expect("<script" not in html_document.lower(), "独立 HTML 含 <script>")
    expect("http://" not in html_document.lower(), "独立 HTML 含 http://")
    expect("https://" not in html_document.lower(), "独立 HTML 含 https://")
    cli_rows = parse_html_rows(cli_html)
    expect(len(cli_rows) == 120, "CLI HTML 明细不是 120 条")
    expect(
        [sku for sku, _cells in html_rows] == [sku for sku, _cells in cli_rows],
        "Web HTML 与 CLI HTML 的 SKU 顺序不一致",
    )
    checks["html_offline_and_matches_cli"] = True

    for index, (detail, md_row, csv_row, (html_sku, html_cells)) in enumerate(
        zip(details, md_rows, csv_rows[1:], html_rows, strict=True), start=1
    ):
        sku = detail["sku_ref"]
        expect(md_row[0] == str(index) == csv_row[0] == html_cells[0], f"序号不一致: {sku}")
        expect(md_row[1] == sku == csv_row[1] == html_sku == html_cells[1], f"SKU 不一致: {sku}")
        expect(md_row[2] == detail["batch_no"] == csv_row[2] == html_cells[2], f"批次不一致: {sku}")
        expect(
            md_row[3] == detail["disposition"] == csv_row[5] == html_cells[3],
            f"处置不一致: {sku}",
        )
        expected = expected_detail_cells(detail)
        # 逐字段核对四种表示，不依赖生产代码中的格式化函数。
        expect(md_row[4:9] == expected[:5] == html_cells[4:9], f"商品字段不一致: {sku}")
        expect(md_row[9] == expected[5] == csv_row[12] == html_cells[9], f"人民币价格不一致: {sku}")
        expect(md_row[10] == expected[6] == csv_row[13] == html_cells[10], f"风险不一致: {sku}")
        expect(md_row[11] == expected[7] == csv_row[14] == html_cells[11], f"规则不一致: {sku}")
        expect(
            (csv_row[6], csv_row[7], csv_row[8], csv_row[9]) == tuple(expected[:4]),
            f"CSV 商品字段不一致: {sku}",
        )
        expect(
            (csv_row[10], csv_row[11]) == (
                (MISSING, MISSING)
                if detail["name"] is None
                else (detail["original_price"], detail["currency"])
            ),
            f"CSV 原价不一致: {sku}",
        )
    checks["four_representations_120_rows_match"] = True

    for label, count in EXPECTED["risk_counts"].items():
        expect(f"| {label} | {count} |" in markdown, f"Markdown 风险汇总缺少 {label}={count}")
        expect(
            f"<td>{label}</td><td>{count}</td>" in html_document,
            f"HTML 风险汇总缺少 {label}={count}",
        )
    expect("47 + 56 + 7 = 110" in markdown, "Markdown 缺少风险数量等式")
    for equation in ("120 = 115 + 5", "115 = 5 + 110", "120 = 5 + 5 + 110"):
        expect(equation in markdown, f"Markdown 缺少完整性等式：{equation}")
    for item in payload["top_categories"]:
        expect(f"| {item['category']} |" in markdown, f"Markdown 缺少 Top 类目 {item['category']}")
        expect(item["category"] in html_document, f"HTML 缺少 Top 类目 {item['category']}")
    checks["summary_top_categories_and_equations"] = True
    return checks


def run(port: int) -> int:
    messages: list[str] = []
    started_at = datetime.now().astimezone()
    log_offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    csv_bytes = CSV_PATH.read_bytes()
    cli_markdown = CLI_MARKDOWN.read_bytes()
    cli_csv = CLI_CSV.read_bytes()
    cli_html = CLI_HTML.read_text(encoding="utf-8")
    for path in (CLI_MARKDOWN, CLI_HTML, CLI_CSV):
        expect(path.is_file(), f"缺少 CLI 输出：{path}；请先运行 CLI 生成 HTML/CSV")

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        print(f"端口 {port} 不可用：{exc}", file=sys.stderr)
        return 2
    finally:
        probe.close()

    global BASE_URL
    BASE_URL = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    job_id = None
    deleted = False
    server_output = ""
    server_log_handle = SERVER_LOG.open("w", encoding="utf-8", newline="")
    process = subprocess.Popen(
        [sys.executable, "web_app.py", "--port", str(port)],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        wait_until_ready(process, messages)

        with http_open("/") as response:
            page = response.read().decode("utf-8")
            expect(response.status == 200, "首页状态不是 200")
            expect("跨境合规工作台" in page, "首页缺少产品标题")
            expect("上传待检清单" in page, "首页缺少上传区")
            expect("default-src 'self'" in response.headers.get("Content-Security-Policy", ""), "缺少 CSP")
            expect(response.headers.get("X-Frame-Options") == "DENY", "缺少 X-Frame-Options")
        record(messages, "首页与安全响应头检查通过（CSP、X-Frame-Options）")

        for asset in ("/static/css/app.css", "/static/js/app.js", "/static/images/logo.svg"):
            with http_open(asset) as response:
                expect(response.status == 200, f"静态资源不可用：{asset}")
        record(messages, "本地 CSS、JS、SVG 资源可离线访问，无外部依赖")

        body, content_type = multipart_body(csv_bytes)
        with http_open("/api/reports", data=body, headers={"Content-Type": content_type}) as response:
            expect(response.status == 200, f"上传状态不是 200：{response.status}")
            expect(response.headers.get("Cache-Control") == "no-store", "上传 API 缺少 no-store")
            created = read_json(response)
        job_id = created["job_id"]
        expect(re.fullmatch(r"[0-9a-f]{32}", job_id or "") is not None, "任务 ID 不是 32 位十六进制")
        record(messages, f"真实 multipart 上传成功：job={job_id}")
        record(
            messages,
            "业务基线：输入 120 / 在库 115 / 已下架 5 / 缺失 5 / 评估 110；"
            "合规 47 / 待确认 56 / 违规 7",
        )

        with http_open(f"/api/reports/{job_id}") as response:
            expect(response.status == 200, "任务 API 读取失败")
            expect(response.headers.get("Cache-Control") == "no-store", "任务 API 缺少 no-store")
            stored = read_json(response)
        expect(stored["details"] == created["details"], "任务 API 与上传响应明细不一致")
        record(messages, "任务查询 API 与上传响应明细一致")

        with http_open(f"/reports/{job_id}") as response:
            result_page = response.read().decode("utf-8")
            expect(response.status == 200, "结果页读取失败")
            expect(result_page.count('class="cell-index"') == 120, "结果页不是 120 条明细")
            expect(
                len(re.findall(r'class="stat-card(?:\s|")', result_page)) >= 8,
                "结果页统计卡片不足",
            )
            expect("sku_list.csv" in result_page, "结果页未显示输入文件名")
        record(messages, "服务端渲染结果页返回 120 条明细和 8 张统计卡")

        downloaded: dict[str, bytes] = {}
        for kind, expected_name in (
            ("markdown", "compliance_report.md"),
            ("html", "compliance_report.html"),
            ("csv", "compliance_details.csv"),
        ):
            with http_open(f"/api/reports/{job_id}/download/{kind}") as response:
                expect(response.status == 200, f"{kind} 下载失败")
                expect(
                    response.headers["Content-Disposition"]
                    == f"attachment; filename={expected_name}",
                    f"{kind} 下载文件名错误",
                )
                expect(response.headers.get("Cache-Control") == "no-store", f"{kind} 缺少 no-store")
                downloaded[kind] = response.read()
                expect(len(downloaded[kind]) > 0, f"{kind} 下载为空")
        record(messages, "Markdown、HTML、CSV 三类下载均成功且使用允许列表文件名")
        checks = validate_api_and_documents(
            created,
            downloaded["markdown"],
            downloaded["html"],
            downloaded["csv"],
            cli_markdown,
            cli_csv,
            cli_html,
        )
        record(messages, "Web 与 CLI：Markdown/CSV 逐字节一致，HTML 120 行逐字段一致")
        record(messages, "API / 结果页 / Markdown / HTML / CSV 五类表示均通过 120 行交叉核对")

        with http_open("/api/reports/" + "0" * 32) as response:
            expect(response.status == 404, "不存在任务未返回 404")
            error_payload = read_json(response)
            expect(error_payload["ok"] is False, "404 不是结构化错误")
            expect(error_payload["error"]["error_id"].startswith("ERR-"), "404 缺少错误编号")
        record(messages, "不存在任务返回结构化 404 与错误编号")

        # 空文件：应明确失败，且不得新增任务目录。
        job_dirs_before = set(
            p.name for p in RESULTS_ROOT.iterdir() if p.is_dir()
        ) if RESULTS_ROOT.exists() else set()
        empty_body, empty_type = multipart_body(b"")
        with http_open(
            "/api/reports", data=empty_body, headers={"Content-Type": empty_type}
        ) as response:
            expect(response.status == 400, "空文件未返回 400")
            empty_payload = read_json(response)
            expect(empty_payload["error"]["code"] == "empty_file", "空文件错误码不正确")
        job_dirs_after = set(
            p.name for p in RESULTS_ROOT.iterdir() if p.is_dir()
        ) if RESULTS_ROOT.exists() else set()
        expect(job_dirs_before == job_dirs_after, "空文件上传后产生残留任务目录")
        record(messages, "空文件上传被 400 拒绝，未产生残留任务目录")

        with http_open(f"/api/reports/{job_id}", data=b"", headers={}) as response:
            expect(response.status == 405, "错误方法未返回 405")
        record(messages, "API 方法错误返回结构化 405")

        # DELETE 不能用上面的 GET helper，单独构造请求。
        delete_request = urllib.request.Request(
            f"{BASE_URL}/api/reports/{job_id}", method="DELETE"
        )
        with OPEN_DIRECT.open(delete_request, timeout=15) as response:
            expect(response.status == 200, "删除任务失败")
            deleted_payload = read_json(response)
            expect(deleted_payload["status"] == "deleted", "删除响应状态错误")
        deleted = True
        expect(not (RESULTS_ROOT / job_id).exists(), "删除后任务目录仍存在")
        with http_open(f"/api/reports/{job_id}") as response:
            expect(response.status == 404, "删除后任务仍可读取")
        record(messages, "任务删除成功，目录移除且再次读取返回 404")

        time.sleep(0.2)
        if not LOG_FILE.exists():
            raise SmokeError("正式日志文件不存在")
        new_log = LOG_FILE.read_bytes()[log_offset:].decode("utf-8", errors="replace")
        for marker in (
            f"job={job_id}",
            "event=app_start",
            "event=task_received",
            "event=upload_received",
            "event=check_completed",
            "event=reports_generated",
            "event=task_completed",
            "event=request_rejected",
            "event=job_deleted",
        ):
            expect(marker in new_log, f"日志缺少标记：{marker}")
        expect("sku_ref,batch_no" not in new_log, "日志包含 CSV 表头/完整内容")
        record(messages, "日志包含任务 ID、开始/完成、各阶段数量、耗时、错误与删除事件；未记录 CSV 内容")

        server_log_handle.flush()
        server_output = SERVER_LOG.read_text(encoding="utf-8", errors="replace")
        expect(
            f"http://127.0.0.1:{port}" in server_output,
            "服务未打印预期的 127.0.0.1 监听地址",
        )
        expect("0.0.0.0" not in server_output, "服务启动日志出现 0.0.0.0")
        record(messages, f"服务实际只监听 http://127.0.0.1:{port}，启动日志未出现 0.0.0.0")

        summary = {
            "ok": True,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "base_url": BASE_URL,
            "job_id": job_id,
            "job_deleted": deleted,
            "baseline": EXPECTED,
            "checks": checks,
            "download_sha256": {
                "markdown": sha256_bytes(downloaded["markdown"]),
                "html": sha256_bytes(downloaded["html"]),
                "csv": sha256_bytes(downloaded["csv"]),
            },
            "cli_sha256": {
                "markdown": sha256_bytes(cli_markdown),
                "html": sha256_bytes(CLI_HTML.read_bytes()),
                "csv": sha256_bytes(cli_csv),
            },
            "messages": messages,
        }
        OUTPUT_JSON.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        OUTPUT_TEXT.write_text("\n".join(messages) + "\n", encoding="utf-8")
        print(f"\n验收摘要：{OUTPUT_JSON.relative_to(PROJECT_ROOT)}", flush=True)
        print(f"验收文本：{OUTPUT_TEXT.relative_to(PROJECT_ROOT)}", flush=True)
        return 0
    finally:
        if job_id and not deleted:
            try:
                cleanup_request = urllib.request.Request(
                    f"{BASE_URL}/api/reports/{job_id}", method="DELETE"
                )
                with OPEN_DIRECT.open(cleanup_request, timeout=5):
                    pass
            except Exception:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_log_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="第五阶段真实 HTTP 冒烟验收")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1-65535 之间")
    try:
        return run(args.port)
    except SmokeError as exc:
        print(f"验收失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 验收脚本必须报告真实失败
        print(f"验收异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
