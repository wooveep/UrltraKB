"""A self-contained, already-normalized input for the existing PageIndex algorithm."""

import base64
import hashlib
import json
from pathlib import Path
from typing import Literal

from openkb.file_state import contained_paths
from openkb.locks import atomic_write_bytes, atomic_write_text
from openkb.source_records import DocName, Record, RelativePath
from openkb.text_source import FrozenText, read_text_selection, text_origins

PACKAGE_POLICY: Literal["openkb-frozen-blocks-v1"] = "openkb-frozen-blocks-v1"


class BlockPackage(Record):
    format: Literal["openkb-frozen-blocks-v1"] = PACKAGE_POLICY
    doc_name: DocName
    source: FrozenText
    assets: dict[RelativePath, str]


def freeze_block_package(source: Path, doc_name: str) -> Path:
    """Freeze units once, after execution mode selection and before any model request."""
    from openkb.content_blocks import BLOCK_POLICY, split_blocks

    wiki = source.parent.parent
    map_path = source.with_suffix(".content.json")
    frozen = FrozenText.model_validate_json(map_path.read_text("utf-8"))
    frozen = FrozenText.model_validate(
        {
            **frozen.model_dump(),
            "unit_kind": "block",
            "blocks": split_blocks(frozen.text),
            "block_policy": BLOCK_POLICY,
        }
    )
    assets = {}
    for name, digest in frozen.assets.items():
        path = wiki / name
        contained_paths(wiki, [path])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Frozen asset changed before packaging")
        assets[name] = base64.b64encode(data).decode("ascii")
    package = BlockPackage(doc_name=doc_name, source=frozen, assets=assets)
    target = source.with_suffix(".okbi")
    atomic_write_text(map_path, frozen.model_dump_json(indent=2))
    atomic_write_text(target, package.model_dump_json(indent=2))
    return target


def read_block_selection(raw: dict, blocks: str | None = None) -> dict:
    from pageindex.index.utils import parse_pages

    from openkb.source_pages import PageRangeError

    frozen = FrozenText.model_validate_json(json.dumps(raw))
    if frozen.unit_kind != "block":
        raise PageRangeError("This source has no frozen content blocks")
    try:
        numbers = (
            parse_pages(blocks) if blocks is not None else list(range(1, len(frozen.blocks) + 1))
        )
    except (ValueError, TypeError) as exc:
        raise PageRangeError(str(exc)) from exc
    if not numbers or set(numbers) - set(range(1, len(frozen.blocks) + 1)):
        raise PageRangeError("Block range is outside the frozen source")
    units = []
    for block in frozen.blocks:
        if block.ordinal not in numbers:
            continue
        origins = []
        for start, end in block.source_spans:
            origins.extend(text_origins(frozen, start, end))
        units.append(
            {
                **block.model_dump(mode="json"),
                "unit_kind": "block",
                "content": "".join(frozen.text[a:b] for a, b in block.source_spans),
                "origin_locators": origins,
            }
        )
    spans = [span for item in units for span in item["source_spans"]]
    ranges: list[list[int]] = []
    for start, end in spans:
        if ranges and ranges[-1][1] == start:
            ranges[-1][1] = end
        else:
            ranges.append([start, end])
    return {
        **read_text_selection(raw),
        "content": "".join(item["content"] for item in units),
        "char_range": None,
        "block_range": list(numbers),
        "units": units,
        "source_spans": spans,
        "origin_locators": [origin for a, b in ranges for origin in text_origins(frozen, a, b)],
    }


class FrozenBlockParser:
    """Adapt the frozen package only; never convert, split, or summarize its input."""

    policy = PACKAGE_POLICY

    def supported_extensions(self) -> list[str]:
        return [".okbi"]

    def parse(self, file_path: str, **kwargs):
        from pageindex.parser.protocol import ContentNode, ParsedDocument
        from pageindex.tokens import count_tokens

        package = BlockPackage.model_validate_json(Path(file_path).read_text("utf-8"))
        frozen = package.source
        if frozen.unit_kind != "block" or set(package.assets) != set(frozen.assets):
            raise ValueError("Incomplete frozen block package")
        base = Path(kwargs["images_dir"]).parent
        for name, encoded in package.assets.items():
            data = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(data).hexdigest() != frozen.assets[name]:
                raise ValueError("Packaged asset digest changed")
            target = base / name
            contained_paths(base, [target])
            atomic_write_bytes(target, data)
        units = read_block_selection(frozen.model_dump(mode="json"))["units"]
        nodes = [
            ContentNode(
                content=unit["content"],
                tokens=count_tokens(unit["content"], kwargs.get("model")),
                index=unit["ordinal"],
                metadata=unit,
            )
            for unit in units
        ]
        return ParsedDocument(
            doc_name=package.doc_name,
            nodes=nodes,
            metadata={
                "unit_kind": "block",
                "unit_count": len(units),
                "coverage": "complete",
                "normalized_fingerprint": frozen.fingerprint,
                "block_policy": frozen.block_policy,
                "source": frozen.model_dump(mode="json"),
                "assets": frozen.assets,
            },
        )


def create_index_client(**kwargs):
    from pageindex import LocalClient

    client = LocalClient(**kwargs)
    client.register_parser(FrozenBlockParser())
    from openkb.office.slide_package import FrozenSlideParser

    client.register_parser(FrozenSlideParser())
    return client
