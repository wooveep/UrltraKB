"""Portable, verifiable inputs shared by full-document and range recovery."""

import copy
import hashlib
import json
from pathlib import Path

from ..config import get_llm_params
from ..tokens import count_tokens

PARSER_POLICY = "physical-pdf-v1"
INDEX_CONTENT_POLICY = "content-based-physical-v4"
IMAGE_REFERENCE_POLICY = "generated-spans-v1"


def _reference_policy(metadata):
    policy = (metadata or {}).get("image_reference_policy")
    if policy is not None and policy != IMAGE_REFERENCE_POLICY:
        raise ValueError("Unsupported generated image reference policy")
    return policy is not None


def digest(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def parser_identity(parser) -> str:
    return f"{type(parser).__module__}.{type(parser).__qualname__}"


def parser_policy(parser) -> str:
    return getattr(parser, "policy", PARSER_POLICY)


def processing_key(path: Path, parser, model, config) -> str:
    # Credentials, transport settings, timestamps and temporary paths are not
    # input identity. Include only generation parameters that affect the tree.
    options = config.model_dump() if config is not None else {}
    params = {**get_llm_params(), **(options.pop("llm_params", None) or {})}
    options.pop("max_concurrency", None)
    options["generation"] = {
        key: params[key]
        for key in (
            "temperature",
            "top_p",
            "seed",
            "max_tokens",
            "max_completion_tokens",
            "reasoning_effort",
            "drop_params",
        )
        if key in params
    }
    payload = {
        "index_content_policy": INDEX_CONTENT_POLICY,
        "source_digest": digest(path),
        "parser_policy": parser_policy(parser),
        "parser": parser_identity(parser),
        "model": model,
        "config": options,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _relocate_images(content, images, targets, spans):
    """Relocate only parser-recorded image paths, preserving literal PDF text."""
    if spans is None:
        # Preserve the recorded semantics of earlier parsers and paid caches.
        for image, target in zip(images, targets):
            content = content.replace(image["path"], target)
        return content, None
    if not isinstance(spans, list) or len(spans) != len(images):
        raise ValueError("Generated image references require one span per occurrence")
    edits = []
    for i, (image, target, span) in enumerate(zip(images, targets, spans)):
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(value) is not int for value in span)
            or not 0 <= span[0] < span[1] <= len(content)
        ):
            raise ValueError("Invalid generated image reference span")
        start, end = span
        if content[start:end] != image.get("content_reference", image["path"]):
            raise ValueError("Generated image reference differs from its recorded path")
        edits.append((start, end, target, i))
    parts, updated, cursor, size = [], [None] * len(images), 0, 0
    for start, end, target, i in sorted(edits):
        if start < cursor:
            raise ValueError("Generated image reference spans overlap")
        part = content[cursor:start]
        parts.extend((part, target))
        size += len(part)
        updated[i] = [size, size + len(target)]
        size += len(target)
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts), updated


def relocate_page(page, targets):
    """Relocate recorded references and their opaque Unicode content spans.

    Span roles belong to the parser. This transport preserves their coordinates
    without interpreting source grammar; older records keep their saved behavior.
    """
    result = copy.deepcopy(page)
    images = page.get("images", [])
    if len(images) != len(targets):
        raise ValueError("Image relocation requires one target per occurrence")
    old_spans = page.get("image_reference_spans")
    content, spans = _relocate_images(page["content"], images, targets, old_spans)
    if "content_spans" in page:
        if old_spans is None or not isinstance(page["content_spans"], list):
            raise ValueError("Content spans require recorded image relocation spans")

        def position(value):
            if type(value) is not int or not 0 <= value <= len(page["content"]):
                raise ValueError("Invalid content span coordinate")
            shift = 0
            for (left, right), target in zip(old_spans, targets):
                if left < value < right:
                    raise ValueError("Content span cuts through a generated reference")
                if right <= value:
                    shift += len(target) - (right - left)
            return value + shift

        for item in result["content_spans"]:
            if not isinstance(item, dict) or "span" not in item:
                raise ValueError("Invalid recorded content span")
            for key in ("span", "generated_span"):
                if key not in item:
                    continue
                span = item[key]
                if (
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(type(value) is not int for value in span)
                    or not 0 <= span[0] < span[1] <= len(page["content"])
                ):
                    raise ValueError("Invalid recorded content span")
                item[key] = [position(span[0]), position(span[1])]
        if "parts" in result:
            body = page["parts"]["body"]
            if not page["content"].startswith(body):
                raise ValueError("Physical body is not a prefix of its full content")
            result["parts"]["body"] = content[: position(len(body))]
            if "slide" in result:
                result["slide"]["body"] = result["parts"]["body"]
            for origin in result.get("origin_locators", []):
                origin["range"] = [0, len(result["parts"][origin["part"]])]
    result["content"] = content
    if spans is not None:
        result["image_reference_spans"] = spans
        for image, target in zip(result.get("images", []), targets):
            image["content_reference"] = target
            image["path"] = target
    return result


def freeze_pages(parsed, base: Path, model) -> list[dict]:
    """Keep every physical page, including empty bodies, and portable image paths."""
    pages = []
    base = base.resolve()
    generated_references = _reference_policy(parsed.metadata)
    if (parsed.metadata or {}).get("unit_kind") == "block":
        units = [{**node.metadata, "content": node.content} for node in parsed.nodes]
        parsed.metadata["units_digest"] = hashlib.sha256(
            json.dumps(units, sort_keys=True).encode()
        ).hexdigest()
        return units
    for node in parsed.nodes:
        content = node.content
        images = []
        for image in node.images or []:
            original = Path(image["path"])
            if not original.is_absolute():
                original = base / original
            original = original.resolve()
            if not original.is_relative_to(base):
                raise ValueError("Parser image escaped managed input storage")
            relative = original.relative_to(base).as_posix()
            images.append({**image, "path": relative, "digest": digest(original)})
        spans = (node.metadata or {}).get("image_reference_spans")
        if generated_references and spans is None:
            raise ValueError("Parser omitted generated image reference locations")
        relocated = relocate_page(
            {**(node.metadata or {}), "content": content, "images": node.images or []},
            [image["path"] for image in images],
        )
        content = relocated.pop("content")
        relocated.pop("images")
        node.metadata = relocated
        if spans is not None:
            for image in images:
                image["content_reference"] = image["path"]
        pages.append(
            {
                **(node.metadata or {}),
                "page": node.index,
                "content": content,
                **({"images": images} if images else {}),
            }
        )
        if node.content != content:
            node.tokens = count_tokens(content, model=model)
        node.content = content
        node.images = images or None
    if (parsed.metadata or {}).get("notes_policy"):
        for page in pages:
            for locator in page.get("origin_locators", []):
                locator["range"] = [0, len(page["parts"][locator["part"]])]
        parsed.metadata["page_parts"] = [page["parts"] for page in pages]
        if all("content_spans" in page for page in pages):
            parsed.metadata["page_generated_spans"] = [
                [
                    item["generated_span"]
                    for item in page["content_spans"]
                    if "generated_span" in item
                ]
                for page in pages
            ]
        parsed.metadata["units_digest"] = hashlib.sha256(
            json.dumps(pages, sort_keys=True).encode()
        ).hexdigest()
    if generated_references:
        parsed.metadata["units_digest"] = hashlib.sha256(
            json.dumps(pages, sort_keys=True).encode()
        ).hexdigest()
    return pages


def materialize_pages(pages, base: Path, metadata: dict) -> list[dict]:
    """Validate a cache, then resolve its retained assets at the current storage root."""
    if not isinstance(pages, list):
        raise ValueError("Cached input must contain a page list")
    if not isinstance(metadata, dict):
        raise ValueError("Cached input has invalid metadata")
    if metadata.get("unit_kind") == "block":
        from ..index.block_policy import BlockPolicy

        policy = BlockPolicy(metadata)
        if hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest() != metadata.get(
            "units_digest"
        ):
            raise ValueError("Cached content-block source mapping changed")
        if len(pages) != len(policy.units):
            raise ValueError("Cached content-block coverage changed")
        result = copy.deepcopy(pages)
        assets = {}
        for name, expected in metadata.get("assets", {}).items():
            path = Path(name)
            target = (base / path).resolve()
            if (
                path.is_absolute()
                or ".." in path.parts
                or not target.is_relative_to(base.resolve())
            ):
                raise ValueError("Block asset escaped its retained package")
            if digest(target) != expected:
                raise ValueError("Block asset digest changed")
            assets[name] = {"digest": expected, "path": str(target)}
        for cached, unit in zip(result, policy.units):
            if any(cached.get(key) != value for key, value in unit.items()):
                raise ValueError("Cached content-block mapping changed")
            if cached.get("content") != "".join(policy.text[a:b] for a, b in unit["source_spans"]):
                raise ValueError("Cached content-block original changed")
            cached["assets"] = assets
        return result
    result = copy.deepcopy(pages)
    if _reference_policy(metadata):
        if hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest() != metadata.get(
            "units_digest"
        ) or any(not isinstance(page.get("image_reference_spans"), list) for page in pages):
            raise ValueError("Cached generated image reference mapping changed")
    if metadata.get("notes_policy"):
        from ..index.page_parts_policy import PagePartsPolicy

        policy = PagePartsPolicy(metadata)
        if (
            hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()
            != metadata.get("units_digest")
            or [page.get("parts") for page in pages] != policy.parts
        ):
            raise ValueError("Cached physical slide body/notes mapping changed")
        if all("content_spans" in page for page in pages):
            generated = [
                [
                    item["generated_span"]
                    for item in page["content_spans"]
                    if "generated_span" in item
                ]
                for page in pages
            ]
            if generated != metadata.get("page_generated_spans"):
                raise ValueError("Cached physical slide generated content mapping changed")
    ordinals = []
    for page in result:
        if not isinstance(page, dict) or not isinstance(page.get("content"), str):
            raise ValueError("Cached input has an invalid page body")
        number = page.get("page")
        if type(number) is not int or number < 1:
            raise ValueError("Cached input has an invalid ordinal")
        ordinals.append(number)
        if not isinstance(page.get("images", []), list) or any(
            not isinstance(image, dict) or not isinstance(image.get("path"), str)
            for image in page.get("images", [])
        ):
            raise ValueError("Cached input has invalid image references")
        images = copy.deepcopy(page.get("images", []))
        targets = []
        for image in page.get("images", []):
            path = Path(image["path"])
            if metadata.get("parser_policy"):
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Managed image reference is not portable")
                target = (base / path).resolve()
                if not target.is_relative_to(base.resolve()):
                    raise ValueError("Managed image reference escapes input storage")
                if digest(target) != image.get("digest"):
                    raise ValueError("Managed image digest changed")
                image["path"] = str(target)
            targets.append(image["path"])
        relocated = relocate_page({**page, "images": images}, targets)
        page.update({key: value for key, value in relocated.items() if key != "images"})
        if page.get("image_reference_spans") is not None:
            for image in page.get("images", []):
                image["content_reference"] = image["path"]
    if len(set(ordinals)) != len(ordinals) or sorted(ordinals) != ordinals:
        raise ValueError("Cached input ordinals must be unique and ordered")
    if metadata.get("unit_kind") == "page":
        count = metadata.get("unit_count")
        if type(count) is not int or count < 0 or ordinals != list(range(1, count + 1)):
            raise ValueError("Cached physical page coverage is incomplete")
    return result
