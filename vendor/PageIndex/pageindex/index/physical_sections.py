"""Conservative source heading proof and parent bounds for physical-page subtrees."""

import copy
import re
from dataclasses import dataclass

from ..errors import IndexQualityError


class SubtreeQualityError(IndexQualityError):
    """Local recovery is exhausted; repeating the validated parent cannot help."""

    local_recovery_exhausted = True


def compact(value):
    return re.sub(r"\s+", "", value).casefold()


def heading(line):
    stripped = line.strip()
    if re.search(r"\.{2,}|…|⋯", stripped):
        return None
    markdown = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", stripped)
    body = markdown.group(1) if markdown else stripped
    numbered = re.match(r"^(\d+(?:\.\d+)*)(?:[.、)]?\s+)(.+)$", body)
    if numbered:
        return numbered.group(2), numbered.group(1)
    if markdown:
        return body, None
    return None


def heading_candidates(item, page_list, start_index=1):
    title = heading(item['title'])
    wanted = compact(title[0] if title else item['title'])
    result = []
    for number, (text, *_) in enumerate(page_list, start_index):
        offset = 0
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            parsed = heading(line)
            if parsed and compact(parsed[0]) == wanted:
                result.append({'unit': number, 'range': [offset, offset + len(line.rstrip())],
                               'excerpt': line.strip(), 'section': parsed[1]})
            # A wrapped heading may cross the page boundary. Keep its start on
            # the first page and record the continuation, without altering text.
            if parsed and index == len(lines) - 1 and number - start_index + 1 < len(page_list):
                next_lines = page_list[number - start_index + 1][0].splitlines()
                continuation = next_lines[0] if next_lines else ''
                if continuation and compact(parsed[0] + continuation) == wanted:
                    result.append({'unit': number, 'range': [offset, len(text)],
                                   'excerpt': line.strip(), 'section': parsed[1],
                                   'continuation': {'unit': number + 1, 'range': [0, len(continuation)],
                                                    'excerpt': continuation}})
            offset += len(line)
    return result


def unique_heading(item, page_list, start_index=1):
    candidates = heading_candidates(item, page_list, start_index)
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class SectionBoundary:
    title: str
    start: tuple[int, int]
    end: tuple[int, int]
    start_confirmed: bool = True
    end_confirmed: bool = True

    def contains(self, anchor):
        position = (anchor['unit'], anchor['range'][0])
        return self.start <= position < self.end

    def proves_membership(self, anchor):
        return self.start_confirmed and self.end_confirmed and self.contains(anchor)

    @classmethod
    def from_node(cls, node):
        start, end = node.get('anchor'), node.get('end_anchor')
        return cls(
            node['title'],
            (start['unit'], start['range'][0]) if start else (node['start_index'], 0),
            (end['unit'], end['range'][0]) if end else (node['end_index'] + 1, 0),
            bool(start), bool(end),
        )


def prepare_entries(entries, page_list, start_index=1, boundary=None, density=4):
    if not isinstance(entries, list) or not entries:
        raise IndexQualityError('Physical navigation must be a nonempty entry list')
    if len(entries) > density * len(page_list):
        raise IndexQualityError('Physical navigation exceeds the configured node density')
    accepted, seen = [], set()
    for original in entries:
        if not isinstance(original, dict) or not isinstance(original.get('title'), str):
            raise IndexQualityError('Physical navigation requires original heading titles')
        item = copy.deepcopy(original)
        anchor = unique_heading(item, page_list, start_index)
        if anchor:
            if boundary and not boundary.contains(anchor):
                continue  # The untouched boundary page remains in original readers.
            item.update(physical_index=anchor['unit'], anchor=anchor, title_origin='original')
            if anchor['section']:
                item['structure'] = anchor['section']
            identity = (anchor['unit'], anchor['range'][0], compact(item['title']))
            if identity in seen:
                continue
            seen.add(identity)
        accepted.append(item)
    if not accepted:
        raise IndexQualityError('No original headings belong to the requested section')
    return accepted


def merge_continuation(previous, incoming):
    """Continuation may append new positions, never overwrite the accepted prefix."""
    if not isinstance(incoming, list) or any(not isinstance(item, dict) for item in incoming):
        raise IndexQualityError('Physical continuation requires an entry list')
    existing = {(item.get('physical_index'), compact(item.get('title', ''))): item
                for item in previous}
    result = []
    for item in incoming:
        key = (item.get('physical_index'), compact(item.get('title', '')))
        if key in existing:
            if item.get('structure') != existing[key].get('structure'):
                raise IndexQualityError('Continuation changed an existing source position')
            continue
        result.append(item)
        existing[key] = item
    return result


def validate_order(entries, start, length):
    previous = (start, -1)
    for item in entries:
        page = item.get('physical_index')
        if type(page) is not int or not start <= page < start + length:
            raise IndexQualityError('Physical heading is outside its original range')
        offset = item.get('anchor', {}).get('range', [0])[0]
        position = (page, offset)
        if position < previous:
            raise IndexQualityError('Physical headings do not follow original source order')
        previous = position
