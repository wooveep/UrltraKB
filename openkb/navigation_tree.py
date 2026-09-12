"""Checked PageIndex ranges over immutable parser blocks, never physical page aliases."""

import json
import re
from dataclasses import asdict

from openkb.evidence import Evidence, ParseStore, complete_read_bound


def basic_tree(kb_dir, source, parsed):
    from pageindex import IndexConfig
    from pageindex.index.pipeline import build_index
    from pageindex.parser.protocol import ContentNode, ParsedDocument

    reader = ParseStore(kb_dir).reader(source, parsed)

    def boundary(block):
        loc = block.location
        return (
            loc.get("page"),
            loc.get("slide"),
            loc.get("sheet"),
            json.dumps(loc.get("attachment"), sort_keys=True),
        )

    starts = sorted(
        {
            0,
            *(block.order for block in parsed.blocks if block.kind == "heading"),
            *(
                block.order
                for previous, block in zip(parsed.blocks, parsed.blocks[1:])
                if boundary(previous) != boundary(block)
            ),
        }
    )
    inputs = []
    previews = {}
    for i, start in enumerate(starts):
        if start >= len(parsed.blocks):
            continue
        end = starts[i + 1] if i + 1 < len(starts) else len(parsed.blocks)
        block = parsed.blocks[start]
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        title = view.text[:256] if block.kind == "heading" else source.name
        if block.kind != "heading":
            location = block.location
            if "page" in location:
                title = f"Physical page {location['page']}"
            elif "slide" in location:
                title = f"Slide {location['slide']}"
            elif "attachment" in location:
                title = location["attachment"]["name"]
        preview = []
        for member in parsed.blocks[start:end]:
            if sum(map(len, preview)) >= 256:
                break
            text = reader.read(
                Evidence(source.source_id, source.id, parsed.id, member.id),
                max_chars=complete_read_bound(member),
            ).text
            preview.append(text[:256])
        previews[start] = "\n".join(preview)[:256]
        marker = re.match(r"^(#{1,6})\s+", view.text) if block.kind == "heading" else None
        level = (
            len(marker[1])
            if marker
            else block.location.get(
                "heading_level", max(1, len(block.location.get("headings", [])))
            )
        )
        inputs.append(
            ContentNode(
                content="",
                tokens=0,
                title=title,
                index=start + 1,
                level=level,
            )
        )
    tree = build_index(
        ParsedDocument(source.name, inputs),
        opt=IndexConfig(
            if_add_node_summary=False, if_add_doc_description=False, if_add_node_text=False
        ),
    )
    nodes = [
        {
            "id": "n0",
            "parent": None,
            "start": 0,
            "end": len(parsed.blocks),
            "title": source.name,
            "title_origin": "source",
            "summary": "",
            "summary_origin": "preview",
            "structure_origin": "basic",
        }
    ]

    def flatten(branches, parent, end):
        for i, branch in enumerate(branches):
            start = branch["line_num"] - 1
            stop = branches[i + 1]["line_num"] - 1 if i + 1 < len(branches) else end
            identity = f"n{len(nodes)}"
            block = parsed.blocks[start]
            nodes.append(
                {
                    "id": identity,
                    "parent": parent,
                    "start": start,
                    "end": stop,
                    "title": branch["title"],
                    "title_origin": "source",
                    "summary": previews[start],
                    "summary_origin": "preview",
                    "structure_origin": "native" if block.kind == "heading" else "basic",
                }
            )
            flatten(branch.get("nodes", []), identity, stop)

    flatten(tree["structure"], "n0", len(parsed.blocks))
    # A small unstructured source is one real range, without a redundant wrapper.
    if len(nodes) == 2 and nodes[1]["structure_origin"] == "basic":
        nodes = [{**nodes[1], "id": "n0", "parent": None}]
    validate_nodes(nodes, len(parsed.blocks))
    return nodes


def validate_nodes(nodes, count):
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Missing navigation ranges")
    seen = {}
    previous = -1
    last_sibling = {}
    for node in nodes:
        if (
            not isinstance(node, dict)
            or set(node)
            != {
                "id",
                "parent",
                "start",
                "end",
                "title",
                "title_origin",
                "summary",
                "summary_origin",
                "structure_origin",
            }
            or not isinstance(node["id"], str)
            or node["id"] in seen
            or type(node["start"]) is not int
            or type(node["end"]) is not int
            or not 0 <= node["start"] <= node["end"] <= count
            or node["start"] < previous
            or not all(isinstance(node[key], str) for key in ("title", "summary"))
            or node["title_origin"] not in {"source", "inferred"}
            or node["summary_origin"] not in {"preview", "model", "unavailable"}
            or node["structure_origin"] not in {"native", "basic", "inferred"}
        ):
            raise ValueError("Invalid navigation node")
        parent = node["parent"]
        if parent is None:
            if seen or node["start"] != 0 or node["end"] != count:
                raise ValueError("Invalid navigation root")
        elif (
            not isinstance(parent, str)
            or parent not in seen
            or not seen[parent]["start"] <= node["start"] <= node["end"] <= seen[parent]["end"]
            or node["start"] < last_sibling.get(parent, 0)
        ):
            raise ValueError("Invalid navigation parent or overlapping sibling")
        last_sibling[parent] = node["end"]
        seen[node["id"]] = node
        previous = node["start"]


def block_hints(record):
    """Deepest ordered source range selects context; summaries never become evidence."""
    hints = {}
    for node in record["nodes"]:
        for order in range(node["start"], node["end"]):
            hints[order] = {
                # The artifact also identifies execution budgets. Facts depend
                # on this node's actual meaning/range, already bound to the parse.
                "node": node["id"],
                "start": node["start"],
                "end": node["end"],
                "title": node["title"],
                "summary": node["summary"],
                "summary_origin": node["summary_origin"],
            }
    if set(hints) != set(range(len(record["positions"]))):
        raise ValueError("Navigation does not cover the complete parsed source")
    return hints


def snapshot_name(source, parsed):
    return f"sources/snapshots/{source.id}-{parsed.id}.md"


def snapshot_markdown(kb_dir, source, parsed, *, asset_root=None):
    reader = ParseStore(kb_dir).reader(source, parsed)
    root = asset_root or kb_dir / "wiki/sources"
    assets = {
        path.stem: "../" + folder + "/" + path.name
        for folder in ("images", "attachments")
        for path in (root / folder).glob("*")
        if path.is_file()
    }
    text = []
    for block in parsed.blocks:
        reference = Evidence(source.source_id, source.id, parsed.id, block.id)
        view = reader.read(reference, max_chars=complete_read_bound(block))
        import json

        text.append(
            f'<a id="block-{block.id}"></a>\n\n'
            + re.sub(
                r"asset:([0-9a-f]{64})", lambda match: assets.get(match[1], match[0]), view.text
            )
            + "\n\n<!-- source-evidence: "
            + json.dumps(asdict(reference))
            + " -->"
        )
    return "\n\n".join(text)
