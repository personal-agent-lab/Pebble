"""用户上传文件的不可变存储。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

from server.config import get_settings
from server.db import session, write
from server.errors import NotFoundError
from server.sessions import repository as tasks
from server.sessions.service import timestamp


def _root(db_path: Path | None) -> Path:
    return (db_path.parent if db_path is not None else get_settings().data_dir) / "uploads"


def file_record(conn: sqlite3.Connection, file_id: str) -> dict:
    row = conn.execute("SELECT * FROM uploaded_files WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        raise NotFoundError(file_id)
    return dict(row)


def public_file(row: dict) -> dict:
    return {key: row[key] for key in ("file_id", "filename", "mime_type", "size", "sha256")}


def files_for_ids(conn: sqlite3.Connection, file_ids: list[str]) -> list[dict]:
    return [file_record(conn, file_id) for file_id in file_ids]


class UploadStore:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.root = _root(path)

    def save(self, task_id: str, filename: str, mime_type: str, data: bytes) -> dict:
        clean_name = Path(filename).name.strip()
        if not clean_name or any(character in clean_name for character in ("\r", "\n", "\x00")):
            raise ValueError("附件文件名不合法")
        file_id = str(uuid4())
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / file_id
        target.write_bytes(data)
        try:
            with session(self.path) as conn, write(conn):
                tasks.task(conn, task_id)
                now = timestamp()
                conn.execute(
                    "INSERT INTO uploaded_files "
                    "(file_id, task_id, filename, mime_type, size, sha256, "
                    "storage_path, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (file_id, task_id, clean_name, mime_type, len(data), digest, file_id, now),
                )
                return public_file(file_record(conn, file_id))
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def public(self, file_ids: list[str]) -> list[dict]:
        with session(self.path) as conn:
            return [public_file(row) for row in files_for_ids(conn, file_ids)]

    def require_for_task(self, task_id: str, file_ids: list[str]) -> list[dict]:
        with session(self.path) as conn:
            tasks.task(conn, task_id)
            rows = files_for_ids(conn, file_ids)
            if any(row["task_id"] != task_id for row in rows):
                raise NotFoundError("attachment")
            return [public_file(row) for row in rows]

    def require_for_operation(self, operation_id: str, file_ids: list[str]) -> list[dict]:
        with session(self.path) as conn:
            rows = files_for_ids(conn, file_ids)
            linked = {
                row["task_id"]
                for row in conn.execute(
                    "SELECT task_id FROM task_operations WHERE operation_id = ?", (operation_id,)
                )
            }
            if any(row["task_id"] not in linked for row in rows):
                raise NotFoundError("attachment")
            return [public_file(row) for row in rows]

    def contents(self, attachments: list[dict]) -> list[dict]:
        with session(self.path) as conn:
            stored = [file_record(conn, attachment["file_id"]) for attachment in attachments]
        result = []
        root = self.root.resolve()
        for attachment, record in zip(attachments, stored, strict=True):
            if public_file(record) != attachment:
                raise RuntimeError(f"附件元数据与保存记录不一致：{attachment['file_id']}")
            target = (root / record["storage_path"]).resolve()
            if target.parent != root:
                raise RuntimeError(f"附件存储位置不合法：{attachment['file_id']}")
            data = target.read_bytes()
            if (
                len(data) != attachment["size"]
                or hashlib.sha256(data).hexdigest() != attachment["sha256"]
            ):
                raise RuntimeError(f"附件内容与保存记录不一致：{attachment['file_id']}")
            result.append({**attachment, "data": data})
        return result
