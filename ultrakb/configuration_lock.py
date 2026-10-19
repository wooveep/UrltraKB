"""Short coordination shared by settings capture, edits and portable backup."""

from pathlib import Path

from ultrakb.mutation import RecoveryRequired, repair_marker


def configuration_lock_path(root: Path) -> Path:
    return root / ".ultrakb/configuration.lock"


def require_prepared_configuration(root: Path) -> None:
    if repair_marker(root).exists() or any((root / ".ultrakb/journal").glob("*.json")):
        raise RecoveryRequired("Prepare settings explicitly before capturing an execution")
