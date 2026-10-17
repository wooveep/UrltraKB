"""One coherent ledger read per live receipt, regardless of history size."""

import pytest


@pytest.mark.parametrize("operation", ["receipt", "merge"])
def test_usage_projection_reads_the_ledger_once(kb_dir, monkeypatch, operation):
    import ultrakb.llm_usage as usage
    from ultrakb.llm_usage_execution import import_usage_execution
    from ultrakb.llm_usage_models import SourceUsageHistory
    from ultrakb.locks import atomic_write_json

    source = "a" * 32
    reads = []
    original = usage.read_requests
    atomic_write_json(
        kb_dir / ".ultrakb/usage/sources" / f"{source}.json",
        SourceUsageHistory(history_status="recorded").model_dump(),
    )

    def read(root):
        reads.append(root)
        return original(root)

    with import_usage_execution(kb_dir, "fixture"), usage.usage_context(source_id=source):
        request_id = usage.begin_request(model="fixture", stage="document_index.summary")
        usage.finish_request(request_id, state="cancelled")
        retained = usage.usage_receipt(kb_dir, source_id=source)
        monkeypatch.setattr(usage, "read_requests", read)
        projected = (
            usage.usage_receipt(kb_dir, source_id=source)
            if operation == "receipt"
            else usage.merge_usage_receipts(kb_dir, [retained, retained])
        )
    assert len(reads) == 1, f"{operation} scanned the full request ledger {len(reads)} times"
    assert projected["current"]["requests"] == projected["cumulative"]["requests"] == 1
    assert projected["current"]["request_ids"] == [request_id]
    assert projected["current"]["unknown_requests"] == 1
