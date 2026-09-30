"""Execution controls are durable and can interrupt work holding the business lease."""

import concurrent.futures
import time

import pytest

pytest_plugins = ("pending_fixtures",)


def test_group_cancel_reaches_running_import_before_business_lease_is_released(
    kb_dir, embedded_docx, office_runtime, pdf_model, monkeypatch
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.pending import cancel_execution_group, pending_status, process_pending
    from openkb.documents import read_document_source

    parent = import_document(kb_dir, embedded_docx)
    process_pending(kb_dir, max_jobs=1)
    group = pending_status(kb_dir)["groups"][0]
    signal = kb_dir / ".openkb/pending-control" / ("group-" + group["root_import_id"] + ".json")
    completion = litellm.completion
    cancelled = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:

        def cancel_during_model(**kwargs):
            if not cancelled:
                cancelled.append(
                    executor.submit(cancel_execution_group, kb_dir, group["root_import_id"])
                )
                deadline = time.monotonic() + 5
                while not signal.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert signal.exists(), "Cancellation waited for the active business lease"
            return completion(**kwargs)

        monkeypatch.setattr(litellm, "completion", cancel_during_model)
        result = process_pending(kb_dir, max_jobs=1)
        assert cancelled
        cancelled[0].result(timeout=10)
    assert result["outcomes"][0]["status"] == "cancelled"
    assert read_document_source(kb_dir, parent.source_id)["status"] == "completed"
    assert pending_status(kb_dir)["runnable"] == 0


@pytest.mark.parametrize(
    "limit, value",
    [
        ("max_depth", 0),
        ("max_object_bytes", 1),
        ("max_total_bytes", 1),
        ("max_decompressed_bytes", 1),
        ("max_discovery_seconds", 0.000001),
    ],
)
def test_budget_wait_preserves_cursor_and_resumes_only_after_explicit_raise(
    kb_dir, embedded_docx, pdf_model, limit, value
):
    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending, update_execution_budget

    import_document(kb_dir, embedded_docx)
    group = pending_status(kb_dir)["groups"][0]
    update_execution_budget(kb_dir, group["root_import_id"], {limit: value})
    process_pending(kb_dir)
    job = pending_status(kb_dir)["jobs"][0]
    assert job["status"] == "budget_wait" and job["cursor"] == 0
    assert process_pending(kb_dir)["processed"] == 0
    update_execution_budget(kb_dir, group["root_import_id"], {limit: group["budget"][limit]})
    assert process_pending(kb_dir, max_jobs=1)["imports_pending"] == 1
