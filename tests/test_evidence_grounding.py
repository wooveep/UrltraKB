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
    assert result.knowledge_compilation == "unfinished", result
    assert result.reason == "knowledge_evidence_mismatch"
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
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, output in zip(payload["units"], response["units"]):
                output.update(facts=[], empty_reason="Context for the version restriction.")
                if quote in unit["text"]:
                    output["facts"] = [{"topic": "Support", "statement": quote, "quote": quote}]
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
        assert payload["facts"][0].get("quote") == quote
        if payload["stage"] == "verification":
            assert "statement" not in payload["facts"][0]
        neighbors = payload["evidence"][0]["neighbors"]
        assert any(
            item.get("relation") == "heading" and item["text"] == heading for item in neighbors
        )
        assert any(
            item.get("relation") == "previous_block" and item["text"] == "Wait 15 seconds."
            for item in neighbors
        )


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
        if payload["stage"] == "facts":
            response["units"][0]["facts"] = [
                {"topic": "Support", "statement": quote, "quote": quote}
            ]
        if payload["stage"] == "generation":
            response["content"] = quote if payload.get("revision") else incorrect
        if payload["stage"] == "verification":
            checked.append(payload["content"].split("\n\n", 1)[1])
            return {
                "verdict": "supported" if checked[-1] == quote else "unsupported",
                "reason": "The source restricts startup, not shutdown.",
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert checked == [incorrect, quote]
    content = (kb_dir / "wiki/concepts/notes.md").read_text()
    assert quote in content and incorrect not in content


@pytest.mark.parametrize(
    "review,reason",
    [
        (
            {"verdict": "uncertain", "reason": "Evidence cannot decide."},
            "knowledge_evidence_mismatch",
        ),
        ({"verdict": "supported"}, "evidence_verification_invalid"),
        ({"verdict": True, "reason": "Yes"}, "evidence_verification_invalid"),
        ({"verdict": "supported", "reason": " "}, "evidence_verification_invalid"),
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
    assert result.reason == reason
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    model_service.respond = None
    events_before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    stages = [
        json.loads(call["messages"][-1]["content"])["stage"]
        for call in model_service[events_before:]
    ]
    assert "facts" not in stages and "verification" in stages


def test_review_uses_existing_request_budget_and_verified_work_is_reusable(
    kb_dir, tmp_path, model_service
):
    original = tmp_path / "bounded.md"
    original.write_text("The normal pressure limit is 37 kPa.")
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["max_requests"] = 3
    config_path.write_text(yaml.safe_dump(config))
    result = import_document(kb_dir, original)
    assert result.reason == "request_budget_exhausted"
    assert len(model_service) == 3
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == 4  # The completed draft only needs verification.
    again = import_document(kb_dir, original)
    assert again.status == "skipped" and len(model_service) == 4


@pytest.mark.parametrize("damage", ["missing", "verdict", "reason", "content"])
def test_invalid_cached_verification_cannot_publish(kb_dir, tmp_path, model_service, damage):
    original = tmp_path / "cache.md"
    original.write_text("The normal pressure limit is 37 kPa.")

    def stop(event):
        if event.get("stage") == "generated":
            raise OperationCancelled()

    result = import_document(kb_dir, original, context=ExecutionContext(on_event=stop))
    assert result.knowledge_compilation == "stopped"
    # Corrupt a persisted boundary record while retaining its outer integrity hash.
    for path in (SourceStore(kb_dir).root / "compilation").glob("*.json"):
        record = json.loads(path.read_text())
        value = record.get("value", {})
        if "_verification" not in value:
            continue
        if damage == "missing":
            del value["_verification"]
        elif damage == "verdict":
            value["_verification"]["verdict"] = "unsupported"
        elif damage == "reason":
            value["_verification"]["reason"] = " "
        else:
            value["content"] = "The normal pressure limit is 999 kPa."
        record["value_digest"] = content_id(value)
        path.write_text(json.dumps(record))
        break
    else:
        pytest.fail("No verified contribution was persisted")
    before = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.reason == "evidence_verification_invalid"
    assert len(model_service) == before
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_public_topic_title_is_verified_with_its_body(kb_dir, tmp_path, model_service):
    original = tmp_path / "title.md"
    original.write_text("For controller version 6, this startup procedure is unsupported.")
    bad_title = "Version 6 must not use the shutdown procedure"

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "planning":
            response["topics"][0]["title"] = bad_title
        elif payload["stage"] == "generation":
            response["content"] = original.read_text()
        elif payload["stage"] == "verification":
            return {
                "verdict": "unsupported" if payload.get("title") == bad_title else "supported",
                "reason": "The title transfers the startup restriction to shutdown.",
            }
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.reason == "knowledge_evidence_mismatch"
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
            response["topics"][0]["title"] = bad_title
        elif payload["stage"] == "generation":
            response["content"] = "# " + bad_title + "\n\n" + original.read_text()
            if payload.get("revision"):
                response["title"] = good_title
        elif payload["stage"] == "verification":
            return {
                "verdict": (
                    "supported"
                    if payload.get("title") == good_title and bad_title not in payload["content"]
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
    config["processing"].update(context_tokens=4096, output_tokens=1024)
    config_path.write_text(yaml.safe_dump(config))
    (kb_dir / "wiki/AGENTS.md").write_text("Preserve original technical claims.")
    reviews = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, item in zip(payload["units"], response["units"]):
                item["facts"] = [
                    {"topic": "Pressure", "statement": f"Valve {i}.", "quote": f"Valve {i}."}
                    for i in range(4)
                    if f"Valve {i}." in unit["text"]
                ]
        elif payload["stage"] == "generation":
            response["content"] = "\n\n".join(item["text"] for item in payload["evidence"])
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
        assert tokens + call["max_tokens"] <= 4096
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
    config["processing"].update(context_tokens=4096, output_tokens=1024, max_requests=100)
    config_path.write_text(yaml.safe_dump(config))
    generated = []
    reviewed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            title = "Version 6 operations" if not generated else "Shutdown support"
            generated.append(title)
            response.update(
                title=title, content="\n\n".join(e["text"] for e in payload["evidence"])
            )
        elif payload["stage"] == "verification":
            reviewed.append(payload)
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert len(generated) > 1
    assert {p["title"] for p in reviewed} == {"Version 6 operations"}
    assert all(p.get("title_context") for p in reviewed)
    assert all(p["title_context"] == reviewed[0]["title_context"] for p in reviewed)
    content = (kb_dir / "wiki/concepts/notes.md").read_text()
    assert 'description: "Version 6 operations"' in content
    assert "Shutdown support" not in content


def test_verification_thinking_override_rechecks_pages_but_reuses_facts(
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
    assert [json.loads(c["messages"][-1]["content"])["stage"] for c in calls] == [
        "generation",
        "verification",
    ]
    assert [c.get("thinking") for c in calls] == [{"type": "disabled"}, {"type": "enabled"}]
    with pytest.raises(ValueError):
        update("invalid")
    assert read_kb_config(kb_dir).verification_thinking == "enabled"


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
            response["title"] = (
                "Startup " + marker + payload["evidence"][0]["reference"]["source_id"] + " -->"
            )
        return response

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.reason == "topic_generation_incomplete", result
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
            reviewed.append(payload["content"])
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
    assert result.reason == "generated_asset_evidence_invalid", result
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
