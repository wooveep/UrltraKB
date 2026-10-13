"""Original literal context survives projected boundaries and document pagination."""

import pytest

from openkb.agent.evidence_payload import project_evidence, slice_evidence
from openkb.markdown_images import image_references


def test_duplicate_html_attribute_keeps_source_and_native_position_together():
    text = '<img src="first&amp;one.png" src="second.png">'
    (reference,) = image_references(text)
    assert reference.source == "first&one.png"
    assert text[reference.start : reference.end] == "first&amp;one.png"


@pytest.mark.parametrize("tag", ["xmp", "title", "textarea", "pre", "code"])
def test_literal_html_ancestry_survives_markdown_blocks(tag):
    literal = f'<{tag}>\n\n![example](literal.png)\n<img src="literal.png">\n\n</{tag}>'
    text = literal + '\n\n![real](real.png)\n<img src="real-html.png">\n'
    assert [ref.source for ref in image_references(text)] == ["real.png", "real-html.png"]
    assert "figure_interpretation" not in project_evidence({"content": literal})


def test_literal_fence_keeps_its_meaning_in_slices_and_continuations():
    text = "```markdown\n![example](literal.png)\n```\n"
    start = text.index("![")
    source = {"content": text}
    assert "figure_interpretation" not in project_evidence(source, offset=start)
    projected = slice_evidence(source, start, text.index("\n", start))
    assert projected["content"] == "![example](literal.png)"
    assert "figure_interpretation" not in projected


def test_document_pagination_parses_image_positions_once(kb_dir, monkeypatch):
    from test_original_structure import frozen_text

    from openkb import markdown_images
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.locks import atomic_write_json

    text = ("Ordinary source paragraph.\n\n" * 2000) + "![real](figure.png)\n"
    wiki = kb_dir / "wiki"
    atomic_write_json(wiki / "sources/manual.content.json", frozen_text(kb_dir, text))
    calls = []
    original = markdown_images.image_references

    def counted(value):
        calls.append(len(value))
        return original(value)

    monkeypatch.setattr(markdown_images, "image_references", counted)
    windows = list(
        CompileSourceWindows(wiki, "manual").iter_document_windows(
            fits=lambda payload: len(payload["content"]) <= 3500
        )
    )
    assert len(windows) > 10
    assert "".join(window["content"] for window in windows) == text
    assert calls == [len(text)]
    assert "figure_interpretation" in windows[-1]
