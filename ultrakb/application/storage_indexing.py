"""Build retained document navigation with PageIndex, without a private index database."""

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from pageindex.backend.input_package import INDEX_CONTENT_POLICY
from pageindex.index.pipeline import build_index
from pageindex.parser.markdown import MarkdownParser
from pageindex.parser.protocol import ContentNode, ParsedDocument
from pageindex.tokens import count_tokens

from ultrakb.compile_checkpoints import digest
from ultrakb.indexer import _build_index_config
from ultrakb.storage.checkpoint_store import CompileCheckpointStore
from ultrakb.storage.index_checkpoints import StoredIndexStages
from ultrakb.storage.index_store import IndexStore


def _physical_pages(native, doc_name, model):
    from ultrakb.office.slide_content import NOTES_POLICY
    from ultrakb.source_pages import read_page_selection

    raw = json.loads(native.read_text("utf-8"))
    read_page_selection(raw)
    if [page["page"] for page in raw] != list(range(1, len(raw) + 1)):
        raise ValueError("An indexed physical input requires complete page coverage")
    metadata = {
        "unit_kind": "page",
        "unit_count": len(raw),
        "coverage": "complete",
        "units_digest": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
    }
    if any("parts" in page for page in raw):
        metadata.update(notes_policy=NOTES_POLICY, page_parts=[page["parts"] for page in raw])
        if all("content_spans" in page for page in raw):
            metadata["page_generated_spans"] = [
                [
                    entry["generated_span"]
                    for entry in page["content_spans"]
                    if "generated_span" in entry
                ]
                for page in raw
            ]
    return ParsedDocument(
        doc_name=doc_name,
        nodes=[
            ContentNode(
                content=page["content"],
                tokens=count_tokens(page["content"], model=model),
                index=page["page"],
                metadata=page,
            )
            for page in raw
        ],
        metadata=metadata,
    )


def prepare_document_index(base, target, processing, baseline, context, bundle, model):
    config = context.snapshot.values()["effective"]
    options = _build_index_config({**config, "model": model or config["model"]}, bundle=bundle)
    runtime = options.llm_client
    selected_model = runtime.executor.bindings.model("document_index")
    options.model = selected_model
    public = runtime.executor.bindings.public_identity()
    fingerprint = digest(
        {
            "unit_revision": target.unit_revision_id,
            "content_policy": INDEX_CONTENT_POLICY,
            "parser": {
                "markdown": "retained-markdown-v1",
                "text": "retained-block-v1",
                "pages": "retained-physical-pages-v1",
            }[processing.source_kind],
            "model": selected_model,
            "endpoint": runtime.executor.bindings.credentials().base_url,
            "model_table": public["model_table"],
            "generation": public["role_generation"].get("document_index", {}),
            "options": options.model_dump(exclude={"llm_params", "max_concurrency"}),
        }
    )
    indexes = IndexStore(base.root, base.identity)
    if previous := indexes.find(target.unit_revision_id, content_fingerprint=fingerprint):
        return previous.index_revision_id
    if processing.source_kind == "text":
        from ultrakb.block_package import FrozenBlockParser
        from ultrakb.text_source import FrozenText

        native = baseline.directory / f"sources/{target.doc_name}.content.json"
        frozen = FrozenText.model_validate_json(native.read_text("utf-8"))
        parsed = FrozenBlockParser().parse_source(
            frozen, doc_name=target.doc_name, model=selected_model
        )
    elif processing.source_kind == "markdown":
        parsed = MarkdownParser().parse(
            str(baseline.directory / f"sources/{target.doc_name}.md"),
            model=selected_model,
            doc_name=target.doc_name,
        )
    else:
        parsed = _physical_pages(
            baseline.directory / f"sources/{target.doc_name}.json", target.doc_name, selected_model
        )
    execution = context.storage_execution
    runtime.checkpoints = StoredIndexStages(
        CompileCheckpointStore(base.root, base.identity).for_work(
            execution.model_budget_id,
            object_id={"index_unit_revision": target.unit_revision_id},
            conditions={"index": fingerprint},
            execution_id=execution.execution_id,
        )
    )
    runtime.executor.budget.preserve("compile", runtime.executor.policy.compile_reserve_calls)
    context.check_stop()
    document = build_index(parsed, model=selected_model, opt=options)
    document.update(
        doc_id=uuid5(NAMESPACE_URL, fingerprint).hex,
        metadata={
            **(parsed.metadata or {}),
            "content_policy": INDEX_CONTENT_POLICY,
            "source_kind": processing.source_kind,
            "processing_revision_id": target.unit_revision_id,
        },
        pages=(
            [{**node.metadata, "content": node.content} for node in parsed.nodes]
            if processing.source_kind in {"pages", "text"}
            else [{"page": node.index, "content": node.content} for node in parsed.nodes]
        ),
    )
    context.check_stop()
    return indexes.accept(
        target.unit_revision_id, document, content_fingerprint=fingerprint
    ).index_revision_id
