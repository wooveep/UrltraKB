"""Real document conversion and storage with a deterministic external model."""

import json
from types import SimpleNamespace

import pytest
from processing_fixtures import configure_processing

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
    configure_processing(kb_dir)
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


@pytest.mark.parametrize("extension", [".md", ".markdown"])
def test_import_freezes_relative_images_at_the_business_boundary(
    kb_dir, tmp_path, monkeypatch, extension
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext

    source = tmp_path / f"中文 图解{extension}"
    figure = tmp_path / "图.png"
    figure.write_bytes(b"original image")
    source.write_text("# 图解\n![first](图.png)\n![again](图.png)\n![missing](later.png)")
    original = source.read_bytes()

    def after_start(snapshot):
        source.write_text("changed source")
        figure.write_bytes(b"changed image")
        (tmp_path / "later.png").write_bytes(b"created after start")

    values = iter(
        [
            {"description": "Notes", "content": "# Notes"},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(values))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        ),
    )
    result = import_document(kb_dir, source, context=ExecutionContext(on_snapshot=after_start))
    assert result.status == "added"
    assert next((kb_dir / "raw").iterdir()).read_bytes() == original
    images = list((kb_dir / "wiki/sources/images").rglob("*.png"))
    assert len(images) == 1 and images[0].read_bytes() == b"original image"
    converted = next((kb_dir / "wiki/sources").glob("*.md")).read_text()
    assert converted.count("图.png)") == 2 and "![missing](later.png)" in converted


def test_import_refreshes_images_changed_while_waiting_for_the_lease(kb_dir, tmp_path, monkeypatch):
    import threading

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    source = tmp_path / "notes.md"
    source.write_text("![figure](figure.png)")
    figure = tmp_path / "figure.png"
    figure.write_bytes(b"old")
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with kb_ingest_lock(kb_dir / ".openkb"):
            acquired.set()
            assert release.wait(10)

    def waiting(event):
        if event["stage"] == "waiting":
            figure.write_bytes(b"latest")
            release.set()

    values = iter(
        [
            {"description": "Notes", "content": "# Notes"},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(values))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        ),
    )
    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(10)
    try:
        result = import_document(kb_dir, source, context=ExecutionContext(on_event=waiting))
    finally:
        release.set()
        holder.join(10)
    assert result.status == "added"
    assert next((kb_dir / "wiki/sources/images").rglob("*.png")).read_bytes() == b"latest"


def test_watched_source_replaced_with_external_symlink_while_waiting_never_starts(kb_dir, tmp_path):
    import os
    import threading

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    source = kb_dir / "raw/notes.md"
    source.write_text("approved input")
    outside = tmp_path / "outside.md"
    outside.write_text("outside raw")
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with kb_ingest_lock(kb_dir / ".openkb"):
            acquired.set()
            assert release.wait(10)

    def waiting(event):
        if event["stage"] == "waiting":
            source.unlink()
            source.symlink_to(outside)
            release.set()

    context = ExecutionContext(on_event=waiting)
    holder = threading.Thread(target=hold)
    holder.start()
    assert acquired.wait(5)
    try:
        with pytest.raises(ValueError, match="Watched input"):
            import_document(kb_dir, source, source_root=kb_dir / "raw", context=context)
    finally:
        release.set()
        holder.join(5)
    assert context.snapshot is None
    assert outside.read_text() == "outside raw"
    assert not list((kb_dir / "wiki/summaries").iterdir())


def test_import_keeps_frozen_identity_when_original_path_changes_after_start(kb_dir, monkeypatch):
    import os

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext
    from openkb.state import HashRegistry

    if os.name == "nt":
        pytest.skip("POSIX symlink fixture")
    source = kb_dir / "notes.md"
    source.write_text("# Original prepared input")
    digest = HashRegistry.hash_file(source)
    other = kb_dir / "other.md"
    other.write_text("# Another document")
    registry = HashRegistry(kb_dir / ".openkb/hashes.json")
    registry.add(
        HashRegistry.hash_file(other), {"name": "other.md", "doc_name": "other", "path": "other.md"}
    )
    summary = kb_dir / "wiki/summaries/other.md"
    summary.write_text("# Keep this unrelated page")

    def after_start(snapshot):
        source.unlink()
        source.symlink_to(other)

    values = iter(
        [
            {"description": "Notes", "content": "# Notes"},
            {"create": [], "update": [], "related": []},
        ]
    )
    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(values))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        ),
    )
    result = import_document(kb_dir, source, context=ExecutionContext(on_snapshot=after_start))
    assert result.status == "added"
    saved = HashRegistry(kb_dir / ".openkb/hashes.json").get(digest)
    assert saved["path"] == "notes.md"
    assert saved["doc_name"] == "notes"
    assert (kb_dir / "raw/notes.md").read_text() == "# Original prepared input"
    assert summary.read_text() == "# Keep this unrelated page"
