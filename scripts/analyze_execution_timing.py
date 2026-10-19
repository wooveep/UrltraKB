"""Inspect a bounded native execution page, including persisted phase timings.

Example: uv run python scripts/analyze_execution_timing.py /path/to/kb --execution ID
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ultrakb.application.knowledge_tasks import read_knowledge_execution
from ultrakb.locks import atomic_write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("knowledge_base", type=Path)
    parser.add_argument("--execution", required=True, help="Native execution identity")
    parser.add_argument("--limit", type=int, default=63, help="Maximum records per detail page")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = read_knowledge_execution(
        args.knowledge_base, args.execution, limit=args.limit
    ).model_dump(mode="json")
    if args.output:
        atomic_write_json(args.output, report)
    else:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
