"""PageIndex indexer for long documents."""

from __future__ import annotations

import json as json_mod
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pageindex import IndexConfig, LocalClient
from pageindex.errors import IndexQualityError

from openkb.config import (
    LlmCredentialBundle,
    resolve_concurrency,
    resolve_credential_bundle,
    resolve_effective_config,
    resolve_per_request_overrides,
)
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.llm_execution import check_model_stop
from openkb.locks import atomic_write_text
from openkb.tree_renderer import render_summary_md

logger = logging.getLogger(__name__)


@dataclass
class IndexResult:
    """Result of indexing a long document via PageIndex."""

    doc_id: str
    description: str
    tree: dict


def _normalize_page_content(raw_pages: Any) -> list[dict[str, Any]]:
    """Normalize PageIndex/local PDF page content into OpenKB's JSON shape."""
    if not isinstance(raw_pages, list):
        return []

    pages: list[dict[str, Any]] = []
    for index, item in enumerate(raw_pages, start=1):
        if isinstance(item, str):
            content = item.strip()
            pages.append({"page": index, "content": content, "images": []})
            continue

        if not isinstance(item, dict):
            continue

        raw_page = item.get("page", item.get("page_number", item.get("page_num", index)))
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            page_number = index
        if page_number < 1:
            page_number = index

        content = item.get("content", item.get("markdown", item.get("text", "")))
        if content is None:
            content = ""
        content = str(content).strip()

        images = item.get("images", [])
        if not isinstance(images, list):
            images = []
        normalized_images = [
            image
            for image in images
            if isinstance(image, dict) and isinstance(image.get("path"), str)
        ]

        if (
            content
            or normalized_images
            or any(key in item for key in ("page", "page_number", "page_num"))
        ):
            pages.append(
                {
                    "page": page_number,
                    "content": content,
                    "images": normalized_images,
                    **{
                        key: item[key] for key in ("unit_kind", "printed_page_label") if key in item
                    },
                }
            )

    return pages


def _convert_pdf_to_pages(pdf_path: Path, doc_name: str, images_dir: Path) -> list[dict[str, Any]]:
    from openkb.images import convert_pdf_to_pages

    return convert_pdf_to_pages(pdf_path, doc_name, images_dir)


def _write_long_doc_artifacts(
    tree: dict,
    pages: list[dict[str, Any]],
    doc_name: str,
    doc_id: str,
    kb_dir: Path,
    description: str = "",
    *,
    scope: KnowledgeScope | None = None,
) -> Path:
    """Write ``wiki/sources/<doc_name>.json`` + ``wiki/summaries/<doc_name>.md``.

    Returns the summary path. Page images, when present, are written separately by the
    caller's page extractor — this helper only persists page text + summary.
    """
    scope = resolve_scope(kb_dir, scope, writable=True)
    sources_dir = scope.wiki_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        sources_dir / f"{doc_name}.json", json_mod.dumps(pages, ensure_ascii=False, indent=2)
    )

    summaries_dir = scope.wiki_dir / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summaries_dir / f"{doc_name}.md"
    atomic_write_text(
        summary_path, render_summary_md(tree, doc_name, doc_id, description=description)
    )
    return summary_path


def _build_index_config(
    config: dict[str, Any], *, bundle: LlmCredentialBundle | None = None
) -> IndexConfig:
    """Build the PageIndex ``IndexConfig`` for local indexing.

    Forwards the KB's ``concurrency`` setting to PageIndex, which caps how many
    indexing LLM calls run at once (guarding against "too many open files" fd
    exhaustion on large documents). The value is only passed when set *and* the
    installed PageIndex's ``IndexConfig`` declares the field, so OpenKB keeps
    working against a pinned PageIndex that predates it (``IndexConfig``
    forbids unknown kwargs).
    """
    kwargs: dict[str, Any] = {
        "if_add_node_text": True,
        "if_add_node_summary": True,
        "if_add_doc_description": True,
    }
    from openkb.index_llm import PageIndexLLM
    from openkb.llm_execution import compiler_executor
    headers, timeout, _ = resolve_per_request_overrides(config)
    from openkb.llm_runtime import audit_step_headers

    headers = audit_step_headers(headers, "pageindex.index")
    params: dict[str, Any] = {}
    if timeout is not None:
        params["timeout"] = timeout
    if headers:
        params["extra_headers"] = headers
    if bundle is not None:
        if bundle.api_key:
            params["api_key"] = bundle.api_key
        if bundle.base_url:
            params["api_base"] = bundle.base_url
    if params:
        # PageIndex scopes these per index; LiteLLM globals cannot override its
        # own timeout default. Forward the same resolved KB settings as compile.
        kwargs["llm_params"] = params
    executor_options = {"extra_headers": headers, "timeout": timeout}
    kwargs["llm_client"] = PageIndexLLM(
        compiler_executor(config.get("model", "gpt-5.4"), bundle, executor_options)
    )
    kwargs["require_llm_client"] = True
    concurrency = resolve_concurrency(config)
    if concurrency is not None:
        if "max_concurrency" in IndexConfig.model_fields:
            kwargs["max_concurrency"] = concurrency
        else:
            logger.warning(
                "config: 'concurrency' is set but the installed PageIndex "
                "version does not support it yet — ignoring it."
            )
    return IndexConfig(**kwargs)


def index_long_document(
    pdf_path: Path,
    kb_dir: Path,
    doc_name: str | None = None,
    *,
    scope: KnowledgeScope | None = None,
    storage_path: Path | None = None,
) -> IndexResult:
    """Index a long PDF document using PageIndex and write wiki pages.

    ``doc_name`` is the collision-resistant wiki name used for all written
    artifacts; defaults to the PDF's stem for backward compatibility.
    """
    scope = resolve_scope(kb_dir, scope, writable=True)
    source_name = doc_name or pdf_path.stem
    openkb_dir = storage_path if storage_path is not None else kb_dir / ".openkb"
    config = resolve_effective_config(kb_dir)[0]

    model: str = config.get("model", "gpt-5.4")
    index_config = _build_index_config(config, bundle=resolve_credential_bundle(kb_dir))

    client_factory = LocalClient
    if pdf_path.suffix in {".okbi", ".okpi"}:
        from openkb.block_package import create_index_client

        client_factory = create_index_client
    client = client_factory(
        model=model,
        storage_path=str(openkb_dir),
        index_config=index_config,
    )
    col = client.collection()

    # Add PDF (retry up to 3 times — PageIndex TOC accuracy is stochastic)
    max_retries = 3
    doc_id = None
    for attempt in range(1, max_retries + 1):
        try:
            doc_id = col.add(str(pdf_path))
            logger.info(
                "PageIndex added %s → doc_id=%s (attempt %d)", pdf_path.name, doc_id, attempt
            )
            break
        except Exception as exc:
            from pageindex.index.block_policy import BlockContractError
            from pageindex.index.page_parts_policy import PagePartsContractError

            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, (BlockContractError, PagePartsContractError)):
                    raise cause from None
                cause = cause.__cause__
            if not isinstance(exc, IndexQualityError):
                raise
            logger.warning(
                "PageIndex attempt %d/%d failed for %s: %s",
                attempt,
                max_retries,
                pdf_path.name,
                exc,
            )
            if attempt == max_retries:
                raise RuntimeError(
                    f"Failed to index {pdf_path.name} after {max_retries} attempts: {exc}"
                ) from exc

    # The PageIndex blob for doc_id is now durably on disk. The add mutation no
    # longer eagerly snapshots .openkb/files — it registers the new blob via
    # snapshot.track_new() only on a successful return — so if any step below
    # fails, delete the document we just added. Otherwise the blob leaks as an
    # orphan that pageindex.db (rolled back by the snapshot) no longer refs and
    # no reaper reclaims.
    try:
        # Fetch complete document (metadata + structure + text)
        check_model_stop()
        doc = col.get_document(doc_id, include_text=True)
        indexed_doc_name: str = doc.get("doc_name", pdf_path.stem)
        description: str = doc.get("doc_description", "")
        structure: list = doc.get("structure", [])

        # Debug: print doc keys and page_count to diagnose get_page_content range
        logger.info("Doc keys: %s", list(doc.keys()))
        logger.info("page_count from doc: %s", doc.get("page_count", "NOT PRESENT"))

        tree = {
            "doc_name": indexed_doc_name,
            "doc_description": description,
            "structure": structure,
        }

        if (doc.get("metadata") or {}).get("unit_kind") == "block":
            tree["unit_kind"] = "block"
            summary = scope.wiki_dir / "summaries" / f"{source_name}.md"
            atomic_write_text(
                summary, render_summary_md(tree, source_name, doc_id, description=description)
            )
            return IndexResult(doc_id=doc_id, description=description, tree=tree)

        # Write wiki/sources/ — per-page content
        sources_dir = scope.wiki_dir / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        images_dir = sources_dir / "images" / source_name

        if pdf_path.suffix == ".okpi":
            from openkb.office.slide_content import attach_slides
            from openkb.office.slide_package import materialize_slide_package

            with materialize_slide_package(pdf_path) as (snapshot, slides):
                all_pages = attach_slides(
                    _normalize_page_content(
                        _convert_pdf_to_pages(snapshot, source_name, images_dir)
                    ),
                    slides,
                )
        else:
            all_pages = _normalize_page_content(
                _convert_pdf_to_pages(pdf_path, source_name, images_dir)
            )

        if not all_pages:
            raise RuntimeError(f"No page content extracted for {pdf_path.name}")

        _write_long_doc_artifacts(
            tree, all_pages, source_name, doc_id, kb_dir, description=description, scope=scope
        )
        return IndexResult(doc_id=doc_id, description=description, tree=tree)
    except BaseException:
        # Best-effort: remove the blob this add created. A failure here (e.g. a
        # second interrupt) only means the blob may stay orphaned — the original
        # error still propagates so the caller (mutation coordinator) rolls back
        # everything else it snapshotted.
        try:
            col.delete_document(doc_id)
        except Exception:
            logger.warning(
                "PageIndex cleanup of %s failed after error; blob may be orphaned", doc_id
            )
        raise
