"""Interactive multi-turn chat REPL for the UltraKB knowledge base.

Builds on the single-shot Q&A agent in ``ultrakb.agent.query`` and keeps
conversation state in the shared main store. Uses prompt_toolkit for the input
line (history, editing, bottom toolbar) and streams responses directly to
stdout to preserve the existing ``query`` visual.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.terminal_chat_prompt import (
    _bottom_toolbar as _bottom_toolbar,
)
from ultrakb.terminal_chat_prompt import (
    _ChatCompleter as _ChatCompleter,
)
from ultrakb.terminal_chat_prompt import (
    _make_prompt_session as _make_prompt_session,
)
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

    from ultrakb.application.skill_maintenance import critique_artifact

    try:
        target = Path(path)
        rel_str = target.relative_to(kb_dir).as_posix() if target.is_absolute() else path
        _fmt(style, ("class:slash.ok", f"Critiquing {rel_str}...\n"))
        await critique_artifact(kb_dir, rel_str, scope=scope)
    except Exception as exc:
        from ultrakb.application.connection_failures import operation_error_message

        _fmt(style, ("class:error", f"[ERROR] {operation_error_message(exc)}\n"))
        return

    _fmt(style, ("class:slash.ok", f"Critique pass complete: {rel_str}\n"))


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
