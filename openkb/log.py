"""Append-only operation log for the wiki (log.md)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openkb.locks import atomic_write_text, kb_ingest_lock

logger = logging.getLogger(__name__)


def append_log(wiki_dir: Path, operation: str, description: str) -> None:
    """Append an entry to wiki/log.md.

    Format: ``## [YYYY-MM-DD HH:MM:SS] operation | description``
    """
    log_path = wiki_dir / "log.md"
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"## [{date_str}] {operation} | {description}\n\n"

    with kb_ingest_lock(wiki_dir.parent / ".openkb"):
        content = (
            log_path.read_text(encoding="utf-8") if log_path.exists() else "# Operations Log\n\n"
        )
        atomic_write_text(log_path, content + entry)


class DiagnosticLog:
    """Bounded, redacted process diagnostics outside the knowledge-base wiki."""

    def __init__(self, path: Path, *, max_bytes: int = 2_000_000) -> None:
        import os
        import threading

        self.path, self.max_bytes = path, max_bytes
        self._lock = threading.RLock()
        self._secrets: set[str] = set()
        self._file = None
        self.include_secrets(dict(os.environ))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")
        except OSError:
            pass  # Diagnostics must not turn a successful operation into failure.

    def include_secrets(self, values: dict) -> None:
        import re

        with self._lock:
            for key, value in values.items():
                if isinstance(value, dict):
                    self.include_secrets(value)
                    if str(key).lower() == "extra_headers":
                        self._secrets.update(
                            v for v in value.values() if isinstance(v, str) and len(v) >= 4
                        )
                elif (
                    isinstance(value, str)
                    and len(value) >= 4
                    and re.search(r"key|token|secret|password|authorization|cookie", str(key), re.I)
                ):
                    self._secrets.add(value)

    def redact(self, text: str) -> str:
        import re

        with self._lock:
            for secret in sorted(self._secrets, key=len, reverse=True):
                text = text.replace(secret, "<REDACTED>")
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        text = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer <REDACTED>", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<REDACTED>", text)
        text = re.sub(
            r"(?i)([\"']?(?:api[_-]?key|authorization|password|token|secret|cookie)[\"']?\s*[:=]\s*)[^\s,;}]+",
            r"\1<REDACTED>",
            text,
        )
        # Provider exceptions sometimes embed a full request. Keep metadata,
        # never copy prompts or document bodies into the diagnostic history.
        return re.sub(
            r"(?is)([\"']?(?:messages|prompt|input)[\"']?\s*[:=]).*", r"\1<REDACTED>", text
        )

    def write(self, message: str) -> tuple[str, str] | None:
        from datetime import timezone

        message = self.redact(message).strip()
        if not message or not message.strip(". "):
            return None
        message = message[:8000]
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"{at} {message}"
        with self._lock:
            if self._file is not None:
                try:
                    if self._file.tell() >= self.max_bytes:
                        self._file.close()
                        self.path.replace(self.path.with_suffix(".log.1"))
                        self._file = self.path.open("w", encoding="utf-8")
                    self._file.write(line + "\n")
                    self._file.flush()
                except OSError:
                    self.close()
        return at, line

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
