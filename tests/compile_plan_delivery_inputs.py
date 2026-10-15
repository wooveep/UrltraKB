"""Original-only input preparation for the nonempty plan delivery regression.

The native block and structural navigation come from retained Markdown bytes.
No summary, plan, concept page, or model response is prepared here.
"""

from dataclasses import dataclass
from pathlib import Path

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.block_package import freeze_block_package
from ultrakb.config import save_config
from ultrakb.inputs import prepared_input
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.text_source import FrozenText, freeze_markdown

DOC_NAME = "orion-retry-policy"
ORIGINAL = (
    "The Orion retry policy sets `retry_limit = 17` attempts for the local queue. "
    "This documented upper bound does not guarantee delivery.\n"
)


@dataclass(frozen=True)
class DeliveryInput:
    kb: Path
    original: Path
    retained: Path
    package: Path
    navigation: list[dict]

    @property
    def wiki(self):
        return self.kb / "wiki"

    @property
    def summary(self):
        return self.wiki / "summaries" / f"{DOC_NAME}.md"


def prepare_delivery_input(base: Path) -> DeliveryInput:
    kb = base / "knowledge"
    initialize_kb(kb, model="deepseek/deepseek-flash", language="en", seed_environment=False)
    # Persist only the real task controls. Capacity/tokenizer remain resolved by
    # the same default/global capture used by the product; no test alias is set.
    save_config(
        kb / ".ultrakb/config.yaml",
        {
            "model": "deepseek/deepseek-flash",
            "language": "en",
            "timeout": 30,
            "model_policy": {
                "max_calls": 5,
                "retries": 0,
                "backoff": 0,
                "concurrency": 1,
                "deadline_seconds": 150,
            },
            "role_generation": {"compile": {"max_tokens": 768, "reasoning_effort": "none"}},
        },
    )
    original = base / "original.md"
    atomic_write_text(original, ORIGINAL)
    with prepared_input(original) as ready:
        frozen = freeze_markdown(ready, DOC_NAME, kb / "wiki")
    retained = kb / "wiki/sources" / f"{DOC_NAME}.md"
    atomic_write_text(retained, frozen.text)
    atomic_write_json(retained.with_suffix(".content.json"), frozen.model_dump(mode="json"))
    package = freeze_block_package(retained, DOC_NAME)
    frozen = FrozenText.model_validate_json(retained.with_suffix(".content.json").read_text())
    navigation = [
        {
            "node_id": f"original-block-{block.ordinal}",
            "title": f"Original block {block.ordinal}",
            "start_index": block.ordinal,
            "end_index": block.ordinal,
        }
        for block in frozen.blocks
    ]
    return DeliveryInput(kb, original, retained, package, navigation)
