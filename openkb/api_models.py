"""Pydantic request and response models for the OpenKB REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from openkb.api_views import ViewRequest
from openkb.application.settings_data import (
    _KB_CONFIG_WRITABLE_KEYS as _KB_CONFIG_WRITABLE_KEYS,
)

# Compatibility names for the existing REST schemas.
from openkb.application.settings_data import (
    GlobalConfigPatchRequest as GlobalConfigPatchRequest,
)
from openkb.application.settings_data import (
    GlobalConfigResponse as GlobalConfigResponse,
)
from openkb.application.settings_data import (
    GlobalConfigValues as GlobalConfigValues,
)
from openkb.application.settings_data import (
    KbConfigPatchRequest as KbConfigPatchRequest,
)
from openkb.application.settings_data import (
    KbConfigResponse as KbConfigResponse,
)
from openkb.ingest_result import ImportUnitOutcome


class QueryRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    stream: bool = True
    save: bool = False


class QueryResponse(BaseModel):
    answer: str
    saved_path: str | None = None


class ChatRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    stream: bool = True


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    turn_count: int


class ChatSessionItem(BaseModel):
    view_id: str = "legacy"
    id: str
    title: str
    turn_count: int
    updated_at: str
    model: str


class ChatSessionListResponse(BaseModel):
    kb: str
    sessions: list[ChatSessionItem]


class ChatTraceStep(BaseModel):
    """One persisted step of a chat turn's interleaved trace: a run of
    narration/answer text (``kind="text"``) or one tool call (``kind="tool"``,
    with the raw ``name``/``arguments`` so the frontend applies on restore the
    same read-only whitelist it uses live). Turns recorded before traces
    existed or via the CLI have an empty trace, and the client then falls back
    to the flat ``assistant_texts`` entry."""

    kind: str
    text: str | None = None
    name: str | None = None
    arguments: str | None = None


class ChatSessionLoadResponse(BaseModel):
    session_id: str
    title: str
    turn_count: int
    user_turns: list[str]
    assistant_texts: list[str]
    # Parallel to assistant_texts (1:1 by index); an empty inner list means
    # "no trace for this turn — render the flat assistant_texts entry instead".
    assistant_traces: list[list[ChatTraceStep]] = Field(default_factory=list)


class ChatSessionLoadRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


class ChatSessionDeleteRequest(BaseModel):
    kb: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)


class ChatSessionDeleteResponse(BaseModel):
    deleted: bool


class InitRequest(BaseModel):
    kb: str = Field(..., min_length=1)
    model: str | None = None
    api_key: str | None = None
    openai_api_base: str | None = None
    # Optional custom location for the KB: an ABSOLUTE directory (a relative
    # path is a 400). Absent → created under kb_root_dir() / kb. Either way the
    # KB is registered under `kb` so it resolves by name afterwards.
    path: str | None = None


class EnvWritten(BaseModel):
    api_key: bool
    openai_api_base: bool


class InitResponse(BaseModel):
    kb: str
    created: bool
    env_written: EnvWritten
    message: str


class AddFileItem(BaseModel):
    original_name: str
    saved_path: str | None = None
    status: str
    message: str
    source_id: str | None = None
    source_revision_id: str | None = None
    units: tuple[ImportUnitOutcome, ...] = ()
    discovery_pending: int | None = None


class AddResponse(BaseModel):
    kb: str
    files: list[AddFileItem]
    added_count: int
    skipped_count: int
    failed_count: int
    blocked_count: int = 0
    partial_count: int = 0
    source_count: int = 0
    unit_counts: dict[str, int] = Field(default_factory=dict)
    discovery_pending_count: int = 0


class KbRequest(ViewRequest):
    kb: str = Field(..., min_length=1)


class LintRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    fix: bool = False


class DocumentItem(BaseModel):
    validity: str = "current"
    source_generation: int | None = None
    view_id: str = "legacy"
    family_id: str | None = None
    annotation_id: str | None = None
    hash: str
    name: str
    type: str
    display_type: str
    pages: int | None = None
    source_id: str | None = None
    source_revision_id: str | None = None
    status: str | None = None
    message: str | None = None
    units: list[dict] = Field(default_factory=list)
    original_path: str | None = None


class ListResponse(BaseModel):
    documents: list[DocumentItem]
    document_count: int
    summaries: list[str]
    concepts: list[str]
    entities: list[str]
    reports: list[str]


class StatusResponse(BaseModel):
    needs_refresh: dict = Field(default_factory=dict)
    directories: dict[str, int]
    raw_count: int
    total_indexed: int
    last_compile: str | None = None
    last_lint: str | None = None


class LintResponse(BaseModel):
    skipped: bool
    reason: str | None = None
    message: str
    structural_report: str | None = None
    knowledge_report: str | None = None
    report_path: str | None = None
    lint_files_changed: int | None = None
    lint_ghosts_removed: int | None = None


class RemoveRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    identifier: str = Field(..., min_length=1)
    keep_raw: bool = False
    keep_empty: bool = False
    dry_run: bool = False
    stream: bool = False


class RemoveActionItem(BaseModel):
    tag: str
    target: str


class RemoveResponse(BaseModel):
    retained: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    status: str
    name: str | None = None
    doc_name: str | None = None
    actions: list[RemoveActionItem] = []
    concepts_deleted: list[str] = []
    entities_deleted: list[str] = []
    lint_files_changed: int | None = None
    lint_ghosts_removed: int | None = None
    pageindex_message: str | None = None
    pageindex_error: str | None = None
    message: str | None = None
    candidates: list[dict[str, str]] = []


class RecompileDocItem(BaseModel):
    name: str | None = None
    doc_name: str | None = None
    type: str
    status: str
    elapsed: float | None = None
    message: str | None = None


class RecompileRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    doc_name: str | None = None
    all_docs: bool = False
    dry_run: bool = False
    refresh_schema: bool = False
    stream: bool = False


class RecompileTargetItem(BaseModel):
    name: str
    doc_name: str
    type: str


class RecompileResponse(BaseModel):
    status: str
    total: int
    recompiled: int
    skipped: int
    blocked: int = 0
    docs: list[RecompileDocItem] = []
    targets: list[RecompileTargetItem] | None = None
    candidates: list[dict[str, str]] | None = None
    message: str | None = None


class DeckRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    stream: bool = True


class DeckResponse(BaseModel):
    name: str
    status: str
    path: str


class DeckListResponse(BaseModel):
    decks: list[dict]


class SkillRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    stream: bool = True


class SkillResponse(BaseModel):
    name: str
    status: str
    path: str


class SkillListResponse(BaseModel):
    skills: list[dict]


class GraphRequest(ViewRequest):
    kb: str = Field(..., min_length=1)


class GraphResponse(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    types: list[str]


class PageRequest(ViewRequest):
    knowledge_revision_id: str | None = None
    kb: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class PageResponse(BaseModel):
    path: str
    content: str
    validity: str = "current"
    knowledge_revision_id: str | None = None
    source_revision_ids: tuple[str, ...] = ()
    refresh_reasons: tuple[dict, ...] = ()


class DocumentSourceRequest(ViewRequest):
    knowledge_revision_id: str | None = None
    pages: str | None = None
    kb: str = Field(..., min_length=1)
    # SHA-256 hash key from /list — the unique document identifier (avoids
    # ambiguity when two documents share a doc_name/filename stem).
    hash: str = Field(..., min_length=1)
    source_revision_id: str | None = None


class DocumentSourceResponse(BaseModel):
    unit_kind: str | None = None
    page_range: list[int] = Field(default_factory=list)
    coverage: str = "unknown"
    units: list[dict] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    unit_revision_id: str | None = None
    processing_fingerprint: str | None = None
    validity: str = "current"
    view_id: str = "legacy"
    version_metadata: dict = Field(default_factory=dict)
    hash: str
    name: str
    doc_name: str
    type: str
    format: str
    content: str
    # Page count for long docs (per-page JSON); None for short (single .md).
    pages: int | None = None
    source_id: str | None = None
    source_revision_id: str | None = None
    target_source_revision_id: str | None = None
    knowledge_revision_id: str | None = None
    original_path: str | None = None
    base_path: str | None = None
    status: str | None = None
    message: str | None = None
    error_type: str | None = None
    original_kind: str | None = None


class PageDeleteRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    # A '<section>/<name>' wiki-page ref, e.g. "concepts/attention". Only
    # concepts/ and entities/ pages are user-deletable (validated server-side).
    path: str = Field(..., min_length=1)
    # dry_run reports the impacted backlink pages without deleting anything, so
    # the UI can show "these N pages will have their links demoted" + confirm.
    dry_run: bool = False


class PageDeleteResponse(BaseModel):
    status: str
    target: str
    # Content pages whose inbound [[links]] to the target will be / were demoted
    # to plain text (the deletion's blast radius).
    backlinks: list[str] = []
    files_changed: int | None = None
    ghosts_stripped: int | None = None


class PageLinksRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)


class PageLinksResponse(BaseModel):
    status: str
    target: str
    outlinks: list[str] = []  # pages this page links to
    backlinks: list[str] = []  # pages that link to this page


class PageEditRequest(ViewRequest):
    kb: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    # New page BODY. The OKF frontmatter (type/description/sources) is
    # code-managed and preserved server-side; any frontmatter here is dropped.
    content: str


class PageEditResponse(BaseModel):
    status: str
    target: str
    # Dead [[links]] the edit introduced, demoted to plain text on save (so the
    # UI can tell the user which links didn't resolve).
    ghosts_stripped: list[str] = []
    content: str | None = None  # the saved full page (frontmatter + body)


class WatchStartRequest(BaseModel):
    kb: str = Field(..., min_length=1)
    debounce: float = Field(default=2.0, gt=0)


class WatchEventItem(BaseModel):
    ts: float
    event: str
    data: dict[str, Any] = {}


class WatchStatusResponse(BaseModel):
    kb: str
    active: bool
    started_at: float | None = None
    raw_dir: str | None = None
    debounce: float | None = None
    counters: dict[str, int] = {}
    recent_events: list[WatchEventItem] = []


class KbSummaryItem(BaseModel):
    name: str
    # Resolved directory of the KB. Present for every item so a KB registered
    # OUTSIDE kb_root_dir() is addressable/locatable by the UI, not just root
    # children.
    path: str
    document_count: int = 0
    last_compile: str | None = None
    has_raw: bool = False


class KbListResponse(BaseModel):
    root: str
    knowledge_bases: list[KbSummaryItem]


class KbDeleteRequest(BaseModel):
    kb: str = Field(..., min_length=1)
    # Must equal `kb`: a type-the-name confirmation, re-checked server-side so
    # this irreversible delete never fires from a client that skipped the guard.
    confirm_name: str = Field(..., min_length=1)


class KbDeleteResponse(BaseModel):
    deleted: bool
    kb: str
    path: str


class MetaResponse(BaseModel):
    version: str
