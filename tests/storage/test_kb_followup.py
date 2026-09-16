"""资料库跟随用户改动，以及删除、移动与恢复（Phase 3）。

用户在文件系统里直接编辑、新建、移动、删除与复制资料，程序在下一次操作前纳入版本；
移动按 frontmatter 的 id 识别为同一份资料，历史跟随路径。删除保留历史，可以恢复。
"""

import subprocess

import pytest

from server.errors import KbValidationError, NotFoundError, VersionConflictError
from server.tools.personal_kb.service import KbStore

BODY = "## 验收结果\n\n本次验收代号 CORAL-7421，结论为通过。"


def git(data_dir, *args):
    return subprocess.run(
        ["git", "-C", str(data_dir), *args], capture_output=True, text=True, check=True
    ).stdout


def clean(data_dir) -> bool:
    return git(data_dir, "status", "--porcelain", "--", "kb").strip() == ""


def test_manual_edit_becomes_a_version_and_blocks_stale_agent_updates(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY)
    path = settings.data_dir / saved["path"]
    path.write_text(path.read_text(encoding="utf-8").replace("CORAL-7421", "CORAL-9000"))

    # Agent 按旧版本修改：先纳入用户改动，旧版本不再匹配，用户内容不被覆盖
    with pytest.raises(VersionConflictError):
        kb.update(expected_version=saved["version"], doc_id=saved["id"], body="Agent 的改写")
    assert "CORAL-9000" in path.read_text(encoding="utf-8")
    assert clean(settings.data_dir)

    history = kb.history(doc_id=saved["id"])
    assert history["versions"][0]["summary"].startswith("[Kb] User edit")
    assert history["versions"][0]["version"] != saved["version"]
    old = kb.read(doc_id=saved["id"], version=saved["version"])
    assert "CORAL-7421" in old["body"]


def test_new_file_without_frontmatter_gets_identity_and_keeps_its_body(settings):
    kb = KbStore(settings.data_dir)
    folder = settings.data_dir / "kb" / "课程"
    folder.mkdir(parents=True)
    body = "# GSE 实验一\n\n提交截止 10 月 8 日。\n"
    (folder / "lab1.md").write_text(body, encoding="utf-8")

    hits = kb.search(query="提交截止")["results"]

    assert hits and hits[0]["path"] == "kb/课程/lab1.md"
    text = (folder / "lab1.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and text.endswith(body)
    document = kb.read(path="kb/课程/lab1.md")
    assert document["id"].startswith("kb_")
    assert document["title"] == "GSE 实验一"
    assert clean(settings.data_dir)


def test_manual_move_with_edit_is_the_same_document_and_history_follows(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY)
    old = settings.data_dir / saved["path"]
    new = settings.data_dir / "kb" / "项目" / "星云验收.md"
    new.parent.mkdir(parents=True)
    # 移动同时大改正文：Git 的相似度判断认不出，靠 id 识别
    new.write_text(
        old.read_text(encoding="utf-8").replace(BODY, "## 全新内容\n\n完全不同的一段正文。"),
        encoding="utf-8",
    )
    old.unlink()

    listed = kb.list()["documents"]

    assert [item["path"] for item in listed] == ["kb/项目/星云验收.md"]
    assert listed[0]["id"] == saved["id"]
    history = kb.history(doc_id=saved["id"])
    paths = [entry["path"] for entry in history["versions"]]
    assert paths[-1] == saved["path"] and paths[0] == "kb/项目/星云验收.md"
    assert "CORAL-7421" in kb.read(doc_id=saved["id"], version=saved["version"])["body"]
    assert not kb.search(query="CORAL-7421")["results"]
    assert kb.search(query="完全不同")["results"][0]["id"] == saved["id"]


def test_copied_file_gets_a_new_identity(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY)
    original = settings.data_dir / saved["path"]
    copy = original.with_name("副本.md")
    copy.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    documents = kb.list()["documents"]

    ids = {item["path"]: item["id"] for item in documents}
    assert ids[saved["path"]] == saved["id"]
    assert ids["kb/inbox/副本.md"] != saved["id"]


def test_manual_delete_is_recorded_and_can_be_restored(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY)
    (settings.data_dir / saved["path"]).unlink()

    assert not kb.search(query="CORAL-7421")["results"]
    history = kb.history(doc_id=saved["id"])
    assert history["deleted"] is True
    assert history["versions"][0]["deleted"] is True

    with pytest.raises(KbValidationError):
        kb.restore(doc_id=saved["id"], version=history["versions"][0]["version"])
    restored = kb.restore(doc_id=saved["id"], version=saved["version"])

    assert restored["path"] == saved["path"]
    assert restored["restored_from"] == saved["version"]
    assert kb.search(query="CORAL-7421")["results"][0]["id"] == saved["id"]


def test_delete_move_and_restore_through_the_store(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY)
    other = kb.save(title="另一份", body="## 分节\n\n无关正文。", path="项目/占位.md")

    with pytest.raises(VersionConflictError):
        kb.move(expected_version="0" * 40, doc_id=saved["id"], new_path="项目/星云.md")
    with pytest.raises(KbValidationError):
        kb.move(expected_version=saved["version"], doc_id=saved["id"], new_path=other["path"])
    moved = kb.move(expected_version=saved["version"], doc_id=saved["id"], new_path="项目/星云")

    assert moved["previous_path"] == saved["path"]
    assert moved["path"] == "kb/项目/星云.md"
    assert kb.search(query="CORAL-7421")["results"][0]["path"] == "kb/项目/星云.md"

    updated = kb.update(expected_version=moved["version"], doc_id=saved["id"], body="## 新\n\n改过")
    deleted = kb.delete(expected_version=updated["version"], doc_id=saved["id"])

    assert deleted["previous_version"] == updated["version"]
    listed = kb.list(deleted=True)["documents"]
    assert [(item["id"], item["path"], item["version"]) for item in listed] == [
        (saved["id"], "kb/项目/星云.md", updated["version"])
    ]
    assert not (settings.data_dir / "kb" / "项目" / "星云.md").exists()
    assert not kb.search(query="改过")["results"]
    with pytest.raises(NotFoundError):
        kb.read(doc_id=saved["id"])

    # 找回删除前的版本：回到删除时所在的位置，恢复产生新提交
    restored = kb.restore(doc_id=saved["id"], version=updated["version"])
    assert restored["path"] == "kb/项目/星云.md"
    assert kb.list(deleted=True)["documents"] == []
    assert kb.read(doc_id=saved["id"])["body"] == "## 新\n\n改过"

    # 资料仍存在时恢复为更早的版本（当时在旧路径），内容回到最初
    again = kb.restore(doc_id=saved["id"], version=saved["version"])
    assert again["path"] == "kb/项目/星云.md"
    assert again["previous_version"] == restored["version"]
    assert "CORAL-7421" in kb.read(doc_id=saved["id"])["body"]
    assert len(kb.history(doc_id=saved["id"])["versions"]) == 6


def test_restoring_a_deleted_document_refuses_an_occupied_location(settings):
    kb = KbStore(settings.data_dir)
    saved = kb.save(title="验收纪要", body=BODY, path="项目/验收.md")
    kb.delete(expected_version=saved["version"], doc_id=saved["id"])
    kb.save(title="新的一份", body="占用了原位置。", path="项目/验收.md")

    with pytest.raises(KbValidationError):
        kb.restore(doc_id=saved["id"], version=saved["version"])
    # 同一位置先后放过两份资料：按 id 查历史只列出这份资料自己的版本
    versions = kb.history(doc_id=saved["id"])["versions"]
    assert [entry["deleted"] for entry in versions] == [True, False]
