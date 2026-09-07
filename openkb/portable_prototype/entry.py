"""Freezer entry: dispatch multiprocessing before Qt or heavyweight project imports."""

import multiprocessing
import os

if __name__ == "__main__":
    multiprocessing.freeze_support()
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    from openkb.portable_prototype.ui import main

    main()
