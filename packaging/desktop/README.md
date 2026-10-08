# UrltraKB desktop builds and split delivery

## Four-platform GitHub releases

The `Desktop packages` workflow was ported from `dev-1.1.0` to `dev-1.2.0`.
It builds the current branch's application and four local SDKs from committed
sources and the existing dependency locks.

| Output | Native build environment | Installation |
| --- | --- | --- |
| `UrltraKB-VERSION-debian-amd64.deb` | Debian 13, x86_64 | `sudo apt install ./FILE.deb` |
| `UrltraKB-VERSION-debian-arm64.deb` | Debian 13, ARM64 | `sudo apt install ./FILE.deb` |
| `UrltraKB-VERSION-windows-x64.zip` | Windows x86_64 | Extract; run `UrltraKB.exe` |
| `UrltraKB-VERSION-macos-arm64.zip` | macOS ARM64 | Extract; move `UrltraKB.app` to Applications |

Debian packages require glibc 2.41 or newer and install under `/opt/urltrakb`,
with desktop integration and `urltrakb`, `urltrakb-cli`, `urltrakb-api` launchers.
The macOS app requires Apple Silicon and macOS 14 or newer. It is ad-hoc signed,
without Apple notarization; first launch may need approval in Privacy & Security.

Windows x64, Debian amd64 and macOS arm64 bundle the complete pinned LibreOffice runtime for
DOC/DOCX/PPT/PPTX conversion, including its private Python/UNO and licenses. The
build verifies the official runtime and source archives against the existing
Office lock. Linux requires x86-64-v2 for this converter. Debian arm64 retains
PDF/text/workbook import and reports Office conversion as unavailable. Linux and Windows also build
the locked CFB helper for embedded compound documents; macOS has no CFB helper.
See [Office runtime details](../../docs/office-ingest.md).

Pushes to `main`, `dev-1.2.0`, and `v*` tags trigger builds. Manual builds can
select one platform or all four:

```sh
gh workflow run desktop-build.yml --repo wooveep/UrltraKB --ref dev-1.2.0
gh workflow run desktop-build.yml --repo wooveep/UrltraKB --ref dev-1.2.0 -f target=macos-arm64
make desktop
make bundle
make package  # Application wheel/sdist plus all four local SDK wheels
```

To prepare and verify only the native Office runtime before a full build, run
`python scripts/build_native_desktop.py --cache-dir /path/to/downloads --office-only`
from the locked development environment. This checks real DOC/DOCX/PPT/PPTX
conversion without freezing the application; Windows also requires Rust 1.95.0
for its process launcher.

All stages export `COMMIT=HEAD` by default; commit changes before building.
`BUILD_DIR` and `DIST_DIR` override `build/packages` and `dist`. Windows users
can run `python scripts/make_packages.py desktop` and `bundle` without Make.

Each installer is extracted and its CLI and native acceptance runner are
executed before upload. Debian also installs, verifies and uninstalls in a clean
container. Linux and macOS CI use Qt's `offscreen` platform; interactive desktop
integration still needs testing on the destination machine.

Branch builds are development artifacts retained in Actions for 14 days. An
exact stable tag such as `v1.2.0` sets the package version to `1.2.0`. After all
four builds pass, the release job validates every version, source commit and
SHA256. It uploads four installers, four matching `-source.zip` archives, four
`-build.json` inventories and `SHA256SUMS.txt`, then publishes the draft Release.
Failed or incomplete builds cannot pass this publication check. The comprehensive
third-party material audit below remains a separate process.

## Source and license materials

Build and package the actual UrltraKB desktop, CLI, REST and acceptance entry
points. Runtime archives contain the program, original licenses and a reference
to a separate matching source/build archive. Complete source materials are not
bundled into the runtime archive.

PageIndex, ChatIndex, ConDB and LiteLLM are built from tracked `vendor/` sources.
The root `uv.lock` resolves them as editable local dependencies. Both the build
script and PyInstaller spec verify their local versions, import paths and source
hashes. Source exports retain licenses, provenance and required resources; the
inventory attributes each collected file to its corresponding vendor component.
CNKI conversion uses the pinned PyMuPDF/MuPDF native runtime already needed for
PDF input. The freeze includes the converter worker, its hashable source helpers,
native libraries and PyMuPDF license metadata. No external CNKI reader or system
`mutool` is required. Converter provenance, the compatibility patch and platform
wheel hashes are in `openkb/cnki/assets/`; `uv.lock` pins the runtime dependency.
For Python wheel delivery, build and supply all five packages:

```sh
uv build vendor/LiteLLM --wheel --out-dir dist
uv build vendor/PageIndex --wheel --out-dir dist
uv build vendor/ChatIndex --wheel --out-dir dist
uv build vendor/ConDB --wheel --out-dir dist
uv build --wheel --out-dir dist
```

For pip-based source setup, install all four local dependencies alongside this
project. Local version suffixes prevent accidental fallback to registry releases.
See [the vendored dependency guide](../../docs/vendor-sdk.md).

Build on one of the four native hosts listed above. Use CPython
3.12.13, Rust 1.95.0, and the repository's frozen lock. First export the selected
committed source to a new directory:

```sh
.venv/bin/python scripts/export_desktop_source.py --commit HEAD --output /path/to/new-source
```

This reads Git objects and excludes workspace changes, private notes and retired
browser files. It writes `source-export.json` and `openkb/_build_info.json` with
the commit and its exact release tag or development version. In the exported directory, set
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

Linux builds share byte-identical entry points with hard links, retaining all
four launch names without four copies of the embedded Python payload. Use an
archive/copy tool that preserves hard links (`tar`, `cp -a`, or `rsync -H`);
an ordinary file copy remains functional but uses more disk space. Runtime
packaging preserves this sharing. The build excludes LiteLLM's proxy admin web
assets and MathJax's duplicate development/module trees. Dynamic MathJax bundles,
the complete NewCM font package, native renderers, document converters, model
clients, fonts and original license files remain included.

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
