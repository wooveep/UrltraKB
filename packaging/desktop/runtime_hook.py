"""Provide writable Unicode output for frozen consoles, pipes and GUI workers."""

import os
import sys

for name in ("stdout", "stderr"):
    stream = getattr(sys, name)
    if stream is None:
        setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    elif not stream.isatty() and hasattr(stream, "reconfigure"):
        # Frozen Python ignores PYTHONIOENCODING. Pipes otherwise inherit the
        # Windows ANSI code page, which cannot represent Chinese source names.
        stream.reconfigure(encoding="utf-8")
