"""Version ambiguity is a durable, resumable application outcome."""

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def pdf_imports(kb_dir, monkeypatch):
    import pymupdf

    from openkb.application.documents import import_document

    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        value = (
            {"description": "Manual", "content": "Installation knowledge."}
            if len(calls) % 2
            else {}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.completion", complete)

    def run(
        name,
        *,
        title="Installation manual",
        metadata=None,
        body="Install from the retained instructions.",
    ):
        path = kb_dir / name
        with pymupdf.open() as pdf:
            pdf.set_metadata({"title": title})
            pdf.new_page().insert_text((72, 72), body)
            pdf.save(path)
        return import_document(kb_dir, path, metadata=metadata)

    return run, calls


def test_later_ambiguous_manual_waits_and_resumes_from_retained_input(kb_dir, pdf_imports):
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.config import load_config, save_config
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    run, calls = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    first = run("first.pdf", metadata=metadata)
    second = run("second.pdf", metadata=metadata)
    assert first.status == "added" and second.status == "blocked"
    assert len(calls) == 2
    (kb_dir / "second.pdf").unlink()
    pending = list_version_reviews(kb_dir)
    assert len(pending) == 1 and "applicable_versions" in pending[0].missing_fields
    assert pending[0].related_sources[0].name == "first.pdf"
    assert pending[0].related_sources[0].metadata.product == "WinStack"
    supplement_version_reviews(
        kb_dir, {pending[0].review_id: SourceMetadata(applicable_versions=("9.4",))}
    )
    config_path = kb_dir / ".openkb/config.yaml"
    save_config(config_path, {**load_config(config_path), "pageindex_threshold": 1})
    resumed = resume_version_review(kb_dir, pending[0].review_id)
    assert resumed.status == "added" and resumed.source_id == second.source_id
    assert len(calls) == 4
    assert (
        "Install from the retained instructions"
        in read_document_source(kb_dir, resumed.source_id)["content"]
    )
    assert list_version_reviews(kb_dir) == ()


def test_cancelled_clarification_does_not_resume_on_another_import(kb_dir, pdf_imports):
    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        cancel_version_review,
        list_version_reviews,
        read_version_review,
        resume_version_review,
    )
    from openkb.view_records import SourceMetadata

    run, calls = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    run("first.pdf", metadata=metadata)
    run("second.pdf", metadata=metadata)
    pending = list_version_reviews(kb_dir)[0]
    cancel_version_review(kb_dir, pending.review_id)
    assert read_version_review(kb_dir, pending.review_id).status == "cancelled"
    replay = import_document(kb_dir, kb_dir / "second.pdf", metadata=metadata)
    assert replay.status == "blocked" and len(calls) == 2
    with pytest.raises(ValueError, match="ready"):
        resume_version_review(kb_dir, pending.review_id)


def test_failed_resume_can_be_explicitly_retried_after_restart(kb_dir, pdf_imports, monkeypatch):
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    run("first.pdf", metadata=metadata)
    run("second.pdf", metadata=metadata)
    pending = list_version_reviews(kb_dir)[0]
    supplement_version_reviews(
        kb_dir, {pending.review_id: SourceMetadata(applicable_versions=("9.4",))}
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "litellm.completion", lambda **kwargs: (_ for _ in ()).throw(ConnectionError("offline"))
        )
        failed = resume_version_review(kb_dir, pending.review_id)
    assert failed.status == "failed"
    resumed = resume_version_review(kb_dir, pending.review_id)
    assert resumed.status == "added"
    assert resumed.source_revision_id == failed.source_revision_id


def test_confirming_a_completed_unknown_source_preserves_its_original_view(kb_dir, pdf_imports):
    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.application.views import view_scope
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    first = run("unknown.pdf")
    old = view_scope(kb_dir, first.units[0].view_id)
    original = read_document_source(kb_dir, first.source_id, scope=old)
    review = review_source_version(kb_dir, first.source_id, scope=old)
    supplement_version_reviews(
        kb_dir,
        {review.review_id: SourceMetadata(product="WinStack", applicable_versions=("9.4",))},
    )
    resumed = resume_version_review(kb_dir, review.review_id, scope=old)
    assert resumed.status == "added" and resumed.units[0].view_id != old.view_id
    assert read_document_source(kb_dir, first.source_id, scope=old) == original


def test_replaced_pending_input_is_no_longer_offered_for_resume(kb_dir, pdf_imports):
    from openkb.application.version_review import list_version_reviews, read_version_review
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    run("first.pdf", metadata=metadata)
    run("second.pdf", metadata=metadata)
    pending = list_version_reviews(kb_dir)[0]
    replacement = run("second.pdf", title="WinStack V9.4 Installation Manual")
    assert replacement.status == "added"
    assert list_version_reviews(kb_dir) == ()
    assert read_version_review(kb_dir, pending.review_id).status == "superseded"


def test_metadata_can_be_corrected_again_after_a_failed_resume(kb_dir, pdf_imports, monkeypatch):
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    run("first.pdf", metadata=metadata)
    run("second.pdf", metadata=metadata)
    pending = list_version_reviews(kb_dir)[0]
    supplement_version_reviews(
        kb_dir, {pending.review_id: SourceMetadata(applicable_versions=("9.4",))}
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "litellm.completion", lambda **kwargs: (_ for _ in ()).throw(ConnectionError("offline"))
        )
        assert resume_version_review(kb_dir, pending.review_id).status == "failed"
    supplement_version_reviews(
        kb_dir, {pending.review_id: SourceMetadata(applicable_versions=("9.5",))}
    )
    assert resume_version_review(kb_dir, pending.review_id).status == "added"


def test_pre_clarification_source_reuses_its_published_conversion(kb_dir, pdf_imports):
    from pathlib import Path
    from zipfile import ZipFile

    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.config import load_config, save_config
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    with ZipFile(Path(__file__).parent / "fixtures/unknown-view-c0bf3c5.zip") as fixture:
        fixture.extractall(kb_dir)
    identity = "6fbed19fc49948b0b59af752283ed208"
    original = read_document_source(kb_dir, identity)["content"]
    config_path = kb_dir / ".openkb/config.yaml"
    save_config(config_path, {**load_config(config_path), "pageindex_threshold": 1})
    review = review_source_version(kb_dir, identity)
    supplement_version_reviews(
        kb_dir, {review.review_id: SourceMetadata(product="WinStack", applicable_versions=("9.4",))}
    )
    result = resume_version_review(kb_dir, review.review_id)
    assert result.status == "added"
    assert read_document_source(kb_dir, identity)["content"] == original


def test_clarification_rejects_a_corrupt_view_binding(kb_dir, pdf_imports):
    from openkb.application.version_review import list_version_reviews, read_version_review
    from openkb.application.views import view_scope
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    first = run("first.pdf", metadata=metadata)
    run("second.pdf", metadata=metadata)
    review = list_version_reviews(kb_dir)[0]
    path = kb_dir / f".openkb/catalog/version-reviews/{review.review_id}.json"
    corrupt = json.loads(path.read_text())
    corrupt["view_id"] = first.units[0].view_id
    path.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="view"):
        read_version_review(
            kb_dir, review.review_id, scope=view_scope(kb_dir, first.units[0].view_id)
        )
