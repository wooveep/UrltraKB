"""Evidence fidelity gates exercised through the shared document operation."""

import json

import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.execution import ExecutionContext
from openkb.application.source_actions import continue_source
from openkb.cancellation import OperationCancelled
from openkb.sources import SourceStore, content_id
from tests.http_model_fixture import evidence_response


def test_semantic_rejection_keeps_original_and_blocks_knowledge_publication(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "procedure.md"
    original.write_text(
        "# Pump controller version 7 - shutdown procedure\n\n"
        "For controller version 6, this startup procedure is unsupported.\n"
    )
    # The actual 2026-09-10 DeepSeek counterexample, without its run-specific IDs.
    incorrect = (
        "The shutdown procedure described in this document applies to "
        "**pump controller version 7**. The procedure is unsupported for "
        "**controller version 6**; a version 6 controller must not use this shutdown procedure."
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification":
            return {
                "verdict": "unsupported",
                "reason": "The source restricts startup, not shutdown.",
            }
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            if "fragments" in response:
                for fragment in response["fragments"]:
                    fragment["content"] = incorrect
            else:
                response["content"] = incorrect
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.status == "added", result
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    assert result.source_intake == "saved"
    store = SourceStore(kb_dir)
    assert store.original(store.version(result.input_version)).read_bytes() == original.read_bytes()
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    generations = [
        call
        for call in model_service
        if json.loads(call["messages"][-1]["content"])["stage"] == "generation"
    ]
    assert len(generations) == 2  # One correction, then stop; no unbounded regeneration.


def test_generation_and_verification_keep_the_quote_and_context_roles(
    kb_dir, tmp_path, model_service
):
    heading = "# Pump controller version 7 - shutdown procedure"
    quote = "For controller version 6, this startup procedure is unsupported."
    original = tmp_path / "scope.md"
    original.write_text(heading + "\n\nWait 15 seconds.\n\n" + quote)
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "planning":
            blocks = payload["evidence"]["blocks"]
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            heading_block = next(block for block in blocks if block["text"] == heading)
            wait_block = next(block for block in blocks if block["text"] == "Wait 15 seconds.")
            quote_block = next(block for block in blocks if block["text"] == quote)
            return {
                "overview": {
                    "text": "Controller shutdown scope.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "support",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/controller-support",
                        "title": "Controller Support",
                        "purpose": "Describe version-specific controller support.",
                        "subject_ranges": [[quote_block["order"], quote_block["order"] + 1]],
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": [[heading_block["order"], heading_block["order"] + 1]],
                                "basis": heading,
                                "basis_ranges": [
                                    [heading_block["order"], heading_block["order"] + 1]
                                ],
                            },
                            {
                                "relation": "explicit_reference",
                                "ranges": [[wait_block["order"], wait_block["order"] + 1]],
                                "basis": "Wait 15 seconds.",
                                "basis_ranges": [[wait_block["order"], wait_block["order"] + 1]],
                            },
                        ],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        response = evidence_response(payload)
        if payload["stage"] in ("generation", "verification"):
            observed.append(payload)
        if payload["stage"] == "generation":
            response["content"] = quote
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert {payload["stage"] for payload in observed} == {"generation", "verification"}
    for payload in observed:
        assert "facts" not in payload
        evidence = payload["evidence"]["blocks"]
        assert any(
            item["text"] == quote and any(route["route"] == "page_body" for route in item["routes"])
            for item in evidence
        )
        assert any(
            item["text"] == heading
            and {route["route"] for route in item["routes"]} == {"context_only"}
            and item["routes"][0]["relation"] == "applicable_condition"
            for item in evidence
        )
        assert any(
            item["text"] == "Wait 15 seconds."
            and {route["route"] for route in item["routes"]} == {"context_only"}
            and item["routes"][0]["relation"] == "explicit_reference"
            for item in evidence
        )


@pytest.mark.parametrize(
    "original_text",
    [
        "# Reference: auxiliary assembly",
        "# The auxiliary assembly is isolated",
        "Reference: auxiliary assembly",
        "The original table labels its first row 'header role unconfirmed'.",
    ],
)
def test_extractor_interpretation_is_not_a_generation_authority(
    kb_dir, tmp_path, model_service, original_text
):
    original = tmp_path / "labels.md"
    original.write_text(original_text)
    invented = "The assembly has an independent safety function."
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] in {"generation", "verification"}:
            observed.append(payload)
        if payload["stage"] == "generation":
            response["content"] = original_text
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert {p["stage"] for p in observed} == {"generation", "verification"}
    for payload in observed:
        assert invented not in json.dumps(payload)
        assert "facts" not in payload
        assert any(
            block["text"] == original_text and block["reference"]["parse_id"]
            for block in payload["evidence"]["blocks"]
        )
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert pages and original_text in pages[0].read_text()
    assert invented not in pages[0].read_text()


def test_unsupported_draft_is_corrected_from_feedback_and_verified_before_publication(
    kb_dir, tmp_path, model_service
):
    quote = "For controller version 6, this startup procedure is unsupported."
    incorrect = "For controller version 6, the shutdown procedure is unsupported."
    original = tmp_path / "repair.md"
    original.write_text(quote)
    checked = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = quote if payload.get("revision") else incorrect
        if payload["stage"] == "verification":
            checked.append(payload["candidate"]["content"])
            return {
                "verdict": "supported" if quote in checked[-1] else "unsupported",
                "reason": "The source restricts startup, not shutdown.",
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    # Formal review receives the exact private publication proposal, including
    # its source binding, rather than an unbound body string.
    assert len(checked) == 2
    assert incorrect in checked[0]
    assert quote in checked[1]
    content = (kb_dir / "wiki/concepts/notes.md").read_text()
    assert quote in content and incorrect not in content


@pytest.mark.parametrize(
    "review,reason",
    [
        (
            {"verdict": "uncertain", "reason": "Evidence cannot decide."},
            "knowledge_evidence_mismatch",
        ),
        ({"verdict": "supported"}, "document_verification_invalid"),
        ({"verdict": True, "reason": "Yes"}, "document_verification_invalid"),
        ({"verdict": "supported", "reason": " "}, "document_verification_invalid"),
    ],
)
def test_unusable_review_cannot_publish_or_be_reused(
    kb_dir, tmp_path, model_service, review, reason
):
    original = tmp_path / "review.md"
    original.write_text("The normal pressure limit is 37 kPa.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        return review if payload["stage"] == "verification" else evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == reason for row in result.omissions)
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    model_service.respond = None
    events_before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"]
        for call in model_service[events_before:]
    ]
    assert "planning" not in stages
    if review.get("verdict") == "uncertain":
        assert stages == []
        assert any(row["reason"] == reason for row in continued.omissions)
        assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    else:
        assert "verification" in stages


def test_review_uses_existing_request_budget_and_verified_work_is_reusable(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "bounded.md"
    original.write_text("The normal pressure limit is 37 kPa.")
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["max_requests"] = 2
    config_path.write_text(yaml.safe_dump(config))
    result = import_document(kb_dir, original)
    assert result.reason == "request_budget_exhausted"
    assert len(model_service) == 2
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == 3  # The completed draft only needs verification.
    again = import_document(kb_dir, original)
    assert again.status == "skipped" and len(model_service) == 3


@pytest.mark.parametrize("damage", ["missing", "verdict", "reason", "content"])
def test_corrupted_cached_page_response_is_revalidated_before_publication(
    kb_dir, tmp_path, model_service, damage
):
    original = tmp_path / "cache.md"
    original.write_text("The normal pressure limit is 37 kPa.")

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    result = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert result.knowledge_compilation == "stopped"
    # Corrupt a persisted formal page boundary while retaining its outer integrity hash.
    for path in (SourceStore(kb_dir).root / "compilation").glob("*.json"):
        record = json.loads(path.read_text())
        value = record.get("value", {})
        if damage == "content" and isinstance(value, dict) and {"content", "covered"} <= set(value):
            value["content"] = "The normal pressure limit is 999 kPa."
        elif (
            damage != "content" and isinstance(value, dict) and {"verdict", "reason"} <= set(value)
        ):
            if damage == "missing":
                del value["reason"]
            elif damage == "verdict":
                value["verdict"] = True
            else:
                value["reason"] = " "
        else:
            continue
        record["value_digest"] = content_id(value)
        path.write_text(json.dumps(record))
        break
    else:
        pytest.fail("No formal page boundary was persisted")

    def recheck(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "verification" and "999 kPa" in payload["candidate"]["content"]:
            return {
                "verdict": "unsupported",
                "reason": "The cached candidate changes the original pressure limit.",
            }
        return evidence_response(payload)

    model_service.respond = recheck
    before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed"
    assert len(model_service) > before
    page = (kb_dir / "wiki/concepts/notes.md").read_text()
    assert "999 kPa" not in page


def test_public_topic_title_is_verified_with_its_body(kb_dir, tmp_path, model_service):
    original = tmp_path / "title.md"
    original.write_text("For controller version 6, this startup procedure is unsupported.")
    bad_title = "Version 6 must not use the shutdown procedure"

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "planning":
            response["page_changes"][0]["title"] = bad_title
        elif payload["stage"] == "generation":
            response["content"] = original.read_text()
        elif payload["stage"] == "verification":
            return {
                "verdict": (
                    "unsupported" if payload["page"]["title"] == bad_title else "supported"
                ),
                "reason": "The title transfers the startup restriction to shutdown.",
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == "knowledge_evidence_mismatch" for row in result.omissions)
    assert all(bad_title not in path.read_text() for path in (kb_dir / "wiki").rglob("*.md"))
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_corrected_title_is_verified_published_and_restored(kb_dir, tmp_path, model_service):
    original = tmp_path / "repair-title.md"
    original.write_text("For controller version 6, this startup procedure is unsupported.")
    bad_title, good_title = "Shutdown is unsupported", "Startup support"

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "planning":
            response["page_changes"][0]["title"] = good_title
        elif payload["stage"] == "generation":
            response["content"] = "# " + bad_title + "\n\n" + original.read_text()
            if payload.get("revision"):
                response["content"] = original.read_text()
        elif payload["stage"] == "verification":
            return {
                "verdict": (
                    "supported"
                    if bad_title not in payload["candidate"]["content"]
                    else "unsupported"
                ),
                "reason": "The public title must refer to startup support.",
            }
        return response

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    model_service.respond = respond
    result = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert result.knowledge_compilation == "stopped", result
    before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == before
    content = "\n".join(p.read_text() for p in (kb_dir / "wiki").rglob("*.md"))
    assert good_title in content and bad_title not in content


def test_batches_fit_generation_review_and_correction_in_the_same_context(
    kb_dir, tmp_path, model_service
):
    import litellm

    original = tmp_path / "envelope.md"
    original.write_text(
        "Valve 0. Valve 1. Valve 2. Valve 3. " + "Pressure remains below 37 kPa. " * 25
    )
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(context_tokens=6144, output_tokens=1024)
    config_path.write_text(yaml.safe_dump(config))
    (kb_dir / "wiki/AGENTS.md").write_text("Preserve original technical claims.")
    reviews = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = "\n\n".join(
                item["text"] for item in payload["evidence"]["blocks"]
            )
            if payload.get("revision"):
                response["content"] = "## Pressure limit\n\n" + response["content"]
        elif payload["stage"] == "verification":
            reviews.append(payload)
            response = {
                "verdict": "unsupported" if len(reviews) == 1 else "supported",
                "reason": (
                    "Preserve the literal wording and exact pressure limit "
                    "in the original evidence."
                ),
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert reviews
    assert any(
        json.loads(call["messages"][-1]["content"]).get("revision") for call in model_service
    )
    for call in model_service:
        tokens = litellm.token_counter(model="gpt-4o-mini", messages=call["messages"])
        tokens += litellm.token_counter(
            model="gpt-4o-mini", text=json.dumps(call["response_format"])
        )
        assert tokens + call["max_tokens"] <= 6144
    content = (kb_dir / "wiki/concepts/notes.md").read_text()
    assert all(f"Valve {i}." in content for i in range(4))


def test_later_part_cannot_change_the_title_of_verified_parts(kb_dir, tmp_path, model_service):
    original = tmp_path / "title-scope.md"
    original.write_text(
        "\n\n".join(
            (
                "Startup is unsupported for version 6. "
                if i < 4
                else "Shutdown is supported for version 6. "
            )
            * 15
            for i in range(8)
        )
    )
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"].update(context_tokens=6144, output_tokens=1024, max_requests=100)
    config_path.write_text(yaml.safe_dump(config))
    generated = []
    reviewed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "planning":
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            registered = payload["carry"]["page_register"]
            return {
                "overview": {
                    "text": "Version 6 startup and shutdown operations.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "version-6",
                        "target_key": registered[0]["key"] if registered else "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/version-6-operations",
                        "title": "Version 6 operations",
                        "purpose": "Describe supported version 6 operations.",
                        "subject_ranges": ranges,
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        if payload["stage"] == "generation":
            generated.append(payload)
            response.update(
                title="Shutdown support",
                content="\n\n".join(e["text"] for e in payload["evidence"]["blocks"]),
            )
        elif payload["stage"] == "verification":
            reviewed.append(payload)
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert generated
    assert {p["page"]["title"] for p in reviewed} == {"Version 6 operations"}
    content = (kb_dir / "wiki/concepts/version-6-operations.md").read_text()
    assert 'description: "Version 6 operations"' in content
    assert "Shutdown support" not in content


def test_verification_thinking_change_rechecks_candidate_without_regenerating(
    kb_dir, tmp_path, model_service
):
    from openkb.application.settings import apply_kb_config_patch, read_kb_config
    from openkb.application.settings_data import KbConfigPatchRequest

    original = tmp_path / "review-thinking.md"
    original.write_text("Startup is unsupported for controller version 6.")

    def update(mode):
        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(
                kb=str(kb_dir),
                config={
                    "compilation_thinking": "disabled",
                    "verification_thinking": mode,
                },
            ),
        )
        assert read_kb_config(kb_dir).verification_thinking == mode

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    update("disabled")
    result = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert result.knowledge_compilation == "stopped"
    before = len(model_service)
    update("enabled")
    result = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert result.knowledge_compilation == "completed", result
    calls = model_service[before:]
    assert [json.loads(c["messages"][-1]["content"])["stage"] for c in calls] == ["verification"]
    assert [c.get("thinking") for c in calls] == [{"type": "enabled"}]
    with pytest.raises(ValueError):
        update("invalid")
    assert read_kb_config(kb_dir).verification_thinking == "enabled"


def test_generation_thinking_change_replans_and_reverifies_page(kb_dir, tmp_path, model_service):
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    source = tmp_path / "independent-thinking.md"
    source.write_text("Pressure must remain below 37 kPa.")

    def update(mode):
        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(
                kb=str(kb_dir),
                config={"compilation_thinking": mode, "verification_thinking": "enabled"},
            ),
        )

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    update("disabled")
    first = import_document(kb_dir, source, context=ExecutionContext(on_event=stop))
    assert first.knowledge_compilation == "stopped"
    before = len(model_service)
    update("enabled")
    result = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert result.knowledge_compilation == "completed", result
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"] for call in model_service[before:]
    ]
    assert stages == ["planning", "generation", "verification"]


@pytest.mark.parametrize(
    "marker", ["<!-- openkb-source:", "<!-- /openkb-source:", "<!-- source-evidence:"]
)
def test_title_cannot_inject_reserved_provenance_markers(kb_dir, tmp_path, model_service, marker):
    original = tmp_path / "marker.md"
    original.write_text("Startup is unsupported for controller version 6.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = "Startup " + marker + "opaque -->"
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == "document_generation_incomplete" for row in result.omissions), (
        result
    )
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert all(
        json.loads(c["messages"][-1]["content"])["stage"] != "verification" for c in model_service
    )


@pytest.mark.parametrize(
    "wrapper",
    [
        "```markdown\n{}\n```",
        "~~~\n{}\n~~~",
        "    {}",
        "`{}`",
        "> ```\n> {}\n> ```",
        "alpha\u2028beta\u2028gamma\n~~~\n{}\n~~~",
        "alpha\r\n~~~\r\n{}\r\n~~~",
    ],
)
def test_link_examples_in_code_reach_review_and_publication_unchanged(
    kb_dir, tmp_path, model_service, wrapper
):
    from tests.document_fixtures import write_docx

    original = tmp_path / "literal-markup.docx"
    literal = "![Original page 1](asset:example) [[literal/reference]]"
    content = wrapper.format(literal)
    write_docx(original, "<w:p><w:r><w:t>" + literal + "</w:t></w:r></w:p>")
    reviewed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = content
        if payload["stage"] == "verification":
            reviewed.append(payload["candidate"]["content"])
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert reviewed and all(content in value for value in reviewed)
    assert content in (kb_dir / "wiki/concepts/notes.md").read_bytes().decode()


@pytest.mark.parametrize(
    "content",
    [
        "A literal ` marker.\n\n![plot](asset:absent)\n\nAnother ` marker.",
        "- A literal ` marker.\n- ![plot](asset:absent)\n- Another ` marker.",
        "| left | middle | right |\n| --- | --- | --- |\n| ` | ![plot](asset:absent) | ` |",
    ],
)
def test_missing_real_image_between_separate_code_markers_blocks_publication(
    kb_dir, tmp_path, model_service, content
):
    original = tmp_path / "missing-image.md"
    original.write_text("The pump requires an isolation valve.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = content
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed"
    assert any(row["reason"] == "document_generation_incomplete" for row in result.omissions)
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_compilation_keeps_reader_annotations_separate_from_original_text(
    kb_dir, tmp_path, model_service
):
    from tests.document_fixtures import write_docx

    original = tmp_path / "row-origin.docx"
    write_docx(
        original,
        "<w:tbl>"
        + "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:tc>" for v in row)
            + "</w:tr>"
            for row in [("Item", "Count"), ("A", "10")]
        )
        + "</w:tbl>",
    )
    claimed = "The original marks this table as header role unconfirmed."
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] in {"generation", "verification"}:
            observed.append(payload)
        if payload["stage"] == "generation":
            response["content"] += "\n" + claimed
        if payload["stage"] == "verification" and any(
            block.get("context_data", {}).get("reader_status")
            for block in payload["evidence"]["blocks"]
        ):
            return {
                "verdict": "unsupported",
                "reason": "The annotation belongs to the reader, not the original author.",
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    assert result.omissions
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    # Native tables now retain literal cells locally; no model fact-extraction
    # request is needed. Generation and review must still carry provenance.
    assert {p["stage"] for p in observed} == {"generation", "verification"}
    for payload in observed:
        rows = payload["evidence"]["blocks"]
        assert any(
            row["context_data"]["reader_status"] == {"header_role": "unconfirmed"} for row in rows
        )
        assert all(claimed not in row["text"] for row in rows)
    store = SourceStore(kb_dir)
    assert store.original(store.version(result.input_version)).read_bytes() == original.read_bytes()
