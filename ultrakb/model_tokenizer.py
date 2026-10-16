"""Explicit offline tokenizer assets and conservative request sizing; no model guesses."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path


class ContextCapacityError(ValueError):
    """The current submission cannot safely enter the configured model context."""


def tokenizer_asset(config: dict) -> dict | None:
    name = (config.get("model_capacity") or {}).get("tokenizer_model")
    if not isinstance(name, str) or not name.startswith("local:"):
        return None
    path = Path(name.removeprefix("local:"))
    if not path.is_absolute():
        raise ContextCapacityError("Local tokenizer_model must specify an absolute JSON path")
    if path.is_dir():
        path /= "tokenizer.json"
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContextCapacityError("Configured local tokenizer JSON is unavailable") from error
    return {"path": str(path.resolve()), "digest": hashlib.sha256(raw).hexdigest()}


@lru_cache(maxsize=8)
def _local_tokenizer(path: str, digest: str):
    from tokenizers import Tokenizer

    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ContextCapacityError("Tokenizer changed after settings capture; start a new task")
    tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    tokenizer.no_truncation()
    return tokenizer


class TokenMeasure:
    def __init__(self, policy: dict):
        if policy["unknown_reason"]:
            raise ContextCapacityError(
                "Configure model_capacity with an input limit and reliable tokenizer_model: "
                + policy["unknown_reason"]
            )
        self.input_limit = policy["input_limit"]
        self.policy = policy
        self.asset = policy.get("tokenizer_asset")
        self.unit = policy.get("measurement_unit", "token")
        if self.unit in {"utf8_byte", "token_upper_bound"} and not self.asset:
            self.tokenizer, self.fingerprint = None, "utf8-byte-bound-v1"
            self.encoding = None
            return
        if self.asset:
            self.tokenizer = _local_tokenizer(**self.asset)
            raw = json.loads(self.tokenizer.to_str())
            model, pre = raw.get("model", {}), raw.get("pre_tokenizer", {})
            if (
                model.get("type") == "BPE"
                and len(model.get("vocab", {})) == 256
                and model.get("merges") == []
                and not raw.get("added_tokens")
                and raw.get("normalizer") is None
                and raw.get("post_processor") is None
                and raw.get("padding") is None
                and not model.get("continuing_subword_prefix")
                and not model.get("end_of_word_suffix")
                and pre.get("type") == "ByteLevel"
                and pre.get("add_prefix_space") is False
                and pre.get("use_regex") is False
            ):
                from tokenizers.pre_tokenizers import ByteLevel

                if set(model["vocab"]) == set(ByteLevel.alphabet()):
                    self.unit = "utf8_byte"
            self.encoding = None
            self.fingerprint = self.asset["digest"]
        else:
            import tiktoken

            self.tokenizer = None
            self.encoding = tiktoken.encoding_for_model(policy["measurement_model"])
            self.fingerprint = self.encoding.name

    def encode(self, text: str) -> list[int]:
        if self.unit in {"utf8_byte", "token_upper_bound"}:
            return list(text.encode("utf-8"))
        if self.tokenizer:
            return self.tokenizer.encode(text, add_special_tokens=False).ids
        assert self.encoding is not None
        return self.encoding.encode(text, disallowed_special=())

    def count(self, text: str) -> int:
        return len(self.encode(text))

    def payload_tokens(self, payload) -> int:
        # Serialize role delimiters, schemas and locator metadata too. The
        # provider's usage remains authoritative for billing, not this estimate.
        return self.count(json.dumps(payload, ensure_ascii=False, default=str))

    def require_current(self, question: str) -> None:
        if self.count(question) > self.input_limit:
            unit = "UTF-8 bytes" if self.unit == "utf8_byte" else "tokens"
            raise ContextCapacityError(
                f"Current question exceeds model capacity ({self.input_limit} input {unit}); "
                "the original submission is retained without truncation"
            )

    def prefix(self, text: str, budget: int) -> str:
        """Take a bounded character prefix without cutting a Unicode character."""
        if self.count(text) <= budget:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            middle = (lo + hi + 1) // 2
            if self.count(text[:middle]) <= budget:
                lo = middle
            else:
                hi = middle - 1
        return text[:lo]


def question_measure(snapshot) -> TokenMeasure:
    from ultrakb.execution_capacity import capacity_policy

    values = snapshot.values()
    policy = capacity_policy(
        values["effective"],
        custom_endpoint=bool(values["credentials"]["base_url"]),
        tokenizer_snapshot=values.get("tokenizer_asset"),
        base_url=values["credentials"]["base_url"],
    )
    return TokenMeasure(policy)
