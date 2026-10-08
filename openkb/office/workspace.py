"""Keep native Office scratch paths short without losing orphan cleanup."""

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def office_directory(parent: Path | None, *, prefix: str) -> Iterator[Path]:
    if sys.platform != "win32":
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
            yield Path(directory)
        return

    from openkb.runtime.input_store import InputStore

    # LibreOffice creates deeply nested extension registries below its profile.
    # Nesting that profile in a retained artifact can exceed Windows MAX_PATH
    # during startup even when the document itself has a valid path.
    store = InputStore(Path(tempfile.gettempdir()).resolve() / "openkb-office")
    try:
        yield Path(store.name)
    finally:
        store.cleanup()
