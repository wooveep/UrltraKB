"""Prompt editing, command completion and the disposable terminal input history."""

from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style

from ultrakb.terminal_chat_state import TerminalConversation

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
