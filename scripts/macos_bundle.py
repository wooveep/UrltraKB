"""Use PyInstaller's macOS layout for an already-inventoried native program."""

from __future__ import annotations

from pathlib import Path


def bundle_toc(program: Path, inventory: dict) -> list[tuple[str, str, str]]:
    """Retain binary/data classification so metadata never lands in a code directory."""
    entries = []
    for row in inventory["files"]:
        name = row["path"]
        if not name.startswith("_internal/"):
            entries.append((name, str(program / name), "EXECUTABLE"))
        elif "link" in row:
            entries.append((name.removeprefix("_internal/"), row["link"], "SYMLINK"))
        else:
            kind = row["typecode"]
            if kind not in {"DATA", "BINARY", "EXTENSION"}:
                raise ValueError(f"Unsupported bundle input type: {kind}")
            entries.append((name.removeprefix("_internal/"), str(program / name), kind))
    for name, link in inventory.get("directory_links", {}).items():
        entries.append((name.removeprefix("_internal/"), link, "SYMLINK"))
    for name in ("BUILD-NOTICE.txt", "LICENSE"):
        entries.append(("build-docs/" + name, str(program / name), "DATA"))
    return entries


def info_plist(identity: dict) -> dict:
    return {
        "CFBundleDisplayName": "UrltraKB",
        "CFBundleVersion": identity["version"].split(".dev")[1].split("+")[0],
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    }


def stage_macos(program: Path, app: Path, identity: dict, inventory: dict) -> Path:
    from PyInstaller.building.osx import BUNDLE
    from PyInstaller.config import CONF
    from PyInstaller.configure import get_config

    work = app.parent / "bundle-work"
    work.mkdir()
    CONF.update(get_config())
    CONF.update(workpath=str(work), distpath=str(app.parent), specpath=str(work), noconfirm=True)

    class NativeBundle(BUNDLE):
        def assemble(self):
            # A TOC input has no EXE/COLLECT object from which BUNDLE can inherit
            # these settings. Apply the same native settings as desktop.spec.
            self.target_arch = "arm64"
            self.console = False
            self.upx_exclude = []
            super().assemble()

    NativeBundle(
        bundle_toc(program, inventory),
        name=app.name,
        version="0.1.0",
        bundle_identifier="io.github.wooveep.urltrakb",
        info_plist=info_plist(identity),
        icon=str(
            Path(__file__).resolve().parents[1] / "openkb/desktop/assets/brand/openkb-app-icon.ico"
        ),
    )
    return app / "Contents/MacOS"
