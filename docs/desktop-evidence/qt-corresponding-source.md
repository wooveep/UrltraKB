# Qt / PySide 6.11.2 corresponding-source mapping

Recorded 2026-09-08 for program commit `87bdcc7b6e3306b2f1981b80af5a705b258e0c35`.
This closes the previously unresolved Qt/PySide source provenance and packaging
script mapping. It does not, by itself, certify the entire OpenKB distribution.

## Binary → official wheel → source

Every frozen file owned by `python/pyside6-essentials@6.11.2` or
`python/shiboken6@6.11.2` was compared to its original official PyPI wheel:
**189/189 Linux inventory entries and 140/140 Windows entries match SHA-256**.
The Linux count includes 18 top-level symlink aliases; its 171 ordinary entries
also match. No OpenKB modification to these Qt/PySide/Shiboken files needs a
separate patch. The complete per-file comparison is
`packaging/desktop/build/source-cache/qt-resolution/frozen-wheel-map.json`.
Original wheel release records were saved from the
[PySide Essentials API](https://pypi.org/pypi/pyside6-essentials/6.11.2/json) and
[Shiboken API](https://pypi.org/pypi/shiboken6/6.11.2/json).

| Official wheel | SHA-256 |
| --- | --- |
| `pyside6_essentials-6.11.2-cp310-abi3-manylinux_2_34_x86_64.whl` | `aaf9f25f0f324874085fa5b26a610318db8a8e243cf85bb3e5400595191c7778` |
| `pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl` | `c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1` |
| `shiboken6-6.11.2-cp310-abi3-manylinux_2_34_x86_64.whl` | `7a7a0a72a9ed26c9bf77d42246b1c736486befb8f31aa2fb29957ea4cdd1c1c2` |
| `shiboken6-6.11.2-cp310-abi3-win_amd64.whl` | `6ab0eba1c904455df621f9a6df3ca2bb896bab8670572d2bc4e37804ae91f19a` |

The official PySide source's `coin/dependencies.yaml` fixes Qt's supermodule to
`713a36536903d172f9e6737584d428753c119496`. That is also the commit of the official
Qt `v6.11.2` tag. The cached Qt source archive's root `.tag` and each relevant
module `.tag` agree with the corresponding Git trees. This establishes an exact
release-source mapping rather than relying solely on the displayed version.
[PySide dependency pin](https://github.com/pyside/pyside-setup/blob/24627cd36e1593adf22eb1f2950e4248e7bcc1ec/coin/dependencies.yaml),
[Qt release tag](https://api.github.com/repos/qt/qt5/git/tags/7257fb67596163b908a12732948c15e95f520cb2).

The official PySide source matches release commit
`24627cd36e1593adf22eb1f2950e4248e7bcc1ec`: 4,600 ordinary files match their Git
blobs; the two Git symlinks materialized by the official source archive match
their target blobs. There are no differing source contents or extra files.
The check and exact link mappings are retained in
`qt-resolution/pyside-source-tag-comparison.json`.
[PySide release tag](https://api.github.com/repos/pyside/pyside-setup/git/tags/bd13ae01c34bd018aa53f29b6044043a255cf9e0).

## Actual component scope

| Source component | Exact commit | Frozen content covered |
| --- | --- | --- |
| `pyside-setup` | `24627cd36e1593adf22eb1f2950e4248e7bcc1ec` | PySide Core, Gui, Widgets, Network, Test bindings; Linux DBus binding; libpyside and Shiboken runtime |
| `qtbase` | `ef55f427f2c8b410d34f8a7681020a3000cf6866` | Core, Gui, Widgets, Network, Test, OpenGL; Linux DBus, EGL/XCB and Wayland client support; platform, TLS, input and base image plugins |
| `qtsvg` | `17ca512f903f935282ebeca496aac5d11ba4199a` | QtSvg library, SVG image/icon plugins |
| `qtimageformats` | `47b6139dda3b84d1d3ec15caf8d04eff8d744c8d` | ICNS, TGA, TIFF, WBMP, WebP image plugins |
| `qtwayland` | `fd0456558bc1766da85aee42f6a87f79092cbb13` | Linux adwaita decoration, IVI and Qt shell integration plugins |
| `qttranslations` | `570add0b7d3b1f2b7034b0bf41715456a238b29f` | Bundled Qt, Qt Base and Qt Help `.qm` translations and their editable `.ts` sources |
| `qttools` | `8026c0462f19e9549501718152523ea223a19fd3` | Translation/build tools retained as source; this row does not assert that Qt Assistant or QDoc is shipped |

Module commits come from the pinned
[Qt supermodule tree](https://api.github.com/repos/qt/qt5/git/trees/713a36536903d172f9e6737584d428753c119496).
In Qt 6.11, `WaylandClient` and the `qwayland` platform plugin are built in
**qtbase**, not inferred to be in qtwayland merely from their names. The exact
[CMake definition](https://github.com/qt/qtbase/blob/ef55f427f2c8b410d34f8a7681020a3000cf6866/src/plugins/platforms/wayland/CMakeLists.txt)
and the remaining
[qtwayland plugin definitions](https://github.com/qt/qtwayland/blob/fd0456558bc1766da85aee42f6a87f79092cbb13/src/plugins/CMakeLists.txt)
are retained under `qt-resolution/module-build-definitions/`.

Every bundled translation maps to an editable `.ts` file in the pinned translation source, except `qt_en.qm`, `qtbase_en.qm` and `qt_help_en.qm`. Their empty English source catalogs are generated explicitly by the supplied [translation CMake script](https://github.com/qt/qttranslations/blob/570add0b7d3b1f2b7034b0bf41715456a238b29f/translations/CMakeLists.txt); they are not missing source files.

Linux additionally ships the wheel's three ICU 73 libraries. The pinned Qt CI
provisioning explicitly selects **ICU 73.2** and records its upstream binary
package digest; the full ICU source is already cached as `native/ICU-73.2.tar.gz`.
These separately bundled ICU libraries retain their own original licenses.
[Exact ICU provisioning recipe](https://github.com/qt/qt5/blob/713a36536903d172f9e6737584d428753c119496/coin/provisioning/qtci-linux-RHEL-9.6-x86_64/30-install_icu.sh).

Windows also contains `opengl32sw.dll` (SHA-256
`34b444c016289b560662ff896deceb7f4b2c0723aed3d319ae167c9186ce42b3`),
whose embedded strings identify Mesa 11.2.2 and LLVM 3.6.2. Qt's pinned
[Windows provisioning script](https://github.com/qt/qt5/blob/713a36536903d172f9e6737584d428753c119496/coin/provisioning/common/windows/mesa_llvmpipe.ps1)
selects the signed Mesa 11.2.2 package. The archived
[official historical build instructions, revision 28759](https://wiki.qt.io/index.php?title=MesaLlvmpipe&oldid=28759)
explicitly use LLVM 3.6.2, Mesa 11.2.2, Visual Studio 2015 Update 2 and SCons
2.4.1. `native/Mesa-11.2.2.tar.xz` and `native/LLVM-3.6.2.tar.xz` are already
present in the reviewed source set; the new Qt supermodule archive preserves
the installer recipe, and the evidence ZIP preserves the historical instructions.
The historical introduction mentions additional patches but its actual build
steps specify unmodified upstream downloads and contain no patch application;
this audit does not invent a patch set or assert a bit-identical Mesa rebuild.
Mesa/LLVM retain their original component notices; their inclusion does not
turn them into LGPL-covered Qt source. Qt documents the separately shipped
[Mesa component and notices](https://doc.qt.io/qt-6/qt-attribution-llvmpipe.html).

The Windows wheel additionally carries Microsoft Visual C++ runtime DLLs.
Their equality to the wheel is recorded here, but their redistribution terms
are a separate runtime-component record; neither the Qt source nor the Mesa
source is claimed to be their source.

## Build and packaging scripts

The Qt binary itself reports:

- Linux: Qt 6.11.2, x86_64, shared release, GCC 11.5.0 20240719 (Red Hat 11.5.0-5).
- Windows: Qt 6.11.2, x86_64, shared release, MSVC 2022.

The exact-release Qt CI definitions include matching RHEL 9.6/GCC and Windows
11 24H2/MSVC 2022 packaging configurations. Preserve their complete environment
and provisioning definitions, not only the abbreviated arguments below.
The compiler/platform match supports selection of these definitions; it is not
an assertion that a private vendor CMake cache or a bit-identical rebuild was
obtained. [Pinned CI configuration](https://github.com/qt/qt5/blob/713a36536903d172f9e6737584d428753c119496/coin/platform_configs/cmake_platforms.yaml).

The RHEL packaging configure arguments are:

```text
-nomake examples -release -force-debug-info -headersclean -separate-debug-info
-qt-libjpeg -qt-libpng -qt-pcre -qt-harfbuzz -qt-doubleconversion
-no-libudev -bundled-xcb-xinput
```

Its non-qtbase configuration includes bundled TIFF and WebP. The Windows
configuration uses `-debug-and-release -force-debug-info -headersclean
-nomake examples -qt-zlib`, and the distributed wheel contains the release
libraries. Other original CMake arguments and environment substitutions remain
in the same pinned file.

The full public Qt supermodule archive added here includes those configurations,
`coin/instructions`, and the platform provisioning scripts. Qt's complete source
archive supplies module CMake files, `configure`, `configure.bat`, third-party
source, patches already incorporated in that release, and editable translations.
Use the original [Qt configure/build procedure](https://doc.qt.io/qt-6/configure-options.html)
with these versioned inputs; the material set does not rely on the vendor's
filesystem image.

PySide's original `coin/instructions_utils.py` builds with `setup.py build
--standalone --unity --build-tests --log-level=verbose --limited-api=yes`, an
explicit `--qtpaths`, and `--shorter-paths` on Windows. Its release source also
contains `create_wheels.py` and all `build_scripts/` sources. In particular,
`build_scripts/platforms/linux.py` and `build_scripts/main.py` perform the
upstream wheel's library copying and RPATH relocation. Those packaging changes
are already represented by the supplied scripts; OpenKB did not introduce a
second Qt relocation patch.
[PySide CI build procedure](https://github.com/pyside/pyside-setup/blob/24627cd36e1593adf22eb1f2950e4248e7bcc1ec/coin/instructions_utils.py),
[Linux wheel packaging](https://github.com/pyside/pyside-setup/blob/24627cd36e1593adf22eb1f2950e4248e7bcc1ec/build_scripts/platforms/linux.py).

## Materials to include with the release

Paths below are relative to `packaging/desktop/build/source-cache/`.

| Material | SHA-256 |
| --- | --- |
| `upstream/qt-everywhere-src-6.11.2.tar.xz` | `6dcfbca271d76a6502741a2c0dc6fc98ef7dd0b7b4cfd0abcebb285a86a26f33` |
| `upstream/pyside-setup-everywhere-src-6.11.2.tar.xz` | `cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8` |
| `qt-resolution/qt5-713a36536903-source.tar.gz` | `c293a8ad7d7ce0b73236d6042fb43a56c0be8158f739912cedd086b091c9ea71` |
| `qt-resolution/qt-6.11.2-licenses-and-attributions.zip` | `0cdb90c3aec57648fdf6783147412c8327e7c341653b70211eb0fb94ef28a41b` |

The source archives came from the official
[Qt source download](https://download.qt.io/official_releases/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.tar.xz),
[PySide source download](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz),
and the fixed [Qt supermodule source](https://api.github.com/repos/qt/qt5/tarball/713a36536903d172f9e6737584d428753c119496).

The supplemental ZIP contains **366 original files**: 153 license files plus
213 attribution/REUSE metadata files from the relevant source modules. It adds
12 named license files missed by the earlier filename-based collector, including
FreeType's BDF/PCF/zlib terms, IJG, public-suffix data, SHA3 and Wayland protocol
licenses. All `LicenseFile`/`LicenseFiles` references in the **94 attribution
entries** resolve to included files. The original metadata preserves copyright
statements and per-platform/optional-use conditions. `qt-thirdparty-attribution-index.json`
is a readable index, not a new blanket license; some entries are build tools,
tests, optional libraries or other platforms and must not all be labeled as
linked runtime components. The complete original source remains authoritative.

Keep the original Qt/PySide license choices and individual third-party notices
when assembling OpenKB's chosen distribution license. Supplying just a generic
LGPL text does not replace corresponding source or the original component
notices. [Qt's own source and notice obligations](https://www.qt.io/development/open-source-lgpl-obligations).

`qt-resolution/resolution-evidence-manifest.json` hashes the detailed evidence.
The wheels themselves are retained as provenance inputs; corresponding-source
archives, license texts and build instructions are the recipient-facing materials.
No full Qt/PySide rebuild or release publication was performed by this audit.

`qt-resolution/assembly.json` is the final assembly entry point. Original wheels
are explicitly excluded from recipient materials; their SHA-256 receipts and
per-file mappings are retained in the evidence ZIP.
