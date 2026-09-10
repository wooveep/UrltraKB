# Build the actual desktop, CLI, REST and acceptance entry points together.
import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

packaging = Path(SPECPATH)
repo = packaging.parents[1]
assets = repo / "openkb/rendering/assets"
if not (assets / "manifest.json").is_file():
    raise RuntimeError("Run scripts/prepare_desktop_assets.py before freezing")
datas = collect_data_files(
    "openkb",
    excludes=[
        "**/web/**", "**/rendering/assets/**", "**/desktop/assets/fonts/**", "**/rust-helper/**",
    ],
)
datas += [(str(assets), "openkb/rendering/assets")]
# OCR executes standalone sources and fingerprints workers/result adapters;
# PYZ bytecode alone cannot serve these physical-file boundaries.
datas += [
    (str(repo / "openkb/ocr" / name), "openkb/ocr")
    for name in (
        "worker.py", "openvino_worker.py", "supervisor.py", "loading.py",
        "cloud_result.py", "local_result.py",
    )
]
datas += [(str(repo / "openkb/runtime/process_tree.py"), "openkb/runtime")]
# The manifest is the runtime whitelist; reference fonts stay in the source tree.
fonts = json.loads((repo / "assets/fonts/manifest.json").read_text("utf-8"))
font_files = {"manifest.json", "README.md"} | {f[k] for f in fonts for k in ("file", "license")}
datas += [
    (str(repo / "assets/fonts" / name), "openkb/desktop/assets/fonts")
    for name in sorted(font_files)
]
datas += [(str(packaging / "build/token-cache"), "openkb/token-cache")]
datas += copy_metadata("openkb", recursive=True)
# MarkItDown's optional DOCX dependency participates in every parser profile.
datas += copy_metadata("mammoth")
for skill in ("openkb-deck-neon", "openkb-deck-editorial", "openkb-html-critic"):
    datas.append((str(repo / "skills" / skill), "openkb/_skills/" + skill))
hidden = ["openkb.cli", "openkb.api", "tiktoken_ext.openai_public"]
for package in ("litellm", "magika", "pageindex", "trafilatura", "markitdown"):
    datas += collect_data_files(package)
    hidden += collect_submodules(package, on_error="warn once")
datas += collect_data_files("agents")

analysis = Analysis(
    [str(packaging / "launcher.py")],
    pathex=[str(repo)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(packaging / "runtime_hook.py")],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executables = [
    EXE(
        pyz, analysis.scripts, [], exclude_binaries=True, name=name,
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=name != "UrltraKB", disable_windowed_traceback=False,
        icon=str(repo / "openkb/desktop/assets/brand/openkb-app-icon.ico"),
    )
    for name in ("UrltraKB", "UrltraKBCLI", "UrltraKBAPI", "UrltraKBVerify")
]
COLLECT(*executables, analysis.binaries, analysis.datas, strip=False, upx=False, name="UrltraKB")
