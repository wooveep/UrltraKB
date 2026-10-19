"""A corrected saved response is reused before claiming another paid send."""

import json

import pytest
from compile_work_fixtures import accepted_payload, checkpoint_record, response_record, work_records

from ultrakb.compile_checkpoints import digest

pytest_plugins = ("compile_work_fixtures",)


@pytest.mark.asyncio
async def test_resume_revalidates_saved_third_summary_before_sending_the_fourth(
    kb_dir, compile_work
):
    from ultrakb.agent.document_summary import (
        build_document_summary,
        summary_messages,
        window_sections,
    )
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.locks import atomic_write_text

    atomic_write_text(
        kb_dir / "wiki/sources/guide.md",
        "part1=KEEP\n\npart2=KEEP\n\npart3\u00a0=\u00a03\n\npart4=KEEP\n\n",
    )
    checkpoint = compile_work({"document": "guide"}, {})
    windows = CompileSourceWindows(kb_dir / "wiki", "guide", navigation=[])
    system = {"role": "system", "content": "Explain original conditions."}

    def fits(messages):
        return len(json.loads(messages[1]["content"].split("\n", 1)[1])["content"]) <= 15

    batches = list(windows.iter_document_windows(fits=lambda window: len(window["content"]) <= 15))
    assert len(batches) == 4
    requests = [
        summary_messages(system, "guide", batch, structured=True, request_format=3)
        for batch in batches
    ]

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
    accepted = {key: accepted_payload(checkpoint, key) for key in ("summary:1", "summary:2")}
    third_digest = digest(requests[2])
    checkpoint.claim_repair("summary:3", third_digest)
    checkpoint.record_invalid_response(
        "summary:3",
        third_digest,
        json.dumps(
            {
                "description": "Retained original",
                "sections": [
                    {
                        "id": section["id"],
                        "parts": [{"kind": "prose", "text": "```text\npart3 = 3\n```", "refs": []}],
                    }
                    for section in window_sections(batches[2])
                ],
            }
        ),
        stage="document-summary:3",
        response_protocol="summary-parts-json",
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
    assert "part3 = 3" in restored["sections"][0]["parts"][0]["text"]
    assert {key: accepted_payload(checkpoint, key) for key in accepted} == accepted
    assert checkpoint_record(checkpoint, "summary:3")["repair_used"] is True
    assert response_record(checkpoint, "summary:3")[1]["response_protocol"] == "summary-parts-json"


@pytest.fixture
def captured_summary(compile_work):
    checkpoint = compile_work({"document": "guide"}, {})
    signature = digest("same-request")
    checkpoint.claim_repair("summary:3", signature)
    raw = '{"description":"Guide","sections":[{"id":"s1","content":"Retained fact"}]}'
    reference = checkpoint.record_invalid_response(
        "summary:3",
        signature,
        raw,
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("s1",),
        code="page_literal_conflict",
        detail="Previous validator",
        attempt=1,
    )
    return checkpoint, signature, reference


@pytest.mark.parametrize(
    "damage", ["body", "request_digest", "expected_sections", "stage", "protocol"]
)
def test_saved_response_integrity_and_identity_cannot_be_bypassed(captured_summary, damage):
    from ultrakb.storage.blobs import blob_path
    from ultrakb.storage.coordination import storage_stage

    checkpoint, signature, reference = captured_summary
    options = {"protocol": "summary-json", "sections": ("s1",), "stage": "document-summary:3"}
    if damage == "body":
        with storage_stage(checkpoint.root, checkpoint.identity, read_only=True) as db:
            content = db.connection.execute(
                "SELECT content_digest FROM ukb_compile_responses WHERE response_id = ?",
                (reference.removeprefix("compile-response/"),),
            ).fetchone()[0]
        blob_path(checkpoint.root, content).write_bytes(b"Changed content")
    elif damage == "request_digest":
        signature = digest("different-request")
    elif damage == "expected_sections":
        options["sections"] = ("different",)
    else:
        options[damage] = "different"
    before = work_records(checkpoint)
    with pytest.raises(ValueError):
        checkpoint.retained_response("summary:3", signature, **options)
    assert work_records(checkpoint) == before


def test_truncated_saved_output_is_not_reused_even_if_its_json_is_complete(captured_summary):
    checkpoint, signature, _ = captured_summary
    raw, _ = response_record(checkpoint, "summary:3")
    checkpoint.record_invalid_response(
        "summary:3",
        signature,
        raw,
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("s1",),
        code="page_truncated_response",
        detail="Incomplete delivery",
        attempt=1,
    )
    assert (
        checkpoint.retained_response(
            "summary:3",
            signature,
            protocol="summary-json",
            sections=("s1",),
            stage="document-summary:3",
        )
        is None
    )


def test_still_invalid_saved_output_does_not_reset_the_repair_gate(captured_summary):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.retained_summary import reuse_saved_summary

    checkpoint, signature, _ = captured_summary
    checkpoint.record_invalid_response(
        "summary:3",
        signature,
        '{"description":"Guide","sections":"invalid"}',
        stage="document-summary:3",
        response_protocol="summary-json",
        expected_sections=("s1",),
        code="summary_invalid_shape",
        detail="Invalid sections",
        attempt=1,
    )
    before = work_records(checkpoint)
    assert (
        reuse_saved_summary(
            checkpoint,
            "summary:3",
            signature,
            {"content": "Retained fact", "supplementary_context": []},
            protocol="summary-json",
            section_ranges=[{"id": "s1", "start": 0, "end": 13}],
        )
        is None
    )
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        checkpoint.claim_generation("summary:3", signature, stage="compile.document-summary:3")
    assert work_records(checkpoint) == before
