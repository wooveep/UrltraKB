"""Replace conflicting TOC subtrees with validated navigation from original body pages.

TOC numbers describe a proposal, never the identity of a body heading. Verified
neighbors delimit the replacement interval; their original pages remain intact.
Literal coordinates locate candidates. The normal heading-role verifier still
decides whether those candidates can become navigation.
"""

import asyncio
import copy
import hashlib
import json
import re

from ..errors import IndexQualityError
from ..llm import current_client, run_sync
from .physical_sections import (
    PHYSICAL_NAVIGATION_POLICY,
    PhysicalNavigationError,
    heading_candidates,
    literal_heading_matches,
    literal_title_pattern,
    prepare_entries,
    title_line_positions,
    validate_order,
)


def _position(anchor):
    return anchor["unit"], anchor["range"][0]


def _body_heading(item, pages, start):
    """Locate a title on its verified page, without imposing the TOC number."""
    number = item.get("physical_index")
    if type(number) is not int or not start <= number < start + len(pages):
        return None
    local = pages[number - start : number - start + 2]
    candidates = [a for a in heading_candidates(item, local, number) if a["unit"] == number]
    matching = [a for a in candidates if a["section"] == item.get("structure")]
    if len(matching) == 1:
        return {**matching[0], "evidence": "text_match"}
    if len(candidates) == 1:
        return {**candidates[0], "evidence": "text_match"}
    if candidates:
        return None
    positions = title_line_positions(item, local[:1], number)
    if len(positions) == 1:
        anchor = positions[0]
        text = pages[number - start][0]
        first, last = anchor["range"]
        return {
            **anchor,
            "excerpt": text[first:last],
            "section": None,
            "evidence": "text_match",
        }
    # Native PDF title lines can wrap after the number and inside the title.
    # Match complete source lines, preserving their original whitespace/span.
    text = pages[number - start][0]
    matches = literal_heading_matches(literal_title_pattern(item), text)
    if len(matches) == 1:
        match = matches[0]
        return {
            "unit": number,
            **match,
            "evidence": "text_match",
        }
    return None


def _merge(spans):
    merged = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return merged


def _subtrees(proposals, conflicts):
    spans = []
    for first in sorted(conflicts):
        section = proposals[first].get("structure")
        last = first + 1
        if isinstance(section, str) and section:
            depth = len(section.split(".")) if re.fullmatch(r"\d+(?:\.\d+)*", section) else None
            while last < len(proposals):
                child = proposals[last].get("structure")
                # A partially updated TOC may already use the body's new prefix
                # for its children. Include the deeper outline entries as hints
                # for the recovery range; only the rebuilt body decides hierarchy.
                descendant = isinstance(child, str) and (
                    child.startswith(section + ".")
                    or (
                        depth is not None
                        and re.fullmatch(r"\d+(?:\.\d+)*", child)
                        and len(child.split(".")) > depth
                    )
                )
                if not descendant:
                    break
                last += 1
        spans.append((first, last))
    return _merge(spans)


def _scopes(entries, proposals, incorrect, pages, start, body_start):
    failed = {item["list_index"] for item in incorrect}
    anchors, conflicts = {}, set(failed)
    for index, item in enumerate(entries):
        if index in failed:
            continue
        anchor = _body_heading(item, pages, start)
        if (
            anchor is None
            or item.get("title_correction")
            or (anchor and anchor["section"] and anchor["section"] != item.get("structure"))
        ):
            conflicts.add(index)
        elif anchor:
            anchors[index] = anchor
    spans = _subtrees(proposals, conflicts)
    # A non-locatable neighbor is not a safe boundary. Include that proposal
    # in recovery and continue to the next independently verified heading.
    blocked = {i for first, last in spans for i in range(first, last)}
    trusted = {i: a for i, a in anchors.items() if i not in blocked}
    expanded = []
    for first, last in spans:
        left = next((i for i in range(first - 1, -1, -1) if i in trusted), -1)
        right = next((i for i in range(last, len(entries)) if i in trusted), len(entries))
        expanded.append((left + 1, right))
    result = []
    for first, last in _merge(expanded):
        left, right = trusted.get(first - 1), trusted.get(last)
        lower = (left["unit"], left["range"][1]) if left else (body_start, 0)
        upper = _position(right) if right else (start + len(pages), 0)
        if lower >= upper:
            raise PhysicalNavigationError("Conflicting body navigation bounds are not ordered")
        result.append((first, last, lower, upper))
    return result


def _literal_entries(proposed, pages, start, lower, upper, density, *, normalize_order=False):
    """Check delivered coordinates against the unchanged body, before role verification."""
    # The supplied pages include neighboring headings as context. Count only
    # distinct original headings that will actually replace the conflict.
    prepared = prepare_entries(
        proposed, pages, start, density=None, prefer_declared_page=True, respect_parents=True
    )
    accepted = []
    for item in prepared:
        anchor = item.get("anchor")
        if anchor is None or anchor["unit"] != item.get("physical_index"):
            error = IndexQualityError(
                f"Rebuilt navigation lacks an unambiguous body heading: "
                f"{item['title']!r} (section {item.get('structure')!r}, "
                f"physical page {item.get('physical_index')})"
            )
            error.failed_entry = copy.deepcopy(item)
            raise error
        if not lower <= _position(anchor) < upper:
            continue  # Neighboring headings are supplied as context only.
        item["anchor"] = anchor
        accepted.append(item)
    if normalize_order:
        accepted.sort(key=lambda item: _position(item["anchor"]))
    if not accepted:
        raise IndexQualityError("Body recovery produced no headings inside the conflict range")
    if len(accepted) > density * len(pages):
        raise IndexQualityError("Physical navigation exceeds the configured node density")
    validate_order(accepted, start, len(pages))
    identities = [item["structure"] for item in accepted if item.get("structure") is not None]
    if len(identities) != len(set(identities)):
        raise IndexQualityError("Rebuilt body navigation repeats a section identity")
    return accepted


def _checkpoint_name(pages, lower, upper, proposals, density):
    identity = {
        "policy": PHYSICAL_NAVIGATION_POLICY,
        "pages": pages,
        "lower": lower,
        "upper": upper,
        "proposals": proposals,
        "density": density,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return "body-navigation:" + digest


def _require_distinct_neighbors(recovered, protected):
    collisions = {
        item["structure"]
        for item in recovered
        if item.get("structure") is not None and item["structure"] in protected
    }
    if collisions:
        raise IndexQualityError(
            "Rebuilt body navigation conflicts with neighboring section identities: "
            + ", ".join(sorted(collisions))
        )


def _location_key(anchor):
    continuation = anchor.get("continuation")
    return (
        anchor["unit"],
        tuple(anchor["range"]),
        anchor.get("section"),
        (continuation["unit"], tuple(continuation["range"])) if continuation else None,
    )


async def rebuild_toc_conflicts(
    entries, incorrect, page_list, start_index, opt, logger, proposals, toc_page_list
):
    """Keep healthy navigation; replace whole conflicted subtrees with body evidence."""
    # Import at invocation to keep the existing pipeline as the generation and
    # verification seam, including its request partitions and injected transport.
    from .page_index import process_no_toc, verify_toc

    body_start = max(start_index, toc_page_list[-1] + 2) if toc_page_list else start_index
    scopes = _scopes(entries, proposals, incorrect, page_list, start_index, body_start)
    if not scopes:
        return entries
    failed_indices = {item["list_index"] for item in incorrect}
    verified = set()
    rejected_roles = set()
    for i, item in enumerate(entries):
        if i not in failed_indices:
            anchor = _body_heading(item, page_list, start_index)
            if anchor:
                verified.add(_location_key(anchor))
    retained = getattr(current_client(), "stage_result", None)
    density = getattr(opt, "max_nodes_per_unit", 4)
    result, cursor = [], 0
    replaced = {i for first, last, *_ in scopes for i in range(first, last)}
    protected = {
        item["structure"]
        for i, item in enumerate(entries)
        if i not in replaced and item.get("structure") is not None
    }
    for first, last, lower, upper in scopes:
        page_start = max(start_index, lower[0])
        page_end = min(start_index + len(page_list) - 1, upper[0])
        pages = page_list[page_start - start_index : page_end - start_index + 1]
        old = [
            {key: item.get(key) for key in ("structure", "title", "physical_index")}
            for item in proposals[first:last]
        ]
        key = _checkpoint_name(pages, lower, upper, old, density)
        recovered = retained(key) if retained else None
        if recovered is not None:
            try:
                recovered = _literal_entries(recovered, pages, page_start, lower, upper, density)
                _require_distinct_neighbors(recovered, protected)
            except IndexQualityError:
                # A stale local result cannot trigger a whole-document retry.
                # Rebuild just this scope and replace its checkpoint on success.
                logger.info({"physical_body_recovery": "invalid_checkpoint", "key": key})
                recovered = None
        if recovered is None:
            from .body_fragments import BodyFragments

            async def verify_fragment(items, known):
                _require_distinct_neighbors(items, protected)
                current = {_location_key(item["anchor"]) for item in items}
                verified.update(
                    _location_key(item["anchor"])
                    for item in known
                    if item.get("anchor") and _location_key(item["anchor"]) in current
                )
                unchecked = [
                    item
                    for item in items
                    if _location_key(item["anchor"]) not in verified | rejected_roles
                ]
                if unchecked:
                    _, failed = await verify_toc(
                        page_list, unchecked, start_index=start_index, model=opt.model
                    )
                    failed_indices = {item["list_index"] for item in failed}
                    for index, item in enumerate(unchecked):
                        (rejected_roles if index in failed_indices else verified).add(
                            _location_key(item["anchor"])
                        )
                for item in items:
                    if _location_key(item["anchor"]) in rejected_roles:
                        error = IndexQualityError(
                            f"Rebuilt body heading roles remain unverified: {item['title']!r} "
                            f"on physical page {item['physical_index']}"
                        )
                        error.failed_entry = copy.deepcopy(item)
                        raise error

            loop = asyncio.get_running_loop()

            def verify_sync(items, known):
                return asyncio.run_coroutine_threadsafe(
                    verify_fragment(items, known), loop
                ).result()

            fragments = BodyFragments(
                pages,
                page_start,
                lower,
                upper,
                density,
                opt.model,
                logger,
                retained,
                verify_sync,
                opt.max_subtree_attempts,
            )
            try:
                recovered = await run_sync(
                    process_no_toc,
                    pages,
                    start_index=page_start,
                    model=opt.model,
                    logger=logger,
                    fragment_handler=fragments,
                )
                _require_distinct_neighbors(recovered, protected)
            except IndexQualityError as error:
                if getattr(error, "local_recovery_exhausted", False):
                    raise
                raise PhysicalNavigationError(
                    f"Body navigation recovery exhausted on physical pages "
                    f"{page_start}-{page_end}: {error}"
                ) from error
            if retained:
                retained(key, recovered)
        protected.update(
            item["structure"] for item in recovered if item.get("structure") is not None
        )
        recovered = copy.deepcopy(recovered)
        recovered[0]["toc_recovery"] = {
            "entries": old,
            "physical_range": [page_start, page_end],
        }
        result.extend(entries[cursor:first])
        result.extend(recovered)
        cursor = last
        logger.info(
            {
                "physical_body_recovery": "validated",
                "replaced_entries": len(old),
                "body_entries": len(recovered),
                "physical_range": [page_start, page_end],
            }
        )
    result.extend(entries[cursor:])
    try:
        validate_order(result, start_index, len(page_list))
        identities = [item["structure"] for item in result if item.get("structure") is not None]
        if len(identities) != len(set(identities)):
            raise IndexQualityError(
                "Rebuilt navigation conflicts with a neighboring section identity"
            )
    except IndexQualityError as error:
        raise PhysicalNavigationError(
            "Rebuilt body navigation conflicts with its neighbors"
        ) from error
    return result
