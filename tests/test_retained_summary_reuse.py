"""A corrected saved response is reused before claiming another paid send."""

import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_resume_revalidates_saved_third_summary_before_sending_the_fourth(kb_dir):
    from ultrakb.agent.document_summary import (
        build_document_summary,
        summary_messages,
        window_sections,
    )
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.compile_checkpoints import CompileCheckpoints, digest
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / "wiki/sources/guide.md",
        "part1=KEEP\n\npart2=KEEP\n\npart3\u00a0=\u00a03\n\npart4=KEEP\n\n",
    )
    checkpoint = CompileCheckpoints(
        kb_dir,
        SimpleNamespace(task_id="a" * 32, parent_task_id=None, compiler_request_version=2),
        {"document": "guide"},
        {},
    )
    windows = CompileSourceWindows(kb_dir / "wiki", "guide", navigation=[])
    system = {"role": "system", "content": "Explain original conditions."}

    def fits(messages):
        return len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= 15

    batches = list(windows.iter_document_windows(fits=lambda window: len(window["content"]) <= 15))
    assert len(batches) == 4
    requests = [summary_messages(system, "guide", batch) for batch in batches]

    def response(batch, body=None):
        return {
            "description": "Retained original",
            "sections": [
                {"id": section["id"], "content": body or batch["content"]}
                for section in window_sections(batch)
            ],
        }

    for number in (1, 2):
        checkpoint.save(
            f"summary:{number}", digest(requests[number - 1]), response(batches[number - 1])
        )
    accepted = {path: path.read_bytes() for path in checkpoint.directory.glob("payload-*.json")}
    third_digest = digest(requests[2])
    checkpoint.claim_repair("summary:3", third_digest)
    checkpoint.record_invalid_response(
        "summary:3",
        third_digest,
        json.dumps(response(batches[2], "```text\npart3 = 3\n```")),
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=tuple(section["id"] for section in window_sections(batches[2])),
        code="page_literal_conflict",
        detail="Previous whitespace validation rejected this quotation",
        attempt=1,
    )

    class ReachedFourth(Exception):
        pass

    sent = []

    async def next_model_request(messages, stage, originals, sections):
        sent.append(stage)
        assert stage == "document-summary:4", "The saved third response must not be sent again"
        raise ReachedFourth

    with pytest.raises(ReachedFourth):
        await build_document_summary(
            windows,
            system_message=system,
            generate=next_model_request,
            fits=fits,
            checkpoints=checkpoint,
        )
    assert sent == ["document-summary:4"]
    restored = checkpoint.load("summary:3", third_digest)
    assert "part3 = 3" in restored["sections"][0]["content"]
    assert all(path.read_bytes() == before for path, before in accepted.items())
    entry = json.loads(checkpoint.path.read_text())["entries"]["summary:3"]
    assert entry["repair_used"] is True
    assert entry["reused_response"]["response_sha256"]


@pytest.fixture
def captured_summary(kb_dir):
    from ultrakb.compile_checkpoints import CompileCheckpoints

    checkpoint = CompileCheckpoints(
        kb_dir, SimpleNamespace(task_id="b" * 32, parent_task_id=None), {"document": "guide"}, {}
    )
    raw = '{"description":"Guide","sections":[{"id":"s1","content":"Retained fact"}]}'
    body = checkpoint.record_invalid_response(
        "summary:3",
        "same-request",
        raw,
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("s1",),
        code="page_literal_conflict",
        detail="Previous validator",
        attempt=1,
    )
    return checkpoint, kb_dir / body


@pytest.mark.parametrize(
    "damage", ["body", "task_id", "request_digest", "expected_sections", "response_file"]
)
def test_saved_response_integrity_and_identity_cannot_be_bypassed(captured_summary, damage):
    from ultrakb.agent.page_response import PageResponseError

    checkpoint, body = captured_summary
    record_path = body.with_suffix(".json")
    if damage == "body":
        body.write_text("Changed content")
    else:
        record = json.loads(record_path.read_text())
        record[damage] = [] if damage == "expected_sections" else "changed"
        record_path.write_text(json.dumps(record))
    before = checkpoint.path.read_bytes()
    with pytest.raises(PageResponseError, match="compile_checkpoint_invalid"):
        checkpoint.retained_response(
            "summary:3",
            "same-request",
            protocol="summary-json",
            sections=("s1",),
            stage="document-summary:3",
        )
    assert checkpoint.path.read_bytes() == before


def test_truncated_saved_output_is_not_reused_even_if_its_json_is_complete(captured_summary):
    checkpoint, body = captured_summary
    record_path = body.with_suffix(".json")
    record = json.loads(record_path.read_text())
    record["error_code"] = "page_truncated_response"
    record_path.write_text(json.dumps(record))
    assert (
        checkpoint.retained_response(
            "summary:3",
            "same-request",
            protocol="summary-json",
            sections=("s1",),
            stage="document-summary:3",
        )
        is None
    )


def test_still_invalid_saved_output_does_not_reset_the_repair_gate(captured_summary):
    import hashlib

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.retained_summary import reuse_saved_summary
    from ultrakb.compile_checkpoints import digest

    checkpoint, body = captured_summary
    record_path = body.with_suffix(".json")
    invalid = '{"description":"Guide","sections":"invalid"}'
    record = json.loads(record_path.read_text())
    body.write_text(invalid)
    record["response_sha256"] = hashlib.sha256(body.read_bytes()).hexdigest()
    record_path.write_text(json.dumps(record))
    checkpoint.claim_repair("summary:3", "same-request")
    before = digest(json.loads(checkpoint.path.read_text()))
    assert (
        reuse_saved_summary(
            checkpoint,
            "summary:3",
            "same-request",
            {"content": "Retained fact", "supplementary_context": []},
            protocol="summary-json",
            section_ranges=[{"id": "s1", "start": 0, "end": 13}],
        )
        is None
    )
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        checkpoint.claim_generation("summary:3", "same-request", stage="compile.document-summary:3")
    assert digest(json.loads(checkpoint.path.read_text())) == before
