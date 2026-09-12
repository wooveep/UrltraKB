"""Observe compact requests and original citations at the application seam."""

import json

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


def test_short_wire_ids_preserve_literal_hex_and_every_occurrence(kb_dir, tmp_path, model_service):
    literal = "abcdef0123456789" * 4
    source = tmp_path / "checks.md"
    source.write_text(f"First subject checksum: {literal}.\n\nSecond subject checksum: {literal}.")
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            observed.extend(payload["units"])
            return {
                "units": [
                    {
                        "id": unit["id"],
                        "facts": [
                            {"topic": "Checksum", "statement": unit["text"], "quote": unit["text"]}
                        ],
                        "empty_reason": "",
                    }
                    for unit in payload["units"]
                ]
            }
        if payload["stage"] == "generation":
            return {
                "content": "\n\n".join(fact["quote"] for fact in payload["facts"]),
                "covered": [fact["id"] for fact in payload["facts"]],
            }
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(observed) == 2
    assert len({unit["id"] for unit in observed}) == 2
    assert all(len(unit["id"]) < 10 for unit in observed)
    assert all(literal in unit["text"] for unit in observed)
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
    pools = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] == "facts":
            pool = payload.get("context_pool", {})
            pools.append(pool)
            for unit in payload["units"]:
                if " step " not in unit["text"]:
                    continue
                actor = unit["text"].split()[0]
                context = [
                    pool[row["context_ref"]] if "context_ref" in row else row
                    for row in unit["heading_evidence"]
                ]
                assert len(context) == 1
                assert headings[actor].strip() in context[0]["text"]
                observed.append((actor, context[0]["reference"]["block_id"]))
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert any(pools) and len(observed) == 8
    assert len({reference for _, reference in observed}) == 2
    assert all(sum(a == actor for a, _ in observed) == 4 for actor in headings)
