"""跨境商品合规检查工作台：本地 Flask 应用入口。

默认只监听 127.0.0.1:5000，debug 与交互式调试器始终关闭。数据库固定为
项目根目录的 ``compliance.db``；客户端只能上传 CSV，不能指定服务器路径。
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
import tempfile
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from src.compliance_assistant.application_service import (
    REPORT_VERSION,
    build_result_payload,
    run_compliance_check,
    write_details_csv,
)
from src.compliance_assistant.errors import (
    ComplianceError,
    CsvLoadError,
    InputDataError,
)
from src.compliance_assistant.html_report import generate_html_report
from src.compliance_assistant.job_store import (
    InvalidJobIdError,
    JobNotFoundError,
    JobStore,
    JobStoreError,
)
from src.compliance_assistant.logging_config import (
    LoggingSetupError,
    configure_logging,
    task_logger,
)
from src.compliance_assistant.models import RiskLevel
from src.compliance_assistant.report import generate_report, verify_report_file

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "compliance.db"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "reports" / "web_runs"
DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "application.log"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60
APP_VERSION = REPORT_VERSION


class ApiError(Exception):
    """可直接转换为统一 JSON 错误的可预期异常。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _sanitize_display_name(filename: str | None) -> str:
    if not filename:
        return "上传清单.csv"
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(char for char in name if char.isprintable())
    return name[:120] or "上传清单.csv"


def _new_error_id() -> str:
    return "ERR-" + secrets.token_hex(4).upper()


def _error_payload(code: str, message: str, error_id: str) -> dict[str, object]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "error_id": error_id,
        },
    }


def _save_upload_to_temp(upload, destination: Path, limit: int) -> int:
    total = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = upload.stream.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ApiError(
                        413,
                        "file_too_large",
                        f"CSV 文件超过 {limit // (1024 * 1024)} MB 限制",
                    )
                handle.write(chunk)
    except ApiError:
        raise
    except OSError as exc:
        raise ApiError(
            500,
            "upload_storage_error",
            "上传文件暂存失败，请稍后重试",
        ) from exc
    if total == 0:
        raise ApiError(400, "empty_file", "上传的 CSV 文件为空")
    return total


def _safe_client_message(exc: Exception, temp_dir: Path) -> str:
    message = str(exc)
    for value in (str(temp_dir), temp_dir.name):
        if value:
            message = message.replace(value, "<临时文件>")
    return message[:600]


def _cleanup_job(logger, store: JobStore, job_id: str) -> None:
    try:
        store.delete(job_id)
    except JobStoreError:
        logger.warning("event=job_cleanup_failed", exc_info=True)


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    """创建 Flask 应用；测试可传入独立数据库、结果目录和日志路径。"""

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        MAX_UPLOAD_BYTES=MAX_UPLOAD_BYTES,
        DATABASE_PATH=str(DEFAULT_DB),
        RESULTS_ROOT=str(DEFAULT_RESULTS_ROOT),
        LOG_FILE=str(DEFAULT_LOG_FILE),
        LOG_LEVEL=logging.INFO,
        JOB_RETENTION_SECONDS=JOB_RETENTION_SECONDS,
        UPLOAD_TEMP_ROOT=None,
        APP_VERSION=APP_VERSION,
        TESTING=False,
        PROPAGATE_EXCEPTIONS=False,
    )
    if test_config:
        app.config.update(test_config)

    app.json.ensure_ascii = False
    try:
        logger = configure_logging(
            app.config.get("LOG_FILE"),
            level=int(app.config.get("LOG_LEVEL", logging.INFO)),
        )
    except (LoggingSetupError, OSError, ValueError) as exc:
        raise RuntimeError(f"日志初始化失败: {exc}") from exc

    store = JobStore(app.config["RESULTS_ROOT"])
    app.extensions["compliance_logger"] = logger
    app.extensions["job_store"] = store
    app.extensions["app_version"] = APP_VERSION

    def api_error_response(
        error: ApiError,
        job_id: str = "-",
        *,
        error_id: str | None = None,
        log_event: bool = True,
    ) -> Response:
        identifier = error_id or _new_error_id()
        log = task_logger(logger, job_id)
        if log_event:
            if error.status >= 500:
                log.error(
                    "event=request_failed error_id=%s code=%s status=%s",
                    identifier,
                    error.code,
                    error.status,
                    exc_info=error.__cause__ is not None,
                )
            else:
                log.warning(
                    "event=request_rejected error_id=%s code=%s status=%s",
                    identifier,
                    error.code,
                    error.status,
                )
        response = jsonify(_error_payload(error.code, error.message, identifier))
        response.status_code = error.status
        return response

    def render_error_page(
        status: int,
        title: str,
        message: str,
        error_id: str | None = None,
    ) -> tuple[str, int]:
        return (
            render_template(
                "error.html",
                status=status,
                title=title,
                message=message,
                error_id=error_id,
                page_name="请求失败",
                app_version=APP_VERSION,
            ),
            status,
        )

    def links_for(job_id: str) -> dict[str, str]:
        return {
            "self": url_for("api_get_report", job_id=job_id),
            "page": url_for("report_page", job_id=job_id),
            "markdown": url_for(
                "api_download_report", job_id=job_id, kind="markdown"
            ),
            "html": url_for(
                "api_download_report", job_id=job_id, kind="html"
            ),
            "csv": url_for("api_download_report", job_id=job_id, kind="csv"),
        }

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' "
            "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if request.path.startswith("/api/") or request.path.startswith(
            "/reports/"
        ):
            # send_file 会自带 no-cache；任务结果属于本地敏感数据，
            # 这里统一提升为不允许任何缓存的 no-store。
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            page_name="检查工作台",
            app_version=APP_VERSION,
        )

    @app.post("/api/reports")
    def api_create_report() -> Response:
        job_id = store.new_job_id()
        g.job_id = job_id
        log = task_logger(logger, job_id)
        started = time.perf_counter()
        job_created = False
        log.info("event=task_received")

        upload = request.files.get("file") or request.files.get("csv")
        if upload is None or not upload.filename:
            return api_error_response(
                ApiError(400, "missing_file", "请选择要上传的 CSV 文件"),
                job_id,
            )

        display_name = _sanitize_display_name(upload.filename)
        if Path(display_name).suffix.lower() != ".csv":
            return api_error_response(
                ApiError(400, "unsupported_file_type", "仅支持 .csv 文件"),
                job_id,
            )

        temp_root = app.config.get("UPLOAD_TEMP_ROOT")
        try:
            with tempfile.TemporaryDirectory(
                prefix="compliance_upload_",
                dir=temp_root,
            ) as tmp:
                temp_dir = Path(tmp)
                upload_path = temp_dir / "upload.csv"
                size = _save_upload_to_temp(
                    upload,
                    upload_path,
                    int(app.config["MAX_UPLOAD_BYTES"]),
                )
                log.info(
                    "event=upload_received size_bytes=%d", size
                )

                try:
                    outcome = run_compliance_check(
                        upload_path,
                        app.config["DATABASE_PATH"],
                        expected_input_count=None,
                        source_filename=display_name,
                    )
                except CsvLoadError as exc:
                    message = "CSV 校验失败：" + _safe_client_message(
                        exc, temp_dir
                    )
                    log.warning(
                        "event=input_rejected error_type=%s",
                        type(exc).__name__,
                    )
                    return api_error_response(
                        ApiError(400, "invalid_csv", message), job_id
                    )
                except InputDataError as exc:
                    message = "清单校验失败：" + _safe_client_message(
                        exc, temp_dir
                    )
                    log.warning(
                        "event=input_rejected error_type=%s",
                        type(exc).__name__,
                    )
                    return api_error_response(
                        ApiError(400, "invalid_input", message), job_id
                    )

                log.info(
                    "event=check_completed n_input=%d n_matched=%d "
                    "n_deleted=%d n_missing=%d n_evaluated=%d "
                    "risk_compliant=%d risk_pending=%d risk_violation=%d",
                    outcome.stats.n_input,
                    outcome.stats.n_matched,
                    outcome.stats.n_deleted,
                    outcome.stats.n_missing,
                    outcome.stats.n_evaluated,
                    outcome.risk_counts[RiskLevel.COMPLIANT],
                    outcome.risk_counts[RiskLevel.PENDING],
                    outcome.risk_counts[RiskLevel.VIOLATION],
                )

                try:
                    job_dir = store.create(job_id)
                    job_created = True
                    markdown_path = generate_report(
                        outcome.results,
                        job_dir / "compliance_report.md",
                        expected_input_count=None,
                    )
                    verify_report_file(
                        markdown_path,
                        expected_detail_rows=len(outcome.results),
                        expected_sku_refs=[
                            item.sku_ref for item in outcome.sku_inputs
                        ],
                    )
                    html_path = generate_html_report(
                        outcome,
                        job_dir / "compliance_report.html",
                        report_id=job_id,
                    )
                    csv_path = write_details_csv(
                        outcome, job_dir / "compliance_details.csv"
                    )
                    payload = build_result_payload(
                        outcome,
                        job_id,
                        links=links_for(job_id),
                    )
                    store.write_snapshot(job_id, payload)
                except (ComplianceError, OSError, JobStoreError) as exc:
                    _cleanup_job(log, store, job_id)
                    error_id = _new_error_id()
                    log.error(
                        "event=report_generation_failed error_id=%s "
                        "error_type=%s duration_ms=%.1f",
                        error_id,
                        type(exc).__name__,
                        (time.perf_counter() - started) * 1000,
                        exc_info=True,
                    )
                    return api_error_response(
                        ApiError(
                            500,
                            "report_generation_failed",
                            "报告生成失败，请联系本地维护人员查看日志",
                        ),
                        job_id,
                        error_id=error_id,
                        log_event=False,
                    )

                log.info(
                    "event=reports_generated markdown_bytes=%d html_bytes=%d "
                    "csv_bytes=%d",
                    markdown_path.stat().st_size,
                    html_path.stat().st_size,
                    csv_path.stat().st_size,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                log.info("event=task_completed duration_ms=%.1f", elapsed_ms)
                return jsonify(payload), 200
        except ApiError as exc:
            if job_created:
                _cleanup_job(log, store, job_id)
            return api_error_response(exc, job_id)
        except ComplianceError as exc:
            if job_created:
                _cleanup_job(log, store, job_id)
            error_id = _new_error_id()
            log.error(
                "event=task_failed error_id=%s error_type=%s",
                error_id,
                type(exc).__name__,
                exc_info=True,
            )
            return api_error_response(
                ApiError(
                    500,
                    "server_processing_error",
                    "服务端数据处理失败，请检查数据库配置或联系维护人员",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        except JobStoreError as exc:
            if job_created:
                _cleanup_job(log, store, job_id)
            error_id = _new_error_id()
            log.error(
                "event=task_failed error_id=%s error_type=%s",
                error_id,
                type(exc).__name__,
                exc_info=True,
            )
            return api_error_response(
                ApiError(
                    500,
                    "storage_error",
                    "任务结果保存失败，请联系本地维护人员查看日志",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        except RequestEntityTooLarge:
            return api_error_response(
                ApiError(
                    413,
                    "file_too_large",
                    "上传请求超过允许的大小限制",
                ),
                job_id,
            )
        except Exception:
            if job_created:
                _cleanup_job(log, store, job_id)
            raise

    @app.get("/api/reports/<job_id>")
    def api_get_report(job_id: str) -> Response:
        g.job_id = job_id
        log = task_logger(logger, job_id)
        try:
            payload = store.read_snapshot(job_id)
        except InvalidJobIdError:
            log.warning("event=job_rejected reason=invalid_id")
            return api_error_response(
                ApiError(404, "job_not_found", "任务不存在、已删除或已过期"),
                job_id,
            )
        except JobNotFoundError:
            log.warning("event=job_not_found")
            return api_error_response(
                ApiError(404, "job_not_found", "任务不存在、已删除或已过期"),
                job_id,
            )
        except JobStoreError as exc:
            error_id = _new_error_id()
            log.error(
                "event=job_read_failed error_id=%s error_type=%s",
                error_id,
                type(exc).__name__,
                exc_info=True,
            )
            return api_error_response(
                ApiError(
                    500,
                    "storage_error",
                    "任务结果读取失败，请查看本地日志",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        payload["links"] = links_for(job_id)
        log.info("event=job_read")
        return jsonify(payload)

    @app.get("/reports/<job_id>")
    def report_page(job_id: str):
        g.job_id = job_id
        log = task_logger(logger, job_id)
        try:
            payload = store.read_snapshot(job_id)
        except InvalidJobIdError:
            log.warning("event=report_page_rejected reason=invalid_id")
            return render_error_page(
                404, "任务不存在", "任务编号格式不正确或任务已不存在。"
            )
        except JobNotFoundError:
            log.warning("event=report_page_not_found")
            return render_error_page(
                404, "任务不存在", "该任务不存在、已删除或已过期。"
            )
        except JobStoreError:
            error_id = _new_error_id()
            log.error(
                "event=report_page_failed error_id=%s",
                error_id,
                exc_info=True,
            )
            return render_error_page(
                500,
                "结果读取失败",
                "任务结果暂时无法读取，请查看本地日志。",
                error_id,
            )
        payload["links"] = links_for(job_id)
        log.info("event=report_page_opened")
        return render_template(
            "results.html",
            page_name="结果看板",
            job=payload,
            app_version=APP_VERSION,
            pct=_percent,
        )

    @app.get("/api/reports/<job_id>/download/<kind>")
    def api_download_report(job_id: str, kind: str) -> Response:
        g.job_id = job_id
        log = task_logger(logger, job_id)
        try:
            path, mimetype, download_name = store.download_info(job_id, kind)
        except (InvalidJobIdError, JobNotFoundError):
            log.warning("event=download_rejected kind=%s", kind)
            return api_error_response(
                ApiError(404, "file_not_found", "任务或报告文件不存在"),
                job_id,
            )
        except JobStoreError:
            error_id = _new_error_id()
            log.error(
                "event=download_failed error_id=%s",
                error_id,
                exc_info=True,
            )
            return api_error_response(
                ApiError(
                    500,
                    "storage_error",
                    "报告文件读取失败，请查看本地日志",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        log.info("event=download kind=%s", kind)
        return send_file(
            path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=download_name,
            conditional=True,
        )

    @app.delete("/api/reports/<job_id>")
    def api_delete_report(job_id: str) -> Response:
        g.job_id = job_id
        log = task_logger(logger, job_id)
        try:
            store.delete(job_id)
        except (InvalidJobIdError, JobNotFoundError):
            log.warning("event=delete_rejected")
            return api_error_response(
                ApiError(404, "job_not_found", "任务不存在、已删除或已过期"),
                job_id,
            )
        except JobStoreError:
            error_id = _new_error_id()
            log.error(
                "event=delete_failed error_id=%s",
                error_id,
                exc_info=True,
            )
            return api_error_response(
                ApiError(
                    500,
                    "storage_error",
                    "任务删除失败，请查看本地日志",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        log.info("event=job_deleted")
        return jsonify({"ok": True, "job_id": job_id, "status": "deleted"})

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(exc: RequestEntityTooLarge):
        return api_error_response(
            ApiError(
                413,
                "file_too_large",
                "上传请求超过 2 MB 文件限制",
            )
        )

    @app.errorhandler(404)
    def handle_not_found(exc):
        if request.path.startswith("/api/"):
            return api_error_response(
                ApiError(404, "not_found", "请求的资源不存在")
            )
        return render_error_page(
            404, "页面不存在", "请求的页面不存在或已经被移动。"
        )

    @app.errorhandler(405)
    def handle_method_not_allowed(exc):
        if request.path.startswith("/api/"):
            return api_error_response(
                ApiError(405, "method_not_allowed", "该接口不支持此请求方法")
            )
        return render_error_page(
            405, "请求方法不允许", "当前页面不支持该请求方法。"
        )

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        job_id = getattr(g, "job_id", "-")
        if isinstance(exc, HTTPException):
            if request.path.startswith("/api/"):
                return api_error_response(
                    ApiError(
                        exc.code or 500,
                        "http_error",
                        exc.description or "请求无法处理",
                    ),
                    job_id,
                )
            return exc
        error_id = _new_error_id()
        log = task_logger(logger, job_id)
        log.error(
            "event=unhandled_exception error_id=%s error_type=%s",
            error_id,
            type(exc).__name__,
            exc_info=True,
        )
        if request.path.startswith("/api/"):
            return api_error_response(
                ApiError(
                    500,
                    "server_error",
                    "服务器处理失败，请查看本地日志",
                ),
                job_id,
                error_id=error_id,
                log_event=False,
            )
        return render_error_page(
            500,
            "处理失败",
            "服务器处理失败，请查看本地日志。",
            error_id,
        )

    return app


def _percent(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="启动跨境商品合规检查本地工作台（仅监听 127.0.0.1）。"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="本地监听端口（默认: 5000；仅绑定 127.0.0.1）",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1-65535 之间")

    try:
        application = create_app()
    except RuntimeError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    logger = application.extensions["compliance_logger"]
    store = application.extensions["job_store"]
    log = task_logger(logger, "-")
    retention = int(application.config["JOB_RETENTION_SECONDS"])
    try:
        removed = store.cleanup_expired(retention)
        if removed:
            log.info("event=expired_jobs_cleaned count=%d", len(removed))
    except (OSError, JobStoreError):
        log.warning("event=expired_jobs_cleanup_failed", exc_info=True)

    log.info(
        "event=app_start version=%s host=127.0.0.1 port=%d debug=false "
        "database=%s results_root=%s retention_seconds=%d",
        APP_VERSION,
        args.port,
        application.config["DATABASE_PATH"],
        application.config["RESULTS_ROOT"],
        retention,
    )
    try:
        application.run(
            host="127.0.0.1",
            port=args.port,
            debug=False,
            use_reloader=False,
        )
    except OSError as exc:
        log.error("event=app_start_failed port=%d", args.port, exc_info=True)
        print(
            f"启动失败：端口 {args.port} 不可用或本机拒绝监听。"
            f"请关闭占用程序或使用 --port 指定其他端口。\n{exc}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    # 仅供 python web_app.py 使用；导入本模块（例如测试）不会创建日志文件
    # 或写正式任务目录。Flask CLI 也能自动发现 create_app 工厂。
    raise SystemExit(main())
