"""Observe compact requests and original citations at the application seam."""

import gc
import json
import weakref

import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


def test_request_construction_releases_source_bodies_without_waiting_for_cycle_collection():
    from openkb.agent.evidence_units import messages

    class Body(str):
        pass

    references = []
    collection_enabled = gc.isenabled()
    gc.disable()
    try:
        for number in range(3):
            body = Body(f"Operation {number}: " + "Retain the original condition. " * 2000)
            references.append(weakref.ref(body))
            neighbor = {
                "reference": {"block_id": f"original-{number}"},
                "relation": "operation_context",
                "text": body,
            }
            payload = {
                "stage": "generation",
                "evidence": [{"text": "Restart the service.", "neighbors": [neighbor, neighbor]}],
            }
            request = messages("Use the original operation context.", payload)
            assert body in request[-1]["content"]
            del request, payload, neighbor, body

        # Completed request construction must release originals promptly: a few
        # cyclic objects can otherwise retain large bodies between GC runs.
        assert sum(reference() is not None for reference in references) == 0
    finally:
        if collection_enabled:
            gc.enable()
        gc.collect()


def test_transport_identity_in_prose_is_omitted_without_interrupting_publication(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "local-reference.md"
    source.write_text("The chamber must cool before opening.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] += (
                f" ({payload['evidence']['blocks'][0]['reference']['source_id']})"
            )
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    assert result.omissions
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


@pytest.mark.parametrize("literal", ["r8", "@r:1", "@r:1 and @r_:2", "⟪r:1⟫"])
def test_source_words_resembling_transport_identities_remain_literal(
    kb_dir, tmp_path, model_service, literal
):
    source = tmp_path / "literal-label.md"
    source.write_text(f"The printed label is {literal}.")

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            response["content"] = source.read_text()
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    pages = list((kb_dir / "wiki/concepts").glob("*.md"))
    assert pages and source.read_text() in pages[0].read_text()
    for call in model_service:
        payload = json.loads(call["messages"][-1]["content"])
        if payload["stage"] == "generation":
            assert payload["evidence"]["blocks"][0]["text"] == source.read_text()
            assert payload["identity_protocol"]["namespace"] not in source.read_text()


@pytest.mark.parametrize("field", ["title", "heading", "content"])
def test_transport_identity_cannot_escape_through_a_scoped_fragment(
    kb_dir, tmp_path, model_service, field
):
    source = tmp_path / "separate-tasks.md"
    source.write_text("# Chamber\n\nCool before opening.\n\n# Tank\n\nDrain before cleaning.")
    config = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(config.read_text())
    settings.setdefault("processing", {}).update(context_tokens=32768, output_tokens=4096)
    config.write_text(yaml.safe_dump(settings))

    scoped_attempts = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "generation":
            scoped_attempts.append(payload)
            identity = payload["evidence"]["blocks"][0]["reference"]["source_id"]
            if field == "title":
                response["title"] = f"A reference ({identity})"
            elif field == "heading":
                response["content"] = f"# A reference ({identity})"
            else:
                response["content"] += f" ({identity})"
        return response

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    assert scoped_attempts
    assert result.omissions and not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_short_wire_ids_preserve_literal_hex_and_every_occurrence(kb_dir, tmp_path, model_service):
    literal = "abcdef0123456789" * 4
    source = tmp_path / "checks.md"
    source.write_text(f"First subject checksum: {literal}.\n\nSecond subject checksum: {literal}.")
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "generation":
            observed.extend(payload["evidence"]["blocks"])
            return {
                "content": "\n\n".join(block["text"] for block in payload["evidence"]["blocks"]),
                "covered": [item["id"] for item in payload["occurrences"]],
            }
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(observed) == 2
    assert len({block["id"] for block in observed}) == 2
    assert all(len(block["id"]) < 10 for block in observed)
    assert all(literal in block["text"] for block in observed)
    text = "\n".join(path.read_text() for path in (kb_dir / "wiki/concepts").glob("*.md"))
    assert text.count(literal) >= 2
    assert result.input_version in text


def test_pooled_heading_context_keeps_distinct_actor_scopes(kb_dir, tmp_path, model_service):
    headings = {
        actor: f"{actor} operation requires confirmed isolation. " + "Read all conditions. " * 18
        for actor in ("Alpha", "Beta")
    }
    source = tmp_path / "actors.md"
    source.write_text(
        "\n\n".join(
            f"# {headings[actor]}\n\n"
            + "\n\n".join(f"{actor} step {i} requires pressure 37 kPa." for i in range(4))
            for actor in headings
        )
    )
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "generation":
            for block in payload["evidence"]["blocks"]:
                if " step " not in block["text"]:
                    continue
                actor = block["text"].split()[0]
                context = block["location"]["headings"]
                assert context == [headings[actor].strip()]
                observed.append((actor, context[0]))
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(observed) == 8
    assert len({reference for _, reference in observed}) == 2
    assert all(sum(a == actor for a, _ in observed) == 4 for actor in headings)
