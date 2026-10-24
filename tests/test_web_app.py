"""Flask API、上传安全、任务存储、导出与实际模板测试。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.compliance_assistant.application_service import (
    MISSING_PLACEHOLDER,
    NOT_APPLICABLE_PLACEHOLDER,
)
from src.compliance_assistant.errors import ReportError
from src.compliance_assistant.logging_config import shutdown_logging
from tests.support import ORIGINAL_CSV, ORIGINAL_DB, create_db, write_csv
from web_app import MAX_UPLOAD_BYTES, create_app

JOB_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MISSING_JOB = "0" * 32


def parse_markdown_details(text: str) -> list[list[str]]:
    detail = text.split("## 一、明细区", 1)[1].split("## 二、", 1)[0]
    rows = []
    for line in detail.splitlines():
        if re.match(r"^\|\s*\d+\s*\|", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


class WebAppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="compliance_web_test_"))
        self.upload_root = self.temp_dir / "uploads"
        self.upload_root.mkdir()
        self.results_root = self.temp_dir / "runs"
        self.log_path = self.temp_dir / "application.log"
        self.app = self._make_app()
        self.client = self.app.test_client()
        self.store = self.app.extensions["job_store"]
        self.logger = self.app.extensions["compliance_logger"]
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.addCleanup(shutdown_logging, self.logger)
        self.csv_bytes = ORIGINAL_CSV.read_bytes()
        self.csv_hash = hashlib.sha256(self.csv_bytes).hexdigest()
        self.db_hash = hashlib.sha256(ORIGINAL_DB.read_bytes()).hexdigest()

    def _make_app(
        self,
        *,
        database: Path | str | None = None,
        results_root: Path | None = None,
        log_path: Path | None = None,
    ):
        app = create_app(
            {
                "TESTING": True,
                "PROPAGATE_EXCEPTIONS": False,
                "DATABASE_PATH": str(database or ORIGINAL_DB),
                "RESULTS_ROOT": str(results_root or self.results_root),
                "LOG_FILE": str(log_path or self.log_path),
                "UPLOAD_TEMP_ROOT": str(self.upload_root),
            }
        )
        self.addCleanup(
            shutdown_logging, app.extensions["compliance_logger"]
        )
        return app

    def upload(
        self,
        data: bytes | None = None,
        *,
        filename: str = "sku_list.csv",
        field: str = "file",
        client=None,
    ):
        payload = self.csv_bytes if data is None else data
        target = client or self.client
        return target.post(
            "/api/reports",
            data={field: (io.BytesIO(payload), filename)},
            content_type="multipart/form-data",
        )

    def assert_api_error(self, response, status: int) -> dict:
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertIsInstance(payload, dict)
        self.assertFalse(payload["ok"])
        self.assertEqual(set(payload["error"]), {"code", "message", "error_id"})
        self.assertTrue(payload["error"]["code"])
        self.assertTrue(payload["error"]["message"])
        self.assertTrue(payload["error"]["error_id"])
        raw = response.get_data(as_text=True)
        self.assertNotIn("Traceback", raw)
        self.assertNotIn('File "', raw)
        return payload

    def job_directories(self) -> list[Path]:
        if not self.results_root.exists():
            return []
        return [item for item in self.results_root.iterdir() if item.is_dir()]

    def assert_no_jobs_or_temp_files(self) -> None:
        self.assertEqual(self.job_directories(), [])
        self.assertEqual(list(self.upload_root.iterdir()), [])

    def read_log(self, path: Path | None = None) -> str:
        target = path or self.log_path
        return target.read_text(encoding="utf-8") if target.exists() else ""

    # ------------------------------------------------------------------
    # 页面与静态资源
    # ------------------------------------------------------------------

    def test_home_page_and_local_assets(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        page = response.get_data(as_text=True)
        for marker in (
            "跨境合规工作台",
            "上传待检清单",
            "拖入 CSV 文件",
            "开始检查",
            "风险等级分布",
            "Top 风险类目",
            "商品明细",
            "当前检查结果",
        ):
            self.assertIn(marker, page)
        self.assertIn('href="/static/css/app.css"', page)
        self.assertIn('src="/static/js/app.js"', page)
        self.assertNotRegex(page.lower(), r'(?:src|href)="https?://')
        self.assertNotIn("<script>", page.replace('<script src="/static/js/app.js"', ""))
        self.assertEqual(page.count("stat-card"), 8)
        headers = response.headers
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_static_assets_are_available_and_local(self) -> None:
        for path, prefix in (
            ("/static/css/app.css", "@media (max-width"),
            ("/static/js/app.js", 'fetch("/api/reports"'),
            ("/static/images/logo.svg", "<svg"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data(as_text=True)
            self.assertIn(prefix, body)
            self.assertNotIn("https://", body.lower())
            response.close()

    def test_frontend_implements_required_interactions_without_inner_html(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"
        ).read_text(encoding="utf-8")
        for marker in (
            "PAGE_SIZE = 20",
            'fetch("/api/reports"',
            "search-input",
            "disposition-filter",
            "risk-filter",
            "category-filter",
            "delete-job-button",
            "download-markdown",
            "aria-expanded",
            "textContent",
            "encodeURIComponent",
            "${detail.original_price} ${displayValue(detail.currency)}",
        ):
            self.assertIn(marker, script)
        self.assertNotIn(".innerHTML", script)
        self.assertNotIn("eval(", script)
        dashboard = (
            Path(__file__).resolve().parents[1] / "templates" / "_dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="search-input"', dashboard)
        self.assertIn('id="risk-filter"', dashboard)
        self.assertIn('id="category-filter"', dashboard)
        self.assertIn('id="page-buttons"', dashboard)
        self.assertEqual(dashboard.count('class="stat-card'), 8)

    # ------------------------------------------------------------------
    # 有效上传与完整数据链路
    # ------------------------------------------------------------------

    def test_valid_upload_uses_baseline_and_creates_complete_job(self) -> None:
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        job_id = payload["job_id"]
        self.assertRegex(job_id, JOB_PATTERN)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["source"]["filename"], "sku_list.csv")
        self.assertEqual(payload["source"]["sha256"], self.csv_hash)
        self.assertEqual(payload["source"]["size_bytes"], len(self.csv_bytes))
        self.assertEqual(
            {key: payload["summary"][key] for key in (
                "n_input", "n_matched", "n_deleted", "n_missing", "n_evaluated"
            )},
            {
                "n_input": 120,
                "n_matched": 115,
                "n_deleted": 5,
                "n_missing": 5,
                "n_evaluated": 110,
            },
        )
        self.assertEqual(
            payload["summary"]["risk_counts"],
            {"合规": 47, "待确认": 56, "违规": 7},
        )
        self.assertEqual(
            payload["summary"]["disposition_counts"],
            {"正常评估": 110, "已下架": 5, "库中缺失": 5},
        )
        self.assertEqual(len(payload["details"]), 120)
        self.assertTrue(payload["integrity"]["all_hold"])
        self.assertGreater(len(payload["rules"]), 0)
        self.assertGreater(len(payload["top_categories"]), 0)
        self.assertEqual(
            payload["links"],
            {
                "self": f"/api/reports/{job_id}",
                "page": f"/reports/{job_id}",
                "markdown": f"/api/reports/{job_id}/download/markdown",
                "html": f"/api/reports/{job_id}/download/html",
                "csv": f"/api/reports/{job_id}/download/csv",
            },
        )

        job_dir = self.results_root / job_id
        self.assertTrue(job_dir.is_dir())
        self.assertEqual(
            {item.name for item in job_dir.iterdir()},
            {
                "compliance_report.md",
                "compliance_report.html",
                "compliance_details.csv",
                "result.json",
            },
        )
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertEqual(
            hashlib.sha256(ORIGINAL_CSV.read_bytes()).hexdigest(), self.csv_hash
        )
        self.assertEqual(
            hashlib.sha256(ORIGINAL_DB.read_bytes()).hexdigest(), self.db_hash
        )

        snapshot = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["job_id"], job_id)
        self.assertEqual(snapshot["details"], payload["details"])

        log = self.read_log()
        self.assertIn(f"job={job_id}", log)
        self.assertIn("event=task_received", log)
        self.assertIn(
            "event=check_completed n_input=120 n_matched=115 n_deleted=5 "
            "n_missing=5 n_evaluated=110",
            log,
        )
        self.assertIn("risk_compliant=47 risk_pending=56 risk_violation=7", log)
        self.assertIn("event=reports_generated", log)
        self.assertIn("event=task_completed duration_ms=", log)
        self.assertNotIn("sku_ref,batch_no", log)
        self.assertNotIn(self.csv_bytes.decode("utf-8-sig")[:80], log)

    def test_upload_accepts_alternate_csv_field(self) -> None:
        response = self.upload(field="csv")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["summary"]["n_input"], 120)

    def test_task_ids_are_random_and_unique(self) -> None:
        first = self.upload()
        second = self.upload()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_id = first.get_json()["job_id"]
        second_id = second.get_json()["job_id"]
        self.assertRegex(first_id, JOB_PATTERN)
        self.assertRegex(second_id, JOB_PATTERN)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(self.job_directories()), 2)

    # ------------------------------------------------------------------
    # 上传校验与错误处理
    # ------------------------------------------------------------------

    def test_empty_file_is_rejected_without_creating_a_job(self) -> None:
        response = self.upload(b"", filename="empty.csv")
        payload = self.assert_api_error(response, 400)
        self.assertEqual(payload["error"]["code"], "empty_file")
        self.assertIn("为空", payload["error"]["message"])
        self.assert_no_jobs_or_temp_files()
        self.assertIn("event=request_rejected", self.read_log())

    def test_non_csv_file_is_rejected(self) -> None:
        response = self.upload(self.csv_bytes, filename="sku_list.txt")
        payload = self.assert_api_error(response, 400)
        self.assertEqual(payload["error"]["code"], "unsupported_file_type")
        self.assertIn("仅支持", payload["error"]["message"])
        self.assert_no_jobs_or_temp_files()

    def test_oversized_file_is_rejected_while_streaming(self) -> None:
        oversized = b"a" * (MAX_UPLOAD_BYTES + 1)
        response = self.upload(oversized, filename="large.csv")
        payload = self.assert_api_error(response, 413)
        self.assertEqual(payload["error"]["code"], "file_too_large")
        self.assertIn("2 MB", payload["error"]["message"])
        self.assert_no_jobs_or_temp_files()

    def test_request_over_flask_limit_returns_json_413(self) -> None:
        oversized = b"a" * (MAX_UPLOAD_BYTES + 128 * 1024)
        response = self.upload(oversized, filename="too-large.csv")
        payload = self.assert_api_error(response, 413)
        self.assertEqual(payload["error"]["code"], "file_too_large")
        self.assert_no_jobs_or_temp_files()

    def test_missing_required_header_is_rejected(self) -> None:
        data = "sku_ref\n00001\n".encode("utf-8")
        response = self.upload(data, filename="missing-header.csv")
        payload = self.assert_api_error(response, 400)
        self.assertEqual(payload["error"]["code"], "invalid_csv")
        self.assertIn("CSV 校验失败", payload["error"]["message"])
        self.assert_no_jobs_or_temp_files()

    def test_invalid_sku_format_is_rejected(self) -> None:
        data = "sku_ref,batch_no\nABC,B2026001\n".encode("utf-8")
        response = self.upload(data, filename="bad-sku.csv")
        payload = self.assert_api_error(response, 400)
        self.assertIn(payload["error"]["code"], {"invalid_csv", "invalid_input"})
        self.assertIn("校验失败", payload["error"]["message"])
        self.assert_no_jobs_or_temp_files()

    def test_duplicate_rows_are_preserved_not_silently_dropped(self) -> None:
        # 业务约定：重复的 sku_ref/batch_no 不去重、不合并，按输入行保留。
        data = "sku_ref,batch_no\n00001,B1\n00001,B1\n".encode("utf-8")
        response = self.upload(data, filename="duplicate.csv")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["summary"]["n_input"], 2)
        self.assertEqual(
            [(item["sku_ref"], item["batch_no"]) for item in payload["details"]],
            [("00001", "B1"), ("00001", "B1")],
        )
        self.assertEqual(
            [item["line_no"] for item in payload["details"]],
            [2, 3],
        )
        self.assertTrue(payload["integrity"]["all_hold"])
        self.assertEqual(len(self.job_directories()), 1)

    def test_missing_database_returns_500_without_traceback(self) -> None:
        application = self._make_app(database=self.temp_dir / "missing.db")
        client = application.test_client()
        response = self.upload(client=client)
        payload = self.assert_api_error(response, 500)
        self.assertEqual(payload["error"]["code"], "server_processing_error")
        self.assert_no_jobs_or_temp_files()
        log = self.read_log()
        self.assertIn(f"error_id={payload['error']['error_id']}", log)
        self.assertIn("event=task_failed", log)
        self.assertIn("Traceback", log)

    def test_invalid_database_schema_returns_500(self) -> None:
        broken_db = self.temp_dir / "broken.db"
        broken_db.write_bytes(b"not a sqlite database")
        application = self._make_app(database=broken_db)
        response = self.upload(client=application.test_client())
        payload = self.assert_api_error(response, 500)
        self.assertEqual(payload["error"]["code"], "server_processing_error")
        self.assert_no_jobs_or_temp_files()

    def test_invalid_encoding_does_not_report_success_or_leave_job(self) -> None:
        data = b"sku_ref,batch_no\n\xff\xfe\x00\n"
        response = self.upload(data, filename="invalid-encoding.csv")
        payload = self.assert_api_error(response, 400)
        self.assertEqual(payload["error"]["code"], "invalid_csv")
        self.assertNotIn("job_id", payload)
        self.assert_no_jobs_or_temp_files()

    def test_report_generation_failure_cleans_partial_job(self) -> None:
        with mock.patch(
            "web_app.generate_html_report",
            side_effect=ReportError("模拟报告生成失败"),
        ):
            response = self.upload()
        payload = self.assert_api_error(response, 500)
        self.assertEqual(payload["error"]["code"], "report_generation_failed")
        self.assertNotIn("模拟报告生成失败", response.get_data(as_text=True))
        self.assert_no_jobs_or_temp_files()
        log = self.read_log()
        self.assertIn("event=report_generation_failed", log)
        self.assertIn(f"error_id={payload['error']['error_id']}", log)
        self.assertIn("Traceback", log)

    def test_missing_file_field_is_rejected(self) -> None:
        response = self.client.post(
            "/api/reports",
            data={},
            content_type="multipart/form-data",
        )
        payload = self.assert_api_error(response, 400)
        self.assertEqual(payload["error"]["code"], "missing_file")
        self.assert_no_jobs_or_temp_files()

    # ------------------------------------------------------------------
    # 任务读取页面与导出
    # ------------------------------------------------------------------

    def _create_job(self) -> tuple[str, dict]:
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        return payload["job_id"], payload

    def test_get_api_and_result_page_show_same_job(self) -> None:
        job_id, created = self._create_job()
        api = self.client.get(f"/api/reports/{job_id}")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.headers.get("Cache-Control"), "no-store")
        stored = api.get_json()
        self.assertEqual(stored["details"], created["details"])
        self.assertEqual(stored["summary"], created["summary"])
        self.assertEqual(stored["top_categories"], created["top_categories"])
        self.assertEqual(stored["generated_at"], created["generated_at"])
        self.assertEqual(stored["links"], created["links"])

        page_response = self.client.get(f"/reports/{job_id}")
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.headers.get("Cache-Control"), "no-store")
        page = page_response.get_data(as_text=True)
        self.assertIn(job_id, page)
        self.assertEqual(page.count('class="cell-index"'), 120)
        self.assertIn("全部商品明细", page)
        self.assertIn("sku_list.csv", page)
        self.assertIn("47", page)
        self.assertIn("56", page)
        self.assertIn("TOP RISK CATEGORIES", page)
        self.assertEqual(
            len(re.findall(r'class="stat-card(?:\s|")', page)),
            8,
        )
        self.assertIn("178.97 KRW", page)
        self.assertNotIn("KRW 178.97", page)
        self.assertIn(f'href="/api/reports/{job_id}/download/markdown"', page)
        self.assertIn(f'href="/api/reports/{job_id}/download/html"', page)
        self.assertIn(f'href="/api/reports/{job_id}/download/csv"', page)
        self.assertNotIn('onclick=', page)
        self.assertNotIn("Traceback", page)
        page_response.close()

    def test_downloads_are_consistent_with_api_and_each_other(self) -> None:
        job_id, created = self._create_job()
        downloads: dict[str, tuple[str, bytes]] = {}
        expected_types = {
            "markdown": ("text/markdown", "compliance_report.md"),
            "html": ("text/html", "compliance_report.html"),
            "csv": ("text/csv", "compliance_details.csv"),
        }
        for kind, (mimetype, filename) in expected_types.items():
            response = self.client.get(f"/api/reports/{job_id}/download/{kind}")
            self.assertEqual(response.status_code, 200, kind)
            self.assertTrue(response.content_type.startswith(mimetype), response.content_type)
            self.assertNotIn("charset=utf-8; charset=utf-8", response.content_type)
            self.assertEqual(
                response.headers["Content-Disposition"],
                f"attachment; filename={filename}",
            )
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            downloads[kind] = (response.mimetype, response.data)
            response.close()

        csv_text = downloads["csv"][1].decode("utf-8-sig")
        csv_rows = list(csv.reader(io.StringIO(csv_text)))
        self.assertEqual(len(csv_rows), 121)
        self.assertEqual(len(csv_rows[0]), 15)
        details = created["details"]
        for row, detail in zip(csv_rows[1:], details, strict=True):
            self.assertEqual(row[0], str(detail["index"]))
            self.assertEqual(row[1], detail["sku_ref"])
            self.assertEqual(row[2], detail["batch_no"])
            self.assertEqual(row[5], detail["disposition"])
            expected_cny = detail["cny_price"] or (
                NOT_APPLICABLE_PLACEHOLDER
                if detail["disposition"] == "已下架"
                else MISSING_PLACEHOLDER
            )
            expected_risk = detail["risk"] or (
                NOT_APPLICABLE_PLACEHOLDER
                if detail["disposition"] == "已下架"
                else MISSING_PLACEHOLDER
            )
            self.assertEqual(row[12], expected_cny)
            self.assertEqual(row[13], expected_risk)

        markdown = downloads["markdown"][1].decode("utf-8")
        markdown_rows = parse_markdown_details(markdown)
        self.assertEqual(len(markdown_rows), 120)
        for row, detail in zip(markdown_rows, details, strict=True):
            self.assertEqual(row[0], str(detail["index"]))
            self.assertEqual(row[1], detail["sku_ref"])
            self.assertEqual(row[2], detail["batch_no"])
            self.assertEqual(row[3], detail["disposition"])
            expected_cny = detail["cny_price"] or (
                NOT_APPLICABLE_PLACEHOLDER
                if detail["disposition"] == "已下架"
                else MISSING_PLACEHOLDER
            )
            expected_risk = detail["risk"] or (
                NOT_APPLICABLE_PLACEHOLDER
                if detail["disposition"] == "已下架"
                else MISSING_PLACEHOLDER
            )
            self.assertEqual(row[9], expected_cny)
            self.assertEqual(row[10], expected_risk)
        self.assertIn("| 合规 | 47 |", markdown)
        self.assertIn("| 待确认 | 56 |", markdown)
        self.assertIn("| 违规 | 7 |", markdown)
        for item in created["top_categories"]:
            self.assertIn(f"| {item['category']} |", markdown)

        html_document = downloads["html"][1].decode("utf-8")
        self.assertEqual(html_document.count('class="detail-row"'), 120)
        html_refs = re.findall(
            r'<tr class="detail-row" data-sku-ref="([^"]*)"', html_document
        )
        self.assertEqual(html_refs, [detail["sku_ref"] for detail in details])
        for detail in details:
            if detail["cny_price"] is not None:
                self.assertIn(detail["cny_price"], html_document)
        self.assertNotIn("https://", html_document.lower())
        self.assertNotIn("http://", html_document.lower())

        page = self.client.get(f"/reports/{job_id}").get_data(as_text=True)
        for detail in details:
            self.assertIn(detail["sku_ref"], page)
        self.assertEqual(page.count('class="cell-index"'), 120)

    def test_download_rejects_unknown_kind_and_missing_job(self) -> None:
        job_id, _payload = self._create_job()
        unknown = self.client.get(f"/api/reports/{job_id}/download/pdf")
        self.assert_api_error(unknown, 404)
        missing = self.client.get(f"/api/reports/{MISSING_JOB}/download/html")
        self.assert_api_error(missing, 404)
        available = self.client.get(f"/api/reports/{job_id}/download/csv")
        self.assertEqual(available.status_code, 200)
        available.close()

    # ------------------------------------------------------------------
    # 不存在任务、路径穿越与删除
    # ------------------------------------------------------------------

    def test_unknown_jobs_return_structured_404(self) -> None:
        api = self.client.get(f"/api/reports/{MISSING_JOB}")
        self.assert_api_error(api, 404)
        page = self.client.get(f"/reports/{MISSING_JOB}")
        self.assertEqual(page.status_code, 404)
        body = page.get_data(as_text=True)
        self.assertIn("任务不存在", body)
        self.assertNotIn("Traceback", body)
        page.close()
        delete = self.client.delete(f"/api/reports/{MISSING_JOB}")
        self.assert_api_error(delete, 404)
        download = self.client.get(
            f"/api/reports/{MISSING_JOB}/download/markdown"
        )
        self.assert_api_error(download, 404)

    def test_invalid_and_traversal_job_ids_are_rejected(self) -> None:
        attacks = [
            "/api/reports/not-a-valid-job",
            "/api/reports/../etc/passwd",
            "/api/reports/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/api/reports/..%5c..%5cWindows%5cwin.ini",
            f"/api/reports/{MISSING_JOB}/download/..%2f..%2fresult.json",
            f"/api/reports/{MISSING_JOB}/../download/markdown",
            "/reports/../../../../etc/passwd",
        ]
        for path in attacks:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, {400, 404})
                body = response.get_data(as_text=True)
                self.assertNotIn("root:", body)
                self.assertNotIn("[extensions]", body)
                self.assertNotIn("Traceback", body)
                response.close()
        self.assert_no_jobs_or_temp_files()

    def test_delete_removes_all_result_files_and_updates_log(self) -> None:
        job_id, _payload = self._create_job()
        job_dir = self.results_root / job_id
        self.assertTrue(job_dir.is_dir())
        response = self.client.delete(f"/api/reports/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"ok": True, "job_id": job_id, "status": "deleted"},
        )
        self.assertFalse(job_dir.exists())
        self.assertEqual(self.client.get(f"/api/reports/{job_id}").status_code, 404)
        second_delete = self.client.delete(f"/api/reports/{job_id}")
        self.assert_api_error(second_delete, 404)
        self.assertIn(f"job={job_id}", self.read_log())
        self.assertIn("event=job_deleted", self.read_log())

    def test_api_405_is_structured_json(self) -> None:
        response = self.client.post(f"/api/reports/{MISSING_JOB}")
        self.assert_api_error(response, 405)
        self.assertEqual(response.get_json()["error"]["code"], "method_not_allowed")

    # ------------------------------------------------------------------
    # XSS、原文件保护与本地数据边界
    # ------------------------------------------------------------------

    def test_result_page_escapes_untrusted_filename_and_product_text(self) -> None:
        database = create_db(
            self.temp_dir / "xss.db",
            products=[
                {
                    "sku_id": "SKU-00001",
                    "name": "<script>alert('xss')</script>",
                    "category": "<b>服饰</b>",
                    "material": "棉",
                    "origin": "中国",
                    "price": 10,
                    "currency": "CNY",
                }
            ],
            rules=[
                {
                    "rule_id": "CR-XSS",
                    "title": "<em onmouseover=alert(3)>规则</em>",
                    "applies_to_category": "<b>服饰</b>",
                    "material_keyword": "棉",
                    "origin_keyword": None,
                    "severity": "违规",
                }
            ],
            rates=[{"currency": "CNY", "rate_to_cny": 1}],
        )
        csv_path = write_csv(
            self.temp_dir / "xss.csv",
            [{"sku_ref": "00001", "batch_no": "<B&1>"}],
        )
        application = self._make_app(database=database)
        response = self.upload(
            csv_path.read_bytes(),
            filename="<img src=x onerror=alert(9)>.csv",
            client=application.test_client(),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        job_id = payload["job_id"]
        self.assertIn("<img src=x onerror=alert(9)>.csv", payload["source"]["filename"])
        page_response = application.test_client().get(f"/reports/{job_id}")
        page = page_response.get_data(as_text=True)
        page_response.close()
        self.assertNotIn("<script>alert('xss')</script>", page)
        self.assertNotIn("<img src=x onerror=alert(9)>", page)
        self.assertNotIn("<B&1>", page)
        self.assertIn("&lt;script&gt;alert", page)
        self.assertIn("&lt;img src=x onerror=alert(9)&gt;.csv", page)
        self.assertNotIn("<em onmouseover=alert(3)>规则</em>", page)
        self.assertIn("&lt;em onmouseover=alert(3)&gt;规则&lt;/em&gt;", page)

    def test_upload_never_overwrites_original_files_or_uses_client_paths(self) -> None:
        response = self.client.post(
            "/api/reports",
            data={
                "file": (io.BytesIO(self.csv_bytes), "sku_list.csv"),
                "db_path": "C:\\Windows\\win.ini",
                "output_path": "C:\\Windows\\Temp\\evil.md",
                "results_root": "C:\\",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            response.get_json()["source"]["sha256"],
            self.csv_hash,
        )
        self.assertEqual(
            hashlib.sha256(ORIGINAL_CSV.read_bytes()).hexdigest(), self.csv_hash
        )
        self.assertEqual(
            hashlib.sha256(ORIGINAL_DB.read_bytes()).hexdigest(), self.db_hash
        )
        self.assertFalse((self.temp_dir / "evil.md").exists())

    def test_filename_with_server_path_is_sanitized(self) -> None:
        response = self.upload(
            self.csv_bytes,
            filename="..\\..\\..\\Windows\\Temp\\逃逸.csv",
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["source"]["filename"], "逃逸.csv")
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertFalse((self.temp_dir.parent / "逃逸.csv").exists())

    # ------------------------------------------------------------------
    # 日志与存储
    # ------------------------------------------------------------------

    def test_logging_is_rotating_and_uses_task_context(self) -> None:
        from logging.handlers import RotatingFileHandler

        managed = [
            handler
            for handler in self.logger.handlers
            if getattr(handler, "_compliance_managed", False)
        ]
        self.assertEqual(len(managed), 2)
        file_handlers = [
            handler for handler in managed if isinstance(handler, RotatingFileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)
        self.assertEqual(file_handlers[0].maxBytes, 1_000_000)
        self.assertEqual(file_handlers[0].backupCount, 5)
        self.assertTrue(self.log_path.exists())

    def test_expired_jobs_are_cleaned(self) -> None:
        import os
        import time

        job_id, _payload = self._create_job()
        directory = self.results_root / job_id
        old = time.time() - 10 * 24 * 60 * 60
        os.utime(directory, (old, old))
        removed = self.store.cleanup_expired(7 * 24 * 60 * 60)
        self.assertEqual(removed, [job_id])
        self.assertFalse(directory.exists())

    def test_snapshot_job_id_tampering_is_detected(self) -> None:
        job_id, payload = self._create_job()
        snapshot_path = self.results_root / job_id / "result.json"
        tampered = dict(payload)
        tampered["job_id"] = "f" * 32
        snapshot_path.write_text(
            json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
        )
        response = self.client.get(f"/api/reports/{job_id}")
        self.assert_api_error(response, 500)
        self.assertEqual(response.get_json()["error"]["code"], "storage_error")


if __name__ == "__main__":
    unittest.main()
