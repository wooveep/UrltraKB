# Generated artifacts and evidence

Skill, Deck and nested Query agents share the task's original-source snapshot,
model credentials and request budget. Source tools expose version-bound text,
table context and image references; reading a wiki page alone does not establish
an original-source citation. Hidden provider retries are disabled for these agents
so one admitted SDK request corresponds to one transport attempt.

Generation checks the saved Markdown and HTML, including supporting files changed
by the task. Only observed, valid original bindings can expand into links. Code
examples, scripts and literal HTML elements are preserved. Unknown references are
reported without guessing their source. Supported image destinations are Markdown
images and HTML `img.src`/`source.src`; CSS backgrounds and `srcset` are outside
this mechanical inspection scope.

A versioned record in `.openkb/artifact-quality/` binds the checks to file hashes,
source versions, parsing versions and target snapshot hashes. It lists inspected
files, issues and unchecked areas. A passed citation check verifies the binding;
it does not establish semantic support or recall of every required fact. Execution
status and format checks remain separate. Files with no original-source citations
show citation checking as not checked, including earlier citation-free v1 records.
Missing historical records show unknown;
edited or missing recorded files and source targets invalidate previous checks.

Files and records use the same mutation transaction. An ordinary model failure or
output limit preserves files already saved and reports the incomplete stage.
Overwriting with archive preserves the previous artifact and its bindings;
Skill rollback restores those bindings with the files. Active artifacts and
individual archive iterations retain their original evidence versions. Damaged
records block source cleanup. Explicit source links in partially saved files also
retain evidence; unresolved unrecorded markers conservatively retain history.

From a KB directory:

```sh
openkb artifact list
openkb artifact quality output/skills/install-check
openkb artifact export output/skills/install-check /path/outside/kb
openkb artifact export output/decks/attention /path/outside/kb --include-evidence
openkb artifact delete output/skills/install-check
```

Ordinary export keeps its existing layout. Evidence export adds `_evidence/` with
only cited original ranges, locations, necessary images and the quality record.
References are rewritten to package-relative paths. The package never includes the
whole source store or credentials. Unknown or stale checks remain explicitly marked.
Choose an individual archive iteration to export its historical evidence.

The desktop artifact panel displays the same quality summary and offers **携带引用依据**.
API clients can use authenticated `GET /api/v1/artifacts`, `/artifacts/quality`,
`/artifacts/export` and `DELETE /artifacts`, with `kb` and (except listing) `path`
query parameters. Export accepts `include_evidence=true`. The existing Skill archive
endpoint accepts the same optional flag. Generation responses and streamed terminal
events add `execution`, `quality`, `unfinished`, `resources`, and `artifact_quality`;
nonstream failures retain the existing `detail` field alongside these facts.
