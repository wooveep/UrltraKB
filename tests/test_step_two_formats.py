"""Saved format structure remains usable through the real PageIndex evidence reader."""

from openkb.config import load_config
from openkb.evidence import Evidence
from openkb.locks import kb_ingest_lock
from openkb.navigation import prepare_navigation
from openkb.pageindex_store import indexed_reader
from openkb.parsing import parse_document
from tests.test_native_parsing import source_version


def saved_views(kb, path):
    source = source_version(kb, path)
    parsed = parse_document(kb, source, options={"ocr": {"policy": "off"}})
    settings = {**load_config(kb / ".openkb/config.yaml"), "navigation": {"enabled": False}}
    with kb_ingest_lock(kb / ".openkb"):
        navigation = prepare_navigation(kb, source, parsed, settings)
    reader = indexed_reader(kb, source, parsed, navigation)
    views = [
        reader.read(Evidence(source.source_id, source.id, parsed.id, b.id), max_chars=10000)
        for b in parsed.blocks
    ]
    return parsed, navigation, views


def test_markdown_structures_and_images_keep_their_own_lines(kb_dir, tmp_path):
    path = tmp_path / "guide.md"
    (tmp_path / "figure.png").write_bytes(b"frozen local asset")
    path.write_text(
        "---\ntitle: Guide\n---\n\nInstall\n=======\n\n"
        "- First\n  - Nested\n\n```sh\necho hello\n```\n\n"
        "| Name | Value |\n| --- | --- |\n| Timeout | 42 |\n\n"
        "![Diagram](figure.png)\n\nUnrelated text.\n"
    )
    parsed, navigation, views = saved_views(kb_dir, path)
    assert [b.kind for b in parsed.blocks] == [
        "metadata",
        "heading",
        "list",
        "code",
        "table",
        "paragraph",
        "paragraph",
    ]
    assert [(b.location["line"], b.location["line_end"]) for b in parsed.blocks] == [
        (1, 3),
        (5, 6),
        (8, 9),
        (11, 13),
        (15, 17),
        (19, 19),
        (21, 21),
    ]
    assert parsed.blocks[1].location["heading_level"] == 1
    assert [i for i, b in enumerate(parsed.blocks) if b.assets] == [5]
    assert "asset:" in views[5].text
    assert views[-1].text == "Unrelated text."
    assert any(n["title_origin"] == "source" for n in navigation["nodes"])


def test_csv_multiline_fields_remain_one_record_with_headers(kb_dir, tmp_path):
    path = tmp_path / "records.csv"
    path.write_text('Name;Description;Timeout\nrouter;"first\n\nsecond;part";42\n')
    parsed, _, views = saved_views(kb_dir, path)
    assert len(parsed.blocks) == 2
    assert parsed.blocks[1].kind == "table"
    assert parsed.blocks[1].location == {
        "kind": "csv",
        "row": 2,
        "line": 2,
        "line_end": 4,
        "columns": ["Name", "Description", "Timeout"],
        "delimiter": ";",
    }
    assert '"first\n\nsecond;part"' in views[1].text
    assert "Timeout" in views[1].context


def test_html_keeps_dom_positions_and_freezes_only_owned_images(kb_dir, tmp_path):
    path = tmp_path / "page.html"
    (tmp_path / "plot.png").write_bytes(b"immutable image")
    path.write_text(
        '<html><body><h1 id="install">Install</h1>'
        '<p>Use <b>version 7</b>.</p><figure><img src="plot.png" alt="Chart">'
        "<figcaption>Capacity</figcaption></figure><p>Other.</p></body></html>"
    )
    parsed, _, views = saved_views(kb_dir, path)
    assert all(b.location["kind"] == "html" and "line" not in b.location for b in parsed.blocks)
    assert parsed.blocks[0].location["dom_path"] == "/html/body/h1"
    assert parsed.blocks[0].location["dom_id"] == "install"
    assert all(b.location["selection"] == "body" for b in parsed.blocks)
    assert any(v.text == "Use version 7." for v in views)
    images = [b for b in parsed.blocks if b.assets]
    assert len(images) == 1
    assert images[0].location["dom_path"] == "/html/body/figure/img"


def test_xml_preserves_namespaces_attributes_and_mixed_text_order(kb_dir, tmp_path):
    path = tmp_path / "document.xml"
    path.write_text(
        '<d:article xmlns:d="urn:doc"><d:para mode="strict">Before '
        "<d:emphasis>inside</d:emphasis> after.</d:para></d:article>"
    )
    parsed, _, views = saved_views(kb_dir, path)
    assert [v.text for v in views if v.text.strip()] == ["Before ", "inside", " after."]
    assert all(b.location["kind"] == "xml" and "line" not in b.location for b in parsed.blocks)
    para = next(b for b in parsed.blocks if b.location.get("attributes") == {"mode": "strict"})
    assert para.location["element_path"] == "/d:article/d:para"
    assert para.location["namespaces"] == {"d": "urn:doc"}
    assert parsed.blocks[-1].location["text_role"] == "tail"


def test_xml_configuration_attributes_are_readable_through_next_step(kb_dir, tmp_path):
    path = tmp_path / "settings.xml"
    path.write_text('<settings><timeout seconds="42"/></settings>')
    parsed, _, views = saved_views(kb_dir, path)
    assert any('seconds="42"' in view.text for view in views)
    assert not any(row["status"] == "needs_review" for row in parsed.quality)


def test_html_utf8_text_and_local_image_names_are_preserved(kb_dir, tmp_path):
    path = tmp_path / "页面.html"
    (tmp_path / "图.png").write_bytes(b"image")
    path.write_text('<html><body><p>必须备份。</p><img src="图.png" alt="备份"/></body></html>')
    parsed, _, views = saved_views(kb_dir, path)
    assert views[0].text == "必须备份。"
    assert parsed.blocks[1].assets


def test_markdown_reference_images_freeze_assets_and_keep_definitions(kb_dir, tmp_path):
    path = tmp_path / "references.md"
    (tmp_path / "plot.png").write_bytes(b"reference image")
    path.write_text('![Capacity][plot]\n\nUnrelated.\n\n[plot]: plot.png "Capacity chart"\n')
    parsed, _, views = saved_views(kb_dir, path)
    assert parsed.blocks[0].assets
    assert all(not b.assets for b in parsed.blocks[1:])
    assert "asset:" in views[0].context
    assert any('[plot]: plot.png "Capacity chart"' in v.text for v in views)
