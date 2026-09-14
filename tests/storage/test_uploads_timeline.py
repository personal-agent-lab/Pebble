import hashlib

from server.db import init_db, session
from server.sessions.service import SessionStore
from server.tools.gmail.service import MailDraftStore
from server.uploads import UploadStore


def test_upload_is_immutable_and_filename_never_becomes_storage_path(settings):
    init_db()
    task = SessionStore().create_task("上传证明")
    uploads = UploadStore()
    content = b"certifying letter"
    uploaded = uploads.save(task["task_id"], "../../proof.pdf", "application/pdf", content)

    assert uploaded == {
        "file_id": uploaded["file_id"],
        "filename": "proof.pdf",
        "mime_type": "application/pdf",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    stored = settings.data_dir / "uploads" / uploaded["file_id"]
    assert stored.read_bytes() == content
    assert not (settings.data_dir / "proof.pdf").exists()
    with session() as conn:
        assert (
            conn.execute(
                "SELECT storage_path FROM uploaded_files WHERE file_id = ?", (uploaded["file_id"],)
            ).fetchone()["storage_path"]
            == uploaded["file_id"]
        )

    draft = MailDraftStore().save_email_draft(
        task["task_id"],
        ["office@example.edu"],
        "证明文件",
        "请查收附件。",
        [uploaded["file_id"]],
    )
    assert MailDraftStore().get_draft(draft["operation_id"])["attachments"] == [uploaded]
