# Python extension corresponding-source research — 87bdcc7

This is the bounded source-material audit for Pillow 12.2.0, lxml 6.1.1,
cryptography 48.0.0, PyMuPDF 1.27.2.3/MuPDF 1.27.2, and ONNX Runtime 1.20.1.
Qt/PySide, CPython/PBS, and numpy's GCC components are owned by separate audits.
It supplies inputs to release assembly; it does not claim that a final release,
clean source rebuild, or licence/relinking acceptance has passed.

The machine-readable companion is
[python-native-corresponding-source.json](python-native-corresponding-source.json).
It identifies 96 existing or supplemental source/control archives by SHA256,
265 original licence/NOTICE/header members within those archives, all 32
registry crates in cryptography's Cargo.lock, 32 additional fixed Git files,
and 18 original installed wheel notices. Original source headers and notice
wording are retained. Archive-member pointers deliberately reference the
original bytes rather than rewriting every component as the parent's licence.

## Evidence and confidence

Both `87bdcc7` inventories were used. Target ELF/PE files were extracted from the
internal Linux tar and Windows ZIP and compared with their inventory SHA256.
The five Python extension modules used for runtime inspection also matched
those frozen hashes on each platform. Windows inspection ran a separate Python
process in the isolated `release-src-87bdcc7` build environment; it did not
control a desktop process or read a user KB/configuration. ELF `DT_NEEDED`, PE
imports, selected defined exports and strings are retained in
`packaging/desktop/build/source-cache/python-native-resolution/`:

- `linux-runtime.json`, `windows-runtime.json`: library versions, provider/build
  reports and matching extension hashes.
- `binary-probes.json`, `exported-symbols.json`: directly observed imports and
  selected binary evidence. Strings alone are corroboration, not proof every
  optional code path is enabled.
- `linux-frozen-inputs.json`, `windows-frozen-inputs.json`: actual collected files
  and source input hashes for the five parent packages.
- `wheel-notices.json` and `wheel-notices/`: unmodified notices read from those
  build installations, including the expanded Pillow wheel licences and ONNX
  Runtime's original ThirdPartyNotices.txt. Some were absent from the freezer's
  collected metadata; they are available here for the final licence asset.

“Runtime-confirmed” means observed binary membership or a library/version query
through a hash-matching extension. “Required build input” means the fixed
upstream recipe selects it. “Build superset” means source is retained but no
claim is made that every candidate/test/tool/other-target component was linked.
A notice bundle can be a superset too: its mention of MKL, CUDA, imagequant or
XDMCP is not evidence those components occur in this program.

## Parent → nested components

| Parent | Actual evidence and source mapping | Licence/material treatment |
| --- | --- | --- |
| Pillow, both platforms | libjpeg-turbo **3.1.4.1**, libtiff **4.7.1**, OpenJPEG **2.5.4**, LittleCMS **2.18**, libwebp **1.6.0**, FreeType **2.14.3** match runtime queries. Existing `native/sources.json` archives cover these versions. | Pillow's MIT-CMU licence does not replace IJG/turbo, FTL, BSD, MIT, zlib or other dependency notices. Preserve the complete expanded wheel notice and authoritative original source notices. |
| Pillow → AVIF, both | libavif **1.4.1** reports `dav1d [dec]:1.5.3-0-gb546257, aom [enc]:3.13.2`. Enabled fixed recipes identify libyuv **6067afde563c3946eebd94f146b3824ab7a97a9c** and libsharpyuv from libwebp **1.6.0**. All are already in native/nested source archives. | Preserve libavif/dav1d/libyuv/libwebp licences and patent notices, including AOM's original PATENTS. Other Local*.cmake codecs are not marked runtime. |
| Pillow, Linux shared libraries | Frozen paths and imports confirm Brotli **1.2.0**, libpng **1.6.56**, XZ/liblzma **5.8.3**, HarfBuzz **13.2.1**, libxcb **1.17.0**, Zstd **1.5.7**, and a separate wheel **libXau 1.0.9-3.el8**. | Existing native sources cover the first four; pristine libxcb 1.17.0 already exists in the Debian source cache. Added upstream Zstd 1.5.7 and AlmaLinux Xau source RPM. Do not substitute Debian's separately collected libXau 1.0.11 for the wheel copy. |
| Pillow → Raqm/FriBidi/zlib | Pillow's sdist contains Raqm **0.10.3**, the FriBidi shim and original headers. Linux Raqm is available and the frozen program separately includes Debian FriBidi **1.0.16-1**. Windows reports no working Raqm feature and has no FriBidi DLL. Windows zlib is **1.3.1.zlib-ng**, with zlib-ng **2.3.3**. Linux imports ordinary `libz.so.1` supplied by the separately inventoried Debian zlib **1.3.1**. | FriBidi retains LGPL-2.1-or-later obligations. A missing Raqm runtime capability is not proof vendored Raqm code is absent. Linux's compile-time `zlib_ng` feature value alone does not prove zlib-ng is the library resolved at runtime. |
| lxml, Linux | Runtime reports libxml2 **2.14.6**, libxslt **1.1.43**; the actual etree ELF exports `_libiconv_version=0x112` (**1.18**) and `zlibVersion()` = **1.3.2**. The fixed lxml workflow pins exactly these four versions. | Reuse cached libxml2/libxslt sources; added GNU libiconv 1.18 and reuse the parallel runtime audit's zlib 1.3.2. libiconv's LGPL library requirements survive static inclusion; lxml BSD does not supersede them. |
| lxml, Windows | Runtime instead reports libxml2 **2.11.9** and libxslt **1.1.45**. Their matching official Windows bundle is release **2026.05.17**, commit **4e8ae01f61145dc823ce2ae1d79f06241b7b46de**, using winlibs libiconv **1.17.1** and zlib **1.3.2**. | Four exact submodule trees, build.ps1, workflow and **libiconv.patch** are now cached. The workflow applies that patch before compiling. Do not use the Linux 2.14.6/1.1.43/1.18 rows for Windows. The iconv/zlib source association is supported by the matching official bundle recipe, not a separate vendor build attestation. |
| lxml source/resources | LICENSES.txt identifies ElementTree-derived code and isoschematron resources with their own notices. The GPL test runner is a separate test input. | Preserve source/resource headers and referenced licence texts. Do not label the runtime GPL merely because test.py is in the source archive, or describe every resource as covered solely by lxml BSD. |
| cryptography, both | Both hash-matching `_rust` extensions report **OpenSSL 4.0.0, 14 Apr 2026**, with no dynamically imported OpenSSL DLL/SO. Native OpenSSL source is present. Every one of **32 registry crates** in the sdist Cargo.lock is present and matches that lock's checksum. | Original OpenSSL Apache licence/NOTICE and each crate's declared terms remain separate from cryptography's Apache/BSD choice. Crates used for procedural macros, build scripts or other targets are a locked build-source superset, not automatically runtime components. |
| PyMuPDF → MuPDF, both | Runtime versions match **1.27.2.3 / 1.27.2**. Cached fixed PyMuPDF Git/sdist and official MuPDF source archive cover these identities. | Preserve D1's AGPL combination arrangement and original per-file GPL/AGPL/other licences; no blanket SPDX replacement. |
| MuPDF required third parties | Its own source archive contains FreeType **2.13.3**, patched HarfBuzz **6.0.0**, IJG libjpeg **9f** with patches, the incompatible patched lcms2 **2.16** fork, OpenJPEG **2.5.4**, zlib **1.3.1**, patched Gumbo **0.10.1**, and patched Brotli **1.1.0**. Linux defined exports corroborate their embedded implementations. | These modified trees are already supplied in `mupdf-1.27.2-source.tar.gz`; the similarly named Pillow versions are not substitutes. Original FTL/BSD/MIT/Apache/zlib notices and changes remain intact. |
| MuPDF other embedded code | jbig2dec/MuJS exports, OCR exports/strings for Tesseract **5.5.0** and Leptonica **1.85.0**, and barcode exports accompany sources including Zint **2.13.0.9**, patched ZXing-cpp **2.3.0**, and extract. | jbig2dec/extract have AGPL COPYING; MuJS has ISC; OCR/barcode dependencies retain Apache/BSD-style original texts. Optional FreeGLUT/curl and unselected resources remain a source superset, not an assertion of runtime inclusion. |
| ONNX Runtime, both | Runtime build strings identify **5c1b7ccbff** and CPU/Azure provider entries, agreeing with cached commit **5c1b7ccbff7e5141c1da7a9d963d660e5741c319**. Binary markers corroborate Abseil, ONNX/Protobuf, RE2 and CPUINFO; fixed CMake selects corresponding header/build dependencies. | Core/MLAS source, original ThirdPartyNotices and fixed deps/patches are present. Keep optional/test dependencies explicitly classified; an aggregate Intel MKL notice does not establish MKL linkage. |

The relevant first-party definitions are Pillow's
[fixed wheel workflow](https://github.com/python-pillow/Pillow/blob/3c41c095064200a02672d89cc5ff629eaf4b0d4f/.github/workflows/wheels.yml)
and [dependency recipe](https://github.com/python-pillow/Pillow/blob/3c41c095064200a02672d89cc5ff629eaf4b0d4f/.github/workflows/wheels-dependencies.sh),
lxml's [fixed wheel pins](https://github.com/lxml/lxml/blob/b4a4c595fb875d6f50ae113449834209a364643a/.github/workflows/wheels.yml),
the [fixed Windows bundle](https://github.com/lxml/libxml2-win-binaries/tree/4e8ae01f61145dc823ce2ae1d79f06241b7b46de),
MuPDF's [official 1.27.2 source archive](https://mupdf.com/downloads/archive/mupdf-1.27.2-source.tar.gz)
(`docs/other/third-party.rst` and the actual modified `thirdparty/` trees), and
ONNX Runtime's [fixed deps](https://github.com/microsoft/onnxruntime/blob/5c1b7ccbff7e5141c1da7a9d963d660e5741c319/cmake/deps.txt)
and [patches](https://github.com/microsoft/onnxruntime/tree/5c1b7ccbff7e5141c1da7a9d963d660e5741c319/cmake/patches).

## Added material and rebuild controls

Supplemental files are confined to
`packaging/desktop/build/source-cache/python-native-resolution/`. Existing
complete archives were reused; the downloaded source list records origins,
size and SHA256. The Xau source RPM from the
[AlmaLinux 8.10 vault](https://vault.almalinux.org/8.10/AppStream/Source/Packages/libXau-1.0.9-3.el8.src.rpm)
contains the pristine 1.0.9 archive and the distribution spec. The actual wheel
ELF has debuglink `libXau.so.6.0.0-1.0.9-3.el8.x86_64.debug`; the matching source
spec has no active patch. This is a concrete package/source identity mapping,
not an assertion of a byte-identical rebuild or Debian binary provenance.

Pillow's sdist omits its `wheels/` directory. The fixed Git `wheels/` files,
wheel workflow, cibuildwheel requirement and referenced multibuild commit
**64739327166fcad1fa41ad9b23fa910fa244c84f** have therefore been preserved as
supplementary controls. Windows uses the original `winbuild/build_prepare.py`
patches and `--no-imagequant` selection. Full macOS/iOS/test branches are
retained for source integrity but are not classified as these binaries' runtime.

For lxml, preserve both platform recipes. Linux's static dependency build pins
four versions, including libiconv and zlib, and can consume the cached source
archives. Windows's upstream automatic download chooses a prebuilt bundle;
that moving discovery must be fixed to the recorded bundle or replaced with
the supplied four locally built static libraries. `setupinfo.py` supports
`--static` with explicit `INCLUDE`/`LIBRARY` and compiler settings; use its
`doc/build.txt` instructions to expose the resulting matching libraries.
The fixed Windows workflow's libiconv patch is necessary input. These are
recorded reconstruction paths, not claims that this audit ran those builds.

PyMuPDF's fixed `setup.py` supplies an actual configuration difference: its
1.27.2 Windows build prefixes `TOFU_CJK_EXT` in MuPDF config.h; Unix supplies it
through `XCFLAGS`/`XCXXFLAGS`. It also controls Tesseract and generated wrappers.
The full MuPDF release archive already contains patched third-party trees and
resources. Keep these controls together instead of treating an unconfigured
upstream MuPDF tag as the complete PyMuPDF build description.

ONNX Runtime's main archive contains the patch files applied to its pinned
Abseil, Eigen, CPUInfo, Protobuf, FlatBuffers, ONNX and other inputs. Its CMake
conditions distinguish host tools, test dependencies and optional backends.
The cached Eigen committed tree is correct; its new archive does not match the
old deps.txt ZIP SHA1. A rebuild must explicitly use the verified local tree or
update that download/checksum reference, then apply the recorded patches.
FlatBuffers **23.5.26** is this native build's input; the independently installed
Python FlatBuffers **25.12.19** is a different component.

All 32 cryptography Cargo archives match their lock checksums. The manifest
preserves each original Cargo licence expression and licence member hashes.
The source workspace supplies its own crate sources and OpenSSL integration.
Retain source/patch controls needed to modify and relink the shipped extension,
without claiming a generally available, unmodified compiler's entire source
must accompany the program. This distinction follows [AGPL §1](https://www.gnu.org/licenses/agpl-3.0.html#section1)
and does not remove independently applicable LGPL obligations for distributed
libraries such as libiconv or FriBidi.

## Release assembly checks still required

1. Merge this matrix with the separately owned Qt/PySide, CPython/PBS, numpy/GCC,
   renderer/font and Debian-library results. These components can coexist in
   different versions; deduplicate only identical source identities and bytes.
2. Assemble original full licence/NOTICE text with the corresponding-source
   asset and usable build controls. Preserve the distinction between mandatory
   runtime components and optional/build-source supersets in public metadata.
3. Exercise a clean source reconstruction and required library modification or
   relinking path. No proprietary build attestation or byte-for-byte binary
   reproduction is claimed or made a new requirement here.
4. Validate final package identity, downloadable matching assets and About/REST
   access against the final release. Cached source availability and internal
   acceptance packages alone do not complete this gate.

The evidence index can be regenerated read-only over the archived originals:

```sh
uv run python packaging/desktop/build/source-cache/python-native-resolution/assemble_evidence.py
```

That command verifies source hashes and writes only this audit's JSON; it does
not build, contact a provider, modify a KB or publish a release.
