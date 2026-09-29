"""Portable, verifiable inputs shared by full-document and range recovery."""

import copy
import hashlib
import json
from pathlib import Path

from ..config import get_llm_params
from ..tokens import count_tokens

PARSER_POLICY = "physical-pdf-v1"


def digest(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def parser_identity(parser) -> str:
    return f"{type(parser).__module__}.{type(parser).__qualname__}"


def processing_key(path: Path, parser, model, config) -> str:
    # Credentials, transport settings, timestamps and temporary paths are not
    # input identity. Include only generation parameters that affect the tree.
    options = config.model_dump() if config is not None else {}
    params = {**get_llm_params(), **(options.pop("llm_params", None) or {})}
    options.pop("max_concurrency", None)
    options["generation"] = {
        key: params[key] for key in (
            "temperature", "top_p", "seed", "max_tokens", "max_completion_tokens",
            "reasoning_effort", "drop_params",
        ) if key in params
    }
    payload = {
        "source_digest": digest(path), "parser_policy": PARSER_POLICY,
        "parser": parser_identity(parser),
        "model": model, "config": options,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def freeze_pages(parsed, base: Path, model) -> list[dict]:
    """Keep every physical page, including empty bodies, and portable image paths."""
    pages = []
    base = base.resolve()
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
            content = content.replace(image["path"], relative)
            images.append({**image, "path": relative, "digest": digest(original)})
        pages.append({
            **(node.metadata or {}),
            "page": node.index, "content": content,
            **({"images": images} if images else {}),
        })
        if node.content != content:
            node.tokens = count_tokens(content, model=model)
        node.content = content
        node.images = images or None
    return pages


def materialize_pages(pages, base: Path, metadata: dict) -> list[dict]:
    """Validate a cache, then resolve its retained assets at the current storage root."""
    if not isinstance(pages, list):
        raise ValueError("Cached input must contain a page list")
    if not isinstance(metadata, dict):
        raise ValueError("Cached input has invalid metadata")
    result = copy.deepcopy(pages)
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
                page["content"] = page["content"].replace(image["path"], str(target))
                image["path"] = str(target)
    if len(set(ordinals)) != len(ordinals) or sorted(ordinals) != ordinals:
        raise ValueError("Cached input ordinals must be unique and ordered")
    if metadata.get("unit_kind") == "page":
        count = metadata.get("unit_count")
        if type(count) is not int or count < 0 or ordinals != list(range(1, count + 1)):
            raise ValueError("Cached physical page coverage is incomplete")
    return result
