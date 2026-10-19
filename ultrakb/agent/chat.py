"""Interactive multi-turn chat REPL for the UltraKB knowledge base.

Builds on the single-shot Q&A agent in ``ultrakb.agent.query`` and keeps
conversation state in ``ChatSession``. Uses prompt_toolkit for the input
line (history, editing, bottom toolbar) and streams responses directly to
stdout to preserve the existing ``query`` visual.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path
from threading import Event
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import CompleteStyle, print_formatted_text
from prompt_toolkit.styles import Style

from ultrakb.agent.chat_session import ChatSession
from ultrakb.agent.query import (
    MAX_TURNS,
    build_chat_agent,
    iter_agent_response_events,
)
from ultrakb.config import LlmCredentialBundle
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.log import append_log
from ultrakb.model_outputs import ModelOutputs, model_output_scope
from ultrakb.terminal_chat_state import TerminalConversation

_STYLE_DICT: dict[str, str] = {
    "prompt": "bold #5fa0e0",
    "bottom-toolbar": "noreverse nobold #8a8a8a bg:default",
    "toolbar": "noreverse nobold #8a8a8a bg:default",
    "toolbar.session": "noreverse #8a8a8a bg:default bold",
    "header": "#8a8a8a",
    "header.title": "bold #5fa0e0",
    "tool": "#a8a8a8",
    "tool.name": "#a8a8a8 bold",
    "slash.ok": "ansigreen",
    "slash.help": "#8a8a8a",
    "error": "ansired bold",
    "resume.turn": "#5fa0e0",
    "resume.user": "bold",
    "resume.assistant": "#8a8a8a",
    # Completion menu — lightweight, no heavy background
    "completion-menu": "bg:default #8a8a8a",
    "completion-menu.completion": "bg:default #d0d0d0",
    "completion-menu.completion.current": "bg:#3a3a3a #ffffff bold",
    "completion-menu.meta.completion": "bg:default #6a6a6a",
    "completion-menu.meta.completion.current": "bg:#3a3a3a #8a8a8a",
}

_HELP_TEXT = (
    "Commands:\n"
    "  /exit          Exit (Ctrl-D also works)\n"
    "  /clear         Start a fresh session (current one is kept on disk)\n"
    "  /save [name]   Export transcript to wiki/explorations/\n"
    "  /status        Show knowledge base status\n"
    "  /list          List all documents in the knowledge base\n"
    "  /lint          Lint the knowledge base\n"
    "  /add <path>    Add a document or directory to the knowledge base\n"
    '  /skill new <name> "<intent>"   Compile a skill from the wiki\n'
    '  /deck new [--critique] [--skill <name>] <name> "<intent>"   Generate an HTML deck from the wiki\n'
    "  /critique <path-to-html>   Run html-critic skill on a file (CSS bugs, layout, self-containment)\n"
    "  /help          Show this"
)

_SIGINT_EXIT_WINDOW = 2.0

# The read-only tools whose calls are recorded into a chat turn's persisted
# trace. Mirrors the frontend's toolCallSource whitelist (chat.ts): these are
# the only tool reads the UI renders, and limiting the trace to them keeps a
# non-read tool's large arguments (e.g. write_file's full file `content`) out
# of the session JSON and off the restore wire.
_TRACE_READ_TOOLS = frozenset({"read_file", "get_page_content"})


def _use_color(force_off: bool) -> bool:
    if force_off:
        return False
    if os.environ.get("NO_COLOR", ""):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def _build_style(use_color: bool) -> Style:
    return Style.from_dict(_STYLE_DICT if use_color else {})


def _fmt(style: Style, *fragments: tuple[str, str]) -> None:
    # prompt_toolkit's print_formatted_text constructs a Win32Output on
    # Windows that requires a real console handle, raising
    # NoConsoleScreenBufferError when stdout is a pipe, file, or captured
    # subprocess stream. Fall back to plain text when the output isn't a
    # usable console.
    if not _use_color(force_off=False):
        for _, text in fragments:
            sys.stdout.write(text)
        sys.stdout.flush()
        return
    print_formatted_text(FormattedText(list(fragments)), style=style, end="")


def _format_tool_line(name: str, args: str, width: int = 78) -> str:
    args = args or ""
    args = args.replace("\n", " ")
    base = f"  \u00b7 {name}({args})"
    if len(base) > width:
        base = base[: width - 1] + "\u2026"
    return base


def _extract_preview(text: str, limit: int = 150) -> str:
    text = " ".join((text or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _ultrakb_version() -> str:
    from ultrakb import __version__

    return __version__


def _display_kb_dir(kb_dir: Path) -> str:
    home = str(Path.home())
    s = str(kb_dir)
    if s == home:
        return "~"
    if s.startswith(home + "/"):
        return "~" + s[len(home) :]
    return s


def _repl_scope(kb_dir: Path, scope: KnowledgeScope | None, *, cancelled=None) -> KnowledgeScope:
    """A new command sees the current head, within the captured database generation."""
    selected = resolve_scope(kb_dir, scope)
    if selected.knowledge is not None:
        from ultrakb.application.views import storage_scope

        return storage_scope(
            kb_dir, selected.knowledge.identity, view_id=selected.view_id, cancelled=cancelled
        )
    return selected


def _print_header(session: TerminalConversation, kb_dir: Path, style: Style) -> None:
    disp_dir = _display_kb_dir(kb_dir)
    version = _ultrakb_version()
    version_suffix = f" v{version}\n" if version else "\n"
    print()
    _fmt(
        style,
        ("class:header.title", "UltraKB Chat"),
        ("class:header", version_suffix),
    )
    _fmt(
        style,
        (
            "class:header",
            f"{disp_dir} \u00b7 {session.model} \u00b7 session {session.id}\n",
        ),
    )
    _fmt(
        style,
        (
            "class:header",
            "Type /help for commands, Ctrl-D to exit, Ctrl-C to abort current response.\n",
        ),
    )
    print()


def _print_resume_view(session: TerminalConversation, style: Style) -> None:
    turns = list(zip(session.user_turns, session.assistant_texts))
    if not turns:
        return
    total = session.turn_count
    omitted = total - min(5, len(turns))
    if omitted:
        _fmt(
            style,
            ("class:header", f"... {omitted} earlier turn(s) omitted\n"),
        )
        turns = turns[-5:]
        start = omitted + 1
    else:
        start = 1

    _fmt(
        style,
        ("class:header", f"Resumed session  {total} turn(s)\n"),
    )
    for i, (u, a) in enumerate(turns, start):
        _fmt(
            style,
            ("class:resume.turn", f"[{i}] "),
            ("class:resume.user", f">>> {u}\n"),
        )
        if a:
            preview = _extract_preview(a, 180)
            extra = ""
            if len(a) > len(preview):
                extra = f"  ({len(a)} chars)"
            _fmt(
                style,
                ("class:resume.turn", f"[{i}] "),
                ("class:resume.assistant", f"    {preview}{extra}\n"),
            )
    print()


def _bottom_toolbar(session: TerminalConversation) -> FormattedText:
    return FormattedText(
        [
            ("class:toolbar", " session "),
            ("class:toolbar.session", session.id),
            (
                "class:toolbar",
                f"  {session.turn_count} turn(s)  {session.model} ",
            ),
        ]
    )


_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/exit", "Exit (Ctrl-D also works)"),
    ("/quit", "Exit (alias)"),
    ("/help", "Show available commands"),
    ("/clear", "Start a fresh session"),
    ("/save", "Export transcript to wiki/explorations/"),
    ("/status", "Show knowledge base status"),
    ("/list", "List all documents"),
    ("/lint", "Lint the knowledge base"),
    ("/add", "Add a document or directory"),
    ("/skill", 'Compile a skill (try `/skill new <name> "intent"`)'),
    ("/deck", 'Generate a deck (try `/deck new <name> "intent"`)'),
    ("/critique", "Run html-critic skill on a file (e.g. `/critique output/decks/foo/index.html`)"),
]


class _ChatCompleter(Completer):
    """Complete slash commands and file paths after /add."""

    def __init__(self) -> None:
        self._path_completer = PathCompleter(expanduser=True)

    def get_completions(self, document: Document, complete_event: Any) -> Any:
        text = document.text_before_cursor

        # After "/add ", complete file paths (skip dotfiles)
        if text.lstrip().lower().startswith("/add "):
            path_text = text.lstrip()[5:]
            # Strip leading quote so PathCompleter resolves the real path
            quote_char = ""
            if path_text and path_text[0] in ("'", '"'):
                quote_char = path_text[0]
                path_text = path_text[1:]
            path_doc = Document(path_text, len(path_text))
            for c in self._path_completer.get_completions(path_doc, complete_event):
                # Hide dotfiles unless the user explicitly typed a dot
                basename = c.text.lstrip("/")
                if basename.startswith(".") and not path_text.rpartition("/")[2].startswith("."):
                    continue
                # Append closing quote for files; skip for directories so
                # the user can keep navigating into subdirectories.
                if quote_char and not c.text.endswith("/"):
                    comp_text = c.text + quote_char
                else:
                    comp_text = c.text
                yield Completion(
                    comp_text,
                    start_position=c.start_position,
                    display=c.display,
                    display_meta=c.display_meta,
                )
            return

        # Complete slash commands with descriptions
        if text.startswith("/"):
            for cmd, desc in _SLASH_COMMANDS:
                if cmd.startswith(text.lower()):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)


def _make_prompt_session(
    session: TerminalConversation, style: Style, use_color: bool, kb_dir: Path
) -> PromptSession:
    from prompt_toolkit.filters import has_completions
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("tab", filter=has_completions)
    def _accept_completion(event: Any) -> None:
        """Tab accepts the current completion (like zsh), not cycle."""
        buf = event.current_buffer
        state = buf.complete_state
        if not state:
            return
        # Only one candidate or already selected — accept immediately
        if state.current_completion:
            buf.apply_completion(state.current_completion)
        elif len(state.completions) == 1:
            buf.apply_completion(state.completions[0])
        else:
            # Multiple candidates, nothing selected — highlight first
            buf.go_to_completion(0)

    @kb.add("tab", filter=~has_completions)
    def _trigger_completion(event: Any) -> None:
        """Tab triggers completion when menu is not open."""
        buf = event.current_buffer
        buf.start_completion()

    history_path = kb_dir / ".ultrakb" / "chat_history"
    return PromptSession(
        message=FormattedText([("class:prompt", ">>> ")]),
        style=style,
        completer=_ChatCompleter(),
        complete_style=CompleteStyle.MULTI_COLUMN,
        complete_while_typing=False,
        key_bindings=kb,
        history=FileHistory(str(history_path)),
        bottom_toolbar=(lambda: _bottom_toolbar(session)) if use_color else None,
    )


def _make_rich_console() -> Any:
    from rich.console import Console

    return Console()


def _make_markdown(text: str) -> Any:
    from ultrakb.agent._markdown import render

    return render(text)


async def _run_turn(
    session: TerminalConversation,
    user_input: str,
    style: Style,
    *,
    use_color: bool,
    raw: bool = False,
    scope: KnowledgeScope | None = None,
) -> None:
    from ultrakb.application.conversations import continue_conversation
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.context.workers import run_blocking
    from ultrakb.terminal_answers import AnswerRenderer

    root = session.kb_dir
    stopped = Event()
    selected = await run_blocking(
        lambda: _repl_scope(root, scope or session.scope, cancelled=stopped.is_set),
        on_cancel=stopped.set,
    )
    identity = selected.knowledge.identity if selected.knowledge else None
    with AnswerRenderer(use_color=use_color, raw=raw) as renderer:
        context = ExecutionContext(
            on_event=renderer, storage_identity=identity, cancelled=stopped.is_set
        )
        result = await continue_conversation(
            root,
            user_input,
            session_id=session.id if session.persisted else None,
            new_session_id=session.id if not session.persisted else None,
            context=context,
            scope=selected,
        )
        if result.status == "completed":
            renderer.finish(result.answer)
    await run_blocking(lambda: session.reload(context=context), on_cancel=stopped.set)
    if result.status != "completed":
        raise RuntimeError(result.error or result.status)


def _save_transcript(
    kb_dir: Path,
    session: TerminalConversation,
    name: str | None,
    *,
    scope: KnowledgeScope | None = None,
    context=None,
):
    scope = resolve_scope(kb_dir, scope)
    from ultrakb.application.sessions import export_conversation

    return export_conversation(kb_dir, session.id, name, scope=scope, context=context)


async def _run_add(
    arg: str, kb_dir: Path, style: Style, *, scope: KnowledgeScope | None = None
) -> None:
    """Add a document or directory to the knowledge base from the chat REPL."""
    scope = resolve_scope(kb_dir, scope)
    from ultrakb.application.documents import add_single_file
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.inputs import SUPPORTED_EXTENSIONS

    context = ExecutionContext()

    def report(line):
        _fmt(style, ("class:slash.help", line + "\n"))

    target = Path(arg).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()

    if not target.exists():
        _fmt(style, ("class:error", f"Path does not exist: {arg}\n"))
        return

    if target.is_dir():
        files = [
            f
            for f in sorted(target.rglob("*"))
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            _fmt(style, ("class:error", f"No supported files found in {arg}.\n"))
            return
        total = len(files)
        _fmt(style, ("class:slash.help", f"Found {total} supported file(s) in {arg}.\n"))
        for i, f in enumerate(files, 1):
            _fmt(style, ("class:slash.help", f"\n[{i}/{total}] "))
            await asyncio.to_thread(
                add_single_file, f, kb_dir, scope=scope, context=context, report=report
            )
    else:
        if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
            _fmt(style, ("class:error", f"Unsupported file type: {target.suffix}\n"))
            return
        await asyncio.to_thread(
            add_single_file, target, kb_dir, scope=scope, context=context, report=report
        )


async def _handle_slash_skill(
    arg: str, kb_dir: Path, style: Style, *, scope: KnowledgeScope | None = None
) -> None:
    """Dispatch ``/skill new <name> "<intent>"`` and any future skill subcommands."""
    import shlex

    try:
        parts = shlex.split(arg) if arg else []
    except ValueError as exc:
        _fmt(style, ("class:error", f"[ERROR] Could not parse: {exc}\n"))
        return
    if not parts:
        _fmt(style, ("class:error", 'Usage: /skill new <name> "<intent>"\n'))
        return

    sub = parts[0].lower()
    if sub != "new":
        _fmt(style, ("class:error", f"Unknown skill subcommand: {sub}. Try /skill new.\n"))
        return

    if len(parts) < 3:
        _fmt(style, ("class:error", 'Usage: /skill new <name> "<intent>"\n'))
        return

    name = parts[1]
    intent = " ".join(parts[2:])

    # Use the same safety gates as the CLI (name validation, wiki dir,
    # wiki content). Chat doesn't have a -y flag, so existing skills
    # block with a clear instruction to delete first.
    from ultrakb.application.generators import preflight_generation as _preflight_skill_new

    err = _preflight_skill_new(kb_dir, name, scope=scope)
    if err:
        _fmt(style, ("class:error", f"[ERROR] {err}\n"))
        return

    from ultrakb.skill import skill_dir

    target = skill_dir(kb_dir, name)
    if target.exists():
        _fmt(
            style,
            (
                "class:error",
                f"[ERROR] output/skills/{name}/ already exists. Remove it first "
                f"with `rm -rf output/skills/{name}` and re-run.\n",
            ),
        )
        return

    from ultrakb.application.generators import GenerationOptions, generate_artifact

    _fmt(style, ("class:slash.help", f"Compiling skill '{name}'...\n"))
    gen = await generate_artifact(kb_dir, GenerationOptions("skill", name, intent), scope=scope)
    if gen.status != "completed":
        _fmt(style, ("class:error", f"[ERROR] {gen.message}\n"))
        return

    # Surface validation issues from Generator.run. Unlike the CLI
    # (which exits 1 on validation errors), chat is interactive — print
    # issues inline and continue so the user can inspect and iterate.
    result = gen.validation
    if result is not None and (result.errors or result.warnings):
        _fmt(style, ("class:error", "[WARN] Validation found issues:\n"))
        for err in result.errors:
            _fmt(style, ("class:error", f"  ERROR:   {err}\n"))
        for warn in result.warnings:
            _fmt(style, ("class:error", f"  WARN:    {warn}\n"))
        _fmt(
            style,
            (
                "class:slash.help",
                f"Run `ultrakb skill validate {name}` to re-check, or "
                f"`ultrakb skill rollback {name}` to revert.\n",
            ),
        )

    _fmt(style, ("class:slash.ok", f"Saved: output/skills/{name}/\n"))
    _fmt(
        style,
        (
            "class:slash.help",
            f"Iterate: ask follow-up questions in this chat and the agent can "
            f"edit files under output/skills/{name}/ directly.\n",
        ),
    )


async def _handle_slash_deck(
    arg: str, kb_dir: Path, style: Style, *, scope: KnowledgeScope | None = None
) -> None:
    """Dispatch ``/deck new [--critique] <name> "<intent>"``.

    Mirrors :func:`_handle_slash_skill`: validates the name, runs the
    same wiki preflight gate, refuses to overwrite an existing deck
    (chat has no ``-y`` flag), then invokes ``Generator(target_type="deck")``.
    """
    import shlex

    try:
        parts = shlex.split(arg) if arg else []
    except ValueError as exc:
        _fmt(style, ("class:error", f"[ERROR] Could not parse: {exc}\n"))
        return
    if not parts:
        _fmt(style, ("class:error", 'Usage: /deck new [--critique] <name> "<intent>"\n'))
        return

    sub = parts[0].lower()
    if sub != "new":
        _fmt(style, ("class:error", f"Unknown deck subcommand: {sub}. Try /deck new.\n"))
        return

    # Parse optional --critique flag and --skill <name> option. Both can
    # appear anywhere among the remaining tokens.
    rest = parts[1:]
    critique = False
    skill_name: str | None = None
    filtered: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--critique":
            critique = True
        elif tok == "--skill" and i + 1 < len(rest):
            skill_name = rest[i + 1]
            i += 1
        elif tok.startswith("--skill="):
            skill_name = tok.split("=", 1)[1]
        else:
            filtered.append(tok)
        i += 1

    if len(filtered) < 2:
        _fmt(
            style,
            ("class:error", 'Usage: /deck new [--critique] [--skill <skill>] <name> "<intent>"\n'),
        )
        return

    name = filtered[0]
    intent = " ".join(filtered[1:])

    # Reuse the shared safety gates from the CLI (name validation,
    # wiki dir, wiki content). Chat has no -y flag, so existing decks
    # block with a clear instruction to delete first.
    from ultrakb.application.generators import preflight_generation as _preflight_skill_new

    err = _preflight_skill_new(kb_dir, name, scope=scope)
    if err:
        # Reword "Skill name" → "Deck name" so error matches the command.
        err = err.replace("Skill name", "Deck name")
        _fmt(style, ("class:error", f"[ERROR] {err}\n"))
        return

    from ultrakb.deck import deck_dir

    target = deck_dir(kb_dir, name)
    if target.exists():
        _fmt(
            style,
            (
                "class:error",
                f"[ERROR] output/decks/{name}/ already exists. Remove it first "
                f"with `rm -rf output/decks/{name}` and re-run.\n",
            ),
        )
        return

    from ultrakb.application.generators import GenerationOptions, generate_artifact
    from ultrakb.deck.creator import DEFAULT_DECK_SKILL

    skill_label = skill_name if skill_name else f"{DEFAULT_DECK_SKILL} (default)"
    _fmt(
        style,
        ("class:slash.help", f"Generating deck '{name}' via skill {skill_label}...\n"),
    )
    gen = await generate_artifact(
        kb_dir,
        GenerationOptions("deck", name, intent, critique=critique, skill_name=skill_name),
        scope=scope,
    )
    if gen.status != "completed":
        _fmt(style, ("class:error", f"[ERROR] {gen.message}\n"))
        return

    # Surface validation issues from Generator.run. Unlike the CLI
    # (which exits 1 on validation errors), chat is interactive — print
    # issues inline and continue so the user can inspect and iterate.
    result = gen.validation
    if result is not None and (result.errors or result.warnings):
        _fmt(style, ("class:error", "[WARN] Validation found issues:\n"))
        for err in result.errors:
            _fmt(style, ("class:error", f"  ERROR:   {err}\n"))
        for warn in result.warnings:
            _fmt(style, ("class:error", f"  WARN:    {warn}\n"))

    _fmt(style, ("class:slash.ok", f"Saved: output/decks/{name}/index.html\n"))
    _fmt(
        style,
        (
            "class:slash.help",
            f"Iterate: ask follow-up questions in this chat and the agent can "
            f"edit files under output/decks/{name}/ directly.\n",
        ),
    )


async def _handle_slash(
    cmd: str,
    kb_dir: Path,
    session: TerminalConversation,
    style: Style,
    *,
    scope: KnowledgeScope | None = None,
) -> str | None:
    """Return ``"exit"`` to end the REPL, ``"new_session"`` to swap sessions,
    or ``None`` to continue with the current session."""
    query_scope = scope
    scope = resolve_scope(kb_dir, scope)
    parts = cmd.split(maxsplit=1)
    head = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    # Strip surrounding quotes (user may type /add '/path/to file')
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ("'", '"'):
        arg = arg[1:-1]
    elif arg and arg[0] in ("'", '"'):
        arg = arg[1:]

    if head in ("/exit", "/quit"):
        _fmt(style, ("class:header", "Bye. Thanks for using UltraKB.\n\n"))
        return "exit"

    if head == "/help":
        _fmt(style, ("class:slash.help", _HELP_TEXT + "\n"))
        return None

    if head == "/clear":
        old_id = session.id
        _fmt(
            style,
            ("class:slash.ok", f"Started new session (previous: {old_id})\n"),
        )
        return "new_session"

    if head == "/save":
        if not session.user_turns:
            _fmt(style, ("class:error", "Nothing to save yet.\n"))
            return None
        from ultrakb.application.execution import ExecutionContext
        from ultrakb.context.workers import run_blocking

        stopped = Event()
        context = ExecutionContext(cancelled=stopped.is_set)
        result = await run_blocking(
            lambda: _save_transcript(
                kb_dir,
                session,
                arg or None,
                scope=_repl_scope(kb_dir, scope, cancelled=stopped.is_set),
                context=context,
            ),
            on_cancel=stopped.set,
        )
        if result.status == "exported":
            _fmt(style, ("class:slash.ok", f"Saved to {result.resources[0]}\n"))
            if result.publication_id:
                _fmt(style, ("class:slash.ok", f"Publication: {result.publication_id}\n"))
            if result.projection and result.projection.status not in {"synced", "obsolete"}:
                _fmt(
                    style,
                    ("class:slash.help", f"Wiki synchronization: {result.projection.status}\n"),
                )
        else:
            _fmt(style, ("class:error", f"Transcript export: {result.status}\n"))
        if result.candidate_revision_id:
            _fmt(style, ("class:slash.help", f"Candidate: {result.candidate_revision_id}\n"))
        return None

    if head == "/status":
        from ultrakb.application.execution import ExecutionContext
        from ultrakb.context.workers import run_blocking
        from ultrakb.terminal_maintenance import print_status

        stopped = Event()
        context = ExecutionContext(cancelled=stopped.is_set)
        await run_blocking(
            lambda: print_status(kb_dir, scope=scope, context=context), on_cancel=stopped.set
        )
        return None

    if head == "/list":
        from ultrakb.application.execution import ExecutionContext
        from ultrakb.context.workers import run_blocking
        from ultrakb.terminal_maintenance import print_list

        stopped = Event()
        context = ExecutionContext(cancelled=stopped.is_set)
        await run_blocking(
            lambda: print_list(kb_dir, scope=scope, context=context), on_cancel=stopped.set
        )
        return None

    if head == "/lint":
        from ultrakb.terminal_maintenance import run_lint

        await run_lint(kb_dir, scope=scope)
        return None

    if head == "/add":
        if not arg:
            _fmt(style, ("class:error", "Usage: /add <path>\n"))
            return None
        await _run_add(arg, kb_dir, style, scope=scope)
        return None

    if head == "/skill":
        await _handle_slash_skill(arg, kb_dir, style, scope=query_scope)
        return None

    if head == "/deck":
        await _handle_slash_deck(arg, kb_dir, style, scope=query_scope)
        return None

    if head == "/critique":
        await _handle_slash_critique(arg, kb_dir, style, scope=query_scope)
        return None

    _fmt(
        style,
        ("class:error", f"Unknown command: {head}. Try /help.\n"),
    )
    return None


async def _handle_slash_critique(
    arg: str, kb_dir: Path, style: Style, *, scope: KnowledgeScope | None = None
) -> None:
    """``/critique <path>`` — run the ultrakb-html-critic skill on a file.

    The skill reads the HTML, fixes CSS specificity bugs / missing nav /
    self-containment violations, and writes the corrected file back. It
    will not touch slide content (numbers, names, quotes).
    """
    path = arg.strip()
    if not path:
        _fmt(
            style,
            ("class:slash.help", "Usage: /critique <path-to-html>\n"),
        )
        return

    target = (kb_dir / path).resolve() if not Path(path).is_absolute() else Path(path)
    if not target.is_file():
        _fmt(style, ("class:error", f"[ERROR] File not found: {path}\n"))
        return

    from ultrakb.application.skill_maintenance import critique_artifact

    rel_str = str(target.relative_to(kb_dir)) if target.is_relative_to(kb_dir) else str(target)
    _fmt(style, ("class:slash.ok", f"Critiquing {rel_str}...\n"))
    try:
        await critique_artifact(kb_dir, path, scope=scope)
    except (RuntimeError, ValueError, OSError) as exc:
        _fmt(style, ("class:error", f"[ERROR] {exc}\n"))
        return

    _fmt(style, ("class:slash.ok", f"Critique pass complete: {rel_str}\n"))


def build_chat_session_agent(
    kb_dir: Path,
    session: ChatSession,
    bundle: "LlmCredentialBundle | None" = None,
    *,
    scope: KnowledgeScope | None = None,
) -> Any:
    """Build the write- and skill-capable agent for one chat session (REST ``/chat``).

    Thin wrapper that resolves language from the session/config and delegates
    to ``build_chat_agent`` with the session's model. Kept in chat.py (not
    query.py) so the query module's ``build_chat_agent`` signature stays
    unchanged for the CLI; the API uses this session-aware variant instead.
    """
    from ultrakb.config import resolve_effective_config

    scope = resolve_scope(kb_dir, scope)
    session.require_view(scope.view_id)
    config = resolve_effective_config(kb_dir)[0]
    language = session.language or config.get("language", "en")
    return build_chat_agent(kb_dir, session.model, language=language, bundle=bundle, scope=scope)


async def iter_chat_turn_events(
    agent: Any,
    session: ChatSession,
    user_input: str,
    *,
    run_config: Any = None,
    outputs: ModelOutputs | None = None,
    attempt_id: str | None = None,
    scope: KnowledgeScope | None = None,
    prepared_input=None,
    selection=None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield non-TTY events for one chat turn and persist the final turn.

    Used by the REST ``/chat`` SSE stream: forwards delta/tool_call events from
    ``iter_agent_response_events`` unchanged, and on the final event records
    the turn into the session (history + counts) before re-emitting it with
    ``session_id``/``turn_count``.
    """
    from ultrakb.locks import async_kb_lock, async_session_lock

    kb_dir = session.path.parent.parent.parent
    query_scope = scope
    scope = resolve_scope(kb_dir, scope)
    async with async_session_lock(kb_dir, session.id):
        async with async_kb_lock(kb_dir / ".ultrakb", exclusive=True):
            session.reload()
            session.require_view(scope.view_id)
            append_log(scope.wiki_dir, "query", user_input, scope=scope)
            from ultrakb.context.preparation import recent_input

            new_input = (prepared_input or recent_input(session, user_input)).sdk_input
            from ultrakb.application.query_views import resolve_query_views

            selection = selection or resolve_query_views(kb_dir, user_input, scope=query_scope)

            # Accumulate the ordered, interleaved trace (narration text + tool reads) in
            # SSE arrival order, mirroring the frontend's live fold, so a RESTORED turn
            # renders step-by-step identically instead of collapsing to one text block.
            # Persisted per turn via record_turn(trace=...). Only READ tools go into the
            # trace (see _TRACE_READ_TOOLS): they are all the frontend renders, and
            # recording a non-read tool like write_file would persist its full `content`
            # argument (the whole generated file) into the session JSON and ship it to
            # the browser on restore only to be dropped — the same large-payload waste
            # the `final` frame already avoids by stripping history.
            trace: list[dict[str, Any]] = []

            data = None
            base_usage = session.token_usage
            usage_recorded = False
            with model_output_scope(kb_dir, outputs):
                stream = iter_agent_response_events(
                    agent,
                    new_input,
                    max_turns=MAX_TURNS,
                    run_config=run_config,
                    selection=selection,
                    readers_bound=prepared_input is not None,
                )
                async with aclosing(stream):
                    async for event in stream:
                        kind = event["event"]
                        if kind == "usage":
                            session.record_usage(event["data"], baseline=base_usage)
                            usage_recorded = True
                            yield event
                            continue
                        if kind in {"delta", "tool_call"}:
                            from ultrakb.agent.chat_trace import append_event

                            append_event(trace, event, read_tools=_TRACE_READ_TOOLS)
                            yield event
                            continue
                        if kind != "final":
                            yield event
                            continue

                        data = event["data"]
                        answer = data["answer"]
                        from ultrakb.agent.chat_trace import complete_trace

                        complete_trace(trace, answer)
                        break
            if data is not None and "answer" in data:
                from ultrakb.llm_execution import check_model_stop

                check_model_stop()
                from ultrakb.application.execution import activity

                with activity("saving"):
                    session.record_turn(
                        user_input,
                        answer,
                        data["history"],
                        trace=trace,
                        attempt_id=attempt_id,
                        usage=None if usage_recorded else data.get("usage"),
                        turn_usage=data.get("usage"),
                        citations=data.get("citations"),
                        answer_outcome=data.get("answer_outcome"),
                    )
                yield {
                    "event": "final",
                    "data": {
                        "answer": answer,
                        "session_id": session.id,
                        "turn_count": session.turn_count,
                        "usage": data.get("usage"),
                        "answer_outcome": data.get("answer_outcome"),
                        "unfinished": data.get("unfinished", ()),
                    },
                }


async def run_chat(
    kb_dir: Path,
    session: TerminalConversation,
    *,
    no_color: bool = False,
    raw: bool = False,
    scope: KnowledgeScope | None = None,
) -> None:
    """Run the chat REPL against ``session`` until the user exits."""
    query_scope = scope
    scope = resolve_scope(kb_dir, scope)
    session.require_view(scope.view_id)
    use_color = _use_color(force_off=no_color)
    style = _build_style(use_color)

    _print_header(session, kb_dir, style)
    if session.turn_count > 0:
        _print_resume_view(session, style)

    prompt_session = _make_prompt_session(session, style, use_color, kb_dir)

    last_sigint = 0.0

    while True:
        try:
            user_input = await prompt_session.prompt_async()
            last_sigint = 0.0
        except KeyboardInterrupt:
            now = time.monotonic()
            if last_sigint and (now - last_sigint) < _SIGINT_EXIT_WINDOW:
                _fmt(style, ("class:header", "\nBye. Thanks for using UltraKB.\n\n"))
                return
            last_sigint = now
            _fmt(style, ("class:header", "\n(Press Ctrl-C again to exit)\n"))
            continue
        except EOFError:
            _fmt(style, ("class:header", "Bye. Thanks for using UltraKB.\n\n"))
            return

        user_input = (user_input or "").strip()
        if not user_input:
            continue

        if user_input.startswith("/"):
            try:
                action = await _handle_slash(user_input, kb_dir, session, style, scope=query_scope)
            except KeyboardInterrupt:
                _fmt(style, ("class:error", "\n[aborted]\n"))
                continue
            except Exception as exc:
                _fmt(style, ("class:error", f"[ERROR] {exc}\n"))
                continue
            if action == "exit":
                return
            if action == "new_session":
                session = TerminalConversation.new(
                    kb_dir, session.model, session.language, scope=scope
                )
                prompt_session = _make_prompt_session(session, style, use_color, kb_dir)
            continue

        try:
            await _run_turn(
                session, user_input, style, use_color=use_color, raw=raw, scope=query_scope
            )
        except KeyboardInterrupt:
            _fmt(style, ("class:error", "\n[aborted]\n"))
        except Exception as exc:
            _fmt(style, ("class:error", f"[ERROR] {exc}\n"))
