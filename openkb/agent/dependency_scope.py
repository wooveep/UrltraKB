"""Select complete structural contexts for review of omitted prerequisites.

Headed sections and explicit references locate complete original contexts. Every
candidate also sees the omitted scopes: failure to find a heading/keyword link
never proves semantic independence. Unlocated gaps keep document-wide review.
"""

import re

from openkb.sources import content_id

_POINTER = re.compile(
    r"\b(?:see|refer\s+to|above|below|previous\s+section|following\s+section)\b"
    r"|参见|参考.{0,8}(?:节|章|表|图)|上述|下述|前述|上文|下文|同上",
    re.I,
)
_GLOBAL = re.compile(
    r"\b(?:all|every)\s+(?:sections?|procedures?|operations?|tasks?)\b|\bthroughout\b"
    r"|适用于所有|以下各|通用(?:条件|要求|限制)|所有.{0,12}(?:必须|均须)",
    re.I,
)


def _sections(original):
    paths, titles, stack = {}, {}, []
    for row in original:
        location = row["location"]
        if "attachment" in location or location.get("kind") not in {"text", "docx"}:
            return None
        bid = row["reference"]["block_id"]
        if row["kind"] == "heading":
            match = re.match(r"^(#{1,6})\s+(.+)", row["text"])
            depth = len(match[1]) if match else len(location.get("headings", []))
            if not depth:
                return None
            title = match[2] if match else row["text"].strip()
            stack = [item for item in stack if item[0] < depth]
            stack.append((depth, bid))
            titles[bid] = title.casefold()
        paths[bid] = tuple(item[1] for item in stack)
    return (paths, titles) if titles else None


def _omitted_blocks(row, blocks, facts, groups):
    ids = set(blocks)
    if row.get("stage") == "facts":
        selected = set(row["items"])
    elif row.get("stage") == "planning":
        topics = set(row["items"])
        selected = {f["scope"]["block_id"] for f in facts if content_id(f["topic"]) in topics}
        if topics - {content_id(f["topic"]) for f in facts}:
            return None
    elif row.get("stage") == "generation":
        paths = set(row["items"])
        members = {m for group in groups if group["path"] in paths for m in group["members"]}
        if paths - {group["path"] for group in groups}:
            return None
        selected = {f["scope"]["block_id"] for f in facts if f["topic"] in members}
    elif row.get("block"):
        selected = {row["block"]}
    else:
        # A diagnostic without an exact block identity cannot safely be scoped
        # by its free-form reason or by a guessed neighboring paragraph.
        return None
    return selected if selected and selected <= ids else None


def _review_scope(original, omissions, candidates, facts, groups):
    """Return (complete source, applicable omissions, affected candidates) batches."""
    fallback = [(original, omissions, candidates)]
    sections = _sections(original)
    if sections is None or any(_GLOBAL.search(row["text"]) for row in original):
        return fallback
    paths, titles = sections
    from openkb.agent.dependency_sources import SourceSelection

    blocks = (
        original.by_id
        if isinstance(original, SourceSelection)
        else {row["reference"]["block_id"]: row for row in original}
    )
    missing = [_omitted_blocks(row, blocks, facts, groups) for row in omissions]
    if any(value is None for value in missing):
        return fallback
    all_missing = set().union(*missing)
    references = {}
    for bid, row in blocks.items():
        text = row["text"].casefold()
        references[bid] = {
            target
            for target, title in titles.items()
            if row["kind"] != "heading"
            and ((len(title) >= 2 and title in text) or "#" + re.sub(r"\s+", "-", title) in text)
        }

    def expand(targets):
        while True:
            scopes = [paths[bid] for bid in targets]
            selected = {
                bid
                for bid, path in paths.items()
                if any(
                    path[: len(scope)] == scope or scope[: len(path)] == path for scope in scopes
                )
            }
            linked = set()
            for absent in missing:
                if selected & absent:
                    linked.update(absent - selected)
            for bid in selected:
                if blocks[bid]["kind"] == "heading":
                    continue
                text = blocks[bid]["text"].casefold()
                named = references[bid]
                for pointer in _POINTER.finditer(text):
                    target_text = re.split(r"[.。\n]", text[pointer.end() :], maxsplit=1)[0]
                    if not any(title in target_text for title in titles.values()):
                        return set(blocks)
                if re.search(r"\]\(#[^)]+\)", text) and not named:
                    return set(blocks)
                linked.update(named - selected)
            # Conditions may name the affected section from the omitted side
            # ("Backup is required for Migration"), without a reciprocal link.
            active_sections = {paths[bid][-1] for bid in targets if paths[bid]}
            linked.update(
                bid
                for bid, named in references.items()
                if bid not in selected and named & active_sections
            )
            if linked <= targets:
                return selected
            targets = targets | linked

    selections = {}
    for candidate in candidates:
        seeds = set()
        for fact in candidate["facts"]:
            seeds.add(fact["scope"]["block_id"])
            seeds.update(
                item["reference"]["block_id"]
                for item in fact.get("context_evidence", [])
                # Boundary previews are not semantic dependencies. Their source
                # section is included only when an explicit reference selects it.
                if item["relation"] not in {"previous_block", "following_block", "heading"}
            )
        if not seeds or not seeds <= set(blocks) or any(not paths[bid] for bid in seeds):
            selections[candidate["path"]] = set(blocks)
        else:
            selections[candidate["path"]] = expand(seeds | all_missing)
    # A generated link to another candidate keeps that candidate's source scope
    # in the dependency closure, including indirect links through other topics.
    changed = True
    while changed:
        changed = False
        for candidate in candidates:
            selected = selections[candidate["path"]]
            before = set(selected)
            for target, scope in selections.items():
                if target in candidate["content"]:
                    selected.update(scope)
            changed |= selected != before
    batches = {}
    for candidate in candidates:
        selected = selections[candidate["path"]]
        key = tuple(bid for bid in blocks if bid in selected)
        batches.setdefault(key, []).append(candidate)
    return [
        (
            original.select(key)
            if isinstance(original, SourceSelection)
            else [blocks[bid] for bid in key],
            [row for row, absent in zip(omissions, missing) if set(key) & absent],
            selected,
        )
        for key, selected in batches.items()
    ]


def review_scopes(original, omissions, candidates, facts, groups):
    """Review every candidate against each bounded omission scope.

    Structural boundaries only organize the requests. An explicit model decision
    against both complete scopes proves independence; no missing keyword does.
    """
    sections = _sections(original)
    if sections is None:
        return [(original, omissions, candidates)]
    paths, _ = sections
    blocks = {row["reference"]["block_id"] for row in original}
    # Keep only the routing index; caller-owned bodies remain in private storage.
    index = [{"topic": fact["topic"], "scope": fact["scope"]} for fact in facts]
    grouped = {}
    for omission in omissions:
        absent = _omitted_blocks(omission, blocks, index, groups)
        if absent is None:
            return [(original, omissions, candidates)]
        key = tuple(sorted({paths[bid] for bid in absent}))
        grouped.setdefault(key, []).append(omission)
    return [
        scope
        for rows in grouped.values()
        for scope in _review_scope(original, rows, candidates, index, groups)
    ]
