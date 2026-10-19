import pytest

from ultrakb.mutation import repair_marker


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (".env", "LLM_API_KEY='unterminated"),
    ],
)
def test_repair_retains_block_for_invalid_credentials_or_registry(kb_dir, filename, content):
    from ultrakb.application.repair import repair_knowledge_base

    (kb_dir / filename).write_text(content)
    repair_marker(kb_dir).write_text("{}")
    result = repair_knowledge_base(kb_dir)
    assert not result.repaired
    assert repair_marker(kb_dir).exists()
    assert content not in str(result)


def test_stale_creation_intent_cannot_bypass_existing_storage_checks(kb_dir):
    import json

    from ultrakb.application.repair import repair_knowledge_base

    (kb_dir / ".ultrakb/initializing.json").write_text(
        json.dumps({"version": 1, "kb_dir": str(kb_dir)})
    )
    (kb_dir / ".ultrakb/config.yaml").unlink()
    repair_marker(kb_dir).write_text("{}")
    assert not repair_knowledge_base(kb_dir).repaired
    assert repair_marker(kb_dir).exists()
