"""任务归档：原始内容与总结分节写入 archive 目录，校验失败不写入。"""

import pytest

from server.errors import KbValidationError
from server.tools.personal_kb.service import KbStore


def test_archive_separates_original_content_from_summary(settings):
    kb = KbStore(settings.data_dir)

    archived = kb.archive(
        title="与张老师约定课程讨论",
        items=[
            {"kind": "summary", "heading": "执行结果", "text": "回复邮件已发送；日程已创建。"},
            {"kind": "original", "heading": "来信", "text": "下周四下午三点可以吗？"},
        ],
    )

    assert archived["path"].startswith("kb/archive/")
    body = kb.read(path=archived["path"])["body"]
    # 原始内容在前、总结在后，各自成节
    assert body == (
        "## 原始内容：来信\n\n下周四下午三点可以吗？\n\n"
        "## 总结与执行结果：执行结果\n\n回复邮件已发送；日程已创建。"
    )
    assert kb.search(query="日程已创建")["results"][0]["path"] == archived["path"]


def test_archive_rejects_empty_or_unknown_items_without_writing(settings):
    kb = KbStore(settings.data_dir)
    with pytest.raises(KbValidationError):
        kb.archive(title="空归档", items=[])
    with pytest.raises(KbValidationError):
        kb.archive(title="错误类型", items=[{"kind": "note", "text": "内容"}])
    with pytest.raises(KbValidationError):
        kb.archive(title=" ", items=[{"kind": "summary", "text": "内容"}])
    assert kb.list(directory="archive")["documents"] == []
