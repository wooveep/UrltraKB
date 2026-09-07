# Native program-directory builds

These builds exercise the real application and shared operations. They are
internal implementation checkpoints, not release distributions: the complete
daily workflow and matching source/licence distribution gate remain pending.

Build separately on Windows 11 x86_64 and Debian 13.6 x86_64. Use CPython
3.12.13, Rust 1.95.0, and the repository's frozen lock. Install the desktop,
API and development extras explicitly:

```sh
uv sync --frozen --python 3.12.13 --extra desktop --extra api --extra dev
uv pip install --python .venv/bin/python -r packaging/desktop/build-requirements.txt
.venv/bin/python scripts/prepare_desktop_assets.py
.venv/bin/python scripts/build_desktop.py
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
An exported source tree without Git metadata also needs
`SETUPTOOLS_SCM_PRETEND_VERSION` set to the matching build version before
installing the project. Do not silently assign a release version to a dirty tree.

`prepare_desktop_assets.py` verifies the Node and font downloads and builds
the locked native renderer. `build_desktop.py` caches the pinned token
vocabularies, then freezes the desktop and complete core dependency set.
The resulting `dist/OpenKB` directory contains:

- `OpenKB`: native Qt Widgets workbench.
- `OpenKBCLI`: existing Click commands, without Qt initialization.
- `OpenKBAPI`: independent REST service, without Qt initialization.
- `OpenKBVerify`: explicit acceptance runner for this internal build.

Keep the program directory together. Data and settings use the existing
user-selected KB directories and configured user profile, not this directory.
The acceptance runner requires a **new** output directory and isolates its
generated KBs/settings there. It leaves evidence for inspection:

```sh
packaging/desktop/dist/OpenKB/OpenKBVerify --output /tmp/native-check
packaging/desktop/dist/OpenKB/OpenKBVerify --output /tmp/native-corpus \
  --corpus tests/fixtures/native-render-corpus.json
.venv/bin/python scripts/verify_desktop_model.py \
  --program packaging/desktop/dist/OpenKB/OpenKBVerify \
  --output /tmp/native-model --inputs /path/to/document-fixtures
```

The model runner hosts a controlled local HTTP fixture. It exercises actual
SDK serialization, child processes, document compilation and persisted turns;
it does not establish live model quality or a real provider connection.
Corpus checks establish technical output/error handling; visual inspection
of labels, relationships, baselines and clipping remains a separate check.
