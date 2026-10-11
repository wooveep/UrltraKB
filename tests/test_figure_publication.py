"""Published page figures retain native positions and portable content identities."""

import json
import re

import pytest
from PIL import Image
from test_compile_windows import compile_boundary


def test_short_pages_use_final_source_ranges_and_keep_all_original_figures(kb_dir, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.markdown_images import image_references

    original = kb_dir / "manual.md"
    Image.new("RGB", (30, 30), "red").save(kb_dir / "installation.png")
    Image.new("RGB", (30, 30), "blue").save(kb_dir / "maintenance.png")
    original.write_text(
        "# Installation\nDisconnect power.\n![Original](installation.png)\n\n"
        "# Maintenance\nLog each inspection.\n![Original](maintenance.png)\n"
    )
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "maintenance"}]}}, [])
    import litellm

    previous = litellm.acompletion

    async def reply(**kwargs):
        result = await previous(**kwargs)
        for message in kwargs["messages"]:
            content = message.get("content")
            if isinstance(content, str) and content.startswith("Figure source ranges:\n"):
                ranges = json.loads(content.split("\n", 1)[1])["ranges"]
                chosen = next(r for r in ranges if "Maintenance" in r["label"])
                result.choices[0].message.content = json.dumps(
                    {
                        "description": "Maintenance",
                        "content": "# Maintenance\nLog each inspection.",
                        "source_ranges": [chosen["id"]],
                    }
                )
        return result

    monkeypatch.setattr(litellm, "acompletion", reply)
    assert import_document(kb_dir, original).status == "added"
    wiki = kb_dir / "wiki"
    page = wiki / "concepts/maintenance.md"
    images = image_references(page.read_text())
    assert len(images) == 1
    target = (page.parent / images[0].local_path).resolve()
    assert target.read_bytes() == (kb_dir / "maintenance.png").read_bytes()
    assert target.parent.name == "figure-assets"
    summary = wiki / "summaries/manual.md"
    assert len(image_references(summary.read_text())) == 2
    assert "source_revision_id" in page.read_text()
    assert "未进行图片理解" in page.read_text()


def test_replacing_source_keeps_old_page_figures_in_the_same_snapshot(kb_dir, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.markdown_images import image_references

    source = kb_dir / "guide.md"
    picture = kb_dir / "figure.png"
    Image.new("RGB", (40, 40), "red").save(picture)
    old_bytes = picture.read_bytes()
    source.write_text("# Guide\nDisconnect before maintenance.\n![Figure](figure.png)\n")
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "maintenance"}]}}, [])
    assert import_document(kb_dir, source).status == "added"
    page = kb_dir / "wiki/concepts/maintenance.md"
    old_body = page.read_text()
    Image.new("RGB", (40, 40), "blue").save(picture)
    source.write_text("# Guide\nInspect the new device.\n![Figure](figure.png)\n")
    compile_boundary(monkeypatch, {}, [])
    assert import_document(kb_dir, source).status == "added"
    assert page.read_text() == old_body
    old_target = page.parent / image_references(page.read_text())[0].local_path
    assert old_target.read_bytes() == old_bytes
    summary = kb_dir / "wiki/summaries/guide.md"
    new_target = summary.parent / image_references(summary.read_text())[0].local_path
    assert new_target.read_bytes() == picture.read_bytes()
    assert new_target.resolve() != old_target.resolve()


def test_invalid_final_range_blocks_the_entire_candidate(kb_dir, monkeypatch):
    import litellm

    from openkb.application.documents import import_document

    source = kb_dir / "invalid.md"
    Image.new("RGB", (40, 40), "red").save(kb_dir / "a.png")
    source.write_text("# Section\nA necessary requirement.\n![Original](a.png)")
    compile_boundary(monkeypatch, {"concepts": {"create": [{"name": "good"}, {"name": "bad"}]}}, [])
    original = litellm.acompletion
    attempts = []

    async def invalid_range(**kwargs):
        result = await original(**kwargs)
        if "Write the concept page for: bad" in str(kwargs):
            attempts.append(1)
            obj = json.loads(result.choices[0].message.content)
            obj["source_ranges"] = ["wrong-source-revision"]
            result.choices[0].message.content = json.dumps(obj)
        return result

    monkeypatch.setattr(litellm, "acompletion", invalid_range)
    result = import_document(kb_dir, source)
    assert result.status == "failed"
    assert len(attempts) == 2
    assert not (kb_dir / "wiki/concepts/good.md").exists()
    assert not (kb_dir / "wiki/summaries/invalid.md").exists()


@pytest.mark.parametrize("kind", ["concept", "entity"])
def test_creation_and_repair_contracts_allow_required_figure_ranges(kb_dir, monkeypatch, kind):
    """A field-count restriction must not contradict the required range extension."""
    import litellm

    from openkb.application.documents import import_document

    source = kb_dir / "procedure.md"
    Image.new("RGB", (40, 40), "red").save(kb_dir / "original.png")
    source.write_text("# Reactor\nDisconnect before maintenance.\n![Original](original.png)")
    collection = "concepts" if kind == "concept" else "entities"
    item = {"name": "reactor", **({"type": "product"} if kind == "entity" else {})}
    planned = {collection: {"create": [item]}}
    compile_boundary(monkeypatch, planned, [])
    previous = litellm.acompletion
    requests = []

    async def omit_range_once(**kwargs):
        response = await previous(**kwargs)
        if f"Write the {kind} page" in str(kwargs["messages"]):
            requests.append(kwargs["messages"])
            if len(requests) == 1:
                value = json.loads(response.choices[0].message.content)
                value.pop("source_ranges")
                response.choices[0].message.content = json.dumps(value)
        return response

    monkeypatch.setattr(litellm, "acompletion", omit_range_once)
    assert import_document(kb_dir, source).status == "added"
    assert len(requests) == 2
    for messages in requests:
        prompt = next(m["content"] for m in messages if f"Write the {kind} page" in str(m))
        assert not re.search(r"object with (two|three|[0-9]+) keys", prompt)
        declaration = next(
            m["content"] for m in messages if str(m["content"]).startswith("Figure source ranges:")
        )
        assert json.loads(declaration.split("\n", 1)[1])["source_ranges_schema"]["minItems"] == 1
    correction = requests[1][-1]["content"]
    assert "source_ranges" in correction
    assert "other required fields" in correction
    assert "type" in correction
    assert (kb_dir / f"wiki/{collection}/reactor.md").exists()


def test_invalid_embedded_image_keeps_original_and_text_without_sending_payload(
    kb_dir, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.source_catalog import read_source_revision

    source = kb_dir / "broken-image.md"
    original = "# Procedure\nDisconnect the load.\n![Figure](data:image/png;base64,NOT_VALID!!!)\n"
    source.write_text(original)
    calls = []
    compile_boundary(monkeypatch, {}, calls)
    result = import_document(kb_dir, source)
    assert result.status == "added"
    saved = read_source_revision(kb_dir, result.source_revision_id)
    assert (kb_dir / saved.original).read_text() == original
    assert calls and "NOT_VALID" not in str(calls)
    assert "Disconnect the load." in str(calls)
    frozen = json.loads((kb_dir / "wiki/sources/broken-image.content.json").read_text())
    assert frozen["diagnostics"]
