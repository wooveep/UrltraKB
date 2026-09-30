"""Map the complete post-freeze Office sidecar to pinned upstream/build inputs."""

import os
from pathlib import Path

from openkb.office.inventory import digest
from openkb.office.runtime import validate_runtime


def office_inventory(program: Path, inputs) -> dict[str, dict]:
    root = program / "_internal/office"
    if not root.exists():
        return {}
    manifest = validate_runtime(root)
    office = inputs.add(
        "runtime/LibreOffice",
        manifest.version,
        build_id=manifest.build_id,
        archive=manifest.archive.model_dump(mode="json"),
        corresponding_source=manifest.source.model_dump(mode="json"),
        runtime_fingerprint=manifest.fingerprint,
        declared_license="MPL-2.0 and bundled third-party terms; see complete LICENSE",
        license_files=manifest.licenses,
        fonts=manifest.fonts,
        actual_probe=manifest.probe,
    )
    python = inputs.add("runtime/LibreOffice-Python", manifest.python_version, distribution=office)
    launcher = inputs.add(
        "build/office-launcher",
        "0.1.0",
        source="openkb/office/launcher",
        rust="1.95.0",
        lock_sha256=digest(inputs.source / "openkb/office/launcher/Cargo.lock"),
    )
    generated = inputs.add(
        "build/office-runtime",
        manifest.version,
        source="scripts/prepare_office_runtime.py",
        runtime_fingerprint=manifest.fingerprint,
    )
    result = {}
    for name, checksum in {
        **manifest.files,
        "openkb-office.json": digest(root / "openkb-office.json"),
    }.items():
        component = office
        if name.startswith("program/python-core-"):
            component = python
        elif name == manifest.launcher:
            component = launcher
        elif name.startswith("openkb-provenance/") or name == "openkb-office.json":
            component = generated
        font = inputs.application_fonts.get(Path(name).name)
        if font and name in manifest.fonts:
            component = font
        inputs.used.add(component)
        item = {"component": component, "input": name, "input_sha256": checksum}
        if (root / name).is_symlink():
            item["link"] = os.readlink(root / name)
        result["_internal/office/" + name] = item
    return result
