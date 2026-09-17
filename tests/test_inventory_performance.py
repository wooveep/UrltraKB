"""Browsing the catalog does not materialize optional source detail views."""

import pytest

from openkb.application.documents import DocumentResult
from openkb.application.knowledge_bases import get_kb_list
from openkb.application.source_history import record_source_result, source_status
from openkb.inputs import prepared_input
from openkb.sources import SourceStore


@pytest.fixture
def saved_source(kb_dir, tmp_path):
    original = tmp_path / "inventory.txt"
    original.write_text("Required version 7.")
    store = SourceStore(kb_dir)
    with prepared_input(original) as ready:
        source = store.intake(ready)
    record_source_result(
        kb_dir,
        DocumentResult(
            str(original),
            "unfinished",
            (str(store.original(source)),),
            source_id=source.source_id,
            input_version=source.id,
            source_intake="saved",
            knowledge_compilation="unfinished",
            stage="generation",
            reason="request_budget_exhausted",
            resume=source.id,
        ),
    )
    return source


@pytest.mark.parametrize("detail", ["navigation", "cloud", "local_ocr"])
def test_catalog_does_not_load_unrequested_details(kb_dir, saved_source, monkeypatch, detail):
    def unavailable(*args, **kwargs):
        raise ValueError("Optional source detail is unavailable")

    targets = {
        "navigation": "openkb.navigation.read_navigation",
        "cloud": "openkb.application.source_history.cloud_jobs",
        "local_ocr": "openkb.application.source_history.local_ocr_usage",
    }
    monkeypatch.setattr(targets[detail], unavailable)
    result = get_kb_list(kb_dir)
    row = result["documents"][0]
    assert row["source_id"] == saved_source.source_id
    assert row["stage"] == "generation"
    assert row["knowledge_compilation"] == "unfinished"
    assert row["reason"] == "request_budget_exhausted"
    assert row["cumulative_usage"]["runs"] == 1
    # Explicit detail reads still surface the fault; catalog optimization must
    # not turn a damaged detail into a successful validation.
    with pytest.raises(ValueError, match="Optional source detail"):
        source_status(kb_dir, saved_source.source_id)
