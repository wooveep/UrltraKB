# Frozen Office sources

DOCX and binary DOC import retain the original bytes and create an internal PDF with the
complete private TDF LibreOffice distribution. The existing PDF extraction,
classification, PageIndex and publication paths then process that PDF. Source
readback exposes both artifacts, physical pages and the conversion record.
Recompilation and historical reads use retained artifacts without running Office.
A failed replacement retains the new original and keeps the last successful body.

PPTX uses Impress's full original slide inventory, including hidden slides.
Each explicit slide ordinal is exported separately and checked for exactly one
PDF page before assembly. This binds the stored slide map to the physical PDF.
Speaker notes remain attached to that page in a distinct `notes` part, labelled
“演讲备注 / Speaker notes”; they never increase the page count. CLI `source --part
notes`, the API `part` field and the desktop selector can read notes separately.

Segmented presentations freeze a portable `.okpi` PDF-and-notes package. It wraps
the existing PDF parser and content-based tree algorithm; native PDFs keep their
ordinary route. Notes, slide mappings and PDF bytes all affect cache identity.
Navigation labels carry generated provenance and a validated body/notes anchor.
Cache recovery and recompilation consume the same frozen package without Office.

The locked release is LibreOffice **26.2.6.3**, build
`8221e31b3ac356a1623c672912a3d2b492f7e3d1`, with its Python **3.12.14** and UNO bridge.
Official Linux DEB bundle, Windows MSI and corresponding core source URLs, sizes
and SHA-256 values are in `openkb/office/runtime-lock.json`. Preparation verifies
these archives and probes the actual executables. It extracts the complete Office
tree, preserves upstream licenses and fonts, adds the eight unmodified fonts in
`assets/fonts/manifest.json`, and records every resulting file and contained link.
Conversion verifies that inventory again. System Office is never a fallback.

Office runs in a private process and profile. The main application imports no
UNO modules. The loader sets `MacroExecutionMode=NEVER_EXECUTE`,
`UpdateDocMode=NO_UPDATE`, hidden and read-only mode, and aborts interaction
requests. Writer's final-visible revision display and `PrintHiddenText=false`
exclude deleted and hidden text without accepting revisions or overwriting the
original. The PDF filter is explicit: PDF 1.7, lossless images, no downsampling,
static forms, no hybrid original stream, no notes or transitions, and retained
Writer blank pages. Unsupported detected input filters fail the Office unit.
Binary DOC must be detected by a Word binary filter; renaming OOXML or text to
`.doc` cannot bypass that check. Password requests and damaged-container failures
retain the original with an explicit diagnostic. Embedded-file recovery is a
separate capability and is not implied by successful body conversion.

`office_timeout_seconds` is an integer from 1 through 3600, default 120, with
normal KB/global inheritance. Native startup probes also have finite deadlines.
Cancellation cleans up the owned process tree. On Linux an independent supervisor
watches the application PID and its start identity, terminates the worker process
group and reaps descendants. Windows uses a separately built Rust launcher with
a kill-on-close Job Object; it resets DLL search state in that child before
starting the matching Python. Private task files sit inside the application's
owned input directory so task recovery can collect them after an abrupt exit.

Linux requires x86-64-v2 and successful private Office/Python/UNO native probes.
Office unavailability blocks only Office import; PDF and text remain usable.
The runtime's source and hashes, helper hashes, host, fonts, locale, timeout and
typed load/export properties are part of processing identity. Linux uses a
private Fontconfig directory. Windows also exposes registered OS fonts; their
paths and hashes enter the identity. No source font is rewritten. The record
lists all observed PDF fonts and substitutions proven by matching unique text
runs across spans, including Chinese and wrapped paragraphs. Tables, text frames,
headers and footers contribute requested-font observations. Ambiguous or unmatched
runs retain explicit statuses and font names; hidden/deleted source text is not
copied into these diagnostics. A missing font name alone proves no substitution.

## Build and materials

Use the committed source export and locked environment required by the existing
desktop build. Download the exact archives in the lock, then run:

```sh
python scripts/build_office_desktop.py --office-archive /path/to/official-package --office-source /path/to/libreoffice-26.2.6.3.tar.xz
```

This builds the application, builds the Windows launcher with Rust 1.95.0 where
needed, then stages the private runtime at
`packaging/desktop/dist/UrltraKB/_internal/office`. It keeps Office out of
PyInstaller's main-interpreter dependency collection. Run the existing desktop
inventory next; it records Office, its Python, application fonts, the launcher
and generated provenance under their own components. Existing runtime packaging
copies the complete checksummed sidecar with its executable modes and links.

For development, `scripts/prepare_office_runtime.py --archive ... --source ...
--output ...` prepares an independent runtime. Set the absolute
`office_runtime_path` in KB or global settings; it is also available in desktop
settings. The default development location is `openkb/office/assets/runtime`.

Before assembling companion materials, run
`scripts/prepare_office_materials.py --runtime ... --source ... --inputs ...
--plan ... --output ...`. It adds the verified source archive, complete retained
license files, source/notice references and runtime manifest to a new material
plan. Existing unresolved reviews remain unresolved. The normal assembler and
runtime packager then use that plan and inventory. The complete upstream LICENSE
includes terms and source references for bundled components; the material plan
does not replace the release's nested dependency/license review.

## Validation status

Small real DOCX integration tests run with `OPENKB_TEST_OFFICE_RUNTIME` pointing
to a prepared distribution. They exercise final-visible text, Chinese fonts,
blank pages, missing-font substitution, full/segmented compilation, API/CLI
readback, retained recompile, failure preservation, corrupt PDF references,
timeout, cancellation and application death during both probes and conversion.
Only model calls are substituted in successful conversion tests.

Passing on a development host does not enable the final distribution. Issues
#100–#102 retain the real-model and clean Windows 11/Debian 13.6 GNOME/X11 manual
gates, including frozen-entry-point launch, ordinary user, offline and read-only
installation, concurrent tasks, existing user Office, and forced-stop cleanup.

幻灯片正文锚点使用转换时冻结的逐页纯文本 Unicode 坐标，备注使用独立坐标域。
富文本展示的图片链接不参与正文字符坐标；索引、来源回读及缓存迁移共用冻结的两域文本。

旧 `.ppt` 使用同一隔离运行时的 `MS PowerPoint 97` 输入过滤器，完整导出隐藏幻灯片，
保持正文／备注分域及物理页映射。文件格式与过滤器不符或损坏时保留原件与既有知识，
报告转换失败。PPT/PPTX 中内嵌完整文件的恢复由附件发现流程负责。
