"""Real imports with unchanged production generation and separately bounded test execution."""

import hashlib
import json

import pytest


@pytest.mark.live
@pytest.mark.parametrize("mode", ["short", "indexed", "truncated"])
def test_import_uses_production_generation_and_keeps_incomplete_output_private(
    tmp_path, monkeypatch, production_llm, mode
):
    import pymupdf
    from native_catalog_fixtures import catalog, original_bytes, publication

    from ultrakb import config
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.documents import read_document_source
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "profile/global.yaml")
    source = tmp_path / "reading.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "The indoor room permits quiet reading.")
        pdf.save(source)
    original = source.read_bytes()
    kb = tmp_path / "kb"
    initialize_kb(
        kb,
        model="deepseek/deepseek-flash",
        api_key=production_llm.credentials.api_key,
        openai_api_base=production_llm.credentials.base_url,
        seed_environment=False,
    )
    values = config.load_config(kb / ".ultrakb/config.yaml")
    assert "role_generation" not in values
    values.update(
        language="en",
        model_capacity=template_capacity(CONNECTION_TEMPLATES[2]),
        # These constrain the test execution, independently of generation.
        model_policy={"max_calls": 16, "concurrency": 1, "retries": 0, "deadline_seconds": 240},
    )
    if mode == "indexed":
        values["pdf_short_max_pages"] = 0
    elif mode == "truncated":
        # A complete page cannot fit one output token, even without reasoning.
        values["role_generation"] = {"compile": {"max_tokens": 1}}
    config.save_config(kb / ".ultrakb/config.yaml", values)
    before = len(production_llm.requests)
    head = publication(kb)
    manager = TaskManager(history_dir=tmp_path / "tasks", max_workers=1)
    try:
        task_id = manager.submit(kb, [ImportFile(str(source))])
        task = manager.wait(task_id, timeout=250)
        assert task.processes_reaped
        [result] = task.results
        [unit] = result.import_units
        assert source.read_bytes() == original
        assert result.model_usage["current"]["usage_complete"]
        assert result.model_usage["current"]["in_flight_requests"] == 0
        requests = production_llm.requests[before:]
        assert requests
        replies = []
        for request in requests:
            payload, _ = production_llm._payload(request)
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
            # Inspect the unchanged real provider receipt, including cache reuse.
            replies.append(production_llm._cached_response(fingerprint))
        assert all(isinstance(reply, dict) and reply.get("usage") for reply in replies)
        assert any(
            reply["usage"].get("completion_tokens_details", {}).get("reasoning_tokens", 0) > 0
            for reply in replies
        )
        assert all(request.get("thinking") is None for request in requests)
        assert all(request.get("reasoning_effort") is None for request in requests)
        if mode == "truncated":
            assert task.state == result.status == "failed", result.error
            assert "page_truncated_response" in result.error
            assert len(requests) == 2 and all(request["max_tokens"] == 1 for request in requests)
            assert all(reply["choices"][0]["finish_reason"] == "length" for reply in replies)
            assert publication(kb) == head
            assert unit.knowledge_revision_id is None
            assert not list((kb / "wiki/summaries").glob("reading.md"))
        else:
            assert task.state == result.status == "completed", result.error
            # This template explicitly reserves the provider's 65536-token output
            # allowance within its context window; native admission sends that cap.
            assert all(request.get("max_tokens") == 65536 for request in requests)
            assert unit.knowledge_revision_id and not result.unfinished
            assert publication(kb).revision_id != head.revision_id
            source_id = result.model_usage["source_ids"][0]
            saved = read_document_source(kb, source_id, pages="1")
            assert "indoor room permits quiet reading" in saved["content"]
            assert original_bytes(kb, saved["source_revision_id"]) == original
            from ultrakb.storage.usage_store import UsageStore

            ledger = UsageStore(kb, catalog(kb).identity)
            records = [
                record
                for execution in result.model_usage["execution_ids"]
                for record in ledger.requests(execution_id=execution).records
            ]
            assert any(record.stage.startswith("compile.") for record in records)
            assert any(record.stage.startswith("document_index.") for record in records) == (
                mode == "indexed"
            )
            assert result.model_usage["current"]["output_total"] > 0
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
