"""Native page excerpts do not acquire structure or figures from unrelated text."""

from ultrakb.agent.evidence_payload import project_evidence, slice_evidence
from ultrakb.source_pages import read_page_selection


def test_native_excerpt_does_not_inherit_config_comment_or_previous_section_figure():
    pages = [
        {"page": 1, "unit_kind": "page", "content": "node=backup\n# follower\n"},
        {
            "page": 2,
            "unit_kind": "page",
            "content": "Linux setup\n![image](sources/images/linux.png)\n\nWindows setup\nDo B.\n",
            "images": [{"path": "sources/images/linux.png"}],
        },
    ]
    source = read_page_selection(pages)
    excerpt = slice_evidence(
        source, source["content"].index("Windows setup"), len(source["content"])
    )

    assert excerpt["content"] == "Windows setup\nDo B.\n"
    assert not any(c["kind"] == "heading_path" for c in excerpt.get("display_context", []))
    assert excerpt["units"][0]["images"] == []
    assert excerpt["units"][0]["text_coverage"] == "partial"
    assert excerpt["units"][0]["omitted_page_images"] == 1
    whole = project_evidence(source)
    assert whole["units"][1]["images"] == pages[1]["images"]
    assert whole["units"][1]["text_coverage"] == "complete"
    assert pages[1]["images"] == [{"path": "sources/images/linux.png"}]


def test_unpositioned_page_figure_is_available_without_claiming_excerpt_membership():
    source = read_page_selection(
        [
            {
                "page": 1,
                "unit_kind": "page",
                "content": "First\nSecond",
                "images": [{"path": "x.png"}],
            }
        ]
    )
    complete = project_evidence(source)
    assert complete["units"][0]["images"] == [{"path": "x.png"}]
    assert "[Images:" not in complete["content"]
    excerpt = slice_evidence(source, source["content"].index("Second"), len(source["content"]))
    assert excerpt["units"][0]["images"] == []
    assert excerpt["units"][0]["omitted_page_images"] == 1


def test_native_configuration_comments_do_not_split_summary_sections():
    import json

    from ultrakb.agent.document_summary import summary_messages

    body = "Deployment\nnode=leader\n\n# follower\nnode=backup\n"
    window = {
        "content": body,
        "page_spans": [{"page": 1, "start": 0, "end": len(body)}],
        "supplementary_context": [],
    }
    messages = summary_messages({"role": "system", "content": "Compile"}, "manual", window)
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert payload["output_sections"] == [{"id": "section-1", "start": 0, "end": len(body)}]


def test_compiler_figure_range_uses_the_projected_occurrence_selection(kb_dir):
    import json

    from PIL import Image

    from ultrakb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    images = wiki / "sources/images"
    images.mkdir(exist_ok=True)
    Image.new("RGB", (2, 2)).save(images / "first.png")
    Image.new("RGB", (2, 2), "red").save(images / "second.png")
    pages = [
        {
            "page": 1,
            "unit_kind": "page",
            "content": (
                "First procedure\n![image](sources/images/first.png)\n\n"
                "Second procedure\n![image](sources/images/second.png)\n"
            ),
            "images": [{"path": "sources/images/first.png"}, {"path": "sources/images/second.png"}],
        }
    ]
    body = pages[0]["content"]
    pages[0]["image_reference_spans"] = [
        [body.index(image["path"]), body.index(image["path"]) + len(image["path"])]
        for image in pages[0]["images"]
    ]
    (wiki / "sources/manual.json").write_text(json.dumps(pages))
    source = read_page_selection(pages)
    selected = slice_evidence(
        source, source["content"].index("Second procedure"), len(source["content"])
    )
    figures = CompileSourceWindows(wiki, "manual").page_figures(
        [{"path": "sources/manual.json", **selected}],
        long=True,
    )
    assert [f.original_path for f in figures.figures] == ["sources/images/second.png"]
