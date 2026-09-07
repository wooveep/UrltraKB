"""Cloud ingestion uses the same execution and committed-resource contract."""

from pathlib import Path

import pytest


@pytest.mark.parametrize("log_failure", [False, True])
def test_cloud_import_keeps_execution_settings_and_rawless_resources(
    kb_dir, monkeypatch, log_failure
):
    import os

    from openkb.application.cloud import import_cloud
    from openkb.application.execution import ExecutionContext
    from openkb.config import resolve_effective_config
    from openkb.locks import kb_ingest_lock_held
    from openkb.state import HashRegistry

    settings = kb_dir / ".openkb/config.yaml"
    settings.write_text("model: openai/original\n")
    (kb_dir / ".env").write_text("PAGEINDEX_API_KEY=fixture-cloud-key\n")
    environment = dict(os.environ)
    compiled = []

    class Cloud:
        def __init__(self, *, api_key):
            assert api_key == "fixture-cloud-key"
            assert kb_ingest_lock_held(kb_dir / ".openkb")

        def collection(self):
            return self

        def get_document(self, doc_id, **kwargs):
            settings.write_text("model: openai/later\n")
            return {"doc_name": "Cloud Paper.pdf", "structure": [], "doc_description": "desc"}

        def get_page_content(self, doc_id, pages):
            return [{"page": 1, "content": "Cloud page", "images": []}]

    async def compile_document(name, source, doc_id, root, model, **kwargs):
        compiled.append((model, resolve_effective_config(root)[0]["model"]))

    monkeypatch.setattr("openkb.indexer.PageIndexClient", Cloud)
    monkeypatch.setattr("openkb.application.cloud.compile_long_doc", compile_document)
    if log_failure:

        def fail_log(*args):
            raise OSError("log disk unavailable")

        monkeypatch.setattr("openkb.application.cloud.append_log", fail_log)
    result = import_cloud(kb_dir, "cloud-1", context=ExecutionContext())
    assert result.status == "added"
    assert compiled == [("openai/original", "openai/original")]
    assert {Path(path).name for path in result.resources} == {
        "Cloud-Paper.json",
        "Cloud-Paper.md",
    }
    entry = next(iter(HashRegistry(kb_dir / ".openkb/hashes.json").all_entries().values()))
    assert entry["path"] == "pageindex-cloud:cloud-1"
    assert "raw_path" not in entry
    assert result.unfinished == (("ingest-log",) if log_failure else ())
    assert dict(os.environ) == environment


def test_cloud_duplicate_accepts_legacy_registry_without_resource_paths(kb_dir):
    import hashlib

    from openkb.application.cloud import import_cloud
    from openkb.state import HashRegistry

    digest = hashlib.sha256(b"pageindex-cloud:cloud-old").hexdigest()
    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        digest, {"name": "Old paper", "type": "pageindex_cloud", "doc_id": "cloud-old"}
    )
    result = import_cloud(kb_dir, "cloud-old")
    assert result.status == "skipped"
    assert result.resources == ()
