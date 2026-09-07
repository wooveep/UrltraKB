"""HTTP helpers and SSE streams used by :mod:`openkb.api`."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
import time
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from openkb.agent.chat import build_chat_session_agent, iter_chat_turn_events
from openkb.agent.chat_session import ChatSession, load_session
from openkb.agent.query import (
    build_query_agent,
    build_run_config_from_bundle,
    iter_agent_response_events,
)
from openkb.api_models import (
    AddFileItem,
    AddResponse,
    ChatRequest,
    DeckRequest,
    QueryRequest,
    RecompileRequest,
    RemoveRequest,
    SkillRequest,
)
from openkb.api_recompile import iter_recompile
from openkb.api_uploads import cleanup_uploads as _cleanup_uploads
from openkb.api_uploads import reserve_upload
from openkb.application.answers import save_exploration
from openkb.application.documents import _add_for_api
from openkb.application.knowledge_bases import initialize_kb
from openkb.application.removal import run_remove_for_api
from openkb.application.uploads import published_input
from openkb.config import (
    DEFAULT_CONFIG,
    register_kb_alias,
    resolve_effective_config,
    resolve_kb_alias,
)
from openkb.log import append_log
from openkb.watch_service import WatchRegistry

security = HTTPBearer(auto_error=False)
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_UPLOAD_FILE_BYTES = int(os.environ.get("OPENKB_MAX_UPLOAD_FILE_BYTES", str(100 * 1024 * 1024)))
MAX_UPLOAD_REQUEST_BYTES = int(
    os.environ.get("OPENKB_MAX_UPLOAD_REQUEST_BYTES", str(500 * 1024 * 1024))
)


def _configure_cors(app: FastAPI) -> None:
    """Allow browser frontends to call the API (configurable via env)."""
    raw = os.environ.get("OPENKB_CORS_ORIGINS", "")
    wildcard = raw.strip() == "*"
    if wildcard:
        origins = ["*"]
    else:
        origins = [o.strip() for o in raw.split(",") if o.strip()] or [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:7566",
            "http://127.0.0.1:7566",
        ]
    # A wildcard origin with credentials is insecure: any site can issue
    # credentialed cross-origin requests. Reject this combination by forcing
    # credentials off for wildcards, which still allows unauthenticated
    # cross-origin GETs but blocks cookie/token-bearing requests.
    allow_credentials = not wildcard
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _mount_web_ui(app: FastAPI) -> None:
    """Serve the bundled web UI at ``/`` when the ``web/`` bundle exists.

    The Vite build outputs to ``openkb/web`` (shipped inside the wheel via
    hatchling ``artifacts``), so the bundle sits next to this module for both
    installed packages and source checkouts. Absent (API-only install / UI not
    built) the mount is simply skipped. Mounting under the API origin avoids
    cross-origin fetch from ``file://`` so the SPA can call the REST endpoints.
    """
    web_dir = Path(__file__).resolve().parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web-ui")


def require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    expected = os.environ.get("OPENKB_API_TOKEN")
    if not expected:
        # Auth is opt-in. With no OPENKB_API_TOKEN configured the API is open —
        # the local-first default so `openkb-api` + open the browser just works
        # with no config. A deployer who exposes the server sets
        # OPENKB_API_TOKEN to require a bearer token (main() warns when bound to
        # a non-loopback host without one).
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required.",
        )
    if not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
        )


def _resolve_kb(value: str) -> Path:
    try:
        kb_dir = resolve_kb_alias(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not _is_kb_dir(kb_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Not a knowledge base: {value}",
        )
    return kb_dir


def _is_kb_dir(kb_dir: Path) -> bool:
    """A directory counts as a KB when it has both ``.openkb`` and ``wiki``."""
    return (kb_dir / ".openkb").is_dir() and (kb_dir / "wiki").is_dir()


def _init_kb_for_api(
    kb_dir: Path,
    kb_name: str,
    *,
    model: str | None,
    api_key: str | None,
    openai_api_base: str | None,
) -> dict:
    """Run ``initialize_kb`` + ``register_kb_alias`` off the event loop.

    Both touch file locks (``register_kb_alias`` holds the global-config
    lock); running them in a threadpool gives each request its own
    ``threading.local`` so ``kb_ingest_lock``'s reentrancy bookkeeping is
    correct, and avoids blocking the event loop.
    """
    result = initialize_kb(
        kb_dir,
        template_dir=Path.cwd(),
        model=model,
        api_key=api_key,
        openai_api_base=openai_api_base,
    )
    register_kb_alias(kb_name, kb_dir)
    return result


def _save_query_answer(kb_dir: Path, question: str, answer: str) -> Path | None:
    return save_exploration(kb_dir, question, answer)


def _parse_stream_form(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return value.strip().lower() not in {"false", "0", "no", "off"}


async def _write_upload(
    saved_path: Path,
    upload: UploadFile,
    request_bytes_so_far: int,
) -> int:
    """Stream an upload body into its owned private path.

    Runs outside the per-KB mutation lock so a large or slow upload does not
    block other same-KB mutations (lint/recompile/other adds). Returns the
    bytes written; unlinks the private file on failure.
    """
    try:
        file_bytes = 0
        with saved_path.open("wb") as handle:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                file_bytes += len(chunk)
                request_bytes = request_bytes_so_far + file_bytes
                if file_bytes > MAX_UPLOAD_FILE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(f"Uploaded file exceeds limit of {MAX_UPLOAD_FILE_BYTES} bytes."),
                    )
                if request_bytes > MAX_UPLOAD_REQUEST_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Upload request exceeds limit of {MAX_UPLOAD_REQUEST_BYTES} bytes."
                        ),
                    )
                handle.write(chunk)
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload save failed: {exc}",
        ) from exc
    finally:
        await upload.close()
    return file_bytes


def _summarize_add_results(kb: str, results: list[AddFileItem]) -> AddResponse:
    return AddResponse(
        kb=kb,
        files=results,
        added_count=sum(1 for item in results if item.status == "added"),
        skipped_count=sum(1 for item in results if item.status == "skipped"),
        failed_count=sum(1 for item in results if item.status == "failed"),
    )


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump()


def _reserve_add_uploads(
    kb_dir: Path,
    files: list[UploadFile],
) -> list[tuple[Path, str]]:
    """Reserve private paths; unfinished bodies never appear in a watched tree."""
    reserved: list[tuple[Path, str]] = []
    try:
        for upload in files:
            reserved.append(reserve_upload(upload))
    except BaseException:
        _cleanup_uploads(reserved)
        raise
    return reserved


async def _write_add_uploads(
    reserved: list[tuple[Path, str]],
    files: list[UploadFile],
) -> list[tuple[Path, str]]:
    """Stream each upload's body into its reserved path, outside the lock.

    ``reserved`` and ``files`` are parallel and in the same order (both come
    from the same request). Returns ``reserved`` unchanged so callers keep the
    ``(path, original_name)`` pairs; cleans up every reserved path on failure.
    """
    request_bytes = 0
    try:
        for (saved_path, _original), upload in zip(reserved, files):
            request_bytes += await _write_upload(saved_path, upload, request_bytes)
    except BaseException:
        _cleanup_uploads(reserved)
        raise
    return reserved


async def _run_add_uploads(
    kb: str,
    kb_dir: Path,
    saved_uploads: list[tuple[Path, str]],
    *,
    bundle=None,
) -> AddResponse:
    results = []
    try:
        for saved_path, original_name in saved_uploads:
            results.append(await _add_saved_file(kb_dir, saved_path, original_name, bundle=bundle))
    finally:
        _cleanup_uploads(saved_uploads)
    return _summarize_add_results(kb, results)


async def _stream_add_uploads(
    kb: str,
    kb_dir: Path,
    saved_uploads: list[tuple[Path, str]],
    *,
    bundle=None,
) -> AsyncIterator[str]:
    results: list[AddFileItem] = []
    try:
        yield _sse(
            "start",
            {"endpoint": "add", "kb": kb, "file_count": len(saved_uploads)},
        )
        for saved_path, original_name in saved_uploads:
            loop = asyncio.get_running_loop()
            published: asyncio.Future[Path] = loop.create_future()
            cancelled = threading.Event()

            def on_published(path, future=published):
                loop.call_soon_threadsafe(future.set_result, path)

            worker = asyncio.create_task(
                _add_saved_file(
                    kb_dir,
                    saved_path,
                    original_name,
                    bundle=bundle,
                    on_published=on_published,
                    cancelled=cancelled.is_set,
                )
            )
            try:
                pending: set[asyncio.Future[Any]] = {published, worker}
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if published.done():
                    data = {"original_name": original_name, "saved_path": str(published.result())}
                    yield _sse("uploaded", data)
                    yield _sse("file_start", data)
                item = await asyncio.shield(worker)
            finally:
                cancelled.set()
                # An observer disconnect cannot delete a worker's private input
                # while it is still reading it or release its whole-unit lease.
                with anyio.CancelScope(shield=True):
                    try:
                        await asyncio.shield(worker)
                    except Exception:
                        # The normal await reports business failures. On
                        # disconnect preserve cancellation/GeneratorExit.
                        pass
            results.append(item)
            yield _sse("file_done", _model_payload(item))
        final = _summarize_add_results(kb, results)
        yield _sse("final", _model_payload(final))
    except HTTPException as exc:
        yield _sse("error", {"message": exc.detail})
    except Exception as exc:
        yield _sse("error", {"message": f"Add failed: {exc}"})
    finally:
        _cleanup_uploads(saved_uploads)
    yield _sse("done", {})


async def _add_saved_file(
    kb_dir: Path,
    saved_path: Path,
    original_name: str,
    *,
    bundle=None,
    on_published: Callable[[Path], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> AddFileItem:
    def consume():
        try:
            with published_input(kb_dir, saved_path, cancelled=cancelled) as owned:
                if on_published:
                    on_published(owned.path)
                result = _add_for_api(owned.path, kb_dir, bundle=bundle)
                item = AddFileItem(**result.__dict__)
                item.original_name = original_name
                if item.status == "skipped":
                    owned.discard_if_unregistered()
                    item.saved_path = None
                return item
        finally:
            _cleanup_uploads([(saved_path, original_name)])

    with anyio.CancelScope(shield=True):
        return await run_in_threadpool(consume)


def _load_or_create_session(kb_dir: Path, session_id: str | None) -> ChatSession:
    if session_id:
        try:
            return load_session(kb_dir, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Chat session not found: {session_id}",
            ) from exc

    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    language = config.get("language", "en")
    return ChatSession.new(kb_dir, model, language)


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def _stream_query(
    request: QueryRequest,
    kb_dir: Path,
    model: str,
    fastapi_request: Request,
    *,
    bundle=None,
) -> AsyncIterator[str]:
    yield _sse("start", {"endpoint": "query"})
    run_config = build_run_config_from_bundle(model, bundle)
    try:
        from openkb.locks import async_kb_lock

        async with async_kb_lock(kb_dir / ".openkb", exclusive=True):
            config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
            language = config.get("language", "en")
            agent = build_query_agent(str(kb_dir / "wiki"), model, language=language, bundle=bundle)
            final_answer = ""
            stream = iter_agent_response_events(agent, request.question, run_config=run_config)
            async with aclosing(stream):
                async for event in stream:
                    data = event["data"]
                    if event["event"] == "final":
                        # Persist the fully-computed answer *before* checking for a
                        # disconnect: the caller asked to save it, so a client that
                        # drops at the last moment must not lose the write. Only the
                        # client-facing SSE frame is skipped when disconnected.
                        final_answer = data["answer"]
                        saved_path = (
                            _save_query_answer(kb_dir, request.question, final_answer)
                            if request.save
                            else None
                        )
                        append_log(kb_dir / "wiki", "query", request.question)
                        if await fastapi_request.is_disconnected():
                            break
                        yield _sse(
                            "final",
                            {
                                "answer": final_answer,
                                "saved_path": str(saved_path) if saved_path else None,
                            },
                        )
                    else:
                        if await fastapi_request.is_disconnected():
                            break
                        yield _sse(event["event"], data)
    except Exception as exc:
        yield _sse("error", {"message": f"Query failed: {exc}"})
    yield _sse("done", {})


async def _stream_chat(
    request: ChatRequest,
    kb_dir: Path,
    session: ChatSession,
    fastapi_request: Request,
    *,
    bundle=None,
) -> AsyncIterator[str]:
    yield _sse("start", {"endpoint": "chat", "session_id": session.id})
    run_config = build_run_config_from_bundle(session.model, bundle)
    try:
        agent = await asyncio.to_thread(build_chat_session_agent, kb_dir, session, bundle=bundle)
        stream = iter_chat_turn_events(agent, session, request.message, run_config=run_config)
        async with aclosing(stream):
            async for event in stream:
                if await fastapi_request.is_disconnected():
                    break
                yield _sse(event["event"], event["data"])
    except Exception as exc:
        yield _sse("error", {"message": f"Chat failed: {exc}"})
    yield _sse("done", {})


async def _stream_remove(
    request: RemoveRequest,
    kb_dir: Path,
) -> AsyncIterator[str]:
    """SSE view of remove: start, plan, per-stage progress, final, done.

    Maps ``run_remove_for_api``'s status codes to events so a streaming
    client can react to ``not_found`` / ``multiple`` / ``partial`` without
    waiting on an HTTP error.
    """
    yield _sse("start", {"endpoint": "remove", "identifier": request.identifier})
    try:
        result = await run_in_threadpool(
            run_remove_for_api,
            kb_dir,
            request.identifier,
            keep_raw=request.keep_raw,
            keep_empty=request.keep_empty,
            dry_run=request.dry_run,
        )
        status_value = result.get("status")
        if status_value == "not_found":
            yield _sse("error", {"code": 404, "message": "Document not found."})
        elif status_value == "multiple":
            yield _sse(
                "error",
                {
                    "code": 409,
                    "message": "Identifier matches multiple documents.",
                    "candidates": result.get("candidates", []),
                },
            )
        else:
            yield _sse(
                "plan",
                {
                    "name": result.get("name"),
                    "doc_name": result.get("doc_name"),
                    "actions": result.get("actions", []),
                },
            )
            if status_value == "dry_run":
                yield _sse("final", {"status": "dry_run", **result})
            else:
                yield _sse("progress", {"stage": "wiki_cleanup"})
                yield _sse("final", {"status": status_value, **result})
    except Exception as exc:
        yield _sse("error", {"message": f"Remove failed: {exc}"})
    yield _sse("done", {})


async def _stream_recompile(
    request: RecompileRequest,
    kb_dir: Path,
    mutation_lock: asyncio.Lock,
    fastapi_request: Request,
    *,
    bundle=None,
) -> AsyncIterator[str]:
    """SSE view of recompile: start, per-doc progress, final, done.

    Maps ``iter_recompile``'s events to SSE so a streaming client can react
    to ``doc`` (ok/skipped/error) and terminal ``error`` (404/409/etc.) as
    they happen, without waiting on an HTTP error.
    """
    yield _sse("start", {"endpoint": "recompile"})
    # Hold the per-KB asyncio.Lock for the entire stream so concurrent
    # same-KB recompiles are serialized before kb_ingest_lock (which uses
    # threading.local and miscounts reentrancy on the event-loop thread).
    async with mutation_lock:
        try:
            async for event in iter_recompile(
                kb_dir,
                request.doc_name,
                all_docs=request.all_docs,
                dry_run=request.dry_run,
                refresh_schema=request.refresh_schema,
                bundle=bundle,
            ):
                # Cooperative stop: an aborting client (Stop / navigate-away)
                # must let the generator exit so ``async with mutation_lock``
                # releases the per-KB lock instead of holding it for the whole
                # (unwatched) LLM recompile.
                if await fastapi_request.is_disconnected():
                    break
                name = event.get("event")
                if name == "error":
                    yield _sse(
                        "error",
                        {
                            "code": event.get("code", 500),
                            "message": event.get("message", "Recompile failed."),
                            **(
                                {"candidates": event["candidates"]} if "candidates" in event else {}
                            ),
                        },
                    )
                elif name == "plan":
                    yield _sse("plan", {"targets": event.get("targets", [])})
                elif name == "doc":
                    yield _sse("doc", {k: v for k, v in event.items() if k != "event"})
                elif name == "final":
                    yield _sse("final", {k: v for k, v in event.items() if k != "event"})
        except Exception as exc:
            yield _sse("error", {"message": f"Recompile failed: {exc}"})
    yield _sse("done", {})


async def _iter_deck(
    request: DeckRequest,
    kb_dir: Path,
    *,
    bundle=None,
) -> AsyncIterator[dict[str, Any]]:
    """Raw event generator for deck generation.

    Yields plain dicts (``start`` / ``error`` / ``final``); both
    ``_stream_deck`` (SSE) and ``deck_endpoint``'s non-stream branch consume
    this directly. Mirrors ``iter_recompile``'s split so the SSE formatting
    lives only in the thin ``_stream_deck`` wrapper, never here.
    """
    from openkb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preflight_generation,
    )

    yield {"event": "start", "endpoint": "deck"}
    err = preflight_generation(kb_dir, request.name)
    if err:
        yield {"event": "error", "code": 400, "message": err}
        return
    config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    try:
        result = await generate_artifact(
            kb_dir,
            GenerationOptions("deck", request.name, request.intent, overwrite="overlay"),
            model=model,
            bundle=bundle,
        )
    except Exception as exc:
        yield {"event": "error", "code": 500, "message": f"Deck generation failed: {exc}"}
        return
    if result.status != "completed":
        yield {
            "event": "error",
            "code": 400 if result.status == "invalid" and result.error_type is None else 500,
            "message": f"Deck generation failed: {result.message}",
        }
        return
    yield {"event": "final", "name": request.name, "status": "done", "path": str(result.output_dir)}


async def _stream_deck(
    request: DeckRequest,
    kb_dir: Path,
    mutation_lock: asyncio.Lock,
    fastapi_request: Request,
    *,
    bundle=None,
) -> AsyncIterator[str]:
    """SSE-formatted view of ``_iter_deck``, for the ``stream=true`` branch."""
    # Hold the per-KB asyncio.Lock for the entire stream so concurrent
    # same-KB deck generations are serialized (mirrors ``_stream_recompile``);
    # ``_preflight_skill_new`` does not gate overwrite, so unlocked concurrent
    # streams would race on the same ``output_dir`` bytes.
    async with mutation_lock:
        async for event in _iter_deck(request, kb_dir, bundle=bundle):
            # Cooperative stop on client abort so the lock is released rather
            # than held for the whole unwatched generation (see recompile).
            if await fastapi_request.is_disconnected():
                break
            name = event.pop("event")
            yield _sse(name, event)
    yield _sse("done", {})


async def _iter_skill(
    request: SkillRequest,
    kb_dir: Path,
    *,
    bundle=None,
) -> AsyncIterator[dict[str, Any]]:
    """Raw event generator for skill generation — same shape as ``_iter_deck``."""
    from openkb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preflight_generation,
    )

    yield {"event": "start", "endpoint": "skill"}
    err = preflight_generation(kb_dir, request.name)
    if err:
        yield {"event": "error", "code": 400, "message": err}
        return
    config = (await asyncio.to_thread(resolve_effective_config, kb_dir))[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    try:
        result = await generate_artifact(
            kb_dir,
            GenerationOptions("skill", request.name, request.intent, overwrite="overlay"),
            model=model,
            bundle=bundle,
        )
    except Exception as exc:
        yield {"event": "error", "code": 500, "message": f"Skill generation failed: {exc}"}
        return
    if result.status != "completed":
        yield {
            "event": "error",
            "code": 400 if result.status == "invalid" and result.error_type is None else 500,
            "message": f"Skill generation failed: {result.message}",
        }
        return
    yield {"event": "final", "name": request.name, "status": "done", "path": str(result.output_dir)}


async def _stream_skill(
    request: SkillRequest,
    kb_dir: Path,
    mutation_lock: asyncio.Lock,
    fastapi_request: Request,
    *,
    bundle=None,
) -> AsyncIterator[str]:
    """SSE-formatted view of ``_iter_skill``, for the ``stream=true`` branch."""
    # Hold the per-KB asyncio.Lock for the entire stream so concurrent
    # same-KB skill generations are serialized (mirrors ``_stream_recompile``);
    # ``_preflight_skill_new`` does not gate overwrite, so unlocked concurrent
    # streams would race on the same ``output_dir`` bytes.
    async with mutation_lock:
        async for event in _iter_skill(request, kb_dir, bundle=bundle):
            # Cooperative stop on client abort so the lock is released rather
            # than held for the whole unwatched generation (see recompile).
            if await fastapi_request.is_disconnected():
                break
            name = event.pop("event")
            yield _sse(name, event)
    yield _sse("done", {})


# Default cap for /watch/events SSE so abandoned clients do not poll forever.
_WATCH_SSE_TIMEOUT = float(os.environ.get("OPENKB_WATCH_SSE_TIMEOUT", "300"))


async def _stream_watch_events(
    registry: WatchRegistry,
    kb: str,
    max_events: int | None,
    timeout_seconds: float | None,
    request: Request,
) -> AsyncIterator[str]:
    """Tail a KB's watch event ring buffer as an SSE stream.

    Replays existing events then polls for new ones. Terminates when the
    watcher stops, or when ``max_events``/``timeout_seconds`` is reached (so
    bounded clients and tests can drain without hanging). With both unset the
    stream is capped by a default timeout when none is given.
    """
    state = await asyncio.to_thread(registry.get, kb)
    yield _sse("start", {"endpoint": "watch", "kb": kb, "active": state is not None})
    if state is None:
        yield _sse("error", {"message": f"No active watcher for KB: {kb}"})
        yield _sse("done", {})
        return
    if timeout_seconds is None:
        timeout_seconds = _WATCH_SSE_TIMEOUT
    next_seq = 0
    emitted = 0
    started = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return
            for ev in list(state.events):
                if ev["seq"] < next_seq:
                    continue
                next_seq = ev["seq"] + 1
                yield _sse(ev["event"], ev["data"])
                emitted += 1
                if ev["event"] == "watcher_stopped":
                    yield _sse("done", {})
                    return
                if max_events is not None and emitted >= max_events:
                    yield _sse("done", {})
                    return
            if timeout_seconds is not None and (time.monotonic() - started) >= timeout_seconds:
                yield _sse("done", {})
                return
            await asyncio.sleep(0.5)
    except Exception as exc:
        yield _sse("error", {"message": f"Watch events stream failed: {exc}"})
    yield _sse("done", {})
