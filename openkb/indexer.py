"""PageIndex indexer for long documents."""

from __future__ import annotations

import json as json_mod
import logging
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pageindex import IndexConfig, LocalClient

from openkb.config import resolve_concurrency, resolve_credential_bundle, resolve_effective_config
from openkb.locks import atomic_write_text
from openkb.processing import navigation_execution
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
            if content:
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

        if content or normalized_images:
            pages.append(
                {
                    "page": page_number,
                    "content": content,
                    "images": normalized_images,
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
) -> Path:
    """Write ``wiki/sources/<doc_name>.json`` + ``wiki/summaries/<doc_name>.md``.

    Returns the summary path. Page images are written by the local extractor.
    """
    sources_dir = kb_dir / "wiki" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        sources_dir / f"{doc_name}.json", json_mod.dumps(pages, ensure_ascii=False, indent=2)
    )

    summaries_dir = kb_dir / "wiki" / "summaries"
    summaries_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summaries_dir / f"{doc_name}.md"
    atomic_write_text(
        summary_path, render_summary_md(tree, doc_name, doc_id, description=description)
    )
    return summary_path


def _build_index_config(config: dict[str, Any]) -> IndexConfig:
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


def index_long_document(pdf_path: Path, kb_dir: Path, doc_name: str | None = None) -> IndexResult:
    """Index a long PDF document using PageIndex and write wiki pages.

    ``doc_name`` is the collision-resistant wiki name used for all written
    artifacts; defaults to the PDF's stem for backward compatibility.
    """
    source_name = doc_name or pdf_path.stem
    openkb_dir = kb_dir / ".openkb"
    config = resolve_effective_config(kb_dir)[0]

    model: str = config.get("model", "gpt-5.4")

    index_config = _build_index_config(config)
    bundle = resolve_credential_bundle(kb_dir)
    index_config.llm_params = {
        "timeout": bundle.timeout,
        "api_key": bundle.api_key,
        "api_base": bundle.base_url,
        "extra_headers": bundle.extra_headers,
        "num_retries": 0,
        "max_retries": 0,
    }

    # Own the local store explicitly. Exception tracebacks retain the client,
    # so garbage collection cannot release SQLite before rollback on Windows.
    with ExitStack() as stack:
        stack.enter_context(navigation_execution())
        from pageindex.storage.sqlite import SQLiteStorage

        storage = stack.enter_context(SQLiteStorage(str(openkb_dir / "pageindex.db")))
        client = LocalClient(
            model=model,
            storage_path=str(openkb_dir),
            storage=storage,
            index_config=index_config,
        )
        return _index_document(client.collection(), pdf_path, kb_dir, source_name)


def _index_document(col: Any, pdf_path: Path, kb_dir: Path, source_name: str) -> IndexResult:
    doc_id = col.add(str(pdf_path))
    logger.info("PageIndex added %s → doc_id=%s", pdf_path.name, doc_id)

    # The PageIndex blob for doc_id is now durably on disk. The add mutation no
    # longer eagerly snapshots .openkb/files — it registers the new blob via
    # snapshot.track_new() only on a successful return — so if any step below
    # fails, delete the document we just added. Otherwise the blob leaks as an
    # orphan that pageindex.db (rolled back by the snapshot) no longer refs and
    # no reaper reclaims.
    try:
        # Fetch complete document (metadata + structure + text)
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

        # Write wiki/sources/ — per-page content
        sources_dir = kb_dir / "wiki" / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        images_dir = sources_dir / "images" / source_name

        all_pages = _normalize_page_content(
            _convert_pdf_to_pages(pdf_path, source_name, images_dir)
        )

        if not all_pages:
            raise RuntimeError(f"No page content extracted for {pdf_path.name}")

        _write_long_doc_artifacts(
            tree, all_pages, source_name, doc_id, kb_dir, description=description
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
