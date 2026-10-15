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

    from ultrakb import config
    from ultrakb.application.connection_capacity import template_capacity
    from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.documents import read_document_source
    from ultrakb.model_task import read_task
    from ultrakb.runtime.requests import ImportFile
    from ultrakb.runtime.tasks import TaskManager
    from ultrakb.unit_publication import read_head

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
    head = read_head(kb)
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
        [journal] = result.model_usage["model_tasks"]
        captured = read_task(kb, journal["task_id"]).bindings["role_generation"]
        if mode == "truncated":
            assert task.state == result.status == "failed", result.error
            assert "page_truncated_response" in result.error
            assert captured == {"compile": {"max_tokens": 1}}
            assert len(requests) == 2 and all(request["max_tokens"] == 1 for request in requests)
            assert all(reply["choices"][0]["finish_reason"] == "length" for reply in replies)
            assert read_head(kb) == head
            assert unit.knowledge_revision_id is None
            assert not list((kb / "wiki/summaries").glob("reading.md"))
        else:
            assert task.state == result.status == "completed", result.error
            assert captured == {}
            assert all(request.get("max_tokens") is None for request in requests)
            assert unit.knowledge_revision_id and not result.unfinished
            assert read_head(kb).knowledge_revision_id != head.knowledge_revision_id
            source_id = result.model_usage["source_ids"][0]
            saved = read_document_source(kb, source_id, pages="1")
            assert "indoor room permits quiet reading" in saved["content"]
            assert (kb / saved["original_path"]).read_bytes() == original
            stages = result.model_usage["current"]["stages"]
            assert any(stage.startswith("compile.") for stage in stages)
            assert any(stage.startswith("document_index.") for stage in stages) == (
                mode == "indexed"
            )
            assert result.model_usage["current"]["output_total"] > 0
    finally:
        manager.shutdown(stop=True)
        assert manager.join(10)
