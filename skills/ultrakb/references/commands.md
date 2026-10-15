# UltraKB CLI reference

Read commands the skill calls on. Write commands are listed at the
bottom — the agent MUST NOT run them autonomously.

## `ultrakb status`

KB overview. First line carries the absolute path of the active KB
— parse it before any file read:

```
$ ultrakb status
Knowledge base: /path/to/kb
Knowledge Base Status:
  ...directory counts and timestamps...
```

Resolution: walks up from cwd, then falls back to `ultrakb use`'s
global default. Empty case prints "No knowledge base found. Run
`ultrakb init` first." — stop and tell the user; don't try to read.

## `ultrakb list`

Documents + concepts table. `Type` is mapped via `_TYPE_DISPLAY_MAP`:
long PDFs show as `pageindex`, everything else as `short` (the raw
file extension is internal and not exposed). `Pages` only populated
for long PDFs.

```
$ ultrakb list
Documents (N):
  Name              Type        Pages
  paper.pdf         pageindex   42
  notes.md          short
Summaries (N):
  - paper
Concepts (N):
  - attention
```

## `ultrakb query "<question>"`

Full RAG pipeline — costs an LLM call inside ultrakb. Use only when
no obvious slug matches and direct reads can't answer. Returns
free-form answer text plus cited `[[concepts/...]]` / `[[summaries/...]]`
paths. Add `--save` to persist to `wiki/explorations/<slug>.md` —
only when the user asks for it.

## Read-only commands the skill should NOT call

- `ultrakb chat` — interactive REPL
- `ultrakb watch` — daemon
- `ultrakb lint` — health-check report (run only if the user
  explicitly asks about wiki health)

## Write commands — MUST NOT run autonomously

These mutate the user's knowledge base. Suggest with a one-line
description of what they do; let the user run them:

- `ultrakb add <path>` — ingest a document (LLM cost, modifies wiki)
- `ultrakb remove <doc>` — destructive removal
- `ultrakb lint --fix` — auto-edits wiki pages
- `ultrakb init` — one-time KB setup
- `ultrakb use <path>` — set the default KB

Also: never directly `Edit`/`Write` any file under `<kb>/wiki/` or
`<kb>/.ultrakb/`. That's the user's curated content (and ultrakb's
internal state) — the agent must not patch it directly.
