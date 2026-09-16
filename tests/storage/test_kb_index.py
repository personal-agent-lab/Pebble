"""资料库检索索引：分节切分、关键词检索、增量替换与重建、引用定位。

索引与资料都用真实文件与真实本地 Git，只在临时实例目录中写入；断言直接比对磁盘原文，
不依赖索引内部的中间结构。
"""

import sqlite3
import subprocess

import pytest

from server.errors import (
    KbIndexUnavailableError,
    KbValidationError,
    NotFoundError,
)
from server.tools.personal_kb.index import INDEX_SCHEMA
from server.tools.personal_kb.service import KbStore

TITLE = "验收纪要"
BODY = """## 验收结果

本次验收代号 CORAL-7421，结论为通过。

## 后续安排

复验安排在两周后，负责人张老师。
"""


def store(settings) -> KbStore:
    return KbStore(settings.data_dir)


def lines_of(path) -> list[str]:
    return path.read_text(encoding="utf-8").split("\n")


def test_search_matches_chinese_english_numbered_and_tagged_sections(settings):
    kb = store(settings)
    kb.save(
        title="张老师",
        body="## 沟通偏好\n\n先给结论，再补充细节。",
        tags=["人物", "GSE"],
    )
    kb.save(title=TITLE, body=BODY, tags=["课程"])
    kb.save(
        title="实验室设备清单",
        body="## 设备\n\n离心机 CO-7100 与移液器各两台。",
    )

    assert kb.search(query="沟通偏好")["results"][0]["ref"]["path"].startswith("kb/inbox/")
    assert kb.search(query="结论")["results"][0]["title"] == "张老师"
    assert kb.search(query="CORAL-7421")["results"][0]["title"] == TITLE
    assert kb.search(query="离心机")["results"][0]["title"] == "实验室设备清单"
    # 标签与较短的关键词都能单独限定
    assert [item["title"] for item in kb.search(query="GSE")["results"]] == ["张老师"]
    assert [item["title"] for item in kb.search(query="复验", tag="课程")["results"]] == [TITLE]
    assert kb.search(query="复验", tag="人物")["results"] == []
    assert kb.search(query="先给结论")["results"][0]["title"] == "张老师"


def test_short_keywords_use_containment_on_the_same_index(settings):
    kb = store(settings)
    kb.save(title=TITLE, body=BODY)

    # 1–2 个字无法被 trigram 分词命中，改用同一索引的包含匹配
    two_chars = kb.search(query="复验")["results"]
    one_char = kb.search(query="墙")["results"]

    assert two_chars[0]["title"] == TITLE
    assert one_char == []
    assert kb.search(query="复验 负责人")["results"][0]["title"] == TITLE


def test_sections_follow_second_level_headings_with_real_line_numbers(settings):
    kb = store(settings)
    saved = kb.save(title=TITLE, body=BODY, path="项目/验收.md")
    path = settings.data_dir / saved["path"]
    text_lines = lines_of(path)

    hits = kb.search(query="CORAL-7421")["results"]
    first = hits[0]

    # 行号是文件的真实行号，含 frontmatter，可直接切出原文
    assert first["lines"] == first["ref"]["lines"] == [8, 10]
    assert first["path"] == saved["path"]
    assert first["heading"] == f"{TITLE} / 验收结果"
    assert text_lines[first["ref"]["lines"][0] - 1] == "## 验收结果"
    assert "CORAL-7421" in text_lines[first["ref"]["lines"][1] - 1]
    assert first["ref"] == {
        "id": saved["id"],
        "path": saved["path"],
        "heading": first["heading"],
        "lines": [8, 10],
        "commit": saved["version"],
    }

    second = kb.search(query="负责人")["results"][0]["ref"]
    assert second["heading"] == f"{TITLE} / 后续安排"
    assert text_lines[second["lines"][0] - 1] == "## 后续安排"
    assert second["lines"] == [12, 14]


def test_documents_without_headings_and_empty_sections_are_indexed(settings):
    kb = store(settings)
    kb.save(title="无标题笔记", body="整篇就是一段正文，没有二级标题。")
    kb.save(title="空分节", body="## 只有标题\n\n## 第二节\n\n有正文。")
    kb.save(title="前言与分节", body="写在分节之前的开场白。\n\n## 正文\n\n分节内容。")

    assert kb.search(query="整篇就是一段正文")["results"][0]["heading"] == "无标题笔记"
    empty = kb.search(query="只有标题")["results"][0]["ref"]
    assert empty["heading"] == "空分节 / 只有标题" and empty["lines"][0] == empty["lines"][1]
    preamble = kb.search(query="开场白")["results"][0]
    assert preamble["heading"] == "前言与分节"
    assert "开场白" in preamble["snippet"]


def test_code_fence_headings_are_not_sections(settings):
    kb = store(settings)
    kb.save(
        title="含代码块",
        body=(
            "## 用法\n\n示例：\n\n```markdown\n## 这是代码里的标题\n```\n\n"
            "## 说明\n\n代码里的标题不算分节。"
        ),
    )

    headings = {hit["heading"] for hit in kb.search(query="标题")["results"]}

    assert headings == {"含代码块 / 用法", "含代码块 / 说明"}


def test_results_are_ordered_by_field_priority_and_limited(settings):
    kb = store(settings)
    kb.save(title="甲资料", body="这一段正文提到了锚点这个词。")
    kb.save(title="乙资料", body="无关正文。", tags=["锚点"])
    kb.save(title="丙资料", body="## 锚点\n\n正文与关键词无关。")
    kb.save(title="锚点丁", body="正文与关键词无关。")

    order = [hit["title"] for hit in kb.search(query="锚点")["results"]]

    assert order == ["锚点丁", "丙资料", "乙资料", "甲资料"]
    assert len(kb.search(query="锚点", max_results=2)["results"]) == 2

    with pytest.raises(KbValidationError):
        kb.search(query="锚点", max_results=0)
    with pytest.raises(KbValidationError):
        kb.search(query="锚点", max_results=21)
    with pytest.raises(KbValidationError):
        kb.search(query="   ")


def test_save_and_update_replace_only_the_affected_document(settings):
    kb = store(settings)
    first = kb.save(title="第一份", body="## 分节\n\n旧版本的正文内容。")
    second = kb.save(title="第二份", body="## 分节\n\n另一份资料的正文。")

    updated = kb.update(
        doc_id=first["id"], expected_version=first["version"], body="## 分节\n\n新版本的正文内容。"
    )

    assert kb.search(query="旧版本")["results"] == []
    assert kb.search(query="新版本")["results"][0]["ref"]["path"] == first["path"]
    assert kb.search(query="另一份资料")["results"][0]["ref"]["path"] == second["path"]
    assert updated["index_status"] == "ok"


def test_index_is_rebuilt_when_missing_corrupted_or_out_of_sync(settings):
    kb = store(settings)
    kb.save(title=TITLE, body=BODY)
    index_file = settings.data_dir / "kb-index.sqlite3"

    index_file.unlink()
    assert kb.search(query="CORAL-7421")["results"]

    index_file.write_bytes(b"not a database at all")
    assert kb.search(query="CORAL-7421")["results"]

    _set_meta(index_file, "schema", "999")
    assert kb.search(query="CORAL-7421")["results"]
    assert _meta(index_file, "schema") == str(INDEX_SCHEMA)

    _set_meta(index_file, "tree", "stale-tree")
    assert kb.search(query="CORAL-7421")["results"]
    assert _meta(index_file, "tree") == _tree(settings.data_dir)


def test_user_edits_without_a_commit_are_not_indexed(settings):
    kb = store(settings)
    saved = kb.save(title=TITLE, body=BODY)
    path = settings.data_dir / saved["path"]
    path.write_text(path.read_text(encoding="utf-8") + "\n尚未提交的补充说明。\n", encoding="utf-8")
    kb.save(title="另一份", body="## 分节\n\n与上面无关的正文。")

    # 未纳入版本的内容不冒充已同步资料，也不影响其他资料的检索
    assert kb.search(query="尚未提交")["results"] == []
    assert kb.search(query="另一份")["results"]

    # 重建时同样只收录与 Git 版本一致的文件
    (settings.data_dir / "kb-index.sqlite3").unlink()
    assert kb.search(query="尚未提交")["results"] == []
    assert kb.search(query="CORAL-7421")["results"] == []
    assert kb.search(query="另一份")["results"]


def test_rebuild_failure_refuses_to_answer_from_the_stale_index(settings, monkeypatch):
    kb = store(settings)
    kb.save(title=TITLE, body=BODY)
    assert kb.search(query="CORAL-7421")["results"]

    def fail(_rel):
        raise RuntimeError("模拟索引写入失败")

    monkeypatch.setattr(kb, "_document", fail)
    stale = kb.save(title="另一份", body="## 分节\n\n新增的这一份让 tree 变化。")

    assert stale["index_status"] == "stale"
    with pytest.raises(KbIndexUnavailableError):
        kb.search(query="CORAL-7421")


def test_stale_index_status_is_reported_without_rolling_back_the_file(settings, monkeypatch):
    kb = store(settings)

    def fail(_rel):
        raise RuntimeError("模拟索引写入失败")

    monkeypatch.setattr(kb, "_document", fail)
    saved = kb.save(title=TITLE, body=BODY)

    assert saved["index_status"] == "stale"
    assert (settings.data_dir / saved["path"]).exists()
    assert kb.read(path=saved["path"])["body"] == BODY.strip()


def test_ref_reads_the_exact_section_and_rejects_forged_refs(settings):
    kb = store(settings)
    saved = kb.save(title=TITLE, body=BODY, path="项目/验收.md")
    ref = kb.search(query="CORAL-7421")["results"][0]["ref"]
    text_lines = lines_of(settings.data_dir / saved["path"])

    read = kb.read(ref=ref)

    assert read["body"] == "\n".join(text_lines[ref["lines"][0] - 1 : ref["lines"][1]])
    assert read["ref"] == ref
    assert read["title"] == TITLE and read["id"] == saved["id"]

    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "lines": [ref["lines"][0], 999]})
    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "lines": [0, 1]})
    # 行号在范围内但不落在任何分节边界上：拼接出来的引用不算数
    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "lines": [ref["lines"][0], ref["lines"][1] - 1]})
    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "lines": [9, 13]})
    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "id": "kb_someoneelse"})
    with pytest.raises(KbValidationError):
        kb.read(ref={**ref, "path": "../outside.md"})
    with pytest.raises(KbValidationError):
        kb.read(ref={"path": saved["path"], "commit": ref["commit"]})
    with pytest.raises(NotFoundError):
        kb.read(ref={**ref, "commit": "0" * 40})
    with pytest.raises(NotFoundError):
        kb.read(ref={**ref, "path": "kb/inbox/missing.md"})


def test_whole_body_range_ref_is_accepted(settings):
    kb = store(settings)
    saved = kb.save(title=TITLE, body=BODY, path="项目/验收.md")
    ref = kb.search(query="CORAL-7421")["results"][0]["ref"]

    # 整篇引用（save/read 返回的 ref）覆盖正文区间，可以读回整篇正文
    whole = kb.read(doc_id=saved["id"])
    read = kb.read(ref={**ref, "lines": whole["lines"]})

    assert read["heading"] is None
    assert read["body"] == whole["body"]


def test_best_match_is_not_dropped_when_candidates_exceed_the_cap(settings):
    kb = store(settings)
    sections = "\n".join(f"## 群体 {index}\n\n燕鸥在湿地过冬。" for index in range(501))
    kb.save(title="湿地鸟类普查", body=sections)
    # 标题直接命中关键词的资料最后保存：截断若发生在排序之前，它会被直接丢弃
    kb.save(title="燕鸥迁徙志", body="正文与关键词无关。")

    results = kb.search(query="燕鸥")["results"]

    assert results[0]["title"] == "燕鸥迁徙志"


def test_old_ref_still_locates_the_content_it_quoted(settings):
    kb = store(settings)
    saved = kb.save(title=TITLE, body=BODY, path="项目/验收.md")
    old_ref = kb.search(query="CORAL-7421")["results"][0]["ref"]

    kb.update(
        doc_id=saved["id"],
        expected_version=saved["version"],
        body="## 验收结果\n\n本次验收代号 REVOKED，结论为不通过。",
    )
    new_ref = kb.search(query="REVOKED")["results"][0]["ref"]

    assert new_ref["commit"] != old_ref["commit"]
    assert "REVOKED" in kb.read(ref=new_ref)["body"]
    assert "CORAL-7421" in kb.read(ref=old_ref)["body"]
    assert kb.search(query="CORAL-7421")["results"] == []
    # 旧引用仍能定位到同一份资料
    assert kb.read(ref=old_ref)["id"] == saved["id"]


def _meta(index_file, key: str) -> str:
    conn = sqlite3.connect(index_file)
    try:
        row = conn.execute("SELECT value FROM kb_index_meta WHERE key = ?", (key,)).fetchone()
        return row[0]
    finally:
        conn.close()


def _set_meta(index_file, key: str, value: str) -> None:
    conn = sqlite3.connect(index_file)
    try:
        conn.execute("UPDATE kb_index_meta SET value = ? WHERE key = ?", (value, key))
        conn.commit()
    finally:
        conn.close()


def _tree(data_dir) -> str:
    return subprocess.run(
        ["git", "-C", str(data_dir), "rev-parse", "HEAD:kb"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
