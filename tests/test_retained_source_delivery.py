"""Every original-reading route reports the retained processing state."""

import re
from dataclasses import replace

import pytest
from test_context_retrieval import executor_for, rank_reply, selection

pytest_plugins = ("test_vendor_sdk",)


@pytest.mark.parametrize("reader", ["lexical", "semantic"])
def test_search_delivers_retained_status_with_native_evidence(kb_dir, model_service, reader):
    from openkb.agent.evidence_search import search_originals
    from openkb.config_state import capture_config
    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.locks import atomic_write_text, kb_ingest_lock
    from openkb.model_tokenizer import question_measure

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
        with kb_ingest_lock(kb_dir / ".openkb"):
            executor.question_capacity = question_measure(capture_config(kb_dir))
        search = KnowledgeRetrieval(captured, executor)

        def rank(request):
            ids = re.findall(r"- id: (\S+)", request["messages"][-1]["content"])
            return rank_reply(ids)

        model_service.replies.extend([(200, rank)] * 3)
        result = search.search("When may the inspection valve open?")
        assert result["status"] == "completed", result
        assert len(model_service.requests) == 1
        hit = result["hits"][0]
        assert text in hit["content"]
        assert all(hit.get(k) == v for k, v in status.items())
        cached = search.search("When may the inspection valve open?")
        assert cached["cache_hit"]
        assert cached["hits"][0]["processing_status"] == status["processing_status"]

    context = result["hits"][0]["source_context"]
    assert all(context.get(k) == v for k, v in status.items())
    assert text in context["regions"][0]["nearby_original"]
