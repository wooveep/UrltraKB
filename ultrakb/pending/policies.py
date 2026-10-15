"""Saved discovery policies fix both object enumeration and recursive host coverage."""

CURRENT_POLICY = "office-embedded-files-v3"
HOSTS = {
    "docx-embedded-package-v1": frozenset({"docx"}),
    "ooxml-embedded-package-v1": frozenset({"docx", "pptx", "xlsx"}),
    "office-embedded-files-v2": frozenset({"docx", "pptx", "xlsx", "doc"}),
    CURRENT_POLICY: frozenset({"docx", "pptx", "xlsx", "doc", "xls", "ppt"}),
}


def discovery_hosts(policy):
    try:
        return HOSTS[policy]
    except KeyError:
        raise ValueError(
            "Unknown saved discovery policy; explicit reprocessing is required"
        ) from None
