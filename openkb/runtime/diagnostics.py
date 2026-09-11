"""Worker progress, SDK timing and bounded logs independent of a console."""

from __future__ import annotations

import contextlib
import functools
import io
import logging
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, TextIO, cast

from openkb.cancellation import OperationCancelled
from openkb.log import DiagnosticLog

_CURRENT: ContextVar[WorkerDiagnostics | None] = ContextVar("worker_diagnostics", default=None)


class _Lines(io.TextIOBase):
    def __init__(self, emit: Callable[[str], None]) -> None:
        self.emit = emit
        self.pending = ""
        self.overflow = False
        self.lock = threading.RLock()

    @property
    def encoding(self) -> str:
        return "utf-8"

    @encoding.setter
    def encoding(self, value: str) -> None:
        pass

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            # Click probes with b"" to distinguish binary streams. Reject even
            # empty bytes, or it wraps this text stream as a binary destination.
            raise TypeError("Diagnostic text stream requires str")
        with self.lock:
            for part in text.splitlines(keepends=True):
                self.pending += part
                if len(self.pending) > 16000:
                    self.pending, self.overflow = "", True
                if part.endswith(("\n", "\r")):
                    self.emit("运行输出过长，已省略" if self.overflow else self.pending)
                    self.pending, self.overflow = "", False
        return len(text)

    def flush(self) -> None:
        # A flush may split a credential across writes. Only emit whole lines;
        # model-start events provide progress before a spinner's final newline.
        pass

    def finish(self) -> None:
        with self.lock:
            if self.pending or self.overflow:
                self.emit("运行输出过长，已省略" if self.overflow else self.pending)
            self.pending, self.overflow = "", False


class _Messages(logging.Handler):
    def __init__(self, owner: WorkerDiagnostics) -> None:
        super().__init__(logging.WARNING)
        self.owner = owner

    def emit(self, record: logging.LogRecord) -> None:
        if "LoggingWorker" in record.getMessage():
            self.owner.warnings.add("model_logging_warning")
        # SDKs may already write this same record to our captured stderr.
        if any(
            getattr(h, "stream", None) is self.owner.stderr
            for h in logging.getLogger(record.name).handlers
        ):
            return
        self.owner.emit(f"{record.levelname} {record.name}: {self.format(record)}")


class WorkerDiagnostics:
    """One isolated worker owns streams, timings, redaction and its log file."""

    def __init__(self, path: Path, event: Callable[[dict], None]) -> None:
        self.log = DiagnosticLog(path)
        self.event = event
        self.stdout = _Lines(self.emit)
        self.stderr = _Lines(self.emit)
        self.handler = _Messages(self)
        self._stack = contextlib.ExitStack()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._active: dict[int, float] = {}
        self._sequence = 0
        self._sdk = None
        self.warnings: set[str] = set()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self) -> WorkerDiagnostics:
        token = _CURRENT.set(self)
        self._stack.callback(_CURRENT.reset, token)
        self._stack.enter_context(contextlib.redirect_stdout(cast(TextIO, self.stdout)))
        self._stack.enter_context(contextlib.redirect_stderr(cast(TextIO, self.stderr)))
        logging.getLogger().addHandler(self.handler)
        self._stack.callback(logging.getLogger().removeHandler, self.handler)
        self._thread.start()
        self.emit("工作进程已启动")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import traceback

        self._stop.set()
        self._thread.join(1)
        if isinstance(exc, OperationCancelled):
            self.emit("已响应停止请求，结束当前文档处理")
        elif exc is not None:
            # Preserve the final error before a provider's embedded request
            # causes the redactor to truncate the rest of a chained traceback.
            self.emit(
                f"{exc_type.__name__}: {exc}\n"
                + "".join(traceback.format_exception(exc_type, exc, tb))
            )
        self.stdout.finish()
        self.stderr.finish()
        self.emit(
            "工作进程执行结束" if exc is None else f"工作进程退出当前操作：{exc_type.__name__}"
        )
        self._stack.close()
        self.log.close()

    def emit(self, message: str, *, activity: bool = True) -> None:
        written = self.log.write(message)
        if written is not None:
            at, text = written
            try:
                self.event({"event": "diagnostic", "text": text, "at": at, "activity": activity})
            except (OSError, EOFError, ValueError):
                pass  # A disappearing progress observer cannot fail the work.

    def stage(self, value: dict) -> None:
        self.event(value)
        if value.get("stage"):
            source = Path(value["source"]).name if value.get("source") else ""
            self.emit(f"阶段：{value['stage']} {source}")

    def pulse(self) -> None:
        with self._lock:
            starts = tuple(self._active.values())
        message = (
            f"等待模型响应：{len(starts)} 个请求，最长已等待 {time.monotonic() - min(starts):.0f}s"
            if starts
            else "工作进程仍存活；当前阶段暂无新的模型请求输出"
        )
        self.emit(message, activity=False)

    def _heartbeat(self) -> None:
        while not self._stop.wait(15):
            self.pulse()

    def trace_sdk(self, sdk: Any) -> None:
        if self._sdk is not None:
            return
        self._sdk = sdk
        original_sync, original_async = sdk.completion, sdk.acompletion

        def start(kwargs):
            with self._lock:
                self._sequence += 1
                call_id = self._sequence
                self._active[call_id] = time.monotonic()
            output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
            limit = f" output_limit={output}" if type(output) is int else ""
            self.emit(f"LLM #{call_id} 开始：model={kwargs.get('model', 'unknown')}{limit}")
            return call_id

        def finish(call_id, result=None, error=None, *, streaming=False):
            with self._lock:
                began = self._active.pop(call_id)
            elapsed = time.monotonic() - began
            usage = getattr(result, "usage", None)
            tokens = (
                f" in={getattr(usage, 'prompt_tokens', '?')}"
                f" out={getattr(usage, 'completion_tokens', '?')}"
                if usage
                else ""
            )
            status = (
                "已取消"
                if isinstance(error, OperationCancelled)
                else f"失败：{type(error).__name__}"
                if error is not None
                else "流式响应已建立，继续接收内容"
                if streaming
                else "完成"
            )
            choices = getattr(result, "choices", None)
            reason = getattr(choices[0], "finish_reason", None) if choices else None
            finish = (
                f" finish_reason={reason}"
                if isinstance(reason, str)
                and reason in {"stop", "length", "tool_calls", "function_call", "content_filter"}
                else ""
            )
            self.emit(f"LLM #{call_id} {status} {elapsed:.1f}s{tokens}{finish}")

        @functools.wraps(original_sync)
        def sync(*args, **kwargs):
            call_id = start(kwargs)
            try:
                result = original_sync(*args, **kwargs)
            except BaseException as exc:
                finish(call_id, error=exc)
                raise
            finish(call_id, result=result, streaming=kwargs.get("stream", False))
            return result

        @functools.wraps(original_async)
        async def asynchronous(*args, **kwargs):
            call_id = start(kwargs)
            try:
                result = await original_async(*args, **kwargs)
            except BaseException as exc:
                finish(call_id, error=exc)
                raise
            finish(call_id, result=result, streaming=kwargs.get("stream", False))
            return result

        sdk.completion, sdk.acompletion = sync, asynchronous
        self._stack.callback(setattr, sdk, "completion", original_sync)
        self._stack.callback(setattr, sdk, "acompletion", original_async)


def install_llm_diagnostics(sdk: Any) -> None:
    """Install only after the worker has applied its captured SDK environment."""
    current = _CURRENT.get()
    if current is not None:
        current.trace_sdk(sdk)


def read_task_log(directory: Path, *, limit: int = 100_000) -> str:
    """Bounded tail for historical tasks; never load multi-megabyte logs in Qt."""
    chunks: list[str] = []
    remaining = limit
    paths = (p for p in directory.glob("*.log") if p.stem.isdecimal())
    for path in sorted(paths, key=lambda p: int(p.stem), reverse=True):
        if remaining <= 0:
            break
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - remaining))
                chunk = stream.read(remaining).decode("utf-8", errors="replace")
            chunks.append(chunk)
            remaining -= len(chunk.encode("utf-8"))
        except OSError:
            continue
    return "\n".join(reversed(chunks))
