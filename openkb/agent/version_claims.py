"""Conservative clause-local applicability checks, partitioned by product.

This lexical check is one part of final evidence review, not a truth oracle.
Filenames and uncertainty statements identify evidence without granting the
remainder of the sentence an exemption. Citations are appended by the server.
"""

import re

from openkb.product_identity import product_key
from openkb.version_labels import version_key

VERSION = re.compile(r"(?:\bV\s*|版本\s*[:：]?\s*v?|\bversion\s+v?)(\d+(?:\.\d+){1,3})", re.I)
CLAUSES = re.compile(r"[\n；;。！？]|(?:，|,)\s*(?=但|不过|然而|but\b|however\b)", re.I)
POSITIVE = re.compile(
    r"适用于|(?:^|[^不未])支持|必须|要求|需要|应当|默认|能够|可用于|"
    r"\b(?:supports?|requires?|must|shall|defaults?|applies|compatible)\b",
    re.I,
)
UNCERTAIN = re.compile(
    r"(?:未|不|无法|尚未|不能|没有).{0,8}(?:确认|核实|验证|断言)|"
    r"未知版本|不适用|缺少.{0,16}证据|未.{0,8}(?:提供|获得|给出).{0,20}(?:已验证|已核实|经核实)|"
    r"\b(?:unverified|unconfirmed|unspecified|unknown|not verified|no evidence)\b",
    re.I,
)
REFERENCE = re.compile(r"文件名|资料名|文档名|标题|版本串|filename|file name|title", re.I)


def unsupported_versions(answer, selection) -> tuple[str, ...]:
    products: dict[str, list] = {}
    for view in selection.views:
        key = view.product_id or product_key(view.product or "") or view.view_id
        products.setdefault(key, []).append(view)
    unsupported: set[str] = set()
    for clause in CLAUSES.split(answer):
        # Link targets are references, but their visible text remains a claim.
        clause = re.sub(r"\]\([^)]*\)", "]", clause)
        uncertain = UNCERTAIN.search(clause)
        positive = POSITIVE.search(clause)
        if not positive and (uncertain or REFERENCE.search(clause)):
            continue
        # "cannot confirm X supports V..." negates that predicate, not other clauses.
        if uncertain and positive and uncertain.start() < positive.start():
            between = clause[uncertain.end() : positive.start()]
            if not re.search(r"但|不过|然而|\bbut\b|\bhowever\b", between, re.I):
                continue
        for match in VERSION.finditer(clause):
            if not positive and re.match(
                r"[^\s]*\.(?:pdf|docx|pptx|xlsx|md)\b", clause[match.end() :], re.I
            ):
                continue
            identities = _claim_products(clause, match.start(), products)
            if len(identities) != 1:
                unsupported.add(match.group(1))
                continue
            views = products[next(iter(identities))]
            verified = {
                version_key(v)
                for view in views
                if not view.reference_only
                for v in view.applicable_versions
            }
            if version_key(match.group(1)) not in verified:
                unsupported.add(match.group(1))
    return tuple(sorted(unsupported))


def _claim_products(clause: str, offset: int, products: dict) -> set[str]:
    mentions: list[tuple[int, int, str]] = []
    for identity, views in products.items():
        labels = {
            label for view in views for label in (view.product, *view.product_aliases) if label
        }
        for label in labels:
            pattern = r"[\s\-–—_]*".join(re.escape(part) for part in product_key(label).split())
            for match in re.finditer(
                r"(?<![A-Za-z0-9])" + pattern + r"(?![A-Za-z0-9])", clause, re.I
            ):
                mentions.append((match.start(), match.end(), identity))
    # Prefer the longest label when one product's name is a prefix of another.
    mentions = [
        item
        for item in mentions
        if not any(
            a <= item[0] and b >= item[1] and b - a > item[1] - item[0] for a, b, _ in mentions
        )
    ]
    preceding = [item for item in mentions if item[1] <= offset]
    if preceding:
        nearest = max(item[1] for item in preceding)
        return {identity for _, end, identity in preceding if end == nearest}
    if mentions:
        return {item[2] for item in mentions}
    return set(products)  # Unnamed claims across products must be disambiguated.
