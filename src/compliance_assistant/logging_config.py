"""Web 应用的统一日志配置。

日志同时写入轮转文件和控制台；每条任务日志都带任务 ID，不记录上传
文件的完整内容或整表业务数据。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "compliance_assistant.web"
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-7s | job=%(job_id)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class LoggingSetupError(RuntimeError):
    """日志目录或日志文件不可写。"""


class _TaskContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


def configure_logging(
    log_path: str | Path | None,
    *,
    level: int = logging.INFO,
    max_bytes: int = 1_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """配置并返回 Web 日志器；重复调用会替换本模块管理的 handler。"""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_compliance_managed", False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    task_filter = _TaskContextFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(task_filter)
    console._compliance_managed = True  # type: ignore[attr-defined]
    logger.addHandler(console)

    if log_path is not None:
        path = Path(log_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        except OSError as exc:
            raise LoggingSetupError(
                f"无法创建日志文件 {path}: {exc}"
            ) from exc
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(task_filter)
        file_handler._compliance_managed = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)

    return logger


def task_logger(logger: logging.Logger, job_id: str) -> logging.LoggerAdapter:
    """返回自动附带任务 ID 的日志适配器。"""

    return logging.LoggerAdapter(logger, {"job_id": job_id})


def shutdown_logging(logger: logging.Logger | None = None) -> None:
    """关闭本模块管理的 handler，供测试或进程退出时释放日志文件句柄。"""

    target = logger or logging.getLogger(LOGGER_NAME)
    for handler in list(target.handlers):
        if getattr(handler, "_compliance_managed", False):
            target.removeHandler(handler)
            handler.close()
