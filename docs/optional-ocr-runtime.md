# Optional CPU OCR runtime

Reliable native text parsing works without this package. Uncertain PDF pages use
the selected OCR backend; a local failure never silently uploads a document.
On Windows, an unconfigured local backend automatically uses the installed
Windows OCR engine and language packs. It extracts text and retains page images;
it does not provide PaddleOCR's document layout interpretation. DOCX OCR is
selective and advisory as described in [document processing](document-processing.md).
An explicitly configured local runtime uses the full PaddleOCR-VL-1.6 layout,
recognition and assembly pipeline in a separately supervised process.

The runtime is separate from the main application's Python environment:

| Component | Frozen version |
| --- | --- |
| CPython, Windows/Linux x64 | 3.12.13 |
| paddleocr with doc-parser extra | 3.7.0 |
| paddlex | 3.7.2 |
| paddlepaddle CPU | 3.3.1 |

`packaging/ocr-runtime/uv.lock` pins the transitive closure. `requirements.lock`
is its flattened export, including platform markers. `interpreters.json` pins
the Python Build Standalone release archives by URL, SHA-256 and size. The model
builder pins PP-DocLayoutV3 and PaddleOCR-VL-1.6 repository revisions and checks
each model file against the revision's Git/LFS digest.

## Build on each target platform

Use the same repository revision on Windows and Linux. Install the build
environment explicitly with `uv sync --frozen --project packaging/ocr-runtime`.
The following commands are network acquisition steps. Supply bounds appropriate
to the approved experiment; exhaustion fails the build without increasing them.

```sh
packaging/ocr-runtime/.venv/bin/python scripts/build_ocr_assets.py /absolute/ocr-assets \
  --seconds 600 --max-bytes 2200000000
packaging/ocr-runtime/.venv/bin/python scripts/build_ocr_bundle.py /absolute/ocr-bundle \
  --assets /absolute/ocr-assets --seconds 600 \
  --download-bytes 536870912 --disk-bytes 4294967296
```

On Windows use `packaging\ocr-runtime\.venv\Scripts\python.exe` and Windows paths.
Build once on each OS: the builder selects compatible wheels from the lock, with
no dependency resolution or source builds. It refuses an existing output
directory. A failed build's directory is retained for inspection.

Each bundle contains the extracted relocatable Python, its original verified
archive, compatible wheels, exact hashed installation requirements, model assets,
Source Han Sans font and OFL license, wheel license/notice/metadata files, build
inputs and scripts, and a `bundle.json` file inventory. Python's licenses remain
in the extracted interpreter; model licenses remain with their pinned assets.
Retain the repository revision and bundle manifest digest alongside the package.
Rebuild from that revision using the same lock, platform and model revisions;
compare the manifest inventories before distributing a replacement.

## Install without downloading

Place the bundle at its final location. Invoke its Python, with a new environment
directory **outside** the bundle:

```sh
/absolute/ocr-bundle/python/bin/python3 -I -B \
  /absolute/ocr-bundle/install_ocr_runtime.py /absolute/ocr-runtime --seconds 300
```

```bat
C:\ocr-bundle\python\python.exe -I -B C:\ocr-bundle\install_ocr_runtime.py C:\ocr-runtime --seconds 300
```

The installer checks the file inventory before creating a venv. It uses only
the bundled wheels, requires their hashes, disables package indexes and dependency
resolution, runs `pip check`, and verifies the core versions and CPU imports with
Python network access denied. Installation does not perform model inference.
Keep the bundle in place: the venv references its base interpreter and the
application references its assets.

The environment's `openkb-installation.json` reports `interpreter`, `assets` and
`assets_sha256`. Use these in `parsing.ocr.local` and explicitly provide all
`limits` and `parameters` from `openkb/ocr/config.py`. The application does not
invent resource or output budgets for a machine.

## Recorded verification and limits

On the issue #13 development machines, fresh installations passed the dependency
check and CPU imports on Debian 13 x64 and Windows 11 x64. Linux installation ran
inside `unshare -Urn`, with no network interface. Windows used the same no-index
installation and Python network-denial check; OS-level network isolation was not
verified there. Linux installed in 22.34 seconds and Windows in 45.36 seconds;
these are single local observations, not performance guarantees.

The first Linux synthetic page run crossed an 8 GiB RSS allowance during eager
model loading and was stopped. The application now loads the pinned native
checkpoint one parameter at a time into lazy CPU parameters, validating every
key and shape. This adapter runs only in the isolated worker. A tiny synthetic
checkpoint matched the SDK's ordinary loader tensor for tensor and produced
identical forward logits.

With the same 8 GiB, 180 second and 128 MiB output bounds, the full pipeline
completed one synthetic page on Linux in 71.22 seconds (4.56 GiB sampled peak)
and Windows in 93.97 seconds (4.46 GiB). Both results preserved the four preset
facts: `37 kPa`, version `7`, `--timeout 42`, and the authentication-failure
exception. Four region generations ended with EOS; all full-input token checks
passed. The same page passed the shared intake/parsing/evidence path on Linux
and Windows, with the model compilation stage deliberately stopped and no LLM
request. The synthetic result fixture records this adapter contract in tests.

These minimum samples establish CPU pipeline viability on the tested hosts;
they do not establish minimum RAM, representative recognition accuracy or
long-document throughput. No larger-memory inference was run. GPU deployment
and real PaddleOCR jobs quality remain unverified. The supervisor's resource
sampling can overshoot a threshold before termination and reports its observed
peak. Do not infer a production budget from these single observations.
