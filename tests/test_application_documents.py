"""Real document conversion and storage with a deterministic external model."""

import json
from types import SimpleNamespace

from openkb.application.pages import read_page


def test_import_document_compiles_and_deduplicates(kb_dir, tmp_path, monkeypatch):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list

    responses = iter(
        [
            {"description": "Notes", "content": "# Notes\n\nCompiled knowledge."},
            {"create": [], "update": [], "related": []},
        ]
    )

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    source = tmp_path / "notes.md"
    source.write_text("# Notes\nOriginal knowledge.")
    events = []
    result = import_document(kb_dir, source, on_event=events.append)
    assert result.status == "added"
    assert read_page(kb_dir, "summaries/notes").body.strip() == "# Notes\n\nCompiled knowledge."
    assert get_kb_list(kb_dir)["document_count"] == 1
    assert import_document(kb_dir, source).status == "skipped"
    assert [event["stage"] for event in events] == ["converting", "compiling", "committed"]


def test_import_uses_one_configuration_snapshot_across_model_calls(kb_dir, monkeypatch):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.config import resolve_effective_config

    configuration = kb_dir / ".openkb/config.yaml"
    configuration.write_text(
        "model: openai/initial\nlanguage: en\nextra_headers:\n  X-Profile: initial\n"
    )
    (kb_dir / ".env").write_text("LLM_API_KEY=initial-private-key\n")
    source = kb_dir / "notes.md"
    source.write_text("# Notes\nOriginal knowledge.")
    calls = []
    values = iter(
        [
            {"description": "Notes", "content": "# Notes\n\nCompiled knowledge."},
            {"create": [], "update": [], "related": []},
        ]
    )

    def completion(**kwargs):
        calls.append(kwargs)
        # An external editor changes files while this task is running. The
        # complete operation must continue using the version fixed at start.
        configuration.write_text("model: openai/changed\nlanguage: zh\n")
        (kb_dir / ".env").write_text("LLM_API_KEY=changed-private-key\n")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(values))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(litellm, "completion", completion)
    context = ExecutionContext()
    result = import_document(kb_dir, source, context=context)
    assert result.status == "added"
    assert len(calls) == 2
    assert all(call["model"] == "openai/initial" for call in calls)
    assert all(call["api_key"] == "initial-private-key" for call in calls)
    assert all(call["extra_headers"]["X-Profile"] == "initial" for call in calls)
    assert context.snapshot is not None
    assert "private-key" not in repr(context)
    assert "private-key" not in repr(context.snapshot)
    assert resolve_effective_config(kb_dir)[0]["model"] == "openai/changed"
