"""Long document compilation through the shared document operation."""

import json

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


def _target_ranges(payload):
    target = payload["target"]
    return target.get("ranges", [[target["target_start"], target["target_end"]]])


def _single_page_plan(payload, *, name, title, kind="concept", type_=None, target=""):
    """Return one valid DocumentPlan delta for the current target window."""

    ranges = _target_ranges(payload)
    registered = next(
        (
            item
            for item in payload["carry"]["page_register"]
            if item["name"] == name and item["kind"] == kind
        ),
        None,
    )
    change = {
        "local_key": "page",
        "target_key": registered["key"] if registered else "",
        "target": registered.get("target", target) if registered else target,
        "kind": kind,
        "name": name,
        "title": title,
        "purpose": title + " from original source evidence",
        "subject_ranges": ranges,
        "necessary_context": [],
    }
    if type_ is not None:
        change["type"] = type_
    return {
        "overview": {"text": title + " overview.", "ranges": ranges, "limitations": []},
        "page_changes": [change],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }


def _page_response(payload, content):
    return {
        "content": content,
        "covered": [item["id"] for item in payload["occurrences"]],
    }


def test_planned_groups_delay_dense_omission_projection_until_page_dispatch():
    """A dense plan must not copy every unresolved payload onto every page."""

    from openkb.agent.document_plan import DocumentPlan, PagePlan, UnresolvedItem
    from openkb.agent.evidence_compiler import _group_known_omissions, _planned_groups

    count = 48
    plan = DocumentPlan(
        pages=[
            PagePlan(
                key=f"p{index}",
                kind="concept",
                name=f"concepts/page-{index}",
                title=f"Page {index}",
                purpose="A compact test page.",
                subject_ranges=[[0, 1]],
            )
            for index in range(count)
        ],
        unresolved=[
            UnresolvedItem(
                key=f"u{index}",
                location=[[0, 1]],
                problem_type="missing_prerequisite",
                missing_target=f"missing-{index}",
                affected_pages=[f"p{page}" for page in range(count)],
                blocking=False,
                reason="An intentionally dense unresolved payload.",
            )
            for index in range(count)
        ],
    )
    calls = []
    for item in plan.unresolved:
        original = item.to_dict

        def traced(*, original=original, key=item.key):
            calls.append(key)
            return original()

        item.to_dict = traced  # type: ignore[method-assign]

    groups = _planned_groups(plan)

    assert len(groups) == count
    assert all("known_omissions" not in group for group in groups)
    assert calls == []
    assert len(_group_known_omissions(groups[0], plan)) == count
    assert calls == [f"u{index}" for index in range(count)]


def test_compilation_thinking_mode_reaches_provider_and_invalidates_document_plan(
    kb_dir, tmp_path, model_service
):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    original = tmp_path / "thinking.md"
    original.write_text("Required version 7.", encoding="utf-8")
    previous_calls = 0
    versions = []
    for mode in ("disabled", "enabled"):
        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(kb=str(kb_dir), config={"compilation_thinking": mode}),
        )
        assert read_kb_config(kb_dir).compilation_thinking == mode
        result = import_document(kb_dir, original)
        assert result.knowledge_compilation == "completed", result
        versions.append((result.input_version, result.parse_id))
        new_calls = model_service[previous_calls:]
        assert {json.loads(call["messages"][-1]["content"])["stage"] for call in new_calls} == {
            "planning",
            "generation",
            "verification",
        }
        assert all(call.get("thinking") == {"type": mode} for call in new_calls)
        previous_calls = len(model_service)
    assert versions[0] == versions[1]


def test_all_sections_generate_from_original_evidence_with_bounded_requests(
    kb_dir, tmp_path, model_service
):
    facts = [
        "Required version: 7.",
        "Only start when pressure is below 37 kPa.",
        "Command: start --timeout 42. Never retry authentication failures.",
    ]
    original = tmp_path / "manual.md"
    original.write_text(
        "\n\n".join(
            f"# Section {i}\n\n" + ("Background explanation. " * 800) + "\n\n" + fact
            for i, fact in enumerate(facts, 1)
        )
    )
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(
        context_tokens=32768,
        output_tokens=1024,
        max_requests=80,
        max_tokens=200000,
        stage_timeout=30,
        document_timeout=60,
    )
    config_path.write_text(yaml.safe_dump(config))
    observed = {"planning": set(), "generation": set()}

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            excerpts = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
            observed["planning"].update(fact for fact in facts if fact in excerpts)
            ranges = _target_ranges(payload)
            registered = payload["carry"]["page_register"]
            return {
                "overview": {
                    "text": "Operation guidance.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "operation",
                        "target_key": registered[0]["key"] if registered else "",
                        "name": "concepts/operation",
                        "title": "Operation",
                        "kind": "concept",
                        "purpose": "Operation guidance",
                        "subject_ranges": ranges,
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        assert payload["stage"] == "generation"
        excerpts = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
        selected = [fact for fact in facts if fact in excerpts]
        observed["generation"].update(selected)
        return {
            "content": "# Operation\n" + "\n".join(selected),
            "covered": [item["id"] for item in payload["occurrences"]],
        }

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert observed == {"planning": set(facts), "generation": set(facts)}
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert len(pages) == 1
    content = pages[0].read_text()
    assert all(fact in content for fact in facts)
    assert "source-evidence" in content and result.parse_id in content
    for request in model_service:
        measured = litellm.token_counter(model=request["model"], messages=request["messages"])
        assert measured + request["max_tokens"] <= 32768


def test_changed_generation_language_reuses_document_plan_but_does_not_skip_completed_version(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "language.md"
    original.write_text("Required version 7.")
    first = import_document(kb_dir, original)
    assert first.knowledge_compilation == "completed"
    calls_before = len(model_service)
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["language"] = "zh"
    path.write_text(yaml.safe_dump(config))
    changed = import_document(kb_dir, original)
    assert changed.status == "added", changed
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"]
        for call in model_service[calls_before:]
    ]
    assert "generation" in stages
    assert "facts" not in stages


def test_generation_reads_conditions_omitted_from_the_fact_statement(
    kb_dir, tmp_path, model_service
):
    text = "Pressure: 37 kPa. Only on version 7; never retry authentication failures."
    original = tmp_path / "conditions.md"
    original.write_text(text)

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            return _single_page_plan(payload, name="concepts/pressure", title="Pressure")
        assert payload["stage"] == "generation"
        return _page_response(
            payload, "\n".join(item["text"] for item in payload["evidence"]["blocks"])
        )

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert text in (kb_dir / "wiki/concepts/pressure.md").read_text()


def test_continuation_keeps_document_plan_and_reads_current_wiki_before_generation(
    kb_dir, tmp_path, model_service
):
    from openkb.application.execution import ExecutionContext
    from openkb.application.source_actions import continue_source
    from openkb.application.source_history import source_status
    from openkb.cancellation import OperationCancelled

    original = tmp_path / "manual.md"
    original.write_text("Required version: 7.")
    current_page = kb_dir / "wiki/concepts/operation.md"
    current_page.write_text("# Operation\nInitial manual guidance.\n")
    planning_requests = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            planning_requests.append(payload)
            return _single_page_plan(
                payload,
                name="concepts/operation",
                title="Operation",
                target="concepts/operation",
            )
        assert payload["stage"] == "generation"
        return _page_response(payload, "# Operation\nRequired version: 7.")

    model_service.respond = respond

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    first = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert first.status == "stopped" and len(planning_requests) == 1
    original.unlink()
    current_page.write_text("# Operation\nEmergency override requires approval.\n")
    continued = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert continued.reason == "needs_acceptance", continued
    # The user edit changes the catalogue snapshot, so the retained plan is
    # intentionally invalidated before proposing an update against current wiki.
    assert len(planning_requests) == 2
    from openkb.application.source_actions import review_source_proposal

    review = review_source_proposal(kb_dir, continued.resume)
    assert "Emergency override requires approval." in review["diffs"]["concepts/operation.md"]
    finished = continue_source(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        proposal_id=continued.resume,
        accept_pages=review["protected"],
    )
    assert finished.knowledge_compilation == "completed"
    assert "Emergency override requires approval." in current_page.read_text()
    usage = source_status(kb_dir, first.source_id)["cumulative_usage"]
    assert usage["observable_attempts"] >= 4


def test_one_large_page_is_omitted_when_its_assembled_candidate_cannot_be_reviewed(
    kb_dir, tmp_path, model_service
):
    facts = [
        f"Setting {number}: value {number + 37}; " + "required before startup " * 12
        for number in range(20)
    ]
    original = tmp_path / "settings.md"
    original.write_text("\n\n".join(facts))
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(context_tokens=6144, max_requests=80, max_tokens=200000)
    path.write_text(yaml.safe_dump(config))
    parts = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            return _single_page_plan(payload, name="concepts/settings", title="Settings")
        assert payload["stage"] == "generation"
        parts.append(payload)
        assert not (kb_dir / "wiki/concepts/settings.md").exists()
        return _page_response(
            payload, "\n\n".join(item["text"] for item in payload["evidence"]["blocks"])
        )

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert len(parts) > 1
    # Generation may split evidence safely, but #52 requires one critical
    # review of the assembled final candidate. A capacity refusal must retain
    # this page as an omission rather than publish fragment-level proof.
    assert not (kb_dir / "wiki/concepts/settings.md").exists()
    assert any(
        row["stage"] == "generation" and row["items"] == ["concepts/settings"]
        for row in result.omissions
    )


def test_named_entity_and_concept_share_valid_links_and_preserve_entity_vocabulary(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "entities.md"
    original.write_text("AtlasDB supports atomic commits.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            assert "product" in payload["entity_types"]
            ranges = _target_ranges(payload)
            return {
                "overview": {"text": "AtlasDB operations.", "ranges": ranges, "limitations": []},
                "page_changes": [
                    {
                        "local_key": "atlasdb",
                        "target_key": "",
                        "name": "entities/atlasdb",
                        "title": "AtlasDB",
                        "kind": "entity",
                        "type": "product",
                        "purpose": "AtlasDB identity and capability",
                        "subject_ranges": ranges,
                        "necessary_context": [],
                    },
                    {
                        "local_key": "commits",
                        "target_key": "",
                        "name": "concepts/atomic-commits",
                        "title": "Atomic commits",
                        "kind": "concept",
                        "purpose": "Atomic commit behavior",
                        "subject_ranges": ranges,
                        "necessary_context": [],
                    },
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        assert payload["stage"] == "generation"
        return _page_response(
            payload,
            "# "
            + payload["page"]["title"]
            + "\n[[entities/atlasdb]] supports [[concepts/atomic-commits]]. [[concepts/phantom]]",
        )

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    entity = (kb_dir / "wiki/entities/atlasdb.md").read_text()
    concept = (kb_dir / "wiki/concepts/atomic-commits.md").read_text()
    assert yaml.safe_load(entity.split("---")[1])["type"] == "Product"
    assert "[[concepts/atomic-commits]]" in entity and "[[entities/atlasdb]]" in concept
    assert "[[concepts/phantom]]" not in entity + concept
    summary = next((kb_dir / "wiki/summaries").glob("*.md")).read_text()
    assert "entities/atlasdb" in summary


def test_large_document_plan_covers_all_windows_then_omits_an_unreviewable_page(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "topic-plan.md"
    original.write_text(
        "\n\n".join(f"Setting {i}: value 37, prerequisite version 7." for i in range(120))
    )
    path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["processing"].update(
        context_tokens=6144,
        max_requests=400,
        max_tokens=1500000,
        stage_timeout=40,
        document_timeout=90,
    )
    path.write_text(yaml.safe_dump(config))
    planned = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            planned.extend(item["text"] for item in payload["evidence"]["blocks"])
            return _single_page_plan(payload, name="concepts/settings", title="Settings")
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert all(f"Setting {i}:" in "\n".join(planned) for i in range(120))
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert any(
        row["stage"] == "generation" and row["items"] == ["concepts/settings"]
        for row in result.omissions
    )
    assert (
        sum(
            json.loads(call["messages"][-1]["content"])["stage"] == "planning"
            for call in model_service
        )
        > 1
    )
    for request in model_service:
        measured = litellm.token_counter(model=request["model"], messages=request["messages"])
        assert measured + request["max_tokens"] <= config["processing"]["context_tokens"]


def test_new_source_version_retracts_its_retired_topic_without_deleting_other_sources(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "features.md"
    source.write_text("Retired feature.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            evidence = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
            name = "current" if "Current" in evidence else "retired"
            path = "concepts/" + name
            return _single_page_plan(
                payload,
                name=path,
                title=name.title(),
                target=path if path in payload["existing_targets"] else "",
            )
        return evidence_response(payload)

    model_service.respond = respond
    first = import_document(kb_dir, source)
    other = tmp_path / "other-source.md"
    other.write_text("Retired feature remains available in the legacy edition.")
    second = import_document(kb_dir, other)
    assert second.knowledge_compilation == "completed"
    source.write_text("Current feature.")
    changed = import_document(kb_dir, source)
    assert changed.knowledge_compilation == "completed", changed
    retained = (kb_dir / "wiki/concepts/retired.md").read_text()
    assert first.source_id not in retained
    assert second.source_id in retained
    assert (kb_dir / "wiki/concepts/current.md").exists()


def test_retired_link_is_normalized_before_its_binding_review(kb_dir, tmp_path, model_service):
    source = tmp_path / "retired-link.md"
    source.write_text("Retired feature.")
    reviewed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            evidence = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
            name = "current" if "Current" in evidence else "retired"
            return _single_page_plan(payload, name=f"concepts/{name}", title=name.title())
        if payload["stage"] == "generation":
            if payload["page"]["name"] == "concepts/current":
                return _page_response(
                    payload,
                    "# Current\nSee [[concepts/retired|Retired feature]].",
                )
            return _page_response(payload, "# Retired\nRetired feature.")
        if payload["stage"] == "verification":
            reviewed.append(payload["candidate"]["content"])
            return {"verdict": "supported", "reason": "The candidate matches its evidence."}
        raise AssertionError(payload["stage"])

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    source.write_text("Current feature.")
    changed = import_document(kb_dir, source)

    assert changed.knowledge_compilation == "completed", changed
    current_reviews = [content for content in reviewed if "# Current" in content]
    assert len(current_reviews) == 2
    assert "[[concepts/retired|Retired feature]]" in current_reviews[0]
    assert "[[concepts/retired|Retired feature]]" not in current_reviews[1]
    assert "Retired feature" in current_reviews[1]
    current = (kb_dir / "wiki/concepts/current.md").read_text()
    assert "[[concepts/retired|Retired feature]]" not in current


def test_normalized_page_rejection_keeps_other_verified_pages_publishable(
    kb_dir, tmp_path, model_service
):
    """A second whole-page review failure is a local continuation item."""

    source = tmp_path / "retired-link-rejection.md"
    source.write_text("Retired feature.")
    reviews = []

    def page_change(local_key, name, title, ranges):
        return {
            "local_key": local_key,
            "target_key": "",
            "target": "",
            "kind": "concept",
            "name": name,
            "title": title,
            "purpose": title + " from original source evidence",
            "subject_ranges": ranges,
            "necessary_context": [],
        }

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            evidence = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
            ranges = _target_ranges(payload)
            if "Current feature." not in evidence:
                return _single_page_plan(payload, name="concepts/retired", title="Retired")
            return {
                "overview": {"text": "Two new topics.", "ranges": ranges, "limitations": []},
                "page_changes": [
                    page_change("current", "concepts/current", "Current", [[0, 1]]),
                    page_change("independent", "concepts/independent", "Independent", [[1, 2]]),
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        if payload["stage"] == "generation":
            if payload["page"]["name"] == "concepts/current":
                return _page_response(
                    payload,
                    "# Current\nSee [[concepts/retired|Retired feature]].",
                )
            if payload["page"]["name"] == "concepts/independent":
                return _page_response(payload, "# Independent\nIndependent feature.")
            return _page_response(payload, "# Retired\nRetired feature.")
        if payload["stage"] == "verification":
            content = payload["candidate"]["content"]
            reviews.append(content)
            if "# Current" in content and "[[concepts/retired|Retired feature]]" not in content:
                return {
                    "verdict": "unsupported",
                    "reason": "The normalized candidate needs a fresh source-faithful rewrite.",
                    "issues": ["Retired link removal requires a new review outcome."],
                }
            return {"verdict": "supported", "reason": "The candidate matches its evidence."}
        raise AssertionError(payload["stage"])

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    source.write_text("Current feature.\n\nIndependent feature.")

    changed = import_document(kb_dir, source)

    assert changed.knowledge_compilation == "completed", changed
    assert not (kb_dir / "wiki/concepts/current.md").exists()
    assert (kb_dir / "wiki/concepts/independent.md").exists()
    assert any(
        row["stage"] == "generation"
        and row["reason"] == "knowledge_evidence_mismatch"
        and row["items"] == ["concepts/current"]
        for row in changed.omissions
    ), changed.omissions
    current_reviews = [content for content in reviews if "# Current" in content]
    assert len(current_reviews) == 2
    assert "[[concepts/retired|Retired feature]]" in current_reviews[0]
    assert "[[concepts/retired|Retired feature]]" not in current_reviews[1]


def test_retiring_a_page_does_not_rewrite_an_unrelated_verified_page(
    kb_dir, tmp_path, model_service
):
    """A later source cannot silently mutate another page after its review."""

    from openkb.application.source_actions import continue_source, review_source_proposal

    beta = tmp_path / "beta.md"
    alpha = tmp_path / "alpha.md"
    beta.write_text("Beta feature.")
    alpha.write_text("Alpha feature.")
    reviewed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            evidence = "\n".join(item["text"] for item in payload["evidence"]["blocks"])
            if "Current beta" in evidence:
                name, title = "concepts/current-beta", "Current beta"
            elif "Alpha" in evidence:
                name, title = "concepts/alpha", "Alpha"
            else:
                name, title = "concepts/beta", "Beta"
            return _single_page_plan(payload, name=name, title=title)
        if payload["stage"] == "generation":
            name = payload["page"]["name"]
            if name == "concepts/alpha":
                return _page_response(payload, "# Alpha\nSee [[concepts/beta|Beta]].")
            return _page_response(payload, "# " + payload["page"]["title"] + "\nFeature.")
        if payload["stage"] == "verification":
            reviewed.append(payload["candidate"]["content"])
            return {"verdict": "supported", "reason": "The candidate matches its evidence."}
        raise AssertionError(payload["stage"])

    model_service.respond = respond
    assert import_document(kb_dir, beta).knowledge_compilation == "completed"
    assert import_document(kb_dir, alpha).knowledge_compilation == "completed"
    alpha_page = kb_dir / "wiki/concepts/alpha.md"
    before = alpha_page.read_text(encoding="utf-8")
    assert "[[concepts/beta|Beta]]" in before
    review_count = len(reviewed)

    beta.write_text("Current beta feature.")
    changed = import_document(kb_dir, beta)

    assert changed.reason == "needs_acceptance", changed
    assert alpha_page.read_text(encoding="utf-8") == before
    assert all("# Alpha" not in content for content in reviewed[review_count:])
    review = review_source_proposal(kb_dir, changed.resume)
    assert "concepts/alpha.md" in review["protected"]
    accepted = continue_source(
        kb_dir,
        changed.source_id,
        version_id=changed.input_version,
        proposal_id=changed.resume,
        accept_pages=review["protected"],
    )
    assert accepted.knowledge_compilation == "completed", accepted
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert (kb_dir / "wiki/concepts/current-beta.md").exists()
    assert "[[concepts/beta|Beta]]" not in alpha_page.read_text(encoding="utf-8")


def test_retained_page_is_rechecked_after_a_cascading_link_withdrawal(
    kb_dir, tmp_path, model_service, monkeypatch
):
    """Link cleanup reaches retained pages after normalized candidates are omitted."""

    from openkb.agent import document_orchestrator, document_page_contracts
    from openkb.agent.document_plan import from_dict
    from openkb.application.document_pipeline import compile_version
    from openkb.application.execution import ExecutionContext
    from openkb.config import resolve_effective_config
    from openkb.locks import kb_ingest_lock
    from openkb.sources import SourceStore, read_object

    source = tmp_path / "retained-cascade.md"
    source.write_text("Alpha fact.\n\nBeta fact.\n\nCharlie fact.", encoding="utf-8")
    phase = {"replacement": False}
    reviews = []

    def page_change(payload, local_key, name, title, subject_range, purpose):
        registered = next(
            (
                item
                for item in payload["carry"]["page_register"]
                if item["name"] == name and item["kind"] == "concept"
            ),
            None,
        )
        return {
            "local_key": local_key,
            "target_key": registered["key"] if registered else "",
            "target": registered.get("target", "") if registered else "",
            "kind": "concept",
            "name": name,
            "title": title,
            "purpose": purpose,
            "subject_ranges": [subject_range],
            "necessary_context": [],
        }

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            return {
                "overview": {
                    "text": "Three independently planned facts.",
                    "ranges": _target_ranges(payload),
                    "limitations": [],
                },
                "page_changes": [
                    page_change(
                        payload,
                        "alpha",
                        "concepts/alpha",
                        "Alpha",
                        [0, 1],
                        "Alpha evidence.",
                    ),
                    page_change(
                        payload,
                        "beta",
                        "concepts/beta",
                        "Beta",
                        [1, 2],
                        "Beta evidence.",
                    ),
                    page_change(
                        payload,
                        "charlie",
                        "concepts/charlie",
                        "Charlie",
                        [2, 3],
                        "Charlie evidence.",
                    ),
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        if payload["stage"] == "generation":
            name = payload["page"]["name"]
            if phase["replacement"] and name == "concepts/alpha":
                return _page_response(payload, "# Alpha\nSee [[concepts/charlie|Charlie]].")
            if name == "concepts/beta":
                return _page_response(payload, "# Beta\nSee [[concepts/alpha|Alpha]].")
            return _page_response(payload, "# " + payload["page"]["title"] + "\nFact.")
        if payload["stage"] == "verification":
            content = payload["candidate"]["content"]
            reviews.append((phase["replacement"], content))
            if phase["replacement"] and "# Charlie" in content:
                return {
                    "verdict": "unsupported",
                    "reason": "Charlie is intentionally unavailable in this replacement.",
                    "issues": ["The planned Charlie page must remain pending."],
                }
            if (
                phase["replacement"]
                and "# Alpha" in content
                and "[[concepts/charlie|Charlie]]" not in content
            ):
                return {
                    "verdict": "unsupported",
                    "reason": "The normalized Alpha candidate needs a new source-faithful rewrite.",
                    "issues": ["Alpha cannot be published after Charlie is withheld."],
                }
            return {"verdict": "supported", "reason": "The candidate matches its evidence."}
        raise AssertionError(payload["stage"])

    model_service.respond = respond
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed", first
    assert (kb_dir / "wiki/concepts/beta.md").exists()

    recovery_dir = kb_dir / ".openkb" / "source-store" / "compilation" / "recovery"
    saved_plan = read_object(next(recovery_dir.glob("*-plan.json")))["value"]
    pending_keys = {"p1", "p3"}
    for draft_path in recovery_dir.glob("*-draft.json"):
        draft = read_object(draft_path)
        output = draft.get("value", {}).get("output", {})
        if output.get("page_key") in pending_keys:
            draft_path.unlink()
    for checkpoint_path in recovery_dir.parent.glob("*.json"):
        checkpoint = read_object(checkpoint_path)
        payload = checkpoint.get("contract", {}).get("payload", {})
        page = payload.get("page", {})
        if page.get("name") in {"concepts/alpha", "concepts/charlie"}:
            checkpoint_path.unlink()

    def resume_saved_plan(*_args, **_kwargs):
        return from_dict(saved_plan)

    original_restore = document_page_contracts.restore_published_document_page_candidate

    def restore_only_beta(checkpoints, page):
        if page.name in {"concepts/alpha", "concepts/charlie"}:
            raise ValueError("Simulated lost candidate recovery for retry.")
        return original_restore(checkpoints, page)

    monkeypatch.setattr(
        document_page_contracts,
        "restore_published_document_page_candidate",
        restore_only_beta,
    )
    monkeypatch.setattr(document_orchestrator, "plan_document", resume_saved_plan)

    phase["replacement"] = True
    with kb_ingest_lock(kb_dir / ".openkb"):
        with ExecutionContext().begin(kb_dir) as credentials:
            changed = compile_version(
                kb_dir,
                SourceStore(kb_dir).current(first.source_id),
                resolve_effective_config(kb_dir)[0],
                bundle=credentials,
                force=True,
                retry_omissions=True,
            )

    assert changed.knowledge_compilation == "completed", changed
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/charlie.md").exists()
    beta = (kb_dir / "wiki/concepts/beta.md").read_text(encoding="utf-8")
    assert "[[concepts/alpha|Alpha]]" not in beta
    replacement_beta_reviews = [
        content for replacement, content in reviews if replacement and "# Beta" in content
    ]
    # The critical review sees the exact candidate layout after provenance
    # comments are removed; leading whitespace must not turn literal Markdown
    # into different semantic bytes.
    assert replacement_beta_reviews == ["\n\n# Beta\nSee Alpha.\n\n\n\n"]


def test_nested_headings_and_adjacent_conditions_reach_generation(kb_dir, tmp_path, model_service):
    source = tmp_path / "nested.md"
    source.write_text(
        "# Operations\n\n## Linux\n\nOnly on version 7.\n\nEnable cache with --timeout 42."
    )
    generated = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        if payload["stage"] == "planning":
            return _single_page_plan(payload, name="concepts/cache", title="Cache")
        if payload["stage"] == "generation":
            generated.append(payload)
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    wire = json.dumps(generated)
    assert "Only on version 7." in wire
    assert "Operations" in wire and "Linux" in wire


@pytest.mark.parametrize(
    "wrapper,title",
    [
        ("{0}", ""),
        ("A literal ` marker.\n\n{0}\n\nAnother ` marker.", ""),
        ("- A ` marker.\n- {0}\n- Another ` marker.", ""),
        ("| a | b | c |\n| --- | --- | --- |\n| ` | {0} | ` |", ""),
        ("| a | b | c |\n| --- | --- | --- |\n| `{0}` | {0} | {0} |", "原图说明"),
        ("{0}", "原图说明\n第二行"),
        ("{0}", "Use &copy; literally"),
    ],
)
def test_generated_figure_link_points_to_the_retained_immutable_asset(
    kb_dir, tmp_path, model_service, wrapper, title
):
    import html
    import re

    from markdown_it import MarkdownIt
    from PIL import Image

    original = tmp_path / "figure.md"
    original.write_text("![Valve](valve.png)\n\nValve rated 37 kPa.")
    Image.new("RGB", (80, 80), "blue").save(tmp_path / "valve.png")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {"verdict": "supported", "reason": "Controlled evidence is supported."}
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            evidence = "\n\n".join(item["text"] for item in payload["evidence"]["blocks"])
            figure = re.search(r"!\[[^\]]*\]\(asset:[^)]+\)", evidence)[0]
            if title:
                figure = figure[:-1] + ' "' + html.escape(title) + '")'
            response["content"] = wrapper.format(figure) + "\n\nValve rated 37 kPa."
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    page = kb_dir / "wiki/concepts/notes.md"
    tokens = (
        MarkdownIt("commonmark")
        .enable(["table", "strikethrough"])
        .parse(page.read_text(encoding="utf-8"))
    )
    images = [child for token in tokens for child in token.children or [] if child.type == "image"]
    assert all((item.attrGet("title") or "") == title for item in images)
    links = [item.attrGet("src") for item in images]
    assert links
    assert all((page.parent / link).is_file() for link in links)
    assert all(
        (page.parent / link).read_bytes() == (tmp_path / "valve.png").read_bytes() for link in links
    )
