"""Incomplete update evidence cannot replace published knowledge."""

import json

import pytest
from test_compile_windows import _plan

from ultrakb.locks import atomic_write_text

pytest_plugins = ("processing_fixtures",)


def test_oversized_pdf_compile_window_keeps_paragraphs_and_native_continuation(kb_dir, monkeypatch):
    from ultrakb.agent.source_windows import CompileSourceWindows

    first = "Irrigation valve startup requires a precheck.\n\n"
    body = first + "Unrelated recorded operating context.\n\n" * 100 + "Winter needs supervision.\n"
    (kb_dir / "wiki/sources/current.json").write_text(
        json.dumps([{"page": 1, "unit_kind": "page", "content": body}])
    )
    monkeypatch.setattr("ultrakb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
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
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    first = "Irrigation valve startup requires a precheck.\n\n"
    suffix = "Winter needs trained supervision.\n"
    if unread_suffix:
        suffix = "Winter needs trained supervision. " * 100
    body = first + "Unrelated operating context.\n\n" * 100 + suffix
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/current.json",
        json.dumps([{"page": 1, "unit_kind": "page", "content": body}]),
    )
    atomic_write_text(wiki / "concepts/valve.md", "# Valve\n\nRetained conditions.")
    monkeypatch.setattr("ultrakb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
    windows = CompileSourceWindows(wiki, "current")
    plan = _plan(wiki, {"update": [{"name": "valve", "reading_locations": {"pages": "1"}}]})
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=0, retries=0),
    )
    with executor.activate():
        assert windows.validate_updates(plan, long=True) is None
        context = windows.prepare(plan.concepts.update[0], "concepts/valve.md", long=True)
    # Ordinary reader continuation is local and must not request a new model plan.
    assert executor.calls == 0
    content = "".join(w["content"] for w in context.windows)
    assert first in content and suffix in content
    native = [span for window in context.windows for span in window.get("page_spans", [])]
    assert any(span["page"] == 1 and span["start"] == 0 for span in native)
    assert all(left["end"] == right["start"] for left, right in zip(native, native[1:]))
    assert native[-1]["end"] == len(body)


def test_compile_matching_uses_aliases_from_the_retained_page(kb_dir):
    from ultrakb.agent.compile_plan import normalize_plan
    from ultrakb.agent.source_windows import CompileSourceWindows

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
    from ultrakb.agent.source_windows import CompileSourceWindows

    text = "# Prior conditions\n\nRecorded setup.\n\n# Winter\n\nSupervision is required.\n"
    offset = text.index("# Winter")
    (kb_dir / "wiki/sources/current.md").write_text(text)
    context = CompileSourceWindows(kb_dir / "wiki", "current").prepare(
        {"name": "valve", "reading_locations": {"chars": f"0:{len(text)}", "offset": offset}},
        long=True,
    )
    assert context.windows[0]["content"] == text[offset:]
    assert context.windows[0]["source_spans"] == [[offset, len(text)]]


def test_pdf_compile_projection_returns_local_metadata_without_pixels(kb_dir, monkeypatch):
    from PIL import Image

    from ultrakb.agent.source_windows import CompileSourceWindows

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
    monkeypatch.setattr("ultrakb.agent.source_windows.MAX_EVIDENCE_CHARS", 900)
    context = CompileSourceWindows(wiki, "current").prepare(
        {"name": "setup", "reading_locations": {"pages": "1-2"}},
        long=True,
    )
    assert [image["path"] for image in context.windows[0]["images"]] == ["sources/images/first.png"]
    pixels = [part for part in context.message["content"] if part["type"] == "image_url"]
    assert pixels == []


@pytest.mark.parametrize(
    "subject,title",
    [("River Valve", "RIVER VALVE"), ("灌溉阀组", "灌溉阀组"), ("Straße", "STRASSE")],
)
def test_long_update_finds_complementary_originals_without_navigation_match(kb_dir, subject, title):
    from ultrakb.agent.source_windows import CompileSourceWindows

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
    # With only one native section, its conditions may continue beyond a search
    # hit. The full section (including its introduction) is the evidence window.
    assert "Unrelated general introduction" in str(context.message)
    assert context.windows[0]["native_location"]["pages"] == "1-3"


@pytest.mark.parametrize("repair", ["retain", "withdraw", "related", "format_then_update"])
def test_no_current_long_window_keeps_existing_multisource_page(kb_dir, processing_only, repair):
    from test_compile_windows import _publication

    from ultrakb.agent.compile_delivery import publish_compile_results
    from ultrakb.agent.page_updates import PageUpdate
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.application.views import published_scope
    from ultrakb.compilation_report import collect_compile_report

    summaries = []
    for name, action in [("old-a", "create"), ("old-b", "update")]:
        source = kb_dir / f"{name}.txt"
        source.write_text(f"{name}: a supported condition.\n")
        retained = processing_only(source)
        with _publication(kb_dir, retained) as (wiki, doc):
            summaries.append(f"summaries/{doc}.md")
            plan = _plan(wiki, {action: [{"name": "orchard"}]})
            if action == "create":
                body = "# Orchard\n\nold-a: a supported condition."
            else:
                page = wiki / "concepts/orchard.md"
                patch = PageUpdate(page.read_text())
                body = patch.apply(
                    json.dumps(
                        {
                            "base_sha256": patch.digest,
                            "edits": [
                                {
                                    "block_id": patch.request()["blocks"][-1]["id"],
                                    "operation": "insert_after",
                                    "content": "\n\nold-b: a supported condition.",
                                }
                            ],
                        }
                    ),
                    current=page.read_text(),
                )["content"]
            publish_compile_results(
                wiki,
                doc,
                plan,
                summary=f"{name}: a supported condition.",
                rewrite_summary=True,
                concept_results=[("orchard", body, action == "update", "Orchard conditions")],
            )
    wiki = kb_dir / "wiki"
    old = (wiki / "concepts/orchard.md").read_bytes()
    assert all(summary in old.decode() for summary in summaries)
    head = published_scope(kb_dir).publication_id
    atomic_write_text(
        wiki / "sources/current.json",
        json.dumps(
            [{"page": 1, "unit_kind": "page", "content": "Unrelated native document content."}]
        ),
    )
    windows = CompileSourceWindows(wiki, "current", kb_dir=kb_dir)
    with collect_compile_report() as report:
        if repair == "format_then_update":
            with pytest.raises(ValueError):
                _plan(wiki, {"concepts": 42})
        proposal = _plan(wiki, {"update": [{"name": "orchard"}]})
        diagnostic = windows.validate_updates(proposal, long=True)
        assert diagnostic is not None
        assert "Relevant current originals have not been located" in diagnostic
        if repair in {"withdraw", "related"}:
            final = _plan(wiki, {} if repair == "withdraw" else {"related": ["orchard"]})
            assert windows.validate_updates(final, long=True) is None
            assert "compile_plan_update_withdrawn" in report.quality
            publish_compile_results(
                wiki, "current", final, summary="New native navigation.", rewrite_summary=True
            )
        else:
            assert windows.validate_updates(proposal, long=True) is not None
            assert published_scope(kb_dir).publication_id == head
    assert (wiki / "concepts/orchard.md").read_bytes() == old
    if repair == "related":
        assert "[[concepts/orchard]]" in (wiki / "summaries/current.md").read_text()


def test_partial_prior_window_has_scoped_update_policy_and_continuation(kb_dir, monkeypatch):
    from ultrakb.agent.source_windows import CompileSourceWindows

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
    monkeypatch.setattr("ultrakb.agent.source_windows.MAX_EVIDENCE_CHARS", 1800)
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
