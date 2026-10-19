"""Retain a conversion independently of model work."""

import hashlib
import json
from pathlib import Path

from ultrakb.config import resolve_effective_config
from ultrakb.inputs import TEXT_SOURCE_EXTENSIONS


def normalization_policy(
    kb_dir: Path,
    *,
    scope=None,
    bundle=None,
    source_format=None,
    input_digest=None,
    asset_digests=None,
    text_decoding=None,
    doc_name=None,
    resource_policy=None,
) -> str:
    """One content recipe for file and managed inputs, independent of their record layout."""
    from importlib.metadata import version

    from ultrakb.agent.compiler import get_agents_md, short_document_messages
    from ultrakb.config import resolve_credential_bundle
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.pdf_recognition import PDF_RECOGNITION_POLICY

    config = resolve_effective_config(kb_dir)[0]
    credentials = bundle if bundle is not None else resolve_credential_bundle(kb_dir)
    selected = resolve_scope(kb_dir, scope)
    if selected.knowledge is None:
        agents = get_agents_md(selected.wiki_dir)
    else:
        try:
            agents = selected.knowledge.read("AGENTS.md").decode("utf-8")
        except FileNotFoundError:
            agents = ""
    request_basis = short_document_messages("", "", agents, config.get("language", "en"))
    text_policy = {}
    if source_format and f".{source_format}" in TEXT_SOURCE_EXTENSIONS:
        from ultrakb.content_blocks import BLOCK_POLICY
        from ultrakb.text_formats import text_normalization_policy
        from ultrakb.text_measurement import MEASUREMENT_FINGERPRINT

        text_policy = {
            "pipeline": text_normalization_policy(source_format),
            "block_policy": BLOCK_POLICY,
            "classification": {"short_max_tokens": 5000, "measurement": MEASUREMENT_FINGERPRINT},
            "input": {
                "body": input_digest,
                **({"decoding": text_decoding} if text_decoding else {}),
                "doc_name": doc_name,
                "assets": asset_digests or {},
            },
        }
        if source_format in {"html", "htm"}:
            from ultrakb.remote_assets import REMOTE_POLICY, resolve_resource_policy

            selected = resource_policy or resolve_resource_policy(kb_dir)
            text_policy["resources"] = {"policy": REMOTE_POLICY, **selected.model_dump(mode="json")}
    from ultrakb.inputs import CNKI_SOURCE_EXTENSIONS, OFFICE_SOURCE_EXTENSIONS

    pdf_policy = {}
    if source_format is None or f".{source_format}" in {
        ".pdf",
        *OFFICE_SOURCE_EXTENSIONS,
        *CNKI_SOURCE_EXTENSIONS,
    }:
        from ultrakb.images import PDF_IMAGE_POLICY
        from ultrakb.pdf_layout import PDF_LAYOUT_POLICY
        from ultrakb.source_contract import NATIVE_PAGE_POLICY

        pdf_policy["pdf_layout_policy"] = PDF_LAYOUT_POLICY
        pdf_policy["pdf_image_policy"] = PDF_IMAGE_POLICY
        pdf_policy["native_page_policy"] = NATIVE_PAGE_POLICY

    office_policy: dict = {}
    if source_format in {
        *(extension[1:] for extension in TEXT_SOURCE_EXTENSIONS),
        "xlsx",
        "xls",
    }:
        from pageindex.index.block_policy import BLOCK_INDEX_POLICY

        office_policy["block_index_policy"] = BLOCK_INDEX_POLICY
    if source_format in {"xlsx", "xls"}:
        from ultrakb.workbooks.records import WORKBOOK_POLICY, XLS_POLICY

        office_policy["workbook_policy"] = XLS_POLICY if source_format == "xls" else WORKBOOK_POLICY
    if source_format and f".{source_format}" in OFFICE_SOURCE_EXTENSIONS:
        from ultrakb.office.runtime import processing_identity

        office_policy = {"office": processing_identity(kb_dir)}
        if source_format in {"pptx", "ppt"}:
            from pageindex.index.page_parts_policy import PAGE_PARTS_INDEX_POLICY

            from ultrakb.office.slide_content import NOTES_POLICY

            office_policy["notes_policy"] = NOTES_POLICY
            office_policy["page_parts_index_policy"] = PAGE_PARTS_INDEX_POLICY
    if source_format in {"caj", "kdh"}:
        from ultrakb.cnki.runtime import processing_identity as cnki_processing_identity

        office_policy["cnki"] = cnki_processing_identity()
    from pageindex.backend.input_package import INDEX_CONTENT_POLICY

    from ultrakb.source_contract import CELL_VALUE_POLICY
    from ultrakb.source_qualification import SOURCE_STRUCTURE_POLICY

    return json.dumps(
        {
            "pipeline": "pdf-physical-v3",
            "source_structure_policy": SOURCE_STRUCTURE_POLICY,
            "cell_value_policy": CELL_VALUE_POLICY,
            "pdf_recognition_policy": PDF_RECOGNITION_POLICY,
            "classification": config["pdf_limit"],
            "index_model": config.get("model"),
            "index_policy": {
                "version": INDEX_CONTENT_POLICY,
                "navigation": config.get("index_navigation", {}),
            },
            # Recovery changes revalidate and reuse successful checkpoints.
            # Keep their established content identity and paid responses.
            "index_sdk": (
                "0.3.0.dev3+urltrakb.16"
                if version("pageindex") == "0.3.0.dev3+urltrakb.17"
                else "0.3.0.dev3+urltrakb.13"
                if version("pageindex") == "0.3.0.dev3+urltrakb.14"
                else "0.3.0.dev3+urltrakb.5"
                if version("pageindex")
                in {
                    "0.3.0.dev3+urltrakb.6",
                    "0.3.0.dev3+urltrakb.7",
                    "0.3.0.dev3+urltrakb.8",
                }
                else version("pageindex")
            ),
            "index_endpoint": hashlib.sha256(
                (credentials.base_url or "provider-default").encode()
            ).hexdigest(),
            # Request budgets belong to execution snapshots, not normalized
            # original evidence. Changing capacity must not re-convert a source.
            "request_basis": hashlib.sha256(
                json.dumps(request_basis, sort_keys=True).encode()
            ).hexdigest(),
            **text_policy,
            **office_policy,
            **pdf_policy,
        },
        sort_keys=True,
    )
