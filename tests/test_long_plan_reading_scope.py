"""Native plan validation and genuine page generation preserve parent reading scopes."""

import json

import pytest

from ultrakb.agent.compile_plan import normalize_plan
from ultrakb.agent.compiler import _sanitize_concept_name
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text


@pytest.fixture
def reading_scope(kb_dir):
    wiki = kb_dir / "wiki"
    pages = [
        "Unrelated preface.",
        "Irrigation procedure: begin the irrigation setup.",
        "Before continuing, isolate the supply and obtain a supervisor's approval.",
        "Irrigation pressure adjustment.",
        "Irrigation valve calibration.",
        "Irrigation schedule configuration.",
        "Irrigation flow verification.",
        "Irrigation setup completion.",
        "Unrelated storage maintenance.",
    ]
    atomic_write_text(
        wiki / "sources/guide.json",
        json.dumps(
            [{"page": i, "unit_kind": "page", "content": text} for i, text in enumerate(pages, 1)]
        ),
    )
    navigation = [
        {"node_id": "preface", "title": "Preface", "start_index": 1, "end_index": 1},
        {
            "node_id": "procedure",
            "title": "Irrigation procedure",
            "start_index": 2,
            "end_index": 2,
            "nodes": [
                {"node_id": f"step-{i}", "title": text, "start_index": i, "end_index": i}
                for i, text in enumerate(pages, 1)
                if 3 <= i <= 8
            ],
        },
        {"node_id": "maintenance", "title": "Maintenance", "start_index": 9, "end_index": 9},
    ]
    return CompileSourceWindows(wiki, "guide", navigation=navigation, kb_dir=kb_dir)


def plan_for(kind, action, item):
    return normalize_plan(
        {kind: {action: [item]}},
        existing={"concepts": set(), "entities": set()}
        if action == "create"
        else {"concepts": {"irrigation"}, "entities": {"irrigation"}},
        entity_types=frozenset({"other"}),
        sanitize=_sanitize_concept_name,
    )


@pytest.mark.parametrize("kind", ["concepts", "entities"])
@pytest.mark.parametrize("action", ["create", "update"])
def test_repaired_parent_scope_reaches_actual_page_generation(kb_dir, reading_scope, kind, action):
    item = {"name": "irrigation", "title": "Irrigation procedure"}
    if kind == "entities":
        item["type"] = "other"
    if action == "update":
        atomic_write_text(
            kb_dir / f"wiki/{kind}/irrigation.md",
            "---\ndescription: Irrigation notes\ntype: other\n"
            "sources: []\n---\n# Irrigation\n\nInitial notes.\n",
        )
    diagnostic = reading_scope.validate_updates(plan_for(kind, action, item), long=True)
    assert "reading_locations" in diagnostic
    repaired = {**item, "reading_locations": {"node_ids": ["procedure"]}}
    assert reading_scope.validate_updates(plan_for(kind, action, repaired), long=True) is None
    originals = reading_scope.prepare(
        repaired, f"{kind}/irrigation.md" if action == "update" else None, long=True
    )
    assert originals.has_current_original
    if action == "update":
        originals.require_update(f"{kind}/irrigation.md")
    sent = json.dumps(originals.message)
    assert "isolate the supply and obtain a supervisor's approval" in sent
    assert "Irrigation setup completion" in sent
    assert "Unrelated storage maintenance" not in sent


@pytest.mark.parametrize("locations", [None, {}])
def test_missing_scope_exhausts_only_the_existing_plan_repair(kb_dir, reading_scope, locations):
    item = {"name": "irrigation", "title": "Irrigation procedure"}
    if locations is not None:
        item["reading_locations"] = locations
    plan = plan_for("concepts", "create", item)
    first = reading_scope.validate_updates(plan, long=True)
    second = reading_scope.validate_updates(plan, long=True)
    assert first == second and "Select relevant native reading_locations" in second
    assert not (kb_dir / "wiki/concepts/irrigation.md").exists()


@pytest.mark.asyncio
async def test_real_long_document_planning_exposes_parent_scope(
    kb_dir, monkeypatch, reading_scope, live_llm
):
    from ultrakb.agent.compile_messages import build_page_messages, planning_messages
    from ultrakb.agent.compiler import _CONCEPT_PAGE_USER, _llm_call_page_async, _parse_page_json
    from ultrakb.source_navigation import planning_navigation

    live_llm.install(monkeypatch)
    topic_hint = "Generated locator: isolation, calibration, scheduling and flow verification."
    next(n for n in reading_scope.navigation if n["node_id"] == "procedure")["summary"] = topic_hint
    navigation = planning_navigation(
        [
            n
            for n in reading_scope.navigation
            if n["node_id"] in {"preface", "procedure", "maintenance"}
        ]
    )
    prerequisite = next(n for n in navigation if n["node_id"] == "step-3")
    parent_id = prerequisite["parent_node_id"]
    parent = next(n for n in navigation if n["node_id"] == parent_id)
    assert parent["start_index"] == parent["end_index"] == 2
    assert parent["navigation_summary"] == topic_hint
    assert len({n["node_id"] for n in navigation}) == len(navigation)
    planning = planning_messages(
        {"role": "system", "content": "Use only the supplied original source."},
        {"role": "user", "content": "Document navigation:\n" + json.dumps(navigation)},
        "Irrigation procedure",
        concept_briefs="",
        entity_briefs="",
        long=True,
    )
    assert "reading_locations" in planning[-1]["content"]
    item = {"name": "irrigation", "reading_locations": {"node_ids": [parent_id]}}
    originals = reading_scope.prepare(item, long=True)
    messages = build_page_messages(
        system_message={"role": "system", "content": "Use only the supplied original source."},
        current_original=None,
        original_context=originals.message,
        known_targets_message={"role": "user", "content": "Known targets: summaries/guide"},
        task_message={
            "role": "user",
            "content": _CONCEPT_PAGE_USER.format(
                title="Irrigation procedure", doc_name="guide", update_instruction=""
            ),
        },
    )
    before = len(live_llm.calls)
    raw = await _llm_call_page_async(
        "deepseek/deepseek-flash",
        messages,
        "irrigation page",
        bundle=live_llm.credentials,
        response_format={"type": "json_object"},
        max_tokens=768,
        original_excerpts=reading_scope.original_excerpts(originals, long=True),
    )
    page = _parse_page_json(raw)
    body = page["content"].lower()
    assert "isolate" in body and "supervisor" in body and "completion" in body
    assert page["description"]
    assert len(live_llm.calls) > before
    for call in live_llm.calls[before:]:
        sent = json.dumps(call["messages"])
        assert "isolate the supply and obtain a supervisor's approval" in sent
        assert "Irrigation setup completion" in sent
        assert "Unrelated storage maintenance" not in sent
        assert topic_hint not in sent
