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


def test_title_candidates_distinguish_applicability_and_manual_revision(kb_dir, pdf_imports):
    from openkb.application.views import list_views
    from openkb.source_catalog import read_record, read_source
    from openkb.view_records import VersionAnnotation

    run, _ = pdf_imports
    result = run("guide.pdf", title="WinStack V9.4 Installation Manual (Document R2)")
    annotation = read_record(
        kb_dir,
        "annotations",
        read_source(kb_dir, result.source_id).annotation_id,
        VersionAnnotation,
    )
    assert annotation.metadata.product == "WinStack"
    assert annotation.metadata.applicable_versions == ("9.4",)
    assert annotation.metadata.document_revision == "R2"
    assert "pdf.metadata.title" in annotation.evidence["applicable_versions"]
    selected = next(view for view in list_views(kb_dir) if view.view_id == result.units[0].view_id)
    assert selected.unknown_source_id is None


def test_conflicting_titles_wait_for_a_user_correction(kb_dir, pdf_imports):
    from openkb.application.version_review import (
        list_version_reviews,
        resume_version_review,
        supplement_version_reviews,
    )
    from openkb.source_catalog import read_record, read_source
    from openkb.view_records import SourceMetadata, VersionAnnotation

    run, calls = pdf_imports
    run("first.pdf", title="WinStack V9.3 Installation Manual")
    result = run(
        "second.pdf",
        title="WinStack V9.4 Installation Manual",
        body="WinStack V9.5 Installation Manual",
    )
    assert result.status == "blocked" and len(calls) == 2
    pending = list_version_reviews(kb_dir)[0]
    assert {item.values for item in pending.candidates if item.field == "applicable_versions"} == {
        ("9.4",),
        ("9.5",),
    }
    supplement_version_reviews(
        kb_dir, {pending.review_id: SourceMetadata(applicable_versions=("9.4",))}
    )
    resumed = resume_version_review(kb_dir, pending.review_id)
    assert resumed.status == "added"
    annotation = read_record(
        kb_dir,
        "annotations",
        read_source(kb_dir, resumed.source_id).annotation_id,
        VersionAnnotation,
    )
    assert annotation.metadata.applicable_versions == ("9.4",)
    assert annotation.evidence["applicable_versions"] == "user"
    assert "pdf.metadata.title" in annotation.evidence["product"]


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


@pytest.mark.parametrize("entrypoint", ["cli", "api", "worker"])
def test_version_review_entries_resume_the_same_retained_source(
    kb_dir, pdf_imports, entrypoint, monkeypatch
):
    from openkb.application.version_review import list_version_reviews, supplement_version_reviews
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    run, _ = pdf_imports
    metadata = SourceMetadata(product="WinStack", family="installation")
    run("first.pdf", metadata=metadata)
    pending_source = run("second.pdf", metadata=metadata)
    pending = list_version_reviews(kb_dir)[0]
    if entrypoint == "cli":
        from click.testing import CliRunner

        from openkb.cli import cli

        runner = CliRunner()
        prefix = ["--kb-dir", str(kb_dir), "versions"]
        result = runner.invoke(
            cli,
            prefix
            + ["supplement", pending.review_id, "--metadata", '{"applicable_versions":["9.4"]}'],
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, prefix + ["resume", pending.review_id])
        assert result.exit_code == 0, result.output
    elif entrypoint == "api":
        from fastapi.testclient import TestClient

        from openkb.api import create_app

        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda name: kb_dir)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/version-reviews/supplement",
                json={"updates": {pending.review_id: {"applicable_versions": ["9.4"]}}},
            )
            assert response.status_code == 200, response.text
            response = client.post(
                "/api/v1/version-review/resume", json={"review_id": pending.review_id}
            )
            assert response.status_code == 200 and response.json()["status"] == "added", (
                response.text
            )
    else:
        from openkb.application.execution import ExecutionContext
        from openkb.runtime.requests import ResumeVersionReview
        from openkb.runtime.worker import _execute

        supplement_version_reviews(
            kb_dir, {pending.review_id: SourceMetadata(applicable_versions=("9.4",))}
        )
        result = _execute(
            ResumeVersionReview(pending.review_id),
            SimpleNamespace(kb_dir=str(kb_dir)),
            ExecutionContext(),
        )
        assert result.status == "completed"
    assert (
        "retained instructions" in read_document_source(kb_dir, pending_source.source_id)["content"]
    )


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


def test_cli_partial_metadata_preserves_other_title_candidates(kb_dir, pdf_imports):
    from click.testing import CliRunner

    from openkb.cli import cli
    from openkb.source_catalog import read_record, read_source
    from openkb.view_records import VersionAnnotation

    run, _ = pdf_imports
    result = run("manual.pdf", title="WinStack V9.4 Installation Manual")
    outcome = CliRunner().invoke(
        cli,
        ["--kb-dir", str(kb_dir), "add", str(kb_dir / "manual.pdf"), "--document-revision", "R2"],
    )
    assert outcome.exit_code == 0, outcome.output
    annotation = read_record(
        kb_dir,
        "annotations",
        read_source(kb_dir, result.source_id).annotation_id,
        VersionAnnotation,
    )
    assert annotation.metadata.product == "WinStack"
    assert annotation.metadata.applicable_versions == ("9.4",)
    assert annotation.evidence["product"].startswith("pdf.metadata.title")


def test_conflicting_product_candidates_still_find_the_related_series(kb_dir, pdf_imports):
    from openkb.application.version_review import list_version_reviews

    run, calls = pdf_imports
    run("first.pdf", title="WinStack V9.3 Installation Manual")
    result = run(
        "ambiguous.pdf",
        title="WinStack V9.4 Installation Manual",
        body="WinSphere V9.4 Installation Manual",
    )
    assert result.status == "blocked" and len(calls) == 2
    assert "product" in list_version_reviews(kb_dir)[0].missing_fields


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
