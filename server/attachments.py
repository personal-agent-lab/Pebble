"""任务附件的验证、持久化与任务内读取边界。"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from server.config import Settings, get_settings
from server.errors import AttachmentValidationError, NotFoundError

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_FILES = 10
IMAGE_SIGNATURES = {
    "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP",
}
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".html",
    ".css",
    ".sh",
    ".sql",
    ".toml",
    ".ini",
    ".cfg",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".r",
}


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PreparedAttachment:
    file_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    data: bytes

    @property
    def storage_name(self) -> str:
        # Runtime 的 Read 按扩展名识别 PDF 与图片；扩展名取自白名单校验后的小写后缀，
        # 原文件名的其余部分不参与路径。
        suffix = Path(self.filename).suffix.lower()
        return f"{self.file_id}{suffix}"


def _validation(filename: str, message: str) -> AttachmentValidationError:
    return AttachmentValidationError([{"field": filename or "file", "message": message}])


async def prepare_uploads(files: list[UploadFile]) -> list[PreparedAttachment]:
    if len(files) > MAX_FILES:
        raise AttachmentValidationError(
            [{"field": "files", "message": f"每条消息最多上传 {MAX_FILES} 个附件"}]
        )
    prepared = []
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename or filename in {".", ".."} or "\x00" in filename:
            raise _validation(filename, "文件名不合法")
        data = await upload.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise _validation(filename, "单个附件不能超过 20 MB")
        suffix = Path(filename).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            mime_type = IMAGE_SUFFIXES[suffix]
            if not IMAGE_SIGNATURES[mime_type](data):
                raise _validation(filename, "图片内容与扩展名不符")
        elif suffix == ".pdf":
            mime_type = "application/pdf"
            if not data.startswith(b"%PDF-"):
                raise _validation(filename, "PDF 内容与扩展名不符")
        elif suffix in TEXT_SUFFIXES:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _validation(filename, "文本或代码文件必须使用 UTF-8 编码") from error
            mime_type = mimetypes.guess_type(filename)[0] or "text/plain"
        else:
            raise _validation(filename, "不支持这种文件类型")
        prepared.append(
            PreparedAttachment(
                file_id=str(uuid4()),
                filename=filename,
                mime_type=mime_type,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        )
    return prepared


class AttachmentStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.root = self.settings.data_dir / "agent" / "workspaces"

    def task_workspace(self, task_id: str) -> Path:
        return self.root / task_id

    def save_files(self, task_id: str, prepared: list[PreparedAttachment]) -> None:
        if not prepared:
            return
        directory = self.task_workspace(task_id) / "attachments"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for item in prepared:
                (directory / item.storage_name).write_bytes(item.data)
        except BaseException:
            shutil.rmtree(self.task_workspace(task_id), ignore_errors=True)
            raise

    def insert(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        item_id: str,
        prepared: list[PreparedAttachment],
    ) -> list[str]:
        ids = []
        for position, item in enumerate(prepared):
            relative = f"attachments/{item.storage_name}"
            conn.execute(
                "INSERT INTO uploaded_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.file_id,
                    task_id,
                    item.filename,
                    item.mime_type,
                    item.size,
                    item.sha256,
                    relative,
                    timestamp(),
                ),
            )
            conn.execute(
                "INSERT INTO timeline_item_attachments VALUES (?, ?, ?)",
                (item_id, item.file_id, position),
            )
            ids.append(item.file_id)
        return ids

    def records(self, conn: sqlite3.Connection, file_ids: list[str]) -> list[dict]:
        if not file_ids:
            return []
        placeholders = ",".join("?" for _ in file_ids)
        rows = conn.execute(
            f"SELECT * FROM uploaded_files WHERE file_id IN ({placeholders})", file_ids
        ).fetchall()
        by_id = {row["file_id"]: dict(row) for row in rows}
        return [by_id[file_id] for file_id in file_ids if file_id in by_id]

    def get(self, conn: sqlite3.Connection, task_id: str, file_id: str) -> tuple[dict, Path]:
        row = conn.execute(
            "SELECT * FROM uploaded_files WHERE task_id = ? AND file_id = ?",
            (task_id, file_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(file_id)
        record = dict(row)
        path = (self.task_workspace(task_id) / record["storage_path"]).resolve()
        try:
            path.relative_to(self.task_workspace(task_id).resolve())
        except ValueError as error:
            raise NotFoundError(file_id) from error
        if not path.is_file():
            raise NotFoundError(file_id)
        return record, path

    def delete_task_files(self, task_id: str) -> None:
        shutil.rmtree(self.task_workspace(task_id), ignore_errors=True)

    def discard(self, task_id: str, prepared: list[PreparedAttachment]) -> None:
        for item in prepared:
            (self.task_workspace(task_id) / "attachments" / item.storage_name).unlink(
                missing_ok=True
            )
        for directory in (
            self.task_workspace(task_id) / "attachments",
            self.task_workspace(task_id),
        ):
            with suppress(OSError):
                directory.rmdir()
