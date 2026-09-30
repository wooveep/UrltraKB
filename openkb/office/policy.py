"""Explicit UNO values and reproducible environment for the pinned Office adapter."""

import os
import sys
from pathlib import Path

OFFICE_POLICY = "libreoffice-26.2.6.3-final-visible-pdf17-v1"
LOAD_OPTIONS = {
    "Hidden": ("boolean", True),
    "ReadOnly": ("boolean", True),
    "MacroExecutionMode": ("short", 0),  # NEVER_EXECUTE (verified against the pinned IDL)
    "UpdateDocMode": ("short", 0),  # NO_UPDATE
}
PDF_OPTIONS = {
    "SelectPdfVersion": ("long", 17),
    "UseLosslessCompression": ("boolean", True),
    "ReduceImageResolution": ("boolean", False),
    "ExportFormFields": ("boolean", False),
    "IsAddStream": ("boolean", False),
    "UseTransitionEffects": ("boolean", False),
    "ExportNotes": ("boolean", False),
    "ExportNotesInMargin": ("boolean", False),
    "ExportNotesPages": ("boolean", False),
    "ExportOnlyNotesPages": ("boolean", False),
    "ExportHiddenSlides": ("boolean", True),
    "IsSkipEmptyPages": ("boolean", False),
    "ExportTrackedChanges": ("boolean", False),
}


def office_environment(directory: Path) -> dict[str, str]:
    # Do not inherit application Python/Qt/native paths or model credentials.
    environment = {
        key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC") if key in os.environ
    }
    environment.update(
        PATH=os.defpath,
        HOME=str(directory),
        USERPROFILE=str(directory),
        TMPDIR=str(directory),
        TMP=str(directory),
        TEMP=str(directory),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
        TZ="UTC",
        SAL_DISABLE_OPENCL="1",
        SAL_DISABLEGL="1",
    )
    if sys.platform == "linux":
        environment["SAL_USE_VCLPLUGIN"] = "svp"
    return environment
