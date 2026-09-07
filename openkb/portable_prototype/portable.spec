# Throwaway, broad collection first: prove real dependencies before optimizing size.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
datas = collect_data_files("openkb", excludes=["**/desktop_prototype/**", "**/portable_prototype/**", "**/web/**"])
datas += [(str(root / ".runtime"), "probe_assets")]
datas += copy_metadata("openkb", recursive=True)
hidden = ["openkb.cli", "tiktoken_ext.openai_public"]
for package in ("litellm", "magika", "pageindex", "trafilatura", "markitdown"):
    datas += collect_data_files(package)
    hidden += collect_submodules(package, on_error="warn once")
datas += collect_data_files("agents")

a = Analysis([str(root / "entry.py")], pathex=[], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "openkb.api"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="OpenKBProbe",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=True, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OpenKBProbe")
