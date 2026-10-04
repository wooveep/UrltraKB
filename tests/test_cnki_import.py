"""Public admission, normalization and readback share one CNKI PDF."""

import json
from types import SimpleNamespace

import pymupdf
import pytest

pytest_plugins = ("cnki_fixtures", "test_pdf_readback")


@pytest.fixture
def conversions(monkeypatch):
    from openkb.cnki.convert import convert_cnki

    calls = []

    def conversion(*args, **kwargs):
        calls.append(args)
        return convert_cnki(*args, **kwargs)

    monkeypatch.setattr("openkb.cnki.convert.convert_cnki", conversion)
    return calls


def test_cnki_preflight_and_publication_use_one_conversion(kb_dir, monkeypatch):
    from openkb.application.documents import import_document
    from openkb.cnki.convert import convert_cnki
    from openkb.documents import read_document_source
    from openkb.state import HashRegistry
    from openkb.view_records import SourceMetadata

    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Original CNKI content.")
        data = pdf.tobytes()
    key = b"FZHMEI"
    source = kb_dir / "中文 空格.CAJ"
    source.write_bytes(
        b"KDH 2.00".ljust(254, b"\0")
        + bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))
    )
    original_digest = HashRegistry.hash_file(source)
    calls = []

    def conversion(*args, **kwargs):
        calls.append(args)
        return convert_cnki(*args, **kwargs)

    monkeypatch.setattr("openkb.cnki.convert.convert_cnki", conversion)
    replies = iter([{"description": "CNKI paper", "content": "Original CNKI content."}, {}])
    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    result = import_document(
        kb_dir,
        source,
        metadata=SourceMetadata(
            product="CNKI research", applicable_versions=("1",), family="paper"
        ),
    )
    assert result.status == "added", result.message
    assert len(calls) == 1
    assert HashRegistry.hash_file(source) == original_digest
    assert not source.with_suffix(".pdf").exists()
    retained = read_document_source(kb_dir, result.source_id, pages="1")
    assert retained["name"] == source.name
    assert retained["cnki"]["internal_format"] == "KDH"
    assert "office" not in retained
    assert "Original CNKI content." in retained["content"]
    assert (kb_dir / retained["internal_pdf_path"]).is_file()
    assert retained["cnki"]["input_digest"] == original_digest
    repeated = import_document(kb_dir, source)
    assert repeated.status == "skipped"
    assert len(calls) == 1


def test_model_retry_and_recompile_reuse_the_retained_conversion(
    kb_dir, cnki_source, conversions, pdf_model, monkeypatch
):
    import asyncio

    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    with monkeypatch.context() as failed:

        def unavailable(**kwargs):
            raise ConnectionError("fixture unavailable model")

        failed.setattr("litellm.completion", unavailable)
        first = import_document(
            kb_dir,
            cnki_source,
            metadata=SourceMetadata(product="CNKI", family="paper", applicable_versions=("1",)),
        )
    assert first.status == "failed", first.message
    assert len(conversions) == 1
    retried = import_document(kb_dir, cnki_source)
    assert retried.status == "added", retried.message
    assert len(conversions) == 1
    source = read_document_source(kb_dir, first.source_id)
    assert source["cnki"]["internal_format"] == "KDH"
    cnki_source.unlink()
    result = asyncio.run(recompile_document(kb_dir, first.source_id))
    assert result.status == "compiled", result.message
    assert len(conversions) == 1


def test_changed_converter_requires_explicit_reprocessing_and_history_keeps_old_pdf(
    kb_dir, cnki_source, conversions, pdf_model, monkeypatch
):
    from openkb.application.documents import import_document
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    first = import_document(
        kb_dir,
        cnki_source,
        metadata=SourceMetadata(product="CNKI", family="paper", applicable_versions=("1",)),
    )
    assert first.status == "added", first.message
    before = read_document_source(kb_dir, first.source_id)
    monkeypatch.setattr("openkb.cnki.runtime.CNKI_POLICY", "cnki-pdf-fixture-next")
    assert import_document(kb_dir, cnki_source).status == "skipped"
    assert len(conversions) == 1
    cnki_source.unlink()
    preview = preview_reprocessing(kb_dir, first.source_id)
    assert preview["status"] == "ready" and preview["units"][0]["policy_changed"]
    assert len(conversions) == 1
    processed = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    assert processed.status == "added", processed.message
    assert len(conversions) == 2
    now = read_document_source(kb_dir, first.source_id)
    history = read_document_source(
        kb_dir, first.source_id, source_revision_id=first.source_revision_id
    )
    assert now["source_revision_id"] != before["source_revision_id"]
    assert now["cnki"]["processing_identity"]["policy"] == "cnki-pdf-fixture-next"
    assert history["cnki"] == before["cnki"]
    assert history["internal_pdf_path"] == before["internal_pdf_path"]
    assert history["content"] == before["content"]
    assert len(conversions) == 2


@pytest.mark.parametrize("failure", ["unsupported", "runtime"])
def test_conversion_failure_retains_original_without_publishing_knowledge(
    kb_dir, cnki_source, monkeypatch, failure
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    if failure == "unsupported":
        cnki_source.write_bytes(b"HN\0\0unsupported container")
        message = "Unsupported CNKI internal format: HN"
    else:
        monkeypatch.setattr("pymupdf.version", ("0.0", "0.0", "fixture"))
        message = "CNKI runtime is unavailable"
    original = cnki_source.read_bytes()
    result = import_document(kb_dir, cnki_source)
    assert result.status == "failed" and message in result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["knowledge_revision_id"] is None
    assert "internal_pdf_path" not in saved and "cnki" not in saved
    assert (kb_dir / saved["original_path"]).read_bytes() == original


def test_pending_recovered_cnki_shares_one_conversion(
    kb_dir, cnki_source, conversions, pdf_model, tmp_path
):
    from zipfile import ZipFile

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources

    outer = tmp_path / "container.docx"
    with ZipFile(outer, "w") as package:
        package.writestr("word/embeddings/Recovered.CAJ", cnki_source.read_bytes())
    parent = import_document(kb_dir, outer)
    assert parent.discovery_pending == 1
    drained = process_pending(kb_dir)
    assert drained["processed"] >= 2
    imported = next(
        source for source in list_sources(kb_dir) if source.source_id != parent.source_id
    )
    source = read_document_source(kb_dir, imported.source_id)
    assert source["name"] == "Recovered.CAJ"
    assert source["cnki"]["internal_format"] == "KDH"
    assert source["knowledge_revision_id"]
    assert len(conversions) == 1
    job = next(item for item in pending_status(kb_dir)["jobs"] if item["kind"] == "import")
    assert job["status"] == "completed"


def test_version_confirmation_uses_the_retained_pdf(kb_dir, cnki_source, conversions, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    first = import_document(kb_dir, cnki_source)
    assert first.status == "added", first.message
    review = review_source_version(kb_dir, first.source_id)
    supplement_version_reviews(
        kb_dir,
        {
            review.review_id: SourceMetadata(
                product="Confirmed research", family="paper", applicable_versions=("2",)
            )
        },
    )
    cnki_source.unlink()
    resumed = resume_version_review(kb_dir, review.review_id)
    assert resumed.status == "added", resumed.message
    assert len(conversions) == 1
    assert read_document_source(kb_dir, first.source_id)["cnki"]["internal_format"] == "KDH"


def test_long_cnki_uses_the_same_physical_pdf_pipeline(
    kb_dir, physical_pdf, pdf_model, conversions, tmp_path
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    key = b"FZHMEI"
    source = tmp_path / "long-paper.caj"
    source.write_bytes(
        b"KDH 2.00".ljust(254, b"\0")
        + bytes(byte ^ key[i % len(key)] for i, byte in enumerate(physical_pdf.read_bytes()))
    )
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": 1})
    )
    cnki = import_document(
        kb_dir,
        source,
        metadata=SourceMetadata(product="CNKI", family="paper", applicable_versions=("1",)),
    )
    direct = import_document(
        kb_dir,
        physical_pdf,
        metadata=SourceMetadata(product="Direct PDF", family="paper", applicable_versions=("1",)),
    )
    assert cnki.status == direct.status == "added", (cnki.message, direct.message)
    assert len(conversions) == 1
    saved = read_document_source(kb_dir, cnki.source_id, pages="1-3")
    pdf = read_document_source(kb_dir, direct.source_id, pages="1-3")
    assert saved["pages"] == pdf["pages"] == 3
    assert saved["length_class"] == pdf["length_class"] == "long"
    assert saved["execution_mode"] == pdf["execution_mode"] == "segmented"
    assert saved["units"][1]["content"] == pdf["units"][1]["content"] == ""
    assert "First section" in saved["content"] and "Last section" in saved["content"]
    assert saved["cnki"]["pages"] == 3
