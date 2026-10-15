"""Actual source and editable-page inputs shared by publication regressions."""

from dataclasses import replace

from ultrakb.agent.compile_messages import build_page_messages
from ultrakb.agent.page_updates import PageUpdate
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text


def update_inputs(
    kb_dir, original, source, *, name="rules", task="Update all required conditions."
):
    wiki = kb_dir / "wiki"
    page = wiki / f"concepts/{name}.md"
    atomic_write_text(page, original)
    atomic_write_text(wiki / "sources/manual.md", source)
    reader = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    originals = reader.prepare(
        {"name": name, "reading_locations": {"chars": f"0:{len(source)}"}},
        f"concepts/{name}.md",
        long=True,
    )
    patch = PageUpdate(original, originals=[source])

    def scoped(blocks, *, metadata=True, ranges=None):
        return PageUpdate(
            patch.original,
            known_targets=patch.known_targets,
            originals=patch.originals,
            allowed_types=patch.allowed_types,
            preserved_fragments=patch.preserved_fragments,
            allowed_blocks=(
                frozenset(blocks)
                if patch.allowed_blocks is None
                else frozenset(blocks) & patch.allowed_blocks
            ),
            allow_metadata=metadata,
            source_ranges=ranges if ranges is not None else patch.source_ranges,
        )

    def messages(batch, editable, extra=""):
        result = build_page_messages(
            system_message={
                "role": "system",
                "content": "Compile source facts into the wiki page.",
            },
            current_original=None,
            original_context=originals.for_windows(batch),
            known_targets_message={"role": "user", "content": name},
            task_message={"role": "user", "content": task + extra},
            update_message=editable.message(),
        )
        excerpts = reader.original_excerpts(replace(originals, windows=tuple(batch)), long=True)
        return excerpts.messages(result)

    return page, reader, originals, patch, scoped, messages
