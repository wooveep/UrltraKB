"""Inspect retained phase events without scanning usage records on the live path.

Example: uv run python scripts/analyze_execution_timing.py /path/to/kb --execution ID
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ultrakb.execution_timing import timing_report
from ultrakb.locks import atomic_write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("knowledge_base", type=Path)
    parser.add_argument("--execution", action="append", dest="execution_ids")
    parser.add_argument("--source", dest="source_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = timing_report(
        args.knowledge_base, execution_ids=args.execution_ids, source_id=args.source_id
    )
    if args.output:
        atomic_write_json(args.output, report)
    else:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
