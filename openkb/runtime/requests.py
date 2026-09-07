"""Explicit execution requests; content stays in memory and local IPC."""

from __future__ import annotations

from dataclasses import dataclass, field


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

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Enter a message")


@dataclass(frozen=True)
class ImportFile:
    source: str

    def __post_init__(self) -> None:
        from pathlib import Path

        if not Path(self.source).is_absolute():
            raise ValueError("Import source must be an absolute path")


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
    version: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.file_hash, str)
            or not self.file_hash
            or not isinstance(self.version, str)
            or not self.version
        ):
            raise ValueError("Choose an indexed document to recompile")


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


UnitRequest = (
    SavePage
    | AskQuestion
    | ContinueConversation
    | ImportFile
    | ImportUrl
    | RemoveDocument
    | RecompileDocument
    | DeleteConversation
    | ExportConversation
)
REQUEST_TYPES = (
    SavePage,
    AskQuestion,
    ContinueConversation,
    ImportFile,
    ImportUrl,
    RemoveDocument,
    RecompileDocument,
    DeleteConversation,
    ExportConversation,
)
