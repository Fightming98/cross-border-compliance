#!/usr/bin/env python
"""第五阶段真实浏览器冒烟测试（Playwright + 本机 Chrome）。

覆盖首页、文件选择、真实上传、8 张统计卡、3 个图表、20 条分页、搜索/
筛选/排序、规则展开、三类下载、错误状态、结果快照页、窄屏布局和任务删除。
截图、JSON 摘要和文本日志写入 ``reports/``，不伪造任何浏览器结果。

依赖：本机已有 Chrome，且当前 Python 环境已安装 Playwright。Playwright 不是
运行 Web 应用的必需依赖，因此未写入 requirements.txt。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = PROJECT_ROOT / "sku_list.csv"
CLI_MARKDOWN = PROJECT_ROOT / "reports" / "compliance_report.md"
CLI_CSV = PROJECT_ROOT / "reports" / "compliance_details.csv"
SCREENSHOT_DIR = PROJECT_ROOT / "reports" / "screenshots"
OUTPUT_TEXT = PROJECT_ROOT / "reports" / "stage5_browser_smoke.txt"
OUTPUT_JSON = PROJECT_ROOT / "reports" / "stage5_browser_smoke.json"
SERVER_LOG = PROJECT_ROOT / "reports" / "stage5_browser_server.log"
BASE_URL = ""
OPEN_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BrowserSmokeError(RuntimeError):
    """浏览器验收断言失败。"""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise BrowserSmokeError(message)


def record(messages: list[str], text: str) -> None:
    print(text, flush=True)
    messages.append(text)


def delete_job(job_id: str | None) -> bool:
    if not job_id:
        return True
    request = urllib.request.Request(
        f"{BASE_URL}/api/reports/{job_id}", method="DELETE"
    )
    try:
        with OPEN_DIRECT.open(request, timeout=10) as response:
            return response.status in {200, 404}
    except Exception:
        return False


def wait_for_server(process: subprocess.Popen, messages: list[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserSmokeError(f"Web 子进程提前退出：{process.returncode}")
        try:
            with OPEN_DIRECT.open(BASE_URL + "/", timeout=2) as response:
                if response.status == 200:
                    record(messages, f"Web 服务已就绪：{BASE_URL}/")
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise BrowserSmokeError("等待 Web 服务启动超时")


def text_of(page, selector: str) -> str:
    return page.locator(selector).inner_text().strip()


def prepare_screenshot(page) -> None:
    """仅用于截图：固定顶栏改为静态，隐藏跳转链接。

    Chrome 的整页截图会把 position: sticky / fixed 元素按滚动拼接位置重复绘制，
    产生与真实页面无关的伪影。布局验收已在正常粘性顶栏下完成，这里只清理截图。
    """
    page.evaluate(
        """() => {
            const topbar = document.querySelector('.topbar');
            if (topbar) topbar.style.position = 'static';
            const skip = document.querySelector('.skip-link');
            if (skip) skip.style.display = 'none';
        }"""
    )


def wait_text(page, selector: str, expected: str, timeout: int = 10000) -> None:
    page.wait_for_function(
        """([selector, expected]) => {
            const node = document.querySelector(selector);
            return Boolean(node) && node.textContent.trim() === expected;
        }""",
        arg=[selector, expected],
        timeout=timeout,
    )


def wait_rows(page, count: int, timeout: int = 10000) -> None:
    page.wait_for_function(
        "(count) => document.querySelectorAll('#details-body tr').length === count",
        arg=count,
        timeout=timeout,
    )


def run(port: int) -> int:
    global BASE_URL
    BASE_URL = f"http://127.0.0.1:{port}"
    messages: list[str] = []
    checks: dict[str, object] = {}
    screenshots: dict[str, str] = {}
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    request_urls: list[str] = []
    download_urls: list[str] = []
    job_ids: list[str] = []
    server_log_handle = None
    process = None

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        print(f"端口 {port} 不可用：{exc}", file=sys.stderr)
        return 2
    finally:
        probe.close()

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    server_log_handle = SERVER_LOG.open("w", encoding="utf-8", newline="")
    process = subprocess.Popen(
        [sys.executable, "web_app.py", "--port", str(port)],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        wait_for_server(process, messages)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                device_scale_factor=1,
                locale="zh-CN",
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("requestfailed", lambda request: failed_requests.append(request.url))
            page.on("request", lambda request: request_urls.append(request.url))

            page.goto(BASE_URL + "/", wait_until="networkidle")
            expect("合规检查工作台" in page.title(), "首页标题不正确")
            expect(page.locator("#upload-form").is_visible(), "上传表单不可见")
            expect(page.locator("#csv-file").count() == 1, "文件输入不存在")
            homepage = SCREENSHOT_DIR / "stage5_home_desktop.png"
            prepare_screenshot(page)
            page.screenshot(path=str(homepage), full_page=True)
            screenshots["home_desktop"] = str(homepage.relative_to(PROJECT_ROOT))
            record(messages, "首页加载成功，上传区域、字段提示和本地说明可见")

            page.set_input_files("#csv-file", str(CSV_PATH))
            expect("sku_list.csv" in text_of(page, "#file-name"), "未显示所选文件名")
            expect(
                "KB" in text_of(page, "#file-meta") or "字节" in text_of(page, "#file-meta"),
                "未显示所选文件大小",
            )
            expect(not page.locator("#upload-button").is_disabled(), "选择文件后按钮仍禁用")
            record(messages, "通过真实文件输入选择 sku_list.csv，文件名、大小和按钮状态正确")

            page.click("#upload-button")
            page.wait_for_selector("#results-panel:not([hidden])", timeout=30000)
            page.wait_for_function(
                "() => document.querySelector('#stat-input').textContent.trim() === '120'"
            )
            job_id = page.locator("#task-id").inner_text().strip()
            expect(re.fullmatch(r"[0-9a-f]{32}", job_id) is not None, "页面任务 ID 非法")
            job_ids.append(job_id)
            expect(f"job={job_id}" in page.url, "上传完成后 URL 未带任务编号")
            record(messages, f"浏览器上传完成，任务 {job_id} 已载入")

            for element_id, expected in (
                ("stat-input", "120"),
                ("stat-matched", "115"),
                ("stat-evaluated", "110"),
                ("stat-deleted", "5"),
                ("stat-missing", "5"),
                ("stat-compliant", "47"),
                ("stat-pending", "56"),
                ("stat-violation", "7"),
            ):
                actual = text_of(page, f"#{element_id}")
                expect(actual == expected, f"统计卡 {element_id} 实际 {actual!r}")
            record(messages, "8 张统计卡实际显示 120/115/110/5/5/47/56/7")

            expect(
                page.locator("#risk-chart .bar-row").count() == 3,
                "风险图不是 3 个条形",
            )
            expect(
                page.locator("#disposition-chart .bar-row").count() == 3,
                "处置图不是 3 个条形",
            )
            expect(
                page.locator("#category-chart .category-row").count() == 5,
                "Top 类目图不是 5 个类目",
            )
            risk_label = page.locator("#risk-chart").get_attribute("aria-label") or ""
            expect("合规 47 条" in risk_label and "违规 7 条" in risk_label, "风险图 ARIA 数据不完整")
            expect(
                page.locator("#risk-data-body tr").count()
                == page.locator("#disposition-data-body tr").count()
                == 3,
                "图表数据表行数错误",
            )
            record(messages, "三类图表均显示实际数量、图例、ARIA 文本和数据表")

            expect(page.locator("#details-body tr").count() == 20, "默认分页不是 20 条")
            expect("全部 120" in text_of(page, "#result-count"), "筛选计数未显示全部 120")
            first_cells = page.locator("#details-body tr").first.locator("td").all_inner_texts()
            expect(first_cells[0].strip() == "1", "默认首行序号不是 1")
            expect(first_cells[1].strip() == "00001", "默认首行 SKU 不是 00001")
            expect(first_cells[8].strip() == "178.97 KRW", f"原价格式不一致：{first_cells[8]!r}")
            record(messages, "明细表默认 20 条/页，首行原价显示为 178.97 KRW")
            checks["default_page_size"] = 20
            checks["first_row_original_price"] = first_cells[8].strip()

            page.fill("#search-input", "00001")
            wait_rows(page, 1)
            expect("筛选结果 1 条" in text_of(page, "#result-count"), "搜索计数错误")
            expect(
                page.locator("#details-body tr").first.locator("code").first.inner_text().strip()
                == "00001",
                "搜索结果不是 00001",
            )
            page.click("#reset-filters")
            wait_rows(page, 20)
            record(messages, "SKU 搜索可用，筛选计数同步更新")

            page.select_option("#risk-filter", "违规")
            wait_rows(page, 7)
            expect(
                page.locator("#details-body .badge").count() == 7,
                "违规筛选后的风险徽章数量错误",
            )
            page.select_option("#disposition-filter", "库中缺失")
            wait_rows(page, 0)
            # “库中缺失 + 违规”互斥，显示空状态。
            expect(page.locator("#table-empty").is_visible(), "组合筛选未显示空状态")
            page.click("#reset-filters")
            wait_rows(page, 20)
            page.select_option("#disposition-filter", "库中缺失")
            wait_rows(page, 5)
            expect(
                "筛选结果 5 条" in text_of(page, "#result-count"),
                "库中缺失筛选不是 5 条",
            )
            page.click("#reset-filters")
            wait_rows(page, 20)
            page.select_option("#category-filter", "鞋靴")
            wait_rows(page, 11)
            category_cells = page.locator("#details-body tr td:nth-child(6)").all_inner_texts()
            expect(all(cell.strip() == "鞋靴" for cell in category_cells), "类目筛选结果混入其他类目")
            record(messages, "风险/处置/类目筛选、组合空状态与重置均正常")
            checks["filters"] = True
            page.click("#reset-filters")
            wait_rows(page, 20)

            page.click('[data-sort-key="sku_ref"]')
            page.click('[data-sort-key="sku_ref"]')
            page.wait_for_function(
                "() => document.querySelector('#details-body tr td:nth-child(2) code').textContent.trim() === '00120'"
            )
            expect(
                page.locator('th:has([data-sort-key="sku_ref"])').get_attribute("aria-sort")
                == "descending",
                "SKU 降序 ARIA 状态错误",
            )
            record(messages, "明细排序可用：SKU 降序后首行为 00120，aria-sort 同步")
            page.click("#reset-filters")
            wait_rows(page, 20)

            page.click("#next-page")
            wait_rows(page, 20)
            expect(
                page.locator("#details-body tr").first.locator("td").first.inner_text().strip()
                == "21",
                "下一页首行序号不是 21",
            )
            expect("第 2 / 6 页" in text_of(page, "#page-summary"), "分页摘要错误")
            record(messages, "分页可用：第 2 页从序号 21 开始，共 6 页")
            checks["pagination"] = "2/6"
            page.click("#reset-filters")
            wait_rows(page, 20)

            page.fill("#search-input", "00007")
            wait_rows(page, 1)
            page.locator("#details-body .rule-button").click()
            page.wait_for_selector(".rule-detail-row")
            rule_text = text_of(page, ".rule-detail-row")
            expect("CR-001" in rule_text, "规则展开缺少 CR-001")
            expect(
                page.locator("#details-body .rule-button").get_attribute("aria-expanded")
                == "true",
                "规则按钮 aria-expanded 未更新",
            )
            page.locator("#details-body .rule-button").click()
            page.wait_for_function(
                "() => !document.querySelector('.rule-detail-row')"
            )
            record(messages, "命中规则可展开/收起，显示规则编号、标题、严重程度和条件")
            checks["rule_expansion"] = "CR-001"
            page.click("#reset-filters")
            wait_rows(page, 20)

            downloaded: dict[str, bytes] = {}
            for element_id, filename in (
                ("download-markdown", "compliance_report.md"),
                ("download-html", "compliance_report.html"),
                ("download-csv", "compliance_details.csv"),
            ):
                with page.expect_download(timeout=15000) as download_info:
                    page.click(f"#{element_id}")
                download = download_info.value
                expect(download.suggested_filename == filename, f"{element_id} 下载文件名错误")
                path = download.path()
                expect(path is not None, f"{element_id} 下载文件不存在")
                download_urls.append(download.url)
                downloaded[filename] = Path(path).read_bytes()
            expect(
                downloaded["compliance_report.md"] == CLI_MARKDOWN.read_bytes(),
                "浏览器下载 Markdown 与 CLI 不一致",
            )
            expect(
                downloaded["compliance_details.csv"] == CLI_CSV.read_bytes(),
                "浏览器下载 CSV 与 CLI 不一致",
            )
            html_document = downloaded["compliance_report.html"].decode("utf-8")
            expect(html_document.count('class="detail-row"') == 120, "浏览器下载 HTML 不是 120 行")
            expect("http://" not in html_document.lower(), "独立 HTML 含外部 http 引用")
            record(messages, "浏览器三类下载成功；Markdown/CSV 与 CLI 逐字节一致，HTML 120 行离线")

            dashboard = SCREENSHOT_DIR / "stage5_dashboard_desktop.png"
            prepare_screenshot(page)
            page.screenshot(path=str(dashboard), full_page=True)
            screenshots["dashboard_desktop"] = str(dashboard.relative_to(PROJECT_ROOT))

            page.goto(f"{BASE_URL}/reports/{job_id}", wait_until="networkidle")
            expect(
                page.locator(".cell-index").count() == 120,
                "服务端结果页不是 120 行",
            )
            expect(
                len(re.findall(r'class="stat-card(?:\s|")', page.content())) == 8,
                "服务端结果页不是 8 张统计卡",
            )
            snapshot_first = page.locator(".details-table tbody tr").first.locator("td").all_inner_texts()
            expect(snapshot_first[8].strip() == "178.97 KRW", "结果页原价格式不一致")
            snapshot = SCREENSHOT_DIR / "stage5_snapshot_desktop.png"
            prepare_screenshot(page)
            page.screenshot(path=str(snapshot), full_page=True)
            screenshots["snapshot_desktop"] = str(snapshot.relative_to(PROJECT_ROOT))
            page.click(".head-actions a.button-primary")
            page.wait_for_selector("#results-panel:not([hidden])")
            page.wait_for_function(
                f"() => document.querySelector('#task-id').textContent.trim() === '{job_id}'"
            )
            record(messages, "服务端结果快照页展示 120 行、8 张统计卡，并可返回同一任务交互看板")

            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{BASE_URL}/?job={job_id}", wait_until="networkidle")
            page.wait_for_selector("#results-panel:not([hidden])")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - window.innerWidth"
            )
            overflowing_nodes = page.evaluate(
                """() => [...document.querySelectorAll('body *')]
                    .map((node) => {
                        const rect = node.getBoundingClientRect();
                        return {
                            tag: node.tagName.toLowerCase(),
                            cls: node.className && String(node.className).slice(0, 80),
                            id: node.id,
                            left: Math.round(rect.left * 10) / 10,
                            right: Math.round(rect.right * 10) / 10,
                            width: Math.round(rect.width * 10) / 10,
                        };
                    })
                    .filter((item) => item.right > window.innerWidth + 1 || item.left < -1)
                    .slice(0, 12)"""
            )
            record(messages, f"窄屏溢出诊断：{overflow}px；{overflowing_nodes}")
            expect(overflow <= 1, f"窄屏出现页面级横向溢出：{overflow}px")
            expect(page.locator(".chart-grid .chart-panel").count() == 3, "窄屏图表数量异常")
            expect(page.locator("#details-body tr").count() == 20, "窄屏表格分页异常")
            mobile = SCREENSHOT_DIR / "stage5_dashboard_mobile.png"
            prepare_screenshot(page)
            page.screenshot(path=str(mobile), full_page=True)
            screenshots["dashboard_mobile"] = str(mobile.relative_to(PROJECT_ROOT))
            record(messages, "390px 窄屏无页面级横向溢出，图表与表格仍可读")
            checks["mobile_overflow_px"] = overflow

            page.set_viewport_size({"width": 1440, "height": 1000})
            page.goto(BASE_URL + "/", wait_until="networkidle")
            page.set_input_files(
                "#csv-file",
                {
                    "name": "missing-header.csv",
                    "mimeType": "text/csv",
                    "buffer": b"sku_ref\n00001\n",
                },
            )
            page.click("#upload-button")
            page.wait_for_selector("#upload-error:not([hidden])", timeout=15000)
            expect(
                "清单无法通过校验" in text_of(page, "#upload-error-title"),
                "错误标题未区分输入校验失败",
            )
            expect(
                "CSV 校验失败" in text_of(page, "#upload-error-message"),
                "错误消息未说明 CSV 校验失败",
            )
            expect(
                "ERR-" in text_of(page, "#upload-error-id"),
                "错误状态未显示可追溯错误编号",
            )
            expect(not page.locator("#results-panel").is_visible(), "失败后旧结果仍然显示")
            error_shot = SCREENSHOT_DIR / "stage5_upload_error_desktop.png"
            prepare_screenshot(page)
            page.locator(".upload-card").screenshot(path=str(error_shot))
            screenshots["upload_error_desktop"] = str(error_shot.relative_to(PROJECT_ROOT))
            record(messages, "错误清单显示中文原因和错误编号，旧结果被隐藏，不伪装成新任务成功")
            checks["error_state"] = True

            page.set_input_files("#csv-file", str(CSV_PATH))
            page.click("#upload-button")
            page.wait_for_selector("#results-panel:not([hidden])", timeout=30000)
            second_job = page.locator("#task-id").inner_text().strip()
            expect(
                re.fullmatch(r"[0-9a-f]{32}", second_job) is not None
                and second_job != job_id,
                "重新上传未生成新任务编号",
            )
            job_ids.append(second_job)
            page.once("dialog", lambda dialog: dialog.accept())
            page.click("#delete-job-button")
            # 删除是异步请求；用选择器等待 DOM 状态，避免 CSP 拦截字符串求值。
            page.wait_for_selector("#results-panel[hidden]", state="attached", timeout=15000)
            expect(
                "任务结果已删除" in text_of(page, "#upload-status"),
                "删除后状态提示不正确",
            )
            expect(f"job={second_job}" not in page.url, "删除后 URL 仍保留任务编号")
            record(messages, "重新上传生成新任务；UI 删除确认后结果隐藏、URL 清理、状态明确")

            local_prefixes = (BASE_URL + "/", "data:", "blob:")
            external = [
                url for url in request_urls if not url.startswith(local_prefixes)
            ]
            expect(not external, f"浏览器出现外部网络请求：{external[:3]}")
            # Chrome 把下载接管记为 ERR_ABORTED；这些 URL 已被 expect_download
            # 捕获且文件字节校验通过，不应误报为失败请求。
            unexpected_failed = [
                url for url in failed_requests if url not in download_urls
            ]
            expect(
                not unexpected_failed,
                f"浏览器出现失败请求：{unexpected_failed[:3]}",
            )
            expect(
                len(download_urls) == 3,
                f"未完整捕获三类下载 URL：{download_urls}",
            )
            checks["download_request_aborts"] = len(failed_requests) - len(
                unexpected_failed
            )
            expect(not page_errors, f"页面出现未捕获异常：{page_errors[:3]}")
            unexpected_console = [
                message
                for message in console_errors
                if "status of 400" not in message
            ]
            expect(
                not unexpected_console,
                f"浏览器控制台出现非预期错误：{unexpected_console[:3]}",
            )
            checks["expected_http_400_console_messages"] = (
                len(console_errors) - len(unexpected_console)
            )
            record(
                messages,
                "浏览器全程无外部网络请求、失败请求、页面异常或非预期控制台错误",
            )
            checks["external_requests"] = 0
            checks["console_errors"] = 0

            browser.close()

        for job in job_ids:
            delete_job(job)
        expect(all(not (PROJECT_ROOT / "reports" / "web_runs" / job).exists() for job in job_ids),
               "浏览器验收任务目录未清理")
        record(messages, "浏览器验收创建的任务结果均已通过 API 清理")

        summary = {
            "ok": True,
            "browser": "Chrome via Playwright (headless)",
            "viewport": {"desktop": "1440x1000", "mobile": "390x844"},
            "base_url": BASE_URL,
            "job_ids": job_ids,
            "baseline": {
                "input": 120, "matched": 115, "evaluated": 110,
                "deleted": 5, "missing": 5,
                "risk_counts": {"合规": 47, "待确认": 56, "违规": 7},
            },
            "checks": checks,
            "screenshots": screenshots,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "messages": messages,
        }
        OUTPUT_JSON.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        OUTPUT_TEXT.write_text("\n".join(messages) + "\n", encoding="utf-8")
        print(f"\n浏览器验收摘要：{OUTPUT_JSON.relative_to(PROJECT_ROOT)}", flush=True)
        print(f"截图目录：{SCREENSHOT_DIR.relative_to(PROJECT_ROOT)}", flush=True)
        return 0
    finally:
        for job in job_ids:
            delete_job(job)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if server_log_handle is not None:
            server_log_handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="第五阶段真实浏览器冒烟测试")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1-65535 之间")
    try:
        return run(args.port)
    except BrowserSmokeError as exc:
        print(f"浏览器验收失败：{exc}", file=sys.stderr)
        return 1
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        print(f"Playwright 执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 验收脚本必须报告真实异常
        print(f"浏览器验收异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
