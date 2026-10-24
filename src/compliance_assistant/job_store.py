"""固定目录下的任务结果存储。

任务 ID 只允许 32 位小写十六进制；所有可读文件都来自服务端允许列表，
客户端不能指定文件名、目录或任意路径。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class JobStoreError(Exception):
    """任务存储层的可预期错误。"""


class InvalidJobIdError(JobStoreError):
    """任务 ID 格式非法。"""


class JobNotFoundError(JobStoreError):
    """任务或任务文件不存在。"""


RESULT_FILES: dict[str, tuple[str, str, str]] = {
    "markdown": (
        "compliance_report.md",
        "text/markdown",
        "compliance_report.md",
    ),
    "html": (
        "compliance_report.html",
        "text/html",
        "compliance_report.html",
    ),
    "csv": (
        "compliance_details.csv",
        "text/csv",
        "compliance_details.csv",
    ),
}


def _remove_tree(directory: Path, *, attempts: int = 4) -> None:
    """Windows 上短暂的文件索引/杀毒扫描可能占用句柄，做有限重试。"""

    for attempt in range(attempts):
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


class JobStore:
    """把每个任务限制在 ``root/<job_id>/`` 内。"""

    SNAPSHOT_NAME = "result.json"
    MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def new_job_id() -> str:
        return secrets.token_hex(16)

    def validate_job_id(self, job_id: str) -> str:
        if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise InvalidJobIdError("任务 ID 格式非法")
        return job_id

    def _ensure_root(self) -> Path:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise JobStoreError(f"无法创建任务结果目录: {exc}") from exc
        return self.root.resolve()

    def create(self, job_id: str) -> Path:
        self.validate_job_id(job_id)
        root = self._ensure_root()
        path = root / job_id
        try:
            path.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError as exc:
            raise JobStoreError("任务 ID 冲突，请重试") from exc
        except OSError as exc:
            raise JobStoreError(f"无法创建任务目录: {exc}") from exc
        return path

    def job_dir(self, job_id: str, *, must_exist: bool = True) -> Path:
        self.validate_job_id(job_id)
        root = self._ensure_root()
        candidate = (root / job_id).resolve(strict=False)
        if candidate.parent != root or candidate.name != job_id:
            raise InvalidJobIdError("任务路径越界")
        if must_exist and not candidate.is_dir():
            raise JobNotFoundError("任务不存在、已删除或已过期")
        return candidate

    def write_snapshot(self, job_id: str, payload: dict[str, object]) -> Path:
        directory = self.job_dir(job_id)
        path = directory / self.SNAPSHOT_NAME
        tmp_path = directory / (self.SNAPSHOT_NAME + ".tmp")
        try:
            content = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            tmp_path.write_text(content + "\n", encoding="utf-8", newline="\n")
            os.replace(tmp_path, path)
        except (OSError, TypeError, ValueError) as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise JobStoreError(f"无法保存任务结果: {exc}") from exc
        return path

    def read_snapshot(self, job_id: str) -> dict[str, object]:
        directory = self.job_dir(job_id)
        path = directory / self.SNAPSHOT_NAME
        if not path.is_file():
            raise JobNotFoundError("任务结果尚未生成或已被删除")
        try:
            size = path.stat().st_size
            if size > self.MAX_SNAPSHOT_BYTES:
                raise JobStoreError("任务结果文件异常过大")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except JobStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JobStoreError(f"任务结果不可读: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("job_id") != job_id:
            raise JobStoreError("任务结果内容与任务 ID 不匹配")
        return payload

    def result_path(
        self,
        job_id: str,
        kind: str,
        *,
        must_exist: bool = True,
    ) -> Path:
        if kind not in RESULT_FILES:
            raise InvalidJobIdError("不支持的报告类型")
        directory = self.job_dir(job_id)
        filename = RESULT_FILES[kind][0]
        candidate = (directory / filename).resolve(strict=False)
        if candidate.parent != directory:
            raise InvalidJobIdError("报告路径越界")
        if must_exist and not candidate.is_file():
            raise JobNotFoundError("任务报告文件不存在或已删除")
        return candidate

    def download_info(self, job_id: str, kind: str) -> tuple[Path, str, str]:
        path = self.result_path(job_id, kind)
        _, mimetype, download_name = RESULT_FILES[kind]
        return path, mimetype, download_name

    def delete(self, job_id: str) -> None:
        directory = self.job_dir(job_id)
        try:
            _remove_tree(directory)
        except OSError as exc:
            raise JobStoreError(f"删除任务结果失败: {exc}") from exc

    def cleanup_expired(
        self,
        max_age_seconds: int,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """删除超过保留期的任务目录，只处理合法 ID 的直属目录。"""

        if max_age_seconds < 0:
            raise ValueError("max_age_seconds 不能为负数")
        if not self.root.is_dir():
            return []
        root = self.root.resolve()
        current = now or datetime.now(timezone.utc)
        removed: list[str] = []
        for entry in root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if JOB_ID_PATTERN.fullmatch(entry.name) is None:
                continue
            try:
                age = current.timestamp() - entry.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            try:
                _remove_tree(entry)
            except OSError:
                continue
            removed.append(entry.name)
        return removed
