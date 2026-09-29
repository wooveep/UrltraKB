"""The reading catalog contains knowledge rather than wiki maintenance internals."""

from openkb.application.knowledge import list_knowledge
from openkb.locks import atomic_write_text


def test_knowledge_catalog_uses_titles_descriptions_and_excludes_internal_files(kb_dir):
    for path in ("AGENTS.md", "index.md", "log.md", "reports/check.md", "sources/raw.md"):
        atomic_write_text(kb_dir / "wiki" / path, "# Internal")
    atomic_write_text(
        kb_dir / "wiki/concepts/file-slug.md", "---\ndescription: 检索线索\n---\n# 可读标题\n\n正文"
    )
    atomic_write_text(kb_dir / "wiki/explorations/note.md", "# 探索结论")
    atomic_write_text(kb_dir / "wiki/concepts/.hidden.md", "# Hidden")
    values = list_knowledge(kb_dir)
    assert {v.path for v in values} == {"concepts/file-slug.md", "explorations/note.md"}
    concept = next(v for v in values if v.section == "concepts")
    assert concept.title == "可读标题" and concept.description == "检索线索"
