"""Literal title locations and proved parent bounds for physical-page subtrees."""

import copy
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from ..errors import IndexQualityError

PHYSICAL_NAVIGATION_POLICY = "body-first-v2"

# Native PDF extraction can remove the space between a section number and a
# Chinese title. Recognizing that spelling locates text; it does not establish
# heading role, which still requires the normal delivered verification verdict.
NUMBERED_HEADING_PREFIX = (
    r"(?P<section>\d+(?:\.\d+)*)(?:[.、)](?=\D)\s*|\s+|"
    r"(?=[\u3400-\u9fff\uf900-\ufaff]))"
)


class SubtreeQualityError(IndexQualityError):
    """Local recovery is exhausted; repeating the validated parent cannot help."""

    local_recovery_exhausted = True


class PhysicalNavigationError(IndexQualityError):
    """Physical correction/rebuilding is exhausted; do not restart the document."""

    local_recovery_exhausted = True


def requires_local_correction(accuracy, incorrect_results, *, policy=None, boundary=None):
    """A bounded source section must try local correction even below the global threshold."""
    return bool((policy or boundary or accuracy > 0.6) and len(incorrect_results) > 0)


def require_corrected_navigation(incorrect_results, policy=None):
    """Uncorrected anchors cannot become a successful navigation of any source kind."""
    if not incorrect_results:
        return
    from .block_policy import BlockContractError
    from .page_parts_policy import PagePartsContractError, PagePartsPolicy

    if getattr(policy, 'unit_kind', None) == 'block':
        raise BlockContractError("Block correction attempts exhausted")
    if isinstance(policy, PagePartsPolicy):
        raise PagePartsContractError("Unverified slide anchors after correction")
    raise PhysicalNavigationError("Unverified physical-page anchors after correction")


@lru_cache(maxsize=2048)
def _comparison_text(value):
    # Native PDF glyphs can use compatibility characters (⾃/自, ﬁ/fi).
    # Normalize only a comparison view; original readers and spans never change.
    return unicodedata.normalize("NFKD", value).casefold()


def compact(value):
    return re.sub(r"\s+", "", _comparison_text(value))


def heading(line, *, allow_attached=False):
    stripped = line.strip()
    if re.search(r"\.{2,}|…|⋯", stripped):
        return None
    markdown = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", stripped)
    body = markdown.group(1) if markdown else stripped
    chapter = re.match(r"^第\s*(\d+)\s*章\s*(.+)$", body)
    if chapter:
        return chapter.group(2), chapter.group(1)
    numbered = re.match(r"^" + NUMBERED_HEADING_PREFIX + r"(?P<title>.+)$", body)
    if numbered:
        return numbered.group("title"), numbered.group("section")
    if allow_attached:
        # A delivered title supplies the literal wording for this comparison.
        # Dotted section numbers may touch Latin text (e.g. 4.4.7DRS规则).
        # The context-free parser still leaves numbers such as 1.2e3 alone.
        attached = re.match(r"^(\d+(?:\.\d+)+)([^\W\d_].*)$", body)
        if attached:
            return attached.group(2), attached.group(1)
    if markdown:
        return body, None
    return None


def literal_title_pattern(item):
    """Match the delivered wording across native whitespace, not inferred wording."""
    parsed = heading(item['title'], allow_attached=True)
    title = parsed[0] if parsed else item['title']
    literal = r"\s*".join(re.escape(char) for char in compact(title))
    if not literal:
        return r"(?!)"
    return (
        r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?"
        r"(?:(?P<section>\d+(?:\.\d+)*)(?:[.、)](?=\D)\s*|\s+|(?=[^\W\d_]))"
        r"|第\s*(?P<chapter>\d+)\s*章\s*)?" + literal + r"[ \t]*$"
    )


def literal_heading_matches(pattern, text):
    """Map complete comparison-view matches back to unchanged native characters."""
    view = _comparison_text(text)
    matches = list(re.finditer(pattern, view))
    if not matches:
        return []
    offsets = None
    if len(view) != len(text):
        # Decomposition/case folding can expand a glyph into several characters.
        # Retain the source index of every comparison character, including
        # canonical ordering of combining marks within a Unicode cluster.
        offsets = []
        for index, char in enumerate(text):
            for normalized in unicodedata.normalize("NFKD", char).casefold():
                position = len(offsets)
                combining = unicodedata.combining(normalized)
                while (
                    combining and position
                    and unicodedata.combining(offsets[position - 1][0]) > combining
                ):
                    position -= 1
                offsets.insert(position, (normalized, index))
        if ''.join(char for char, _ in offsets) != view:
            return []  # Never fabricate positions for an unproved normalization.
    result = []
    for match in matches:
        first, last = match.span()
        if offsets is not None:
            native = [index for _, index in offsets[first:last]]
            first, last = min(native), max(native) + 1
        result.append({'range': [first, last], 'excerpt': text[first:last],
                       'section': match.group('section') or match.group('chapter')})
    return result


def heading_candidates(item, page_list, start_index=1):
    title = heading(item['title'], allow_attached=True)
    wanted = compact(title[0] if title else item['title'])
    literal_pattern = literal_title_pattern(item)
    result = []
    for number, (text, *_) in enumerate(page_list, start_index):
        offset = 0
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            parsed = heading(line, allow_attached=True)
            if parsed and compact(parsed[0]) == wanted:
                result.append({'unit': number, 'range': [offset, offset + len(line.rstrip())],
                               'excerpt': text[offset:offset + len(line.rstrip())], 'section': parsed[1]})
            # PDF extraction often puts a section number on its own line.
            # Only join the immediately adjacent title, keeping its native span.
            number_only = re.fullmatch(
                r'(?:(\d+(?:\.\d+)*)(?:[.、)])?|第\s*(\d+)\s*章)', line.strip())
            if number_only and index + 1 < len(lines) and compact(lines[index + 1]) == wanted:
                end = offset + len(line) + len(lines[index + 1].rstrip())
                result.append({'unit': number, 'range': [offset, end],
                               'excerpt': text[offset:end],
                               'section': number_only.group(1) or number_only.group(2)})
            # A wrapped heading may cross the page boundary. Keep its start on
            # the first page and record the continuation, without altering text.
            if parsed and index == len(lines) - 1 and number - start_index + 1 < len(page_list):
                next_lines = page_list[number - start_index + 1][0].splitlines()
                continuation = next_lines[0] if next_lines else ''
                if continuation and compact(parsed[0] + continuation) == wanted:
                    result.append({'unit': number, 'range': [offset, offset + len(line.rstrip())],
                                   'excerpt': text[offset:offset + len(line.rstrip())], 'section': parsed[1],
                                   'continuation': {'unit': number + 1, 'range': [0, len(continuation)],
                                                    'excerpt': continuation}})
            offset += len(line)
        for match in literal_heading_matches(literal_pattern, text):
            if match['section'] or match['excerpt'].lstrip().startswith('#'):
                result.append({'unit': number, **match})
    # Line parsing and wrapped literal matching can locate the same heading.
    # Keep one native start, including any cross-page continuation evidence.
    unique = {}
    for anchor in result:
        unique.setdefault((anchor['unit'], anchor['range'][0], anchor['section']), anchor)
    return list(unique.values())


@lru_cache(maxsize=8)
def _numbered_pages(texts, start):
    result = {}
    pattern = re.compile(r'(?m)^[ \t]*(?:#{1,6}[ \t]+)?(?:(\d+(?:\.\d+)*)(?=\s|[^\W\d_]|$)|第\s*(\d+)\s*章)')
    for number, text in enumerate(texts, start):
        for match in pattern.finditer(text):
            section = match.group(1) or match.group(2)
            if number not in result.setdefault(section, []):
                result[section].append(number)
    return result


def unique_heading(item, page_list, start_index=1, *, prefer_declared_page=False, boundary=None):
    """Locate a unique title candidate; raw PDF syntax does not prove its role."""
    if item.get('structure') is not None and not isinstance(item['structure'], str):
        raise IndexQualityError('Physical structure must be a string or null')
    parsed = heading(item['title'], allow_attached=True)
    sections = {value for value in (item.get('structure'), parsed[1] if parsed else None)
                if value}
    if len(sections) > 1:
        return None
    section = next(iter(sections)) if sections else None
    def allowed(anchors):
        return [a for a in anchors if boundary is None or boundary.contains(a)]
    def choose(anchors):
        anchors = allowed(anchors)
        if section:
            exact = [a for a in anchors if a['section'] == section]
            anchors = exact or [a for a in anchors if not a['section']]
        return {**anchors[0], 'evidence': 'text_match'} if len(anchors) == 1 else None
    number = item.get('physical_index')
    local = []
    if prefer_declared_page and type(number) is int and start_index <= number < start_index + len(page_list):
        local = allowed(heading_candidates(item, page_list[number - start_index:number - start_index + 2], number))
        exact = [a for a in local if a['unit'] == number and (not section or a['section'] == section)]
        if len(exact) == 1:
            return {**exact[0], 'evidence': 'text_match'}
    # A native numbered match outranks every unrelated unnumbered namesake.
    # Index source number lines once; generated Markdown section labels need
    # not scan hundreds of pages to prove that their number is not literal.
    if sections:
        numbers = _numbered_pages(tuple(p[0] for p in page_list), start_index).get(section, [])
        exact = {}
        for position in numbers:
            if boundary and not boundary.start[0] <= position <= boundary.end[0]:
                continue
            for anchor in allowed(heading_candidates(item, page_list[position - start_index:position - start_index + 2], position)):
                if anchor['section'] == section:
                    exact[(anchor['unit'], tuple(anchor['range']))] = anchor
        if exact:
            return choose(list(exact.values()))
    local = [a for a in local if a['unit'] == number]
    if local:
        return choose(local)
    first = max(start_index, boundary.start[0]) if boundary else start_index
    last = min(start_index + len(page_list), boundary.end[0] + 1) if boundary else start_index + len(page_list)
    return choose(heading_candidates(item, page_list[first - start_index:last - start_index], first))


def title_line_positions(item, page_list, start_index=1):
    """Locate literal title lines, without claiming that they are headings."""
    parsed = heading(item['title'], allow_attached=True)
    wanted = compact(parsed[0] if parsed else item['title'])
    matches = []
    for number, (text, *_) in enumerate(page_list, start_index):
        offset = 0
        for line in text.splitlines(keepends=True):
            if wanted and compact(line) == wanted:
                matches.append({'unit': number, 'range': [offset, offset + len(line.rstrip())]})
            offset += len(line)
    return matches


def finish_title_verification(item, response, page_list, start_index=1, boundary=None):
    """Apply a delivered heading-role verdict without inventing source coordinates."""
    page_number, title = item["physical_index"], item["title"]
    if 'answer' in response:
        answer = response['answer']
    else:
        answer = 'no'
    if boundary:
        # A model establishes heading role, not physical coordinates. Native
        # positions plus confirmed parent bounds can prove membership even
        # when printed page labels mislead the model's separate membership claim.
        positions = title_line_positions(item, page_list, start_index)
        current = [position for position in positions if position['unit'] == page_number]
        proved = (boundary.start_confirmed and boundary.end_confirmed and len(current) == 1
                  and all(boundary.contains(position) for position in positions))
        if (positions and boundary.start_confirmed and boundary.end_confirmed
                and not any(boundary.contains(position) for position in current)):
            answer = 'no'
        elif not proved and response.get('belongs_to_parent') is not True:
            answer = 'no'
    return {'list_index': item['list_index'], 'answer': answer, 'title': title, 'page_number': page_number}


def physical_child_entries(boundary, entries, page_list, start_index=1):
    """Remove only the first proposal whose native identity proves this parent root."""
    if entries and boundary.is_parent_root(entries[0], page_list, start_index):
        return entries[1:]
    return entries


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
        parsed = heading(item['title'], allow_attached=True)
        title = parsed[0] if parsed else item['title']
        parent = heading(self.title, allow_attached=True)
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


def prepare_entries(entries, page_list, start_index=1, boundary=None, density=4,
                    prefer_declared_page=False, respect_parents=False):
    if not isinstance(entries, list):
        raise IndexQualityError('Physical navigation must be an entry list')
    if not entries:
        if boundary or start_index != 1:
            raise IndexQualityError('Subtree navigation must be a nonempty entry list')
        # The extraction prompt requires original headings. An empty root
        # proposal is legitimate for unheaded prose, not heading evidence.
        return []
    if density is not None and len(entries) > density * len(page_list):
        raise IndexQualityError('Physical navigation exceeds the configured node density')
    if any(not isinstance(item, dict) or not isinstance(item.get('title'), str) for item in entries):
        raise IndexQualityError('Physical navigation requires original heading titles')
    if respect_parents:
        from .body_locations import locate_body_entries
        located = locate_body_entries(entries, page_list, start_index)
    else:
        located = [(item, None) for item in entries]
    accepted, seen = [], set()
    for original, located_anchor in located:
        if not isinstance(original, dict) or not isinstance(original.get('title'), str):
            raise IndexQualityError('Physical navigation requires original heading titles')
        item = copy.deepcopy(original)
        # Proposals cannot attest their own provenance or supply source offsets.
        item.pop('anchor', None)
        item.pop('end_anchor', None)
        item['title_origin'] = 'generated'
        anchor = located_anchor if respect_parents else unique_heading(
            item, page_list, start_index, prefer_declared_page=prefer_declared_page)
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
            identity = (anchor['unit'], tuple(anchor['range']))
            if identity in seen:
                continue
            seen.add(identity)
        accepted.append(item)
    if not accepted:
        raise IndexQualityError('No original headings belong to the requested section')
    return accepted


def document_range_entry(page_list):
    """Coarse navigation for an empty root outline, without a heading claim."""
    if not page_list or not any(page[0].strip() for page in page_list):
        raise IndexQualityError('Document navigation requires original source text')
    return {'structure': '0', 'title': 'Document', 'title_origin': 'generated',
            'physical_index': 1, 'appear_start': 'yes'}


def merge_continuation(previous, incoming, *, page_list=None, start_index=1):
    """Continuation may append new positions, never overwrite the accepted prefix."""
    if not isinstance(incoming, list) or any(not isinstance(item, dict) for item in incoming):
        raise IndexQualityError('Physical continuation requires an entry list')
    def identity(item):
        # Keep prompt/response representations intact. Native locations can
        # distinguish same-title parent/child headings on the overlap page.
        from .utils import convert_physical_index_to_int
        normalized = convert_physical_index_to_int([copy.deepcopy(item)])[0]
        if page_list is not None:
            anchor = unique_heading(normalized, page_list, start_index,
                                    prefer_declared_page=True)
            if anchor:
                return ('source', anchor['unit'], tuple(anchor['range']))
        parsed = heading(item.get('title', ''), allow_attached=True)
        title = parsed[0] if parsed else item.get('title', '')
        return ('proposal', normalized.get('physical_index'), compact(title))

    existing = {identity(item): item for item in previous}
    result = []
    for item in incoming:
        key = identity(item)
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
