"""Original preparation, delivered-page validation and publication regressions.

Parser and storage cases use domain inputs directly. Real prompt/response behavior
is covered by the short and long compiler integrations in test_compiler.py.
"""

import json
from contextlib import contextmanager

import pytest

from ultrakb import frontmatter
from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.compile_messages import build_page_messages
from ultrakb.agent.compile_plan import normalize_plan
from ultrakb.agent.compiler import _ENTITY_TYPE_LIST, _sanitize_concept_name
from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.page_response import PageResponseError, page_repair_message
from ultrakb.agent.page_updates import PageUpdate, UnchangedPage
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_json, atomic_write_text

pytest_plugins = ("test_pdf_readback", "processing_fixtures")


def _plan(wiki, value):
    return normalize_plan(
        value,
        existing={
            kind: {p.stem for p in (wiki / kind).glob("*.md")} for kind in ("concepts", "entities")
        },
        entity_types=frozenset(_ENTITY_TYPE_LIST),
        sanitize=_sanitize_concept_name,
    )


def _checkpoints(kb_dir):
    import uuid

    from ultrakb.compile_checkpoints import CompileCheckpoints
    from ultrakb.lifecycle import current_generation
    from ultrakb.model_task import ModelTask

    task = ModelTask(
        task_id=uuid.uuid4().hex,
        kb_generation=current_generation(kb_dir),
        fingerprint="f" * 64,
        bindings={"models": {"compile": "deepseek/deepseek-flash"}},
        policy={"max_calls": 2},
    )
    return CompileCheckpoints(kb_dir, task, {"document": "manual"}, {"protocol": "page-v1"})


@contextmanager
def _publication(kb_dir, retained, *, recompile=False):
    """Publish genuine retained inputs, without a generator or model replacement."""
    from ultrakb.ingest_records import UnitRevision
    from ultrakb.normalization import normalization_id, read_normalization, restore_normalization
    from ultrakb.source_catalog import read_admission, read_record, read_source
    from ultrakb.source_map import freeze_text_map
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        list_source_units,
        prepare_compile_view,
        publish_unit_revision,
    )

    admission = read_admission(kb_dir, read_source(kb_dir, retained.source_id))
    [unit] = list_source_units(kb_dir, retained.source_id)
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    from ultrakb.locks import kb_ingest_lock

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        state, admitted = begin_unit_attempt(
            kb_dir,
            unit,
            revision,
            retry_confirmed=True,
            recompile=recompile,
            discovery_intent=admission.discovery_intent,
        )
        assert admitted
        directory, normalized = read_normalization(
            kb_dir, normalization_id(revision.source_revision_id, revision.processing_fingerprint)
        )
        with prepare_compile_view(kb_dir) as view:
            converted = restore_normalization(directory, normalized, view.scope.wiki_dir.parent)
            yield view.scope.wiki_dir, unit.doc_name
            state = publish_unit_revision(
                kb_dir,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=converted.source_path.relative_to(view.scope.wiki_dir).as_posix(),
                source_map=freeze_text_map(view.scope.wiki_dir, unit.doc_name),
                processing=converted.processing,
                is_long=converted.is_long_doc,
                normalized_format="markdown",
            )
            assert state.status == "completed"


def _page_request(reader, item, *, kind="concepts", action="create", long=False):
    relative = f"{kind}/{item['name']}.md" if action == "update" else None
    originals = reader.prepare(item, relative, long=long)
    return build_page_messages(
        system_message={"role": "system", "content": "Compile original knowledge."},
        current_original=None if long else reader.current_original_message(),
        original_context=originals.message,
        known_targets_message={"role": "user", "content": "Known page targets."},
        task_message={
            "role": "user",
            "content": (
                f"{'Update' if action == 'update' else 'Write'} the "
                f"{'concept' if kind == 'concepts' else 'entity'} page"
            ),
        },
        update_message=PageUpdate((reader.wiki / relative).read_text()).message()
        if relative
        else None,
    )


def test_short_update_rereads_prior_originals(kb_dir):
    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "sources/old.md", "For Model A, 12 slots are recommended.\n")
    atomic_write_text(wiki / "sources/new.md", "For Model B, 24 slots are required.\n")
    atomic_write_text(
        wiki / "summaries/old.md", "---\nfull_text: sources/old.md\n---\nEarlier navigation."
    )
    atomic_write_text(
        wiki / "concepts/slots.md",
        "---\nsources: [summaries/old.md]\n---\n# Slots\n\nFor Model A, 12 slots are recommended.",
    )
    request = _page_request(CompileSourceWindows(wiki, "new"), {"name": "slots"}, action="update")
    assert "For Model A, 12 slots are recommended." in str(request)
    assert "For Model B, 24 slots are required." in str(request)
    assert "coverage" in str(request)


@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_unchanged_update_resolves_without_new_source_contribution(kb_dir, processing_only, kind):
    from ultrakb.application.removal import run_remove_for_api
    from ultrakb.knowledge_evidence import page_evidence
    from ultrakb.knowledge_scope import live_scope

    old, new = kb_dir / "old.txt", kb_dir / "new.txt"
    old.write_text("River equipment requires supervision during winter.")
    new.write_text("River equipment requires supervision during winter. New setup is recorded.")
    original = processing_only(old)
    with _publication(kb_dir, original) as (wiki, name):
        plan = _plan(wiki, {kind: {"create": [{"name": "river-equipment", "type": "product"}]}})
        body = "# River equipment\n\nRiver equipment requires supervision during winter."
        publish_compile_results(
            wiki,
            name,
            plan,
            summary=body,
            rewrite_summary=True,
            **(
                {"concept_results": [("river-equipment", body, False, "Winter supervision")]}
                if kind == "concepts"
                else {
                    "entity_results": [("river-equipment", body, "Winter supervision", "product")]
                }
            ),
        )
    page = kb_dir / f"wiki/{kind}/river-equipment.md"
    before = page.read_bytes()
    basis = page_evidence(live_scope(kb_dir), f"{kind}/river-equipment.md")
    assert basis["source_revision_ids"] == (original.source_revision_id,)
    duplicate = processing_only(new)
    with _publication(kb_dir, duplicate) as (wiki, name):
        patch = PageUpdate(page.read_text())
        unchanged = patch.apply(
            json.dumps(
                {
                    "status": "unchanged",
                    "base_sha256": patch.digest,
                    "reason": "The winter requirement is already fully expressed.",
                }
            ),
            current=page.read_text(),
        )
        assert isinstance(unchanged, UnchangedPage)
        plan = _plan(
            wiki,
            {
                kind: {
                    "update": [
                        {
                            "name": "river-equipment",
                            "brief": "Check the winter supervision requirement.",
                        }
                    ],
                    "create": [{"name": "new-setup", "type": "other"}],
                }
            },
        )
        request = _page_request(
            CompileSourceWindows(wiki, name, kb_dir=kb_dir),
            plan.__dict__[kind].update[0],
            kind=kind,
            action="update",
        )
        assert "winter" in str(request)
        results = (
            [
                ("river-equipment", unchanged, True, ""),
                ("new-setup", "# New setup\n\nNew setup is recorded.", False, "New setup"),
            ]
            if kind == "concepts"
            else [
                ("river-equipment", unchanged, "", ""),
                ("new-setup", "# New setup\n\nNew setup is recorded.", "New setup", "other"),
            ]
        )
        publish_compile_results(
            wiki,
            name,
            plan,
            summary="New setup is recorded.",
            rewrite_summary=True,
            **({"concept_results": results} if kind == "concepts" else {"entity_results": results}),
        )
    assert page.read_bytes() == before
    assert (kb_dir / f"wiki/{kind}/new-setup.md").exists()
    retained = page_evidence(live_scope(kb_dir), f"{kind}/river-equipment.md")
    assert retained["source_revision_ids"] == basis["source_revision_ids"]
    assert retained["refresh_reasons"] == basis["refresh_reasons"]
    assert run_remove_for_api(kb_dir, "new")["status"] == "removed"
    assert page.read_bytes() == before
    assert page_evidence(live_scope(kb_dir), f"{kind}/river-equipment.md")[
        "source_revision_ids"
    ] == (original.source_revision_id,)


@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize(
    "kind,action", [(k, a) for k in ("concepts", "entities") for a in ("create", "update")]
)
def test_page_transport_uses_originals_without_planning_history(kb_dir, long, kind, action):
    wiki = kb_dir / "wiki"
    original = "# Thermal controls\n\nOriginal fact: supervision is required below freezing.\n"
    atomic_write_text(
        wiki / "sources/prior.md", "Prior condition: keep the safety interlock active."
    )
    if action == "update":
        atomic_write_text(
            wiki / kind / "thermal-controls.md",
            "---\nsources: [sources/prior.md]\n---\n# Thermal controls\n\n"
            "Retained prior condition.",
        )
    atomic_write_text(
        wiki / f"sources/current.{'json' if long else 'md'}",
        json.dumps([{"page": 1, "unit_kind": "page", "content": original}]) if long else original,
    )
    atomic_write_text(
        wiki / "summaries/current.md",
        f"---\nfull_text: sources/current.{'json' if long else 'md'}\n---\nPLANNING_ONLY_INFERENCE",
    )
    request = _page_request(
        CompileSourceWindows(wiki, "current"),
        {"name": "thermal-controls", "reading_locations": {"pages": "1"} if long else {}},
        kind=kind,
        action=action,
        long=long,
    )
    assert "PLANNING_ONLY_INFERENCE" not in str(request)
    assert "Original fact: supervision is required below freezing." in str(request)
    assert all(m["role"] != "assistant" for m in request)
    if action == "update":
        assert "Prior condition: keep the safety interlock active." in str(request)


def test_content_only_entity_update_preserves_confirmed_type(kb_dir):
    wiki = kb_dir / "wiki"
    original = (
        "---\ntype: Organization\nsources: [summaries/old.md]\n---\n"
        "# River Research\n\nAn organization."
    )
    atomic_write_text(wiki / "entities/river-research.md", original)
    patch = PageUpdate(original, allowed_types=frozenset(_ENTITY_TYPE_LIST))
    delivered = patch.apply(
        json.dumps(
            {
                "base_sha256": patch.digest,
                "edits": [
                    {
                        "block_id": patch.request()["blocks"][-1]["id"],
                        "operation": "insert_after",
                        "content": "\n\nRiver Research has a new recorded location.",
                    }
                ],
            }
        ),
        current=original,
    )
    assert "type" not in delivered
    plan = _plan(wiki, {"entities": {"update": [{"name": "river-research"}]}})
    publish_compile_results(
        wiki,
        "new",
        plan,
        summary="Summary.",
        entity_results=[
            (
                "river-research",
                delivered["content"],
                delivered["description"],
                frontmatter.parse(original)["type"].lower(),
            )
        ],
    )
    assert (
        frontmatter.parse((wiki / "entities/river-research.md").read_text())["type"]
        == "Organization"
    )


def test_navigation_only_import_keeps_fact_provenance_and_survives_removal(kb_dir, processing_only):
    from ultrakb.application.removal import run_remove_for_api
    from ultrakb.knowledge_evidence import page_evidence
    from ultrakb.knowledge_scope import live_scope

    old, new = kb_dir / "old.txt", kb_dir / "new.txt"
    old.write_text("Recorded orchard operating conditions.")
    new.write_text("A document that points readers to the orchard topic.")
    first = processing_only(old)
    with _publication(kb_dir, first) as (wiki, name):
        publish_compile_results(
            wiki,
            name,
            _plan(wiki, {"create": [{"name": "orchard"}]}),
            summary="Orchard conditions.",
            rewrite_summary=True,
            concept_results=[
                (
                    "orchard",
                    "# Orchard\n\nRecorded orchard operating conditions.",
                    False,
                    "Conditions",
                )
            ],
        )
    page = kb_dir / "wiki/concepts/orchard.md"
    before = page.read_bytes()
    basis = page_evidence(live_scope(kb_dir), "concepts/orchard.md")
    assert basis["source_revision_ids"] == (first.source_revision_id,)
    current = processing_only(new)
    with _publication(kb_dir, current) as (wiki, name):
        publish_compile_results(
            wiki,
            name,
            _plan(wiki, {"related": ["orchard"]}),
            summary="Pointers to the orchard topic.",
            rewrite_summary=True,
        )
    assert page.read_bytes() == before
    assert "[[concepts/orchard]]" in (kb_dir / "wiki/summaries/new.md").read_text()
    assert frontmatter.parse(page.read_text())["sources"] == ["summaries/old.md"]
    assert (
        page_evidence(live_scope(kb_dir), "concepts/orchard.md")["source_revision_ids"]
        == basis["source_revision_ids"]
    )
    assert run_remove_for_api(kb_dir, "new")["status"] == "removed"
    assert page.read_bytes() == before
    assert (
        page_evidence(live_scope(kb_dir), "concepts/orchard.md")["source_revision_ids"]
        == basis["source_revision_ids"]
    )


def test_short_import_preserves_summary_and_assembles_navigation(kb_dir):
    wiki = kb_dir / "wiki"
    first = "# Capacity\n\nModel K supports 36 slots under the recorded condition."
    atomic_write_text(wiki / "sources/capacity.md", first)
    publish_compile_results(
        wiki,
        "capacity",
        _plan(wiki, {"create": [{"name": "capacity"}]}),
        summary=first,
        rewrite_summary=True,
        concept_results=[("capacity", "# Capacity\n\nRecorded condition.", False, "Capacity")],
    )
    body = frontmatter.split((wiki / "summaries/capacity.md").read_text())[1]
    assert body.lstrip().startswith(first)
    assert "- [[concepts/capacity]]" in body


@pytest.mark.parametrize("repaired", [False, True])
def test_short_summary_format_repair_is_bounded_before_publication(kb_dir, repaired):
    content = "# Capacity\n\nModel K has 36 slots under supervision."
    malformed = (
        json.dumps({"description": "Capacity", "content": content}) + '\n{"type":"json_object"}'
    )
    with pytest.raises(PageResponseError, match="summary_invalid_json") as failure:
        validate_page_delivery(malformed, response_protocol="summary-page-json")
    repair = page_repair_message(
        failure.value.code, str(failure.value), response_protocol="summary-page-json"
    )
    assert "one JSON object" in repair["content"]
    assert "Do not append a second object" in repair["content"]
    checkpoints = _checkpoints(kb_dir)
    checkpoints.claim_repair("summary", "request")
    with pytest.raises(PageResponseError):
        checkpoints.claim_repair("summary", "request")
    summary = kb_dir / "wiki/summaries/capacity.md"
    if repaired:
        raw = validate_page_delivery(
            json.dumps({"description": "Capacity", "content": content}),
            response_protocol="summary-page-json",
        )
        publish_compile_results(
            kb_dir / "wiki",
            "capacity",
            _plan(kb_dir / "wiki", {}),
            summary=json.loads(raw)["content"],
            rewrite_summary=True,
        )
        assert frontmatter.split(summary.read_text())[1].strip() == content
    else:
        with pytest.raises(PageResponseError, match="summary_invalid_json"):
            validate_page_delivery(malformed, response_protocol="summary-page-json")
        assert not summary.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_summary_repair_releases_sdk_client_on_failure(kb_dir, monkeypatch, cancelled):
    import asyncio
    from types import SimpleNamespace

    import httpx
    import litellm

    from ultrakb.agent.compiler import compile_short_doc

    client = httpx.AsyncClient()
    monkeypatch.setitem(
        litellm.in_memory_llm_clients_cache.cache_dict,
        "summary-repair-test-client",
        SimpleNamespace(client=client),
    )

    def interrupted(*args, **kwargs):
        raise asyncio.CancelledError if cancelled else OSError("Local delivery connection closed")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call", interrupted)
    source = kb_dir / "wiki/sources/summary-failure.md"
    atomic_write_text(source, "# Source\n\nSupported facts.")
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else OSError):
            await compile_short_doc("summary-failure", source, kb_dir, "deepseek/deepseek-flash")
        assert client.is_closed
        assert not (kb_dir / "wiki/summaries/summary-failure.md").exists()
    finally:
        await client.aclose()


def test_entity_type_requires_generation_confirmation(kb_dir):
    from ultrakb.agent.page_delivery import confirmed_entity_type

    plan = _plan(
        kb_dir / "wiki", {"entities": {"create": [{"name": "river-valve", "type": "organization"}]}}
    )
    assert plan.entities.create[0]["type"] == "organization"
    delivered = {
        "description": "Flow control",
        "content": "# River Valve\n\nRiver Valve is a flow-control component.",
    }
    actual_type = confirmed_entity_type(delivered, frozenset(_ENTITY_TYPE_LIST))
    assert actual_type == "other"
    publish_compile_results(
        kb_dir / "wiki",
        "component",
        plan,
        summary="Component.",
        entity_results=[
            ("river-valve", delivered["content"], delivered["description"], actual_type)
        ],
    )
    assert (
        frontmatter.parse((kb_dir / "wiki/entities/river-valve.md").read_text())["type"] == "Other"
    )


def test_import_update_keeps_prior_conditions_and_adds_all_new_sections(kb_dir):
    old = (
        "# Conditions\n\nModel A: 12 slots during winter.\n\n"
        "| Object | Limit |\n| --- | --- |\n| Model C | 72 hours |\n"
    )
    wiki = kb_dir / "wiki"
    original = '---\nsources: ["summaries/winter.md"]\n---\n\n' + old
    atomic_write_text(wiki / "concepts/limits.md", original)
    patch = PageUpdate(original)
    result = patch.apply(
        json.dumps(
            {
                "base_sha256": patch.digest,
                "description": "Conditions",
                "edits": [
                    {
                        "block_id": patch.request()["blocks"][-1]["id"],
                        "operation": "insert_after",
                        "content": "\n\nModel B: 36 slots.\n\nMaintenance: 48 hours.\n",
                    }
                ],
            }
        ),
        current=original,
    )
    publish_compile_results(
        wiki,
        "irrigation",
        _plan(wiki, {"update": [{"name": "limits"}]}),
        summary="New conditions.",
        concept_results=[("limits", result["content"], True, result["description"])],
    )
    body = frontmatter.split((wiki / "concepts/limits.md").read_text())[1]
    assert old in body
    assert "Model B: 36 slots." in body
    assert "Maintenance: 48 hours." in body


def test_import_blocks_proven_numeric_conflict_after_one_page_repair(kb_dir):
    table = (
        "# Limits\n\n| Object | Attribute | Condition | Value |\n"
        "| --- | --- | --- | --- |\n| River Valve | Latency | Winter | {} |\n"
    )
    failure = None
    checkpoints = _checkpoints(kb_dir)
    for attempt in range(2):
        with pytest.raises(PageResponseError, match="page_numeric_conflict") as raised:
            validate_page_delivery(
                json.dumps({"description": "Latency", "content": table.format("24 ms")}),
                numeric_sources=[table.format("12 ms")],
            )
        failure = raised.value
        if attempt == 0:
            checkpoints.claim_repair("page", "request")
            assert (
                "page_numeric_conflict"
                in page_repair_message(failure.code, str(failure))["content"]
            )
    with pytest.raises(PageResponseError):
        checkpoints.claim_repair("page", "request")
    wiki = kb_dir / "wiki"
    publish_compile_results(
        wiki,
        "latency",
        _plan(wiki, {"create": [{"name": "latency"}]}),
        summary="Latency.",
        concept_results=[failure],
    )
    assert not (wiki / "concepts/latency.md").exists()


@pytest.mark.parametrize("changed", [False, True])
def test_long_block_compile_receives_only_selected_frozen_image(kb_dir, tmp_path, changed):
    from PIL import Image

    from ultrakb.block_package import freeze_block_package
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_images import request_image_digests
    from ultrakb.text_source import freeze_markdown

    for name, color in [("first.png", "red"), ("other.png", "blue")]:
        Image.new("RGB", (4, 4), color).save(tmp_path / name)
    source = tmp_path / "manual.md"
    source.write_text(
        "# Selected\n\n![Selected figure](first.png)\n\n"
        "# Unrelated\n\n![Unrelated figure](other.png)\n"
    )
    wiki = kb_dir / "wiki"
    with prepared_input(source) as ready:
        frozen = freeze_markdown(ready, "manual", wiki)
    path = wiki / "sources/manual.md"
    atomic_write_text(path, frozen.text)
    atomic_write_json(path.with_suffix(".content.json"), frozen.model_dump(mode="json"))
    freeze_block_package(path, "manual")
    item = {
        "name": "selected",
        "reading_locations": {"chars": f"0:{frozen.text.index('# Unrelated')}"},
    }
    if changed:
        Image.new("RGB", (4, 4), "green").save(wiki / "sources/images/manual/first.png")
        with pytest.raises(PageResponseError, match="changed"):
            CompileSourceWindows(wiki, "manual").prepare(item, long=True)
    else:
        request = _page_request(CompileSourceWindows(wiki, "manual"), item, long=True)
        assert request_image_digests([request]) == set()
        assert "first.png" in str(request)
        context = CompileSourceWindows(wiki, "manual").prepare(item, long=True)
        assert "other.png" not in "".join(w["content"] for w in context.windows)
        assert "data:image/" not in str(request)


@pytest.mark.parametrize("suggestions", [None, {"pages": "3"}, {"pages": [3]}, {"pages": "9"}])
def test_long_import_reads_native_windows_from_confirmed_locations(kb_dir, suggestions):
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/manual.json",
        json.dumps(
            [
                {"page": page, "unit_kind": "page", "content": text}
                for page, text in [
                    (1, "Physical page one."),
                    (2, "Physical page two."),
                    (3, "Physical page three."),
                ]
            ]
        ),
    )
    reader = CompileSourceWindows(
        wiki,
        "manual",
        navigation=[
            {"node_id": str(n), "title": f"Section {n}", "start_index": n, "end_index": n}
            for n in range(1, 4)
        ],
    )
    item = {"name": "last-section"}
    if suggestions:
        item["reading_locations"] = suggestions
    plan = _plan(wiki, {"create": [item]})
    if suggestions is None:
        assert "reading_locations" in reader.validate_updates(plan, long=True)
        item["reading_locations"] = {"pages": "3"}
    if suggestions == {"pages": "9"}:
        with pytest.raises((PageResponseError, ValueError)):
            reader.prepare(plan.concepts.create[0], long=True)
        assert not (wiki / "concepts/last-section.md").exists()
        return
    request = _page_request(reader, item, long=True)
    assert "Physical page three." in str(request)
    assert "Physical page one." not in str(request)
    assert '"unit_kind": "page"' in str(request)
    assert "coverage" in str(request)
    assert "sha256" in str(request)


def test_workbook_page_request_preserves_sparse_native_rows(kb_dir, processing_only):
    from openpyxl import Workbook

    from ultrakb.ingest_records import UnitRevision
    from ultrakb.normalization import normalization_id, read_normalization
    from ultrakb.source_catalog import read_record
    from ultrakb.unit_publication import list_source_units

    book = Workbook()
    sheet = book.active
    sheet.title = "设备清单"
    sheet["A2"], sheet["C2"] = "泵甲", 17
    sheet["A9"], sheet["C9"] = "泵乙", 23
    sheet["F9"] = "=C9*2"
    sheet.row_dimensions[9].hidden = True
    sheet.merge_cells("A12:C12")
    sheet["A12"] = "仅用于冬季"
    source = kb_dir / "设备.xlsx"
    book.save(source)
    retained = processing_only(source)
    [unit] = list_source_units(kb_dir, retained.source_id)
    revision = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    directory, saved = read_normalization(
        kb_dir, normalization_id(revision.source_revision_id, revision.processing_fingerprint)
    )
    reader = CompileSourceWindows(directory / "wiki", unit.doc_name)
    payload = reader.current_original_evidence()
    rows = {r["row"]: r["cells"] for r in payload["worksheet_rows"]}
    assert [c["coordinate"] for c in rows[2]] == ["A2", "C2"]
    assert [c["coordinate"] for c in rows[9]] == ["A9", "C9", "F9"]
    assert "泵甲" in str(rows[2]) and "泵乙" not in str(rows[2])
    assert "泵乙" in str(rows[9]) and "泵甲" not in str(rows[9])
    formula = rows[9][-1]["observations"][0]
    assert formula["formula"] == "=C9*2"
    assert formula["cache_status"] == "missing"
    assert formula["hidden_row"] is True
    assert len(rows) == 3


def test_short_update_rereads_a_published_long_source(kb_dir):
    wiki = kb_dir / "wiki"
    atomic_write_text(
        wiki / "sources/long.json",
        json.dumps(
            [
                {
                    "page": 1,
                    "unit_kind": "page",
                    "content": "Physical page three. Last section facts.",
                }
            ]
        ),
    )
    atomic_write_text(
        wiki / "summaries/long.md", "---\nfull_text: sources/long.json\n---\nGenerated navigation."
    )
    atomic_write_text(
        wiki / "concepts/last-section.md",
        "---\nsources: [summaries/long.md]\n---\n# Last section\n\nPhysical page three.",
    )
    atomic_write_text(
        wiki / "sources/new.md", "Last section: an additional condition is recorded here."
    )
    request = _page_request(
        CompileSourceWindows(wiki, "new"),
        {"name": "last-section", "title": "Last section"},
        action="update",
    )
    assert "Physical page three." in str(request)
    assert "additional condition" in str(request)
