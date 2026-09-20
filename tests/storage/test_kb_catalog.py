"""资料目录：从资料文件现算的常驻指针，按目录分组、最近更新优先，受字符上限约束。"""

from server.tools.personal_kb.catalog import CatalogEntry, render_catalog
from server.tools.personal_kb.service import KbStore


def entry(path: str, title: str, updated_at: str, summary: str | None = None) -> CatalogEntry:
    return CatalogEntry(path=path, title=title, summary=summary, updated_at=updated_at)


def test_small_library_lists_every_document_grouped_by_directory():
    text = render_catalog(
        [
            entry("kb/课程/lab1.md", "GSE 实验一", "2026-09-10", "实验要求与截止日期"),
            entry("kb/项目/星云/验收.md", "星云验收纪要", "2026-09-15", "二期验收结论与代号"),
            entry("kb/项目/星云/周会.md", "周会纪要", "2026-09-12"),
            entry("kb/说明.md", "使用说明", "2026-09-01"),
        ]
    )

    assert text == (
        "资料库共 4 份资料；需要细节时用 kb_search 检索，或用 kb_read 读取原文。\n"
        "- 项目/星云/（2 份）\n"
        "  - 星云验收纪要：二期验收结论与代号\n"
        "  - 周会纪要\n"
        "- 课程/（1 份）\n"
        "  - GSE 实验一：实验要求与截止日期\n"
        "- （根目录）（1 份）\n"
        "  - 使用说明"
    )


def test_large_library_keeps_recent_documents_within_the_limit():
    entries = [
        entry(
            f"kb/归档/{index:03d}.md",
            f"归档记录 {index:03d}",
            f"2026-08-{index % 28 + 1:02d}",
            "x" * 80,
        )
        for index in range(200)
    ]
    entries.append(entry("kb/项目/最新.md", "最新进展", "2026-09-16", "今天的决定"))

    text = render_catalog(entries, limit=600)

    assert len(text) <= 600
    assert text.startswith("资料库共 201 份资料")
    assert "- 项目/（1 份）\n  - 最新进展：今天的决定" in text
    assert "- 归档/（200 份）" in text
    # 说明被截断到一句话的长度，列不下的合并成“另有 N 份”
    assert "x" * 61 not in text
    assert "  - 另有" in text


def test_directories_that_do_not_fit_are_summarised_at_the_end():
    entries = [
        entry(f"kb/目录{index:02d}/a.md", f"资料{index}", f"2026-09-{index + 1:02d}")
        for index in range(20)
    ]

    text = render_catalog(entries, limit=200)

    assert len(text) <= 200
    assert text.endswith("份资料未列出")
    # 最近更新的目录优先保留
    assert "目录19/" in text and "目录00/" not in text


def test_store_catalog_follows_files_summary_updates_and_user_edits(settings):
    kb = KbStore(settings.data_dir)
    assert kb.catalog() == ""

    saved = kb.save(
        title="星云验收纪要", body="## 结果\n\n通过。", path="项目/验收", summary="二期验收结论"
    )
    assert "  - 星云验收纪要：二期验收结论" in kb.catalog()
    assert kb.read(doc_id=saved["id"])["summary"] == "二期验收结论"

    updated = kb.update(
        expected_version=saved["version"], doc_id=saved["id"], summary="验收结论与遗留问题"
    )
    assert "星云验收纪要：验收结论与遗留问题" in kb.catalog()
    kb.update(expected_version=updated["version"], doc_id=saved["id"], summary="")
    assert "  - 星云验收纪要\n" in kb.catalog() + "\n"
    assert "summary" not in (settings.data_dir / saved["path"]).read_text(encoding="utf-8")

    # 用户在文件系统里新建、未写 frontmatter 的文件也进入目录；时间写成未加引号的 YAML 也能排序
    (settings.data_dir / "kb" / "课程").mkdir()
    (settings.data_dir / "kb" / "课程" / "lab1.md").write_text(
        "---\ntitle: GSE 实验一\nsummary: 实验要求\n"
        "updated_at: 2026-09-20T10:00:00+08:00\n---\n\n正文",
        encoding="utf-8",
    )
    (settings.data_dir / "kb" / "随手记.md").write_text("# 想法\n\n一个想法。", encoding="utf-8")
    catalog = kb.catalog()
    assert catalog.startswith("资料库共 3 份资料")
    assert "  - GSE 实验一：实验要求" in catalog
    assert "  - 想法" in catalog
    assert catalog.index("课程/") < catalog.index("项目/")
