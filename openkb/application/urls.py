"""Acquire URLs privately, then publish via the shared complete-document operation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from openkb.application.documents import DocumentResult, import_document
from openkb.application.execution import ExecutionContext


def validate_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter an HTTP or HTTPS URL with a hostname")
    return value


def import_url(
    kb_dir: Path,
    url: str,
    *,
    context: ExecutionContext | None = None,
    prepared_dir: Path | None = None,
) -> DocumentResult:
    from openkb.application.source_history import defer_source_results, finish_source_result
    from openkb.compilation_report import collect_compile_report
    from openkb.config import resolve_effective_config
    from openkb.locks import kb_ingest_lock
    from openkb.processing import ProcessingIncomplete, processing_checkpoint, processing_scope

    url = validate_url(url)
    context = context or ExecutionContext()
    with (
        kb_ingest_lock(kb_dir / ".openkb", cancelled=context.cancelled, on_wait=context.waiting),
        context.begin(kb_dir) as bundle,
        collect_compile_report() as compilation,
    ):
        try:
            with processing_scope(resolve_effective_config(kb_dir)[0]), defer_source_results():
                processing_checkpoint("acquiring")
                result = _import_url(kb_dir, url, context=context, prepared_dir=prepared_dir)
        except ProcessingIncomplete as exc:
            result = DocumentResult(
                url,
                "unfinished",
                (),
                unfinished=(exc.stage,),
                stage=exc.stage,
                reason=exc.reason,
            )
        result = replace(result, usage=compilation.usage)
        if result.source_id is None or result.input_version is None:
            return finish_source_result(kb_dir, result)
        from openkb.application.document_pipeline import finish_compilation
        from openkb.sources import SourceStore

        return finish_compilation(
            kb_dir,
            SourceStore(kb_dir).version(result.input_version),
            resolve_effective_config(kb_dir)[0],
            result,
            bundle=bundle,
        )


def _import_url(
    kb_dir: Path,
    url: str,
    *,
    context: ExecutionContext,
    prepared_dir: Path | None,
) -> DocumentResult:
    from openkb.processing import processing_checkpoint
    from openkb.url_ingest import fetch_url_to_raw

    url = validate_url(url)
    if not (kb_dir / ".openkb/config.yaml").is_file():
        raise ValueError("Knowledge base not found")
    context = context or ExecutionContext()
    context.check_stop()
    context.on_event({"stage": "acquiring"})
    quality: list[str] = []
    # Acquisition remains private while retaining this document's execution
    # lease and budget through compilation. No half-written file is published.
    with (
        tempfile.TemporaryDirectory(prefix="openkb-url-")
        if prepared_dir is None
        else nullcontext(str(prepared_dir))
    ) as temporary:
        root = Path(temporary)
        manifest = root / "input.json"
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        from openkb.state import HashRegistry

        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            source = (root / saved["path"]).resolve()
            if (
                not source.is_relative_to(root.resolve())
                or saved.get("url_hash") != url_hash
                or not source.is_file()
                or HashRegistry.hash_file(source) != saved["hash"]
                or not isinstance(saved.get("quality"), list)
                or not all(isinstance(note, str) for note in saved["quality"])
            ):
                raise ValueError("Prepared URL input is invalid; submit a new import explicitly")
            quality = saved["quality"]
        else:

            def cancelled():
                processing_checkpoint()
                return context.cancelled()

            source = fetch_url_to_raw(
                url,
                root,
                report=lambda *args, **kwargs: None,
                cancelled=cancelled,
                on_quality=quality.append,
            )
            if source is not None:
                from openkb.locks import atomic_write_json

                atomic_write_json(
                    manifest,
                    {
                        "path": source.relative_to(root).as_posix(),
                        "url_hash": url_hash,
                        "hash": HashRegistry.hash_file(source),
                        "quality": quality,
                    },
                )
        context.check_stop()
        processing_checkpoint()
        if source is None:
            return DocumentResult(
                url,
                "failed",
                (),
                tuple(quality),
                ("acquisition",),
                stage="acquiring",
                reason="acquisition_failed",
                knowledge_compilation="not_started",
            )
        result = import_document(kb_dir, source, context=context, origin_url=url)
        return replace(
            result,
            source=url,
            quality=tuple(quality) + result.quality,
            unfinished=result.unfinished + (("ingest",) if result.status == "failed" else ()),
        )
