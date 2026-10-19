"""Cross-process budgets, ambiguous parent membership and streamed usage regressions."""

import importlib
import json

import pytest


@pytest.mark.asyncio
async def test_unanchored_neighbor_requires_parent_membership_verification(monkeypatch, live_llm):
    from pageindex.index.physical_sections import SectionBoundary, prepare_entries

    live_llm.install(monkeypatch)
    engine = importlib.import_module("pageindex.index.page_index")
    pages = [("1 Gardening\n1.1 Roses\nCare.\n", 10), ("Cooking\n2.1 Sauces\nStir.\n", 10)]
    boundary = SectionBoundary.from_node(
        {
            "title": "Gardening",
            "start_index": 1,
            "end_index": 2,
            "anchor": {"unit": 1, "range": [0, 11]},
        }
    )
    entries = prepare_entries(
        [{"title": "Sauces", "physical_index": 2, "list_index": 0}], pages, boundary=boundary
    )
    calls = []
    original = engine.llm_acompletion

    async def verify(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(engine, "llm_acompletion", verify)
    result = await engine.check_title_appearance(entries[0], pages, boundary=boundary)
    assert result["answer"] == "no"
    assert len(calls) == 1


def test_cancellation_receipt_keeps_final_anthropic_stream_usage(kb_dir):
    import httpx
    from native_usage_fixtures import native_usage_execution, read_requests

    from ultrakb.llm_usage_transport import observe_model_call

    chunks = [
        {
            "type": "message_start",
            "message": {
                "id": "anthropic-final",
                "usage": {"input_tokens": 12, "output_tokens": 1, "cache_read_input_tokens": 3},
            },
        },
        {"type": "message_delta", "usage": {"output_tokens": 27}},
    ]
    body = "\n\n".join("data: " + json.dumps(chunk) for chunk in chunks)
    with native_usage_execution(kb_dir, "cancelled"):
        with observe_model_call("fixture", "answer") as observed:
            identity = observed.sending()
            observed.received(identity, httpx.Response(200, content=body))
            observed.finish(state="cancelled")
    [record] = read_requests(kb_dir)
    assert record.output_total == 27
    assert record.cached_input == 3
    assert record.usage_status == "partial"
    assert record.reasoning_output is None
    assert record.business_state == "cancelled"
