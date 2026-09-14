"""Source tools distinguish service rejection from an accepted OCR job."""

import asyncio
import json

import pytest
import requests

from openkb.application.conversations import ask_question
from openkb.application.documents import import_document
from tests.test_cloud_ocr import cloud_settings, scanned_pdf


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "unknown", "failed"])
async def test_answer_observes_rejected_ocr_without_claiming_it_is_queued(
    kb_dir, tmp_path, monkeypatch, model_service, outcome
):
    cloud_settings(kb_dir)
    monkeypatch.setenv("TEST_OCR_TOKEN", "synthetic-test-token")
    document = tmp_path / "scan.pdf"
    scanned_pdf(document)
    submissions = []

    def rejected(self, method, url, **kwargs):
        submissions.append(method)
        if outcome == "unknown":
            raise requests.Timeout("Synthetic lost submission response")
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        value = (
            {"code": 10010, "data": {}}
            if outcome == "rejected"
            else {"code": 0, "data": {"jobId": "accepted"}}
            if method == "POST"
            else {"code": 0, "data": {"state": "failed"}}
        )
        response._content = json.dumps(value).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", rejected)
    imported = await asyncio.to_thread(import_document, kb_dir, document)
    assert imported.knowledge_compilation == "completed"
    expected_requests = {
        "rejected": ["POST", "POST", "POST"],
        "unknown": ["POST"],
        "failed": ["POST", "GET"],
    }[outcome]
    assert submissions == expected_requests
    seen = []

    def chat(body):
        tree = next(
            (json.loads(m["content"]) for m in body["messages"] if m.get("role") == "tool"), None
        )
        if tree is None:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "read_source_tree",
                            "arguments": json.dumps({"source_id": imported.source_id}),
                        },
                    }
                ],
            }
        seen.append(tree)
        status = tree.get("cloud_ocr")
        assert status is not None
        assert status["scope"] == "local_receipts_for_source_version"
        assert status["version"] == imported.input_version
        expected = {
            "rejected": ("rejected", None, "cloud_queue_full", "rejected_before_acceptance"),
            "unknown": ("submission_unknown", None, "cloud_submission_unknown", "unknown"),
            "failed": ("submitted", "failed", "cloud_job_failed", "accepted"),
        }[outcome]
        assert status["jobs"] == [
            {
                "state": expected[0],
                "remote_state": expected[1],
                "reason": expected[2],
                "submission": expected[3],
                "count": 1,
            }
        ]
        return {"role": "assistant", "content": "OCR receipt: " + expected[3]}

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "Was OCR accepted or is it still queued?")
    assert result.status == "completed", result
    assert len(seen) == 1
    assert submissions == expected_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["malformed_version", "unbound_receipt"])
async def test_unverified_old_ocr_receipt_does_not_authorize_status_or_block_original(
    kb_dir, tmp_path, model_service, damage
):
    from openkb.locks import atomic_write_json
    from openkb.sources import SourceStore

    text = "Stop the pump before maintenance."
    document = tmp_path / "maintenance.txt"
    document.write_text(text)
    imported = await asyncio.to_thread(import_document, kb_dir, document)
    assert imported.knowledge_compilation == "completed"
    store = SourceStore(kb_dir)
    version_id = imported.input_version
    if damage == "malformed_version":
        version_id = "f" * 64
        atomic_write_json(store.root / "versions" / f"{version_id}.json", {})
    atomic_write_json(
        store.root / "cloud-jobs" / ("e" * 64 + ".json"),
        {"input": {"source": version_id}, "job_id": "unbound"},
    )
    observed = []

    def chat(body):
        outputs = [json.loads(m["content"]) for m in body["messages"] if m.get("role") == "tool"]
        if not outputs:
            tool = "read_source_tree"
            arguments = {"source_id": imported.source_id}
        elif len(outputs) == 1:
            tree = outputs[0]
            status = tree["cloud_ocr"]
            if damage == "malformed_version":
                assert status["status"] == "unavailable"
            else:
                assert status["jobs"][0]["submission"] == "unknown"
            tool = "read_source_node"
            arguments = {"source_id": imported.source_id, "node_id": tree["nodes"][0]["id"]}
        else:
            row = outputs[-1]["evidence"][0]
            assert row["text"] == text
            observed.append(row)
            return {"role": "assistant", "content": text + " " + row["citation"]}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "read" + str(len(outputs)),
                    "type": "function",
                    "function": {
                        "name": tool,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    model_service.chat_without_tools = True
    result = await ask_question(kb_dir, "What must happen before maintenance?")
    assert result.status == "completed", result
    assert len(observed) == 1
