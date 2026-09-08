# Corresponding source and distribution materials

The program identity is the commit embedded in `openkb/_build_info.json`, not
the checkout used later to assemble its distribution materials. The current
accepted program is `87bdcc7b6e3306b2f1981b80af5a705b258e0c35`, version
`0.1.dev42+g87bdcc7b6e33`. Materials-only changes do not change that identity.

## Contents and license scope

The material directory contains `release.json` and five kinds of materials:

- **source**: the exact exported OpenKB source, third-party source archives,
  original patches, and the source-package/build recipes identified by the
  component records. Original archives are retained with their hashes.
- **licenses**: a ZIP of complete licenses, copyright and NOTICE files, plus
  explicitly identified supplemental texts. Legacy encodings and RTF have
  identified UTF-8 reading copies; their unchanged originals and conversion
  records are retained in the component materials and source archives.
- **notice**: readable distribution terms and instructions for finding source.
- **components**: both actual program inventories and the mappings from their
  components to sources, licenses and supporting provenance records.
- **build**: this guide, the assembly input plan, build instructions and audit
  records. Optional upstream build/test inputs are distinguished from files
  actually present in a program.

The original OpenKB source remains Apache-2.0. The combined distribution that
includes PyMuPDF/MuPDF follows the approved AGPLv3 distribution arrangement.
Original third-party terms are preserved: this does not relicense every file
as AGPL. Independent font resources retain OFL or GUST/LPPL notices; Microsoft
runtime files retain their own terms. Component-specific exceptions are
documented against the actual files, not inferred for all native libraries.

## Rebuilding the application

Extract the source assets into one empty input directory, preserving paths.
`program-source/` is a clean Git-object export with `source-export.json` and
the matching `_build_info.json`. It contains no private configuration or KB.

Build on Windows 11 x86_64 or Debian 13.6 x86_64. The recorded baseline uses
CPython 3.12.13, Rust 1.95.0, Node 24.20.0, and the pinned `uv.lock`, npm lock,
Cargo lock, and `packaging/desktop/build-requirements.txt`. The provenance
records distinguish the two platforms' Python runtime builds and native
dependencies even where their public package versions are identical.

From `program-source/`, follow `packaging/desktop/README.md`. Set
`SETUPTOOLS_SCM_PRETEND_VERSION=0.1.dev42+g87bdcc7b6e33` before installation.
On Debian:

```sh
uv sync --frozen --python 3.12.13 --extra desktop --extra api --extra dev
uv pip install --python .venv/bin/python -r packaging/desktop/build-requirements.txt
.venv/bin/python scripts/prepare_desktop_assets.py
.venv/bin/python scripts/build_desktop.py
.venv/bin/python scripts/inventory_desktop.py --source . --output inventory.json
```

On Windows use `.venv\Scripts\python.exe`. Install the MSVC C++ build tools and
Windows SDK for the Rust MSVC target. Node is a non-browser runtime; the
renderer does not require Chromium or Qt WebEngine. Ordinary, unmodified
development tools can be installed separately; they are not program runtimes
that end users must install.

The source archives support modification and rebuilding. They do not promise
byte-for-byte reproduction of upstream wheels or signed executables. To make
application changes, create a new committed export and identity rather than
editing a verified export and retaining its original provenance.

## Rebuilding or replacing a bundled library

Start with the component's exact source record and its included upstream
build scripts. Do not substitute a similarly numbered library or a host
library without checking ABI compatibility.

- PyMuPDF's fixed source and `setup.py` accompany the full MuPDF source release,
  including its third-party tree. Preserve the original GPL/AGPL file-level
  declarations and build against the recorded MuPDF version.
- Qt and PySide source include the exact Qt supermodule dependency and public
  CI/configuration scripts. PySide wheels are not assumed to cover separately
  supplied ICU or Windows Mesa/LLVM; these have their own source records.
  Qt remains dynamically loaded, and the full application source allows
  rebuilding against a modified compatible Qt/PySide installation.
- Debian libraries include their matching `.dsc`, original source and Debian
  patch archives. Use `dpkg-source -x package.dsc` and that package's
  `debian/rules` with its declared build dependencies.
- The separately shipped CentOS Fortran and Quadmath libraries include their
  complete signed source RPMs. Extract them with `rpm2cpio`/`cpio` (or the
  included extraction helper); the `.spec` identifies the exact patches,
  source inputs and build/install commands. The wheel repair transformations
  are recorded separately from source changes.
- Rust extension sources retain their Cargo locks; the included crate
  archives cover those locks. The normal/build target graphs distinguish
  runtime dependencies from optional, test and other-platform candidates.
- For Python native extensions, use the platform-specific mapping and wheel
  build scripts. In particular, Windows and Linux lxml and CPython builds do
  not have identical nested dependency versions.

No additional restriction is imposed on modifying or debugging the open-source
components to exercise their license rights. Independent proprietary runtime
terms are not replaced by this statement. Rebuild checks should cover the
affected library's operations and then the native acceptance runner.

## Assembling and verifying materials

The included `build/tools/assemble_distribution.py` consumes an explicit reviewed input plan.
It refuses unresolved source/license entries, missing material kinds, unsafe
paths, duplicate archive paths, changed input bytes or an existing output
directory. It also requires the complete committed source export and rejects
case-insensitive path collisions between ZIP assets. It copies only named inputs, re-reads every archived member to
verify its hash, and creates `release.json` after all assets verify.

For reassembly, extract **all** ZIP assets into one empty input directory.
Copy the standalone `NOTICE-87bdcc7.txt` into that directory as
`NOTICE.distribution.txt`, and retain the downloaded `materials-plan.json`.
Run the following command from the extracted input directory:

```sh
python build/tools/assemble_distribution.py --plan materials-plan.json \
  --inputs /path/to/extracted-inputs --output /path/to/new-distribution
```

The script checks integrity and assembly completeness; the component review
supplies the substantive source/license mapping. Passing a hash check alone
does not establish that an arbitrary archive is corresponding source.

`components/COMPONENTS.json` uses paths relative to this merged input directory.
Historical provenance records preserve their original build-machine paths:
`packaging/desktop/build/source-cache/X` normally corresponds to `source-cache/X`.
Some evidence and notices were relocated into `components/` or `licenses/`.
For these, match the original record's SHA256 against `members[].sha256` in
`materials-plan.json`: `members[].path` is the exact extracted location and
the containing group's `name` is its download asset. This also locates readable
license copies through their conversion manifest. Provenance-only binary
downloads and rejected source candidates are intentionally not source inputs;
the component records explain those distinctions.

Place the complete `distribution/` directory beside the program executables.
The native **About / Source and licenses** dialog loads the matching version,
source identity, notices and selectable full license texts. The standalone
REST service exposes the same verified material files at
`/api/v1/distribution`, including to users who lack KB credentials. For a
source/wheel REST deployment, install the matching exported package and set
`OPENKB_DISTRIBUTION_DIR` to the complete material directory.

## Publication

Material assembly is a local operation. Publishing the program, pushing a
branch or creating a release is a separate action. At publication, put the
program archives and every matching source asset on the same freely
accessible download page, together with `release.json`, checksums, notices
and build information. Verify the actual public download paths before
describing the release as published. A development branch URL or a source
archive for a different commit is not a substitute.

## Acceptance record

The maintainer confirmed clean-system complete acceptance on 2026-09-08 in
the implementation task. This closes that acceptance input. The earlier
agent-generated Windows and Debian environment records remain unchanged as
the record of what the agent itself observed; no Sandbox run is asserted.
