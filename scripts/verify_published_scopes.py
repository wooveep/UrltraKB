"""Validate published scope migration from a frozen manifest without KB writes or models."""

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from ultrakb.application.page_scope_validation import (
    PublishedScopeExpectation,
    verify_published_scopes,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        output = args.output.resolve()
        if output.is_relative_to(args.wiki.resolve()) or any(
            (parent / ".ultrakb/config.yaml").is_file() for parent in output.parents
        ):
            parser.error("Acceptance reports must be saved outside any knowledge base")
    expected = TypeAdapter(list[PublishedScopeExpectation]).validate_json(
        args.manifest.read_bytes()
    )
    report = verify_published_scopes(args.wiki, expected)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
