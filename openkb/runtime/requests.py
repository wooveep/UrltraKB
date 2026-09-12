"""Explicit execution requests; content stays in memory and local IPC."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class SavePage:
    path: str
    body: str = field(repr=False)
    version: str

    def __post_init__(self) -> None:
        if not self.path or not isinstance(self.body, str) or not self.version:
            raise ValueError("Page saving requires a path, body and opened version")


@dataclass(frozen=True)
class AskQuestion:
    question: str = field(repr=False)
    save: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("Enter a question")


@dataclass(frozen=True)
class ContinueConversation:
    message: str = field(repr=False)
    session_id: str | None = None
    new_session_id: str | None = None
    attempt_id: str | None = None
    submission_order: int | None = None

    def __post_init__(self) -> None:
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
class ImportFile:
    source: str
    wait_for_stable: bool = False
    upload_origin: str | None = None

    def __post_init__(self) -> None:
        import re
        from pathlib import Path

        if not Path(self.source).is_absolute():
            raise ValueError("Import source must be an absolute path")
        if type(self.wait_for_stable) is not bool:
            raise ValueError("Invalid input stability policy")
        if self.upload_origin is not None and (
            not isinstance(self.upload_origin, str)
            or not re.fullmatch(r"upload:[0-9a-f]{32}/[0-9]+/[^/\\\x00]+", self.upload_origin)
            or self.wait_for_stable
        ):
            raise ValueError("Invalid uploaded source identity")


@dataclass(frozen=True)
class RemoveDocument:
    identifier: str
    version: str
    keep_raw: bool = False
    keep_empty: bool = False

    def __post_init__(self) -> None:
        if not self.identifier or not self.version:
            raise ValueError("Document removal requires a confirmed preview")


@dataclass(frozen=True)
class ImportUrl:
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 HTTP 或 HTTPS 地址")


@dataclass(frozen=True)
class RecompileDocument:
    file_hash: str
    version: str | None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_hash, str)
            or not self.file_hash
            or (self.version is None) == (self.source_revision is None)
            or (
                self.version is not None and (not isinstance(self.version, str) or not self.version)
            )
        ):
            raise ValueError("Choose an indexed document to recompile")
        if self.source_revision is not None:
            from openkb.sources import valid_id

            valid_id(self.source_revision)


@dataclass(frozen=True)
class ContinueSource:
    source_id: str
    version_id: str
    proposal_id: str | None = None
    accept_pages: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)
        if self.proposal_id is not None:
            valid_id(self.proposal_id)
        if self.accept_pages is not None and (
            self.proposal_id is None
            or not isinstance(self.accept_pages, tuple)
            or not all(isinstance(page, str) and page for page in self.accept_pages)
        ):
            raise ValueError("Acceptance requires the reviewed proposal and its exact pages")


@dataclass(frozen=True)
class ReparseSource:
    source_id: str
    version_id: str

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)


@dataclass(frozen=True)
class RebuildSourceNavigation:
    source_id: str
    version_id: str
    parse_id: str

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)
        valid_id(self.parse_id)


@dataclass(frozen=True)
class CleanupSourceHistory:
    preview_id: str

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.preview_id)


@dataclass(frozen=True)
class ReprocessSourcePage:
    source_id: str
    version_id: str
    parse_id: str
    page: int
    acknowledge_unknown: bool = False
    engine: str | None = None

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)
        valid_id(self.parse_id)
        if (
            type(self.page) is not int
            or self.page < 1
            or type(self.acknowledge_unknown) is not bool
            or self.engine not in {None, "system", "local", "cloud"}
        ):
            raise ValueError("Choose the reviewed physical page for a new OCR attempt")


@dataclass(frozen=True)
class ConfirmSourcePage:
    source_id: str
    version_id: str
    parse_id: str
    page: int
    reason: Literal["legitimate_blank", "legitimate_illustration"]

    def __post_init__(self) -> None:
        from openkb.sources import valid_id

        valid_id(self.source_id, source=True)
        valid_id(self.version_id)
        valid_id(self.parse_id)
        if (
            type(self.page) is not int
            or self.page < 1
            or self.reason
            not in {
                "legitimate_blank",
                "legitimate_illustration",
            }
        ):
            raise ValueError("Choose the reviewed physical page and its quality decision")


@dataclass(frozen=True)
class DeleteConversation:
    session_id: str
    version: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id or not self.version:
            raise ValueError("Conversation deletion requires a confirmed completed history")


@dataclass(frozen=True)
class ExportConversation:
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("Select a completed conversation to export")


@dataclass(frozen=True)
class CheckKnowledge:
    semantic: bool = True
    fix: bool = False
    version: str | None = None

    def __post_init__(self) -> None:
        if self.fix and (not isinstance(self.version, str) or not self.version):
            raise ValueError("Link repair requires confirmation of the latest wiki")


@dataclass(frozen=True)
class GenerateArtifact:
    target_type: Literal["skill", "deck"]
    name: str
    intent: str = field(repr=False)
    version: str
    replace: bool = False

    def __post_init__(self) -> None:
        from openkb.application.generators import GenerationOptions

        GenerationOptions(self.target_type, self.name, self.intent)
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Artifact generation requires the latest target version")


@dataclass(frozen=True)
class GenerateGraph:
    pass


UnitRequest = (
    SavePage
    | AskQuestion
    | ContinueConversation
    | ImportFile
    | ImportUrl
    | RemoveDocument
    | RecompileDocument
    | ContinueSource
    | ReparseSource
    | RebuildSourceNavigation
    | ReprocessSourcePage
    | CleanupSourceHistory
    | ConfirmSourcePage
    | DeleteConversation
    | ExportConversation
    | CheckKnowledge
    | GenerateArtifact
    | GenerateGraph
)
REQUEST_TYPES = (
    SavePage,
    AskQuestion,
    ContinueConversation,
    ImportFile,
    ImportUrl,
    RemoveDocument,
    RecompileDocument,
    ContinueSource,
    ReparseSource,
    RebuildSourceNavigation,
    ReprocessSourcePage,
    CleanupSourceHistory,
    ConfirmSourcePage,
    DeleteConversation,
    ExportConversation,
    CheckKnowledge,
    GenerateArtifact,
    GenerateGraph,
)
