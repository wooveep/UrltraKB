# Build the actual desktop, CLI, REST and acceptance entry points together.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

packaging = Path(SPECPATH)
repo = packaging.parents[1]
assets = repo / "openkb/rendering/assets"
if not (assets / "manifest.json").is_file():
    raise RuntimeError("Run scripts/prepare_desktop_assets.py before freezing")
datas = collect_data_files(
    "openkb", excludes=["**/web/**", "**/rendering/assets/**", "**/rust-helper/**"]
)
datas += [(str(assets), "openkb/rendering/assets")]
datas += [(str(packaging / "build/token-cache"), "openkb/token-cache")]
datas += copy_metadata("openkb", recursive=True)
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
