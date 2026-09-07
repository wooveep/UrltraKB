"""Path-based local KB discovery, including root children and registry ghosts."""

from pathlib import Path

from openkb import config
from openkb.lifecycle import registered_path


def knowledge_bases() -> list[tuple[str, Path]]:
    """Keep colliding display names: the desktop always identifies KBs by path."""
    with config._with_global_config_lock():
        values = config.load_global_config()
        root = config.kb_root_dir()
        found: dict[Path, str] = {}
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if config._is_kb_dir(child):
                    found.setdefault(registered_path(child), child.name)
        aliases = values.get("kb_aliases", {})
        if isinstance(aliases, dict):
            for name, raw in aliases.items():
                if isinstance(name, str) and isinstance(raw, str):
                    found.setdefault(registered_path(Path(raw)), name)
        known = values.get("known_kbs", [])
        if isinstance(known, list):
            for raw in known:
                if isinstance(raw, str):
                    path = registered_path(Path(raw))
                    found.setdefault(path, path.name)
        return [(name, path) for path, name in found.items()]


def knowledge_base_overview(kb_dir: Path) -> dict:
    from openkb.application.knowledge_bases import get_kb_list, get_kb_status
    from openkb.locks import kb_read_lock

    with kb_read_lock(kb_dir / ".openkb"):
        status = get_kb_status(kb_dir)
        inventory = get_kb_list(kb_dir)
        status["directories"]["entities"] = len(inventory["entities"])
        status["directories"]["explorations"] = sum(
            1 for path in (kb_dir / "wiki/explorations").glob("*.md") if path.is_file()
        )
        status["raw_count"] = sum(1 for path in (kb_dir / "raw").rglob("*") if path.is_file())
        return status
