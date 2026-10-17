"""Recovery fixes keep retained normalization and paid response identities reusable."""

import importlib.metadata
import json

from ultrakb.normalization import normalization_fingerprint


def test_recovery_sdk_keeps_the_preceding_content_identity(kb_dir, monkeypatch):
    original = importlib.metadata.version
    current = "0.3.0.dev3+urltrakb.16"
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: current if name == "pageindex" else original(name),
    )
    before = normalization_fingerprint(kb_dir)
    current = "0.3.0.dev3+urltrakb.17"
    after = normalization_fingerprint(kb_dir)
    assert after == before
    assert json.loads(after)["index_sdk"] == "0.3.0.dev3+urltrakb.16"
