"""One disposable process owns all native CNKI parsing for an attempt."""

import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        return 2
    source, output, response = map(Path, args)
    try:
        from openkb.cnki.engine import convert

        result = convert(source, output)
        response.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as exc:
        output.unlink(missing_ok=True)
        response.write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
