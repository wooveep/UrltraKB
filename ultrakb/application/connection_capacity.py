"""Explicit, model-bound capacity for the supported connection examples."""

import hashlib

from ultrakb import config
from ultrakb.locks import atomic_write_text


def template_capacity(template):
    if template.model not in {"gpt-5.4", "deepseek/deepseek-flash"}:
        return None
    from tokenizers import Tokenizer, models, pre_tokenizers

    # With no BPE merges, each UTF-8 byte is one unit. This is a conservative
    # text-input bound, not a claim to reproduce either provider's tokenizer.
    # 64 KiB leaves ample output/protocol reserve within these documented
    # service examples' context windows. Unknown models get no guessed policy.
    tokenizer = Tokenizer(
        models.BPE(
            vocab={
                char: index
                for index, char in enumerate(sorted(pre_tokenizers.ByteLevel.alphabet()))
            },
            merges=[],
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=False)
    raw = tokenizer.to_str()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    path = config.GLOBAL_CONFIG_DIR / "tokenizers" / f"utf8-bound-v1-{digest[:16]}.json"
    with config._with_global_config_lock():
        if not path.exists() or path.read_text() != raw:
            atomic_write_text(path, raw)
    return {
        "max_input_tokens": 65536,
        "tokenizer_model": "local:" + str(path.resolve()),
        "applies_to_model": template.model,
    }
