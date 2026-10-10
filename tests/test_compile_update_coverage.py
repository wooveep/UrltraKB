"""Incomplete update evidence cannot replace published knowledge."""

import json

import pytest
from test_compile_windows import compile_boundary

pytest_plugins = ("test_pdf_readback",)


def test_oversized_pdf_compile_window_keeps_paragraphs_and_native_continuation(kb_dir, monkeypatch):
    from openkb.agent.source_windows import CompileSourceWindows

    first = "Irrigation valve startup requires a precheck.\n\n"
    body = first + "Unrelated recorded operating context.\n\n" * 100 + "Winter needs supervision.\n"
    (kb_dir / "wiki/sources/current.json").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": body}])
    )
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
    context = CompileSourceWindows(kb_dir / "wiki", "current").prepare(
        {"name": "valve", "reading_locations": {"pages": "1"}}, long=True
    )
    context.require_update("entities/valve.md")
    window = context.windows[0]
    assert first in window["content"]
    assert window["page_spans"][0]["page"] == 1
    assert window["page_spans"][0]["start"] == 0
    assert type(window["continuation"]) is int
    assert not window["range_complete"]
    assert window["reading_action"]["continuation"]["offset"] == window["continuation"]


@pytest.mark.parametrize("unread_suffix", [False, True])
def test_compile_plan_reads_a_resumed_pdf_suffix_before_patching(
    kb_dir, monkeypatch, unread_suffix
):
    from openkb.agent.compile_plan import CompilePlanError, request_compile_plan
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.source_pages import read_page_selection

    first = "Irrigation valve startup requires a precheck.\n\n"
    suffix = "Winter needs trained supervision.\n"
    if unread_suffix:
        suffix = "Winter needs trained supervision. " * 100
    body = first + "Unrelated operating context.\n\n" * 100 + suffix
    raw = [{"page": 1, "unit_kind": "page", "content": body}]
    (kb_dir / "wiki/sources/current.json").write_text(json.dumps(raw))
    (kb_dir / "wiki/concepts/valve.md").write_text("# Valve\n\nRetained conditions.")
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
    windows = CompileSourceWindows(kb_dir / "wiki", "current")
    requests = []
    offset = read_page_selection(raw, "1")["content"].index(suffix)

    def request(correction):
        requests.append(correction)
        return json.dumps(
            {
                "concepts": {
                    "update": [
                        {
                            "name": "valve",
                            "reading_locations": {
                                "pages": "1",
                                **({"offset": offset} if correction else {}),
                            },
                        }
                    ]
                }
            }
        )

    def get_plan():
        return request_compile_plan(
            request,
            json.loads,
            existing={"concepts": {"valve"}, "entities": set()},
            entity_types=frozenset({"other"}),
            sanitize=lambda name: name,
            validate_updates=lambda plan: windows.validate_updates(plan, long=True),
        )

    if unread_suffix:
        with pytest.raises(CompilePlanError):
            get_plan()
        assert len(requests) == 2
        return
    plan = get_plan()
    assert len(requests) == 2
    context = windows.prepare(plan.concepts.update[0], "concepts/valve.md", long=True)
    content = "\n".join(w["content"] for w in context.windows)
    assert first in content and suffix in content
    native = [span for window in context.windows for span in window.get("page_spans", [])]
    assert any(span["page"] == 1 and span["start"] == 0 for span in native)
    assert any(span["page"] == 1 and span["start"] == body.index(suffix) for span in native)


def test_compile_matching_uses_aliases_from_the_retained_page(kb_dir):
    from openkb.agent.compile_plan import normalize_plan
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {
                    "page": 1,
                    "unit_kind": "page",
                    "content": "RY-7 starts only during the supervised maintenance window.",
                }
            ]
        )
    )
    (wiki / "concepts/orchard-regulator.md").write_text(
        "---\naliases: [RY-7]\n---\n# Orchard regulator\n\nRetained conditions."
    )
    plan = normalize_plan(
        {"concepts": {"update": [{"name": "orchard-regulator"}]}},
        existing={"concepts": {"orchard-regulator"}, "entities": set()},
        entity_types=frozenset({"other"}),
        sanitize=lambda name: name,
    )
    windows = CompileSourceWindows(wiki, "current")
    assert windows.validate_updates(plan, long=True) is None
    context = windows.prepare(plan.concepts.update[0], "concepts/orchard-regulator.md", long=True)
    context.require_update("concepts/orchard-regulator.md")
    assert "supervised maintenance" in context.windows[0]["content"]


def test_compile_text_reader_applies_the_returned_selection_offset(kb_dir):
    from openkb.agent.source_windows import CompileSourceWindows

    text = "# Prior conditions\n\nRecorded setup.\n\n# Winter\n\nSupervision is required.\n"
    offset = text.index("# Winter")
    (kb_dir / "wiki/sources/current.md").write_text(text)
    context = CompileSourceWindows(kb_dir / "wiki", "current").prepare(
        {"name": "valve", "reading_locations": {"chars": f"0:{len(text)}", "offset": offset}},
        long=True,
    )
    assert context.windows[0]["content"] == text[offset:]
    assert context.windows[0]["source_spans"] == [[offset, len(text)]]


def test_pdf_compile_projection_keeps_pixels_for_returned_pages_only(kb_dir, monkeypatch):
    from PIL import Image

    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/images").mkdir(exist_ok=True)
    for name in ("first", "later"):
        Image.new("RGB", (2, 2), "white").save(wiki / f"sources/images/{name}.png")
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {
                    "page": 1,
                    "unit_kind": "page",
                    "content": "Recorded setup.\n",
                    "images": [{"path": "sources/images/first.png"}],
                },
                {
                    "page": 2,
                    "unit_kind": "page",
                    "content": "Long indivisible paragraph. " * 100,
                    "images": [{"path": "sources/images/later.png"}],
                },
            ]
        )
    )
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
    context = CompileSourceWindows(wiki, "current").prepare(
        {"name": "setup", "reading_locations": {"pages": "1-2"}},
        long=True,
    )
    assert [image["path"] for image in context.windows[0]["images"]] == ["sources/images/first.png"]
    pixels = [part for part in context.message["content"] if part["type"] == "image_url"]
    assert len(pixels) == 1


@pytest.mark.parametrize(
    "subject,title",
    [("River Valve", "RIVER VALVE"), ("灌溉阀组", "灌溉阀组"), ("Straße", "STRASSE")],
)
def test_long_update_finds_complementary_originals_without_navigation_match(kb_dir, subject, title):
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": "Unrelated general introduction."},
                {"page": 2, "unit_kind": "page", "content": f"{subject}: capacity is 36 litres."},
                {
                    "page": 3,
                    "unit_kind": "page",
                    "content": f"{subject}: only available during irrigation.",
                },
            ]
        )
    )
    context = CompileSourceWindows(
        wiki,
        "current",
        navigation=[
            {"node_id": "root", "title": "Handbook", "start_index": 1, "end_index": 3},
        ],
    ).prepare({"name": subject, "title": title}, long=True)
    context.require_update("entities/valve.md")
    assert "36 litres" in str(context.message)
    assert "only available during irrigation" in str(context.message)
    assert "Unrelated general introduction" not in str(context.message)


@pytest.mark.parametrize("repair", ["retain", "withdraw", "related", "format_then_update"])
def test_no_current_long_window_keeps_existing_multisource_page(
    kb_dir, physical_pdf, pdf_model, monkeypatch, repair
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.unit_publication import read_head

    index_response = litellm.completion
    calls = []
    for name, action in (("old-a", "create"), ("old-b", "update")):
        path = kb_dir / f"{name}.txt"
        path.write_text(f"{name}: a supported condition.\n")
        compile_boundary(monkeypatch, {"concepts": {action: [{"name": "orchard"}]}}, calls)
        assert import_document(kb_dir, path).status == "added"
    old = (kb_dir / "wiki/concepts/orchard.md").read_bytes()
    head = read_head(kb_dir)
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 0})
    )
    calls.clear()
    planning = []

    def plan(messages):
        planning.append(messages)
        if repair == "format_then_update":
            return (
                {"concepts": 42}
                if len(planning) == 1
                else {"concepts": {"update": [{"name": "orchard"}]}}
            )
        if len(planning) == 1 or repair == "retain":
            return {"concepts": {"update": [{"name": "orchard"}]}}
        return {} if repair == "withdraw" else {"concepts": {"related": ["orchard"]}}

    compile_boundary(monkeypatch, plan, calls)
    compile_response = litellm.completion

    def routed(**kwargs):
        return (
            compile_response
            if "wiki compilation agent" in str(kwargs["messages"])
            else index_response
        )(**kwargs)

    async def asynchronous(**kwargs):
        return routed(**kwargs)

    monkeypatch.setattr("litellm.completion", routed)
    monkeypatch.setattr("litellm.acompletion", asynchronous)
    result = import_document(kb_dir, physical_pdf)
    if repair in {"withdraw", "related"}:
        assert result.status == "added", result
        assert "compile_plan_update_withdrawn" in result.quality
    else:
        assert result.status == "failed", result
        assert "page_update_originals_unread" in result.quality
    assert (kb_dir / "wiki/concepts/orchard.md").read_bytes() == old
    if repair not in {"withdraw", "related"}:
        assert read_head(kb_dir) == head
    if repair == "related":
        assert (
            "[[concepts/orchard]]"
            in (kb_dir / "wiki/summaries" / f"{physical_pdf.stem}.md").read_text()
        )
    assert len(planning) == 2
    assert not any("Update the concept page" in str(call) for call in calls)


def test_partial_prior_window_has_scoped_update_policy_and_continuation(kb_dir, monkeypatch):
    from openkb.agent.source_windows import CompileSourceWindows

    wiki = kb_dir / "wiki"
    (wiki / "sources/current.json").write_text(
        json.dumps(
            [{"page": 1, "unit_kind": "page", "content": "# Irrigation\nOld default is false."}]
        )
    )
    (wiki / "sources/old.md").write_text("Supported arithmetic: 3 * 4 = 12.\n\n" * 1000)
    (wiki / "concepts/topic.md").write_text(
        "---\nsources: [sources/old.md]\n---\nSupported orchard conditions."
    )
    monkeypatch.setattr("openkb.agent.source_windows.MAX_EVIDENCE_CHARS", 1800)
    context = CompileSourceWindows(wiki, "current").prepare(
        {"name": "topic", "reading_locations": {"pages": "1"}},
        "concepts/topic.md",
        long=True,
    )
    context.require_update("concepts/topic.md")
    assert context.windows[1]["coverage"] == "partial"
    assert context.windows[1]["reading_action"]
    assert context.windows[1]["unread"]
    prompt = str(context.message)
    assert "not evidence of error" in prompt
    assert "explicit counterevidence" in prompt
    assert "supported calculations" in prompt
