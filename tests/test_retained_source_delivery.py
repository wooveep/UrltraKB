"""Every original-reading route reports the retained processing state."""

from dataclasses import replace

import pytest
from test_context_retrieval import executor_for, selection

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("reader", ["lexical", "semantic"])
def test_search_delivers_retained_status_with_native_evidence(kb_dir, model_service, reader):
    from ultrakb.agent.evidence_search import search_originals
    from ultrakb.config_state import capture_config
    from ultrakb.context.knowledge import KnowledgeRetrieval
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.model_tokenizer import question_measure

    path = "sources/operations.md"
    text = "The pump must stop before the inspection valve opens."
    atomic_write_text(kb_dir / "wiki" / path, text)
    captured = selection(kb_dir)
    status = {
        "processing_status": "retained_before_reprocessing",
        "target_source_revision_id": "b" * 32,
        "interpretation": "Retained processing result; same-original reprocessing is unfinished.",
    }
    captured = replace(
        captured,
        views=(replace(captured.views[0], retained_source_status={path: status}),),
    )
    if reader == "lexical":
        result = search_originals(captured, ["pump"])
        assert result["status"] == "matches"
    else:
        executor = executor_for(model_service)
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            executor.question_capacity = question_measure(capture_config(kb_dir))
        search = KnowledgeRetrieval(captured, executor)

        result = search.search("When may the inspection valve open?")
        assert result["status"] == "completed", result
        assert 1 <= len(model_service.requests) <= 4
        sent = len(model_service.requests)
        hit = result["hits"][0]
        assert text in hit["content"]
        assert all(hit.get(k) == v for k, v in status.items())
        cached = search.search("When may the inspection valve open?")
        assert cached["cache_hit"]
        assert len(model_service.requests) == sent
        assert cached["hits"][0]["processing_status"] == status["processing_status"]

    context = result["hits"][0]["source_context"]
    assert all(context.get(k) == v for k, v in status.items())
    assert text in context["regions"][0]["nearby_original"]
