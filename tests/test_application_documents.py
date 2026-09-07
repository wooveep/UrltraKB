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
