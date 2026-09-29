"""Fixed, offline document-length measurement, independent of execution models."""

import base64
import hashlib
from functools import lru_cache
from importlib.resources import files

_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
MEASUREMENT_FINGERPRINT = f"tiktoken-0.13.0:cl100k_base:{_HASH}:ordinary-v1"


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken

    data = files("openkb").joinpath("assets/tokenizers/cl100k_base.tiktoken").read_bytes()
    if hashlib.sha256(data).hexdigest() != _HASH:
        raise ValueError("Bundled cl100k_base resource failed its digest check")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }
    return tiktoken.Encoding(
        name="openkb_cl100k_base",
        # The 0.13.0 cl100k_base pattern and ranks are a single pinned policy.
        pat_str=(
            r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"
            r"| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"
        ),
        mergeable_ranks=ranks,
        special_tokens={},
    )


def measure_markdown(text: str) -> int:
    """Count all frozen text once; special-token-looking text remains ordinary text."""
    return len(_encoding().encode_ordinary(text))
