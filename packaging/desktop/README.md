# UrltraKB desktop builds and split delivery

Build and package the actual UrltraKB desktop, CLI, REST and acceptance entry
points. Runtime archives contain the program, original licenses and a reference
to a separate matching source/build archive. Complete source materials are not
bundled into the runtime archive.

## Makefile entry points

From a Git checkout, use GNU Make, Python 3.12+ and `uv`:

```sh
make help
make package             # wheel, sdist, local PageIndex wheel and SHA256SUMS.txt
make desktop             # install locked dependencies, build assets, freeze, inventory
make verify              # run the frozen acceptance runner
# On a headless Linux build host:
QT_QPA_PLATFORM=offscreen make verify
make release MATERIALS=/absolute/path/to/matching/full-distribution
```

All targets select committed source (`COMMIT=HEAD` by default). Commit changes
before building them. `COMMIT=<tag-or-sha>` selects another revision. Exports and
desktop output live in `build/packages/COMMIT12/PLATFORM/source/`; the portable
program is its `packaging/desktop/dist/UrltraKB/` directory. Python packages go to
`dist/COMMIT12/python/`, final desktop archives to `dist/COMMIT12/`. Install the
wheel with `pip install --find-links /path/to/python /path/to/python/openkb-*.whl`
so the matching PageIndex fork can be resolved.

`make desktop` requires Rust 1.95.0 and the platform's native build tools; its
first run downloads the pinned build/runtime resources. OCR inference runtimes
and model weights remain separate optional installations. `make release` requires
the matching, reviewed materials assembled as described below; it does not
create or claim a source/license audit. Existing release archives are preserved
and rejected as outputs; choose a fresh `DIST_DIR` to repeat archive creation.
These commands build locally and do not publish or upload anything.

Override `PYTHON`, `UV`, `BUILD_DIR`, and `DIST_DIR` as needed. Windows builds use
native Windows Python (`make desktop PYTHON=python`) with GNU Make and the
MSVC/Windows SDK tools installed. Make is a convenience entry from a Git checkout;
the commands below also rebuild an extracted source archive without Git.

## Individual build stages

Build separately on Windows 11 x86_64 and Debian 13.6 x86_64. Use CPython
3.12.13, Rust 1.95.0, and the repository's frozen lock. First export the selected
committed source to a new directory:

```sh
.venv/bin/python scripts/export_desktop_source.py --commit HEAD --output /path/to/new-source
```

This reads Git objects and excludes workspace changes, private notes and retired
browser files. It writes `source-export.json` and `openkb/_build_info.json` with
the commit and a development build version. In the exported directory, set
`SETUPTOOLS_SCM_PRETEND_VERSION` to the printed version, then install the desktop,
API and development extras explicitly:

```sh
uv sync --frozen --python 3.12.13 --extra desktop --extra api --extra dev
uv pip install --python .venv/bin/python -r packaging/desktop/build-requirements.txt
.venv/bin/python scripts/prepare_desktop_assets.py
.venv/bin/python scripts/build_desktop.py
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
`build_desktop.py` checks the export's file inventory and installed package
version before freezing, and checks source files again afterwards. Source
changes or additional application files require a new committed export.

After freezing, run the inventory with that export's Python environment:

```sh
.venv/bin/python scripts/inventory_desktop.py --source . --output /path/to/new-inventory.json
```

The inventory verifies the four executable archives, embedded Python modules,
base library and collected files against their actual build inputs. It records
file hashes and primary Python, npm, runtime, font and Debian package owners;
unmapped files or changed inputs fail the check. Nested native dependencies and
their source/licence materials still require a separate audit. On Windows the
freezer uses only its Python and Windows system directories in PATH, avoiding
accidental collection of DLLs from developer utilities.

`prepare_desktop_assets.py` verifies the Node and font downloads and builds
the locked native renderer. `build_desktop.py` caches the pinned token
vocabularies, then freezes the desktop and complete core dependency set.
The resulting `dist/UrltraKB` directory contains:

- `UrltraKB`: native Qt Widgets workbench.
- `UrltraKBCLI`: existing Click commands, without Qt initialization.
- `UrltraKBAPI`: independent REST service, without Qt initialization.
- `UrltraKBVerify`: explicit acceptance runner for this build.

Keep the program directory together. Data and settings use the existing
user-selected KB directories and configured user profile, not this directory.
The acceptance runner requires a **new** output directory and isolates its
generated KBs/settings there. It leaves evidence for inspection:

```sh
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/native-check
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/native-corpus \
  --corpus tests/fixtures/native-render-corpus.json
.venv/bin/python scripts/verify_desktop_model.py \
  --program packaging/desktop/dist/UrltraKB/UrltraKBVerify \
  --output /tmp/native-model --inputs /path/to/document-fixtures
```

The model runner hosts a controlled local HTTP fixture. It exercises actual
SDK serialization, child processes, document compilation and persisted turns;
it does not establish live model quality or a real provider connection.
Corpus checks establish technical output/error handling; visual inspection
of labels, relationships, baselines and clipping remains a separate check.

The frozen program also carries the original `openkb/ocr/worker.py`, `openvino_worker.py`,
`supervisor.py`, `loading.py`, `cloud_result.py`, `local_result.py`, and
`openkb/runtime/process_tree.py` files. The
separately installed optional OCR Python runtime executes these scripts and
verifies worker/loading hashes; Windows also loads the process-tree helper from
its original relative path. OCR result adapters also require source bytes to
identify their interpretation version. Embedded Python bytecode is not a
substitute. Check their presence and source hashes in both platform inventories,
and exercise an actual scanned-page import with local OCR configured. OCR
dependencies and model weights remain in the separate optional runtime package.
The parser also reads Mammoth's distribution version, so its metadata is
included explicitly even though it is an optional MarkItDown dependency.

The lifecycle runner exercises actual Qt event loops and spawned workers. It
checks waiting-batch completion, stopping before execution, and shutdown during
a pending KB deletion. Each restart is a separate process using the previous
run's KB and history. Use a new output directory for every command:

```sh
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/exit-wait --lifecycle wait
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/exit-stop --lifecycle stop
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/exit-delete --lifecycle delete
packaging/desktop/dist/UrltraKB/UrltraKBVerify --output /tmp/restarted \
  --lifecycle restart --lifecycle-state /tmp/exit-wait
```

These checks trigger actual tray menu actions programmatically and record tray
availability. They do not substitute for an OS tray click, stopping an in-flight
model unit, replacing a program directory, or checking all descendant processes
from an external observer. The `shutdown.png` and `lifecycle.json` files record
the tested case and its scope; a restart checks KB/history retention, not the
previous run's global settings.

## Delivery names and contents

The public program directory and executable names always use **UrltraKB**:
`UrltraKB`, `UrltraKBCLI`, `UrltraKBAPI`, `UrltraKBVerify` (with `.exe` on Windows).
Internal Python imports, the `openkb` CLI installation command and existing user
configuration paths remain compatible.

After inventory and complete material assembly, create one separate source/build
archive and one runtime archive per platform with the committed packaging tool:

```sh
.venv/bin/python scripts/package_desktop.py materials --source . \
  --materials /path/to/complete-distribution --output /path/to/delivery
.venv/bin/python scripts/package_desktop.py runtime --source . \
  --program packaging/desktop/dist/UrltraKB --inventory /path/to/inventory.json \
  --materials /path/to/complete-distribution \
  --source-archive /path/to/delivery/UrltraKB-VERSION-materials.zip \
  --output /path/to/delivery
```

Replace `VERSION` with the exported version. The names are
`UrltraKB-VERSION-windows-x64.zip`, `UrltraKB-VERSION-debian13.6-x64.tar.gz`,
and `UrltraKB-VERSION-materials.zip`. Each runtime archive has one `UrltraKB/`
directory. The tool rejects stale identities, changed input bytes and existing
output files, selects only inventoried program files, and rechecks every archive
member. Keep all three downloads and their checksums together in the delivery
location; do not duplicate the full source archive inside either runtime archive.

The runtime's `distribution/` contains full licenses, a notice and a schema-2
manifest identifying the separately provided source archive by name, size and
SHA256. The native About dialog and `/api/v1/distribution` display this reference
and serve only the actual bundled license/notice files. They do not advertise a
local download endpoint for the separate archive.

To install or serve all corresponding source materials, extract the materials
archive at the same location as the runtime archive, merging
`UrltraKB/distribution/`. Its complete schema-1 manifest replaces the compact
manifest and enables the full material downloads. A source/wheel REST deployment
can instead set `OPENKB_DISTRIBUTION_DIR` to that extracted directory, with the
matching exported package containing `_build_info.json` installed. Private KB
routes remain independent of this public material endpoint.
