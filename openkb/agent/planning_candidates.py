"""Coordinate only ambiguous candidate identities; preserve uncertain tasks separately."""

import re
from collections import Counter

from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.processing import InputTooLarge, OutputTruncated
from openkb.sources import content_id


def identities(group):
    labels = [group["title"], *group.get("aliases", [])]
    return {group["path"], *(re.sub(r"\s+", " ", label).strip().casefold() for label in labels)}


def reconcile_candidates(groups, coordinate, fits, max_members, *, existing=frozenset()):
    sets = []
    for group in groups:
        keys = identities(group)
        related = [cluster for cluster in sets if any(keys & identities(row) for row in cluster)]
        merged = [group]
        for cluster in related:
            merged.extend(cluster)
            sets.remove(cluster)
        sets.append(merged)
    resolved = []
    for cluster in sets:
        cluster = sorted(cluster, key=lambda row: (row["path"], row["members"]))
        if len(cluster) == 1:
            resolved.extend(cluster)
            continue
        # Large ambiguity sets are bounded by the actual candidate request. A
        # result from another window is never silently equated with this one.
        window = []
        for group in cluster:
            candidate = [*window, group]
            members = sorted(member for row in candidate for member in row["members"])
            if window and (len(members) > max_members or not fits(candidate)):
                resolved.extend(_resolve(window, coordinate))
                window = []
            window.append(group)
        if window:
            resolved.extend(_resolve(window, coordinate))
    result = {}
    for group in resolved:
        group = dict(group)
        original_name, directory = group["name"], group["path"].split("/")[0]
        collision = 0
        while group["path"] in result or (collision and group["path"] in existing):
            # Coordination did not prove cross-window equivalence. Keep both
            # complete contributions, with a stable identity for each member set.
            members = sorted(group["members"])
            suffix = content_id(members if collision == 0 else [members, collision])[:12]
            group["name"] = original_name[:100] + "-" + suffix
            group["path"] = directory + "/" + group["name"]
            collision += 1
        result[group["path"]] = group
    if Counter(member for row in result.values() for member in row["members"]) != Counter(
        member for row in groups for member in row["members"]
    ):
        raise ResponseIncomplete("topic_coverage_incomplete", "planning")
    return list(result.values())


def _resolve(groups, coordinate):
    if len(groups) == 1:
        return groups
    topics = sorted(member for group in groups for member in group["members"])
    try:
        return coordinate(topics, candidates=groups)
    except (ResponseIncomplete, InputTooLarge, OutputTruncated):
        # Uncertain identity may keep separate pages. It never drops members.
        return groups
