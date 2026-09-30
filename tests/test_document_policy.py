"""Classification and execution remain separate at the public import boundary."""

import pytest

pytest_plugins = ("test_pdf_readback",)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from openkb import config

    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", tmp_path / "settings/.lock")


@pytest.mark.parametrize(
    "page_count,length,mode",
    [(9, "short", "full"), (10, "short", "full"), (11, "long", "segmented")],
)
def test_pdf_default_boundary(kb_dir, physical_pdf, pdf_model, page_count, length, mode):
    import pymupdf

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    with pymupdf.open(physical_pdf) as pdf:
        for _ in range(page_count - 3):
            pdf.new_page(pno=2)
        pdf.saveIncr()
    pdf_model.last_page = page_count
    result = import_document(
        kb_dir,
        physical_pdf,
        metadata=SourceMetadata(product="Boundary", applicable_versions=("1",), family="manual"),
    )
    assert result.status == "added", result.message
    source = read_document_source(kb_dir, result.source_id)
    assert (source["length_class"], source["execution_mode"]) == (length, mode)
    assert source["processing"]["measurement_value"] == page_count


@pytest.mark.parametrize(
    "global_values,kb_values,limit,origin,key,forced",
    [
        (
            {"pdf_short_max_pages": 10},
            {"pageindex_threshold": 4},
            3,
            "kb",
            "pageindex_threshold",
            False,
        ),
        (
            {"pageindex_threshold": 8},
            {"pageindex_threshold": 4, "pdf_short_max_pages": 12},
            12,
            "kb",
            "pdf_short_max_pages",
            False,
        ),
        (
            {"pdf_short_max_pages": 6},
            {"pdf_short_max_pages": None},
            6,
            "global",
            "pdf_short_max_pages",
            False,
        ),
        ({}, {"pageindex_threshold": 0}, 0, "kb", "pageindex_threshold", True),
    ],
)
def test_settings_preserve_pdf_policy_precedence_and_snapshot(
    kb_dir, global_values, kb_values, limit, origin, key, forced
):
    from openkb.application.settings import (
        apply_global_config_patch,
        apply_kb_config_patch,
        read_settings_view,
    )
    from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
    from openkb.config import resolve_effective_config
    from openkb.config_state import capture_config
    from openkb.locks import kb_ingest_lock

    apply_global_config_patch(GlobalConfigPatchRequest(config=global_values))
    apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config=kb_values))
    view = read_settings_view(kb_dir)
    assert (view.values.pdf_short_max_pages, view.sources["pdf_short_max_pages"]) == (limit, origin)
    assert view.values.pdf_limit.key == key and view.values.pdf_limit.legacy_force_index == forced
    with kb_ingest_lock(kb_dir / ".openkb"):
        snapshot = capture_config(kb_dir)
    apply_global_config_patch(GlobalConfigPatchRequest(config={"pdf_short_max_pages": 50}))
    with snapshot.activate():
        assert resolve_effective_config(kb_dir)[0]["pdf_short_max_pages"] == limit


@pytest.mark.parametrize(
    "model,capacity,status,mode,endpoint",
    [
        ("gpt-4o", {"max_input_tokens": 1}, "insufficient", "segmented", None),
        ("openai/private-alias", {"max_input_tokens": 1}, "unknown", "full", None),
        ("gpt-4o", {"context_window_tokens": 1}, "unknown", "full", None),
        ("gpt-4o", {"max_input_tokens": 1}, "unknown", "full", "https://custom.invalid"),
    ],
)
def test_short_pdf_uses_only_known_execution_capacity(
    kb_dir, physical_pdf, pdf_model, model, capacity, status, mode, endpoint
):
    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.config import LlmCredentialBundle
    from openkb.documents import read_document_source
    from openkb.view_records import SourceMetadata

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(kb=str(kb_dir), config={"model": model, "model_capacity": capacity}),
    )
    result = import_document(
        kb_dir,
        physical_pdf,
        bundle=LlmCredentialBundle(api_key="fixture", base_url=endpoint),
        metadata=SourceMetadata(product="Capacity", applicable_versions=("1",), family="manual"),
    )
    assert result.status == "added", result.message
    source = read_document_source(kb_dir, result.source_id)
    assert (source["length_class"], source["execution_mode"]) == ("short", mode)
    assert source["processing"]["capacity_status"] == status


def test_short_segmented_recompile_retains_decision_and_adapters(kb_dir, physical_pdf, pdf_model):
    import asyncio
    import json

    from click.testing import CliRunner

    from openkb.api_models import DocumentItem, DocumentSourceResponse
    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.recompilation import recompile_document, select_recompilation
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.cli import cli
    from openkb.documents import read_document_source
    from openkb.ingest_result import describe_ingest
    from openkb.view_records import SourceMetadata

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    result = import_document(
        kb_dir,
        physical_pdf,
        metadata=SourceMetadata(product="Capacity", applicable_versions=("1",), family="manual"),
    )
    before = read_document_source(kb_dir, result.source_id)
    assert "short / segmented" in "\n".join(describe_ingest(result))
    target = select_recompilation(kb_dir, result.source_id).targets[0]
    assert (target.kind, target.execution_mode) == ("short", "segmented")
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model_capacity": {"max_input_tokens": 100000}}
        ),
    )
    recompiled = asyncio.run(recompile_document(kb_dir, result.source_id))
    assert recompiled.status == "compiled", recompiled.message
    after = DocumentSourceResponse(**read_document_source(kb_dir, result.source_id))
    assert (after.length_class, after.execution_mode) == ("short", "segmented")
    assert after.processing == before["processing"]
    item = DocumentItem(**get_kb_list(kb_dir)["documents"][0])
    assert (item.length_class, item.execution_mode) == ("short", "segmented")
    settings = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "settings"])
    assert settings.exit_code == 0, settings.output
    assert json.loads(settings.output)["values"]["pdf_limit"]["source"] == "default"


def test_invalid_boolean_policy_never_changes_settings(kb_dir):
    from openkb.application.settings import apply_kb_config_patch, read_settings_view
    from openkb.application.settings_data import KbConfigPatchRequest

    before = read_settings_view(kb_dir)
    with pytest.raises(ValueError):
        apply_kb_config_patch(
            kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"pdf_short_max_pages": False})
        )
    assert read_settings_view(kb_dir) == before


@pytest.mark.parametrize("previous_success", [False, True])
def test_failed_compile_keeps_target_processing_visible(
    kb_dir, physical_pdf, pdf_model, monkeypatch, previous_success
):
    import litellm

    from openkb.application.documents import import_document
    from openkb.application.knowledge_bases import get_kb_list
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source
    from openkb.ingest_result import describe_ingest

    def capacity(limit):
        apply_kb_config_patch(
            kb_dir,
            KbConfigPatchRequest(
                kb=str(kb_dir),
                config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": limit}},
            ),
        )

    capacity(100000)
    first = import_document(kb_dir, physical_pdf) if previous_success else None
    capacity(200000)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("model unavailable")

    def unavailable_sync(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(litellm, "completion", unavailable_sync)
    monkeypatch.setattr(litellm, "acompletion", unavailable)
    if first:
        from openkb.application.reprocessing import preview_reprocessing, reprocess_source

        preview = preview_reprocessing(kb_dir, first.source_id)
        result = reprocess_source(kb_dir, first.source_id, version=preview["version"])
    else:
        result = import_document(kb_dir, physical_pdf)
    assert result.status == "failed"
    assert "short / full" in "\n".join(describe_ingest(result))
    reader = read_document_source(kb_dir, result.source_id)
    assert reader["target_processing"]["length_class"] == "short"
    assert reader["knowledge_revision_id"] == (
        first.units[0].knowledge_revision_id if first else None
    )
    assert reader["target_processing"]["input_limit"] == 200000
    assert reader["processing"]["input_limit"] == (100000 if previous_success else 200000)
    assert get_kb_list(kb_dir)["documents"][0]["target_processing"] == reader["target_processing"]


@pytest.mark.parametrize("global_layer", [True, False])
def test_capacity_patch_preserves_unspecified_nested_values(kb_dir, global_layer):
    from openkb.application.settings import (
        apply_global_config_patch,
        apply_kb_config_patch,
        read_settings_view,
    )
    from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest

    def patch(values):
        if global_layer:
            apply_global_config_patch(GlobalConfigPatchRequest(config=values))
        else:
            apply_kb_config_patch(kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config=values))

    patch(
        {
            "model": "gpt-4o",
            "model_capacity": {
                "context_window_tokens": 10000,
                "output_reserve_tokens": 1000,
                "tokenizer_model": "gpt-4o",
            },
        }
    )
    patch({"model_capacity": {"output_reserve_tokens": 2000}})
    assert read_settings_view(kb_dir).capacity["input_limit"] == 8000
    patch({"model_capacity": {"output_reserve_tokens": None}})
    assert read_settings_view(kb_dir).capacity["unknown_reason"] is not None
