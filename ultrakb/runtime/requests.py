"""Explicit execution requests; content stays in memory and local IPC."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, kw_only=True)
class ViewSelection:
    view_id: str | None = None

    def __post_init__(self) -> None:
        if self.view_id is not None:
            from pydantic import TypeAdapter

            from ultrakb.source_records import ViewId

            TypeAdapter(ViewId).validate_python(self.view_id)


@dataclass(frozen=True)
class SavePage(ViewSelection):
    path: str
    body: str = field(repr=False)
    version: str
    publication_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.path or not isinstance(self.body, str) or not self.version:
            raise ValueError("Page saving requires a path, body and opened version")


@dataclass(frozen=True)
class AskQuestion(ViewSelection):
    question: str = field(repr=False)
    save: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("Enter a question")


@dataclass(frozen=True)
class ContinueConversation(ViewSelection):
    message: str = field(repr=False)
    session_id: str | None = None
    new_session_id: str | None = None
    attempt_id: str | None = None
    submission_order: int | None = None
    focus: tuple = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        from ultrakb.application.document_actions import DocumentIdentity

        if not isinstance(self.focus, tuple) or not all(
            isinstance(item, DocumentIdentity) and item.kind in {"source", "legacy"}
            for item in self.focus
        ):
            raise ValueError("Invalid document focus")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Enter a message")
        if self.submission_order is not None and (
            type(self.submission_order) is not int or self.submission_order < 0
        ):
            raise ValueError("Invalid submission order")
        if self.session_id and self.new_session_id:
            raise ValueError("Choose an existing or new conversation identity")
        for identity in (self.new_session_id, self.attempt_id):
            if identity is not None and (
                not isinstance(identity, str)
                or not identity
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in identity)
            ):
                raise ValueError("Invalid conversation submission identity")


@dataclass(frozen=True)
class ImportFile(ViewSelection):
    source: str
    wait_for_stable: bool = False
    download_remote_assets: bool | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        from pathlib import Path

        if not Path(self.source).is_absolute():
            raise ValueError("Import source must be an absolute path")
        if type(self.wait_for_stable) is not bool:
            raise ValueError("Invalid input stability policy")
        if (
            self.download_remote_assets is not None
            and type(self.download_remote_assets) is not bool
        ):
            raise ValueError("Invalid remote asset download policy")


@dataclass(frozen=True)
class RemoveDocument(ViewSelection):
    identifier: str
    version: str
    keep_raw: bool = False
    keep_empty: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.identifier or not self.version:
            raise ValueError("Document removal requires a confirmed preview")


@dataclass(frozen=True)
class RemoveDocuments(ViewSelection):
    preview: str = field(repr=False)
    confirmed_groups: tuple[str, ...] = field(default=("source",), kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.preview, str) or not self.preview:
            raise ValueError("Batch removal requires a reviewed preview")
        if not isinstance(self.confirmed_groups, tuple) or not set(self.confirmed_groups).issubset(
            {"source", "legacy"}
        ):
            raise ValueError("Invalid confirmed removal groups")


@dataclass(frozen=True)
class ImportUrl(ViewSelection):
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 HTTP 或 HTTPS 地址")


@dataclass(frozen=True)
class RecompileDocument(ViewSelection):
    file_hash: str
    version: str
    unit_id: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.file_hash, str)
            or not self.file_hash
            or not isinstance(self.version, str)
            or not self.version
        ):
            raise ValueError("Choose an indexed document to recompile")


@dataclass(frozen=True)
class DeleteConversation(ViewSelection):
    session_id: str
    version: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.session_id, str) or not self.session_id or not self.version:
            raise ValueError("Conversation deletion requires a confirmed completed history")


@dataclass(frozen=True)
class ExportConversation(ViewSelection):
    session_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Select a completed conversation to export")


@dataclass(frozen=True)
class CheckKnowledge(ViewSelection):
    semantic: bool = True
    fix: bool = False
    version: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.fix and (not isinstance(self.version, str) or not self.version):
            raise ValueError("Link repair requires confirmation of the latest wiki")


@dataclass(frozen=True)
class GenerateArtifact(ViewSelection):
    target_type: Literal["skill", "deck"]
    name: str
    intent: str = field(repr=False)
    version: str
    replace: bool = False
    resume_execution_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        from ultrakb.application.generators import GenerationOptions

        GenerationOptions(self.target_type, self.name, self.intent)
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Artifact generation requires the latest target version")
        if self.resume_execution_id is not None:
            from pydantic import TypeAdapter

            from ultrakb.source_records import RecordId

            TypeAdapter(RecordId).validate_python(self.resume_execution_id)


@dataclass(frozen=True)
class AcceptProposal(ViewSelection):
    proposal_id: str
    version: str

    def __post_init__(self) -> None:
        super().__post_init__()
        from pydantic import TypeAdapter

        from ultrakb.source_records import Digest, RecordId

        TypeAdapter(RecordId).validate_python(self.proposal_id)
        TypeAdapter(Digest).validate_python(self.version)


@dataclass(frozen=True)
class GenerateGraph(ViewSelection):
    pass


@dataclass(frozen=True)
class RefreshKnowledge(ViewSelection):
    source_ids: tuple[str, ...] = ()
    page_paths: tuple[str, ...] | None = None

    def __post_init__(self):
        super().__post_init__()
        from pydantic import TypeAdapter

        from ultrakb.source_records import RecordId, RelativePath

        if not isinstance(self.source_ids, tuple) or len(set(self.source_ids)) != len(
            self.source_ids
        ):
            raise ValueError("Select distinct source identities")
        for source in self.source_ids:
            TypeAdapter(RecordId).validate_python(source)
        if self.page_paths is not None:
            if (
                not self.source_ids
                or not isinstance(self.page_paths, tuple)
                or not self.page_paths
                or len(set(self.page_paths)) != len(self.page_paths)
            ):
                raise ValueError("Page selection requires distinct pending pages and sources")
            for path in self.page_paths:
                TypeAdapter(RelativePath).validate_python(path)


@dataclass(frozen=True)
class ConfirmEmptySource(ViewSelection):
    source_id: str
    generation: int

    def __post_init__(self) -> None:
        super().__post_init__()
        from pydantic import TypeAdapter

        from ultrakb.source_records import RecordId

        TypeAdapter(RecordId).validate_python(self.source_id)
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Empty confirmation requires the reviewed source generation")


@dataclass(frozen=True)
class AcceptRefreshProposal(AcceptProposal):
    pass


@dataclass(frozen=True)
class RetryWorksheet(ViewSelection):
    source_id: str
    unit_id: str
    source_revision_id: str | None = field(default=None, kw_only=True)
    unit_revision_id: str | None = field(default=None, kw_only=True)
    retry_failed_response: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.retry_failed_response) is not bool:
            raise ValueError("Failed summary retry requires an explicit boolean")
        if any(not isinstance(value, str) or not value for value in (self.source_id, self.unit_id)):
            raise ValueError("Worksheet retry requires source and unit identities")
        for value in (self.source_revision_id, self.unit_revision_id):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("Retry requires a reviewed target revision")


@dataclass(frozen=True)
class RunPendingJob(ViewSelection):
    job_id: str
    dispatch_id: str

    def __post_init__(self):
        super().__post_init__()
        from pydantic import TypeAdapter

        from ultrakb.source_records import RecordId

        for value in (self.job_id, self.dispatch_id):
            TypeAdapter(RecordId).validate_python(value)


@dataclass(frozen=True)
class RetrySource(ViewSelection):
    source_id: str
    source_revision_id: str | None = field(default=None, kw_only=True)
    unit_revision_id: str | None = field(default=None, kw_only=True)
    retry_failed_response: bool = field(default=False, kw_only=True)

    def __post_init__(self):
        super().__post_init__()
        if type(self.retry_failed_response) is not bool:
            raise ValueError("Failed summary retry requires an explicit boolean")
        from pydantic import TypeAdapter

        from ultrakb.source_records import RecordId

        TypeAdapter(RecordId).validate_python(self.source_id)
        for value in (self.source_revision_id, self.unit_revision_id):
            if value is not None:
                TypeAdapter(RecordId).validate_python(value)


@dataclass(frozen=True)
class ReprocessSource(ViewSelection):
    source_id: str
    version: str

    def __post_init__(self):
        super().__post_init__()
        if any(not isinstance(value, str) or not value for value in (self.source_id, self.version)):
            raise ValueError("Reprocessing requires a source and a reviewed preview token")


UnitRequest = (
    RetrySource
    | ReprocessSource
    | RunPendingJob
    | RetryWorksheet
    | AcceptProposal
    | RefreshKnowledge
    | ConfirmEmptySource
    | AcceptRefreshProposal
    | SavePage
    | AskQuestion
    | ContinueConversation
    | ImportFile
    | ImportUrl
    | RemoveDocument
    | RemoveDocuments
    | RecompileDocument
    | DeleteConversation
    | ExportConversation
    | CheckKnowledge
    | GenerateArtifact
    | GenerateGraph
)
REQUEST_TYPES = (
    RetrySource,
    ReprocessSource,
    RunPendingJob,
    RetryWorksheet,
    AcceptProposal,
    RefreshKnowledge,
    ConfirmEmptySource,
    AcceptRefreshProposal,
    SavePage,
    AskQuestion,
    ContinueConversation,
    ImportFile,
    ImportUrl,
    RemoveDocument,
    RemoveDocuments,
    RecompileDocument,
    DeleteConversation,
    ExportConversation,
    CheckKnowledge,
    GenerateArtifact,
    GenerateGraph,
)


def requires_model(request: UnitRequest) -> bool:
    if isinstance(request, CheckKnowledge):
        return request.semantic
    return isinstance(
        request,
        (
            ImportFile,
            ImportUrl,
            AskQuestion,
            ContinueConversation,
            RecompileDocument,
            RetrySource,
            RetryWorksheet,
            ReprocessSource,
            GenerateArtifact,
            RefreshKnowledge,
            RunPendingJob,
        ),
    )
