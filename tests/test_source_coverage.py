"""Coverage at published source boundaries cannot hide original ranges or grow a read."""

import asyncio
import json

import pytest

from openkb.agent.query import build_query_agent
from openkb.application.conversations import ask_question
from openkb.application.documents import import_document
from openkb.application.source_history import source_status
from openkb.state import HashRegistry


def test_truncated_published_manifest_cannot_claim_complete(kb_dir, tmp_path, model_service):
    source = tmp_path / "range.md"
    source.write_text("The lower field is the minimum; the upper field is the maximum.")
    result = import_document(kb_dir, source)
    assert result.coverage["status"] == "complete"
    registry = HashRegistry(kb_dir / ".openkb/hashes.json")
    entry = registry.get(result.source_id)
    broken = json.loads(entry["compilation_coverage"])
    broken["ranges"] = []
    registry.add(result.source_id, {**entry, "compilation_coverage": json.dumps(broken)})
    with pytest.raises(ValueError, match="coverage denominator"):
        build_query_agent(str(kb_dir / "wiki"), "openai/offline-test")


def test_source_window_returns_compact_coverage_of_the_requested_characters(
    kb_dir, tmp_path, model_service
):
    source = tmp_path / "range.md"
    source.write_text("The lower field is the minimum; the upper field is the maximum.")
    result = import_document(kb_dir, source)
    tree = source_status(kb_dir, result.source_id)["navigation"]
    observed = []

    def chat(body):
        completed = [row for row in body["messages"] if row["role"] == "tool"]
        if completed:
            observed.append(json.loads(completed[-1]["content"])["evidence"][0])
            return {"role": "assistant", "content": "Requested original window returned."}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "window",
                    "type": "function",
                    "function": {
                        "name": "read_source_node",
                        "arguments": json.dumps(
                            {
                                "source_id": result.source_id,
                                "node_id": tree["nodes"][0]["id"],
                                "offset": 0,
                                "start": 2,
                                "max_chars": 3,
                            }
                        ),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    assert asyncio.run(ask_question(kb_dir, "Read the requested window.")).status == "completed"
    row = observed[0]
    assert row["text"] == "e l"
    assert row["next_start"] is not None
    assert len(json.dumps(row["analysis_coverage"])) < 200
    assert sum(row["analysis_coverage"]["characters"].values()) == 3
