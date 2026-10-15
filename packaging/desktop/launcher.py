"""Frozen entry points; spawn dispatch must precede application imports."""

import multiprocessing
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()
    # Console-less Windows children still run legacy libraries that write to
    # stdout/stderr. Their protocol never depends on either stream.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    if sys.argv[1:2] == ["--cnki-worker"]:
        from ultrakb.cnki.worker import main

        raise SystemExit(main(sys.argv[2:]))
    program = Path(sys.executable).stem.lower().removeprefix("ultrakb")
    if program in {"cli", "api"}:
        from ultrakb.runtime.assets import configure_sdk_resources

        configure_sdk_resources()
    if program == "cli":
        from ultrakb.cli import cli

        cli()
    elif program == "api":
        from ultrakb.api import main

        main()
    elif program == "verify":
        from ultrakb.desktop.verification import main

        raise SystemExit(main())
    else:
        from ultrakb.desktop.bootstrap import main

        raise SystemExit(main())
