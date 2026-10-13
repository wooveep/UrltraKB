"""Literal title locations and proved parent bounds for physical-page subtrees."""

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
    numbered = re.match(r"^(\d+(?:\.\d+)*)(?:[.、)](?=\D)\s*|\s+)(.+)$", body)
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
            # PDF extraction often puts a section number on its own line.
            # Only join the immediately adjacent title, keeping its native span.
            number_only = re.fullmatch(r'(\d+(?:\.\d+)*)(?:[.、)])?', line.strip())
            if number_only and index + 1 < len(lines) and compact(lines[index + 1]) == wanted:
                end = offset + len(line) + len(lines[index + 1].rstrip())
                result.append({'unit': number, 'range': [offset, end],
                               'excerpt': text[offset:end], 'section': number_only.group(1)})
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
    """Locate a unique title candidate; raw PDF syntax does not prove its role."""
    if item.get('structure') is not None and not isinstance(item['structure'], str):
        raise IndexQualityError('Physical structure must be a string or null')
    candidates = heading_candidates(item, page_list, start_index)
    parsed = heading(item['title'])
    sections = {value for value in (item.get('structure'), parsed[1] if parsed else None)
                if value}
    if len(sections) > 1:
        return None
    if sections:
        section = next(iter(sections))
        candidates = [anchor for anchor in candidates
                      if not anchor['section'] or anchor['section'] == section]
    return {**candidates[0], 'evidence': 'text_match'} if len(candidates) == 1 else None


def title_line_positions(item, page_list, start_index=1):
    """Locate literal title lines, without claiming that they are headings."""
    parsed = heading(item['title'])
    wanted = compact(parsed[0] if parsed else item['title'])
    matches = []
    for number, (text, *_) in enumerate(page_list, start_index):
        offset = 0
        for line in text.splitlines(keepends=True):
            if wanted and compact(line) == wanted:
                matches.append({'unit': number, 'range': [offset, offset + len(line.rstrip())]})
            offset += len(line)
    return matches


@dataclass(frozen=True)
class SectionBoundary:
    title: str
    start: tuple[int, int]
    end: tuple[int, int]
    start_confirmed: bool = True
    end_confirmed: bool = True
    structure: str | None = None

    def contains(self, anchor):
        position = (anchor['unit'], anchor['range'][0])
        return self.start <= position < self.end

    def proves_membership(self, anchor):
        return self.start_confirmed and self.end_confirmed and self.contains(anchor)

    def page_text(self, text, number):
        """Project confirmed parent scope; keep original text for native anchors."""
        start, end = 0, len(text)
        if self.start_confirmed:
            if number < self.start[0]:
                return ''
            if number == self.start[0]:
                start = self.start[1]
        if self.end_confirmed:
            if number > self.end[0]:
                return ''
            if number == self.end[0]:
                end = self.end[1]
        return text[start:end]

    def is_parent_root(self, item, page_list, start_index=1):
        """Inherit a verified parent position; never treat it as its own child."""
        if item.get('physical_index') != self.start[0]:
            return False
        parsed = heading(item['title'])
        title = parsed[0] if parsed else item['title']
        parent = heading(self.title)
        if compact(title) != compact(parent[0] if parent else self.title):
            return False
        section = item.get('structure') or (parsed[1] if parsed else None)
        if parsed and parsed[1] and section != parsed[1]:
            return False
        parent_section = self.structure or (parent[1] if parent else None)
        if parent_section and section and parent_section != section:
            return False
        page = self.start[0] - start_index
        if not 0 <= page < len(page_list):
            return False
        # A continuation may use the next page, but its heading must start on
        # the verified parent page. An unrelated next-page title is not a root.
        # Resolve the parent from its own evidence first. Filtering with an
        # unverified child's anchor/number would let that child select itself.
        candidates = [anchor for anchor in heading_candidates({'title': self.title}, page_list[page:page + 2], self.start[0])
                      if anchor['unit'] == self.start[0]
                      and (not parent_section or not anchor['section'] or anchor['section'] == parent_section)]
        if self.start_confirmed:
            candidates = [anchor for anchor in candidates
                          if (anchor['unit'], anchor['range'][0]) == self.start]
        # A page-only parent position may be refined only by a unique native
        # title on that same verified page, never a match on a neighboring page.
        if len(candidates) != 1:
            return False
        anchor = candidates[0]
        if section and anchor['section'] and section != anchor['section']:
            return False
        claimed = item.get('anchor')
        return claimed is None or all(claimed.get(key) == anchor.get(key)
                                      for key in ('unit', 'range', 'continuation'))

    @classmethod
    def from_node(cls, node):
        start, end = node.get('anchor'), node.get('end_anchor')
        # Character coordinates prove where text occurs, not a chapter boundary.
        # Legacy/unqualified anchors are also unknown source structure. Physical
        # pages remain readable in full when native structural proof is absent.
        start_confirmed = bool(start and start.get('evidence') == 'source_structure')
        end_confirmed = bool(end and end.get('evidence') == 'source_structure')
        return cls(
            node['title'],
            (start['unit'], start['range'][0]) if start_confirmed else (node['start_index'], 0),
            (end['unit'], end['range'][0]) if end_confirmed else (node['end_index'] + 1, 0),
            start_confirmed, end_confirmed,
            node.get('structure'),
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
        # Proposals cannot attest their own provenance or supply source offsets.
        item.pop('anchor', None)
        item.pop('end_anchor', None)
        item['title_origin'] = 'generated'
        anchor = unique_heading(item, page_list, start_index)
        if not anchor and boundary and boundary.start_confirmed and boundary.end_confirmed:
            positions = title_line_positions(item, page_list, start_index)
            if positions and all(not boundary.contains(position) for position in positions):
                # Every native occurrence is outside this parent. This is
                # location evidence, not proof that a literal line is a heading.
                continue
        if anchor:
            if boundary and not boundary.contains(anchor):
                continue  # The untouched boundary page remains in original readers.
            item.update(physical_index=anchor['unit'], anchor=anchor)
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
    previous_page, previous_offset = start, -1
    for item in entries:
        page = item.get('physical_index')
        if type(page) is not int or not start <= page < start + length:
            raise IndexQualityError('Physical heading is outside its original range')
        if page < previous_page:
            raise IndexQualityError('Physical headings do not follow original source order')
        if page > previous_page:
            previous_offset = -1
        # Model verification establishes a page, not a character offset. An
        # absent local anchor must not become a fabricated offset at page start.
        anchor = item.get('anchor')
        if anchor:
            offset = anchor['range'][0]
            if offset < previous_offset:
                raise IndexQualityError('Physical headings do not follow original source order')
            previous_offset = offset
        previous_page = page
