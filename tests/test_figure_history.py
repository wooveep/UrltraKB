"""Image provenance survives page edits, portable copies and actual Qt rendering."""

import json
import shutil

import pytest
from figure_fixtures import record_markdown
from PIL import Image

from openkb.agent.page_figures import PageFigures, split_gallery
from openkb.agent.page_updates import PageUpdate
from openkb.figure_assets import freeze_figures, relative_figure
from openkb.figure_publication import preserve_page_figures
from openkb.markdown_images import image_references


def original_figures(wiki):
    (wiki / "sources/images").mkdir(parents=True)
    # Identical bytes at separate positions are distinct occurrences.
    Image.new("RGB", (80, 40), "red").save(wiki / "sources/images/图 片.png")
    raw = record_markdown(
        wiki,
        "sources/manual.md",
        "# Install\n![安装](<images/图 片.png>)\n\n# Maintain\n![维护](<images/图 片.png>)\n",
    )
    return freeze_figures(wiki, "sources/manual.content.json", raw, {})


def test_body_updates_keep_unedited_bindings_and_demote_ambiguous_old_figures(tmp_path):
    wiki = tmp_path / "wiki"
    first, second = original_figures(wiki)
    context = PageFigures(wiki, ({"id": "install", "figures": [first.occurrence_id]},), (first,))
    old = context.decorate(
        "concepts/operations.md",
        {"content": "# Install\nDisconnect.\n\n# Maintain\nInspect.", "source_ranges": ["install"]},
    )["content"]
    # A known inline binding in an unedited section remains current.
    second_context = PageFigures(
        wiki, ({"id": "maintain", "figures": [second.occurrence_id]},), (second,)
    )
    old = second_context.decorate(
        "concepts/operations.md",
        {"content": split_gallery(old)[0], "source_ranges": ["maintain"]},
        previous=old,
    )["content"]
    inline = f"![Maintenance]({relative_figure(wiki, 'concepts/operations.md', second.asset_path)})"
    # Same bytes cannot prove which occurrence supports this body: retain both as
    # history when there is no occurrence-specific inline binding.
    patch = PageUpdate(old, source_ranges=("empty-section",))
    assert "records:" not in str(patch.request())
    target = next(b["id"] for b in patch.request()["blocks"] if "Disconnect." in b["text"])
    applied = patch.apply(
        json.dumps(
            {
                "status": "updated",
                "base_sha256": patch.digest,
                "edits": [
                    {
                        "operation": "replace",
                        "block_id": target,
                        "content": "New installation rule.",
                    }
                ],
                "source_ranges": ["empty-section"],
            }
        ),
        current=old,
    )
    empty = PageFigures(wiki, ({"id": "empty-section", "figures": []},), ())
    updated = empty.decorate("concepts/operations.md", applied, previous=old, replaced=True)[
        "content"
    ]
    body, entries = split_gallery(updated)
    assert "Inspect." in body and inline not in body
    assert len(entries) == 2 and all(e["historical"] for e in entries)
    repeated = empty.decorate(
        "concepts/operations.md", {**applied, "content": body}, previous=updated
    )["content"]
    assert split_gallery(repeated)[1] == entries
    assert len({e["figure"]["asset_path"] for e in entries}) == 1


def test_bare_figure_reference_is_repaired_in_candidate_only(tmp_path):
    wiki = tmp_path / "wiki"
    original_figures(wiki)
    (wiki / "concepts/deep").mkdir(parents=True)
    page = wiki / "concepts/deep/guide.md"
    text = "---\nsources: [sources/manual.md]\n---\n原图：`图 片.png`\n"
    page.write_text(text)
    # Two appearances of that filename are ambiguous, even with shared bytes.
    from openkb.agent.page_response import PageResponseError

    with pytest.raises(PageResponseError, match="Ambiguous"):
        preserve_page_figures(tmp_path, wiki, {})
    assert page.read_text() == text
    record_markdown(wiki, "sources/manual.md", "# Install\n![安装](<images/图 片.png>)\n")
    preserve_page_figures(tmp_path, wiki, {})
    assert text.strip() in page.read_text()
    refs = image_references(page.read_text())
    assert len(refs) == 1 and refs[0].local_path.startswith("../../sources/figure-assets/")
    assert (page.parent / refs[0].local_path).is_file()


def test_moved_wiki_figures_render_in_the_real_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QApplication

    from openkb.desktop.reader import MarkdownView

    wiki = tmp_path / "wiki"
    figures = original_figures(wiki)
    context = PageFigures(
        wiki, ({"id": "all", "figures": [f.occurrence_id for f in figures]},), tuple(figures)
    )
    for section in ("sources", "summaries", "concepts/deep"):
        page = wiki / section / "reader.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            context.decorate(
                page.relative_to(wiki).as_posix(),
                {
                    "content": "# 原图回读",
                    "source_ranges": ["all"],
                },
            )["content"]
        )
    moved = tmp_path / "移动后的 Wiki"
    shutil.move(wiki, moved)
    app = QApplication.instance() or QApplication([])
    reader = MarkdownView()
    try:
        for page in moved.rglob("reader.md"):
            reader.show_markdown(page.read_text(), page.parent)
            for future in reader._futures:
                future.result(timeout=10)
            app.processEvents()
            assert len(reader._source_images) == 2
            for ref in image_references(page.read_text()):
                url = reader.document().baseUrl().resolved(QUrl(ref.source))
                picture = reader.document().resource(QTextDocument.ResourceType.ImageResource, url)
                assert not picture.isNull() and picture.width() == 80
                assert picture.toImage().pixelColor(0, 0).name() == "#ff0000"
    finally:
        reader.stop_rendering()
        reader.close()


def test_display_only_repair_does_not_claim_current_factual_inputs(tmp_path):
    from openkb.ingest_records import KnowledgeHead, KnowledgeRevision
    from openkb.knowledge_evidence import publication_dependencies

    wiki = tmp_path / "wiki"
    figures = original_figures(wiki)
    context = PageFigures(
        wiki, ({"id": "all", "figures": [f.occurrence_id for f in figures]},), tuple(figures)
    )
    page = "concepts/guide.md"
    old_input, new_input, revision = "a" * 32, "b" * 32, "c" * 32
    old = tmp_path / ".openkb/knowledge/legacy/revisions" / revision
    (old / "wiki/concepts").mkdir(parents=True)
    body = "---\nsources: [sources/manual.md]\n---\nA recorded procedure."
    (old / "wiki" / page).write_text(body)
    (old / "manifest.json").write_text(
        KnowledgeRevision(
            knowledge_revision_id=revision,
            view_id="legacy",
            change_kind="manual",
            base_revision_id=None,
            generated_baselines={},
            original_references=(),
            input_revisions=(old_input,),
            page_dependencies={page: (old_input,)},
        ).model_dump_json()
    )
    (wiki / "concepts").mkdir()
    (wiki / page).write_text(
        context.decorate(page, {"content": body, "source_ranges": ["all"]})["content"]
    )
    head = KnowledgeHead(view_id="legacy", knowledge_revision_id=revision)
    dependencies = publication_dependencies(
        tmp_path, head, {page: "changed"}, {page: "old"}, [new_input], wiki=wiki
    )
    assert dependencies[page] == (old_input,)
