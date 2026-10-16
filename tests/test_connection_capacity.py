"""Supported examples admit small questions without guessing a private model's capacity."""

import pytest

from ultrakb import config
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.execution_capacity import capacity_policy
from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("index", [1, 2])
def test_example_uses_explicit_utf8_bound_and_rejects_another_model(tmp_path, monkeypatch, index):
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", tmp_path / "settings")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "settings/global.yaml")
    template = CONNECTION_TEMPLATES[index]
    capacity = template_capacity(template)
    values = {"model": template.model, "model_capacity": capacity}
    policy = capacity_policy(values, custom_endpoint=True)
    measure = TokenMeasure(policy)
    text = "English 中文， é 👨‍👩‍👧‍👦 🇨🇳\n\t"
    assert measure.count(text) == len(text.encode("utf-8"))
    measure.require_current(text)
    # Capacity follows the service window, not the retired 64-KiB example.
    measure.require_current("中" * 22000)
    with pytest.raises(ContextCapacityError, match="retained without truncation"):
        measure.require_current("中" * (measure.input_limit // 3 + 1))
    with pytest.raises(ContextCapacityError, match="another model"):
        TokenMeasure(capacity_policy({**values, "model": "private/unknown"}, custom_endpoint=True))
    # The asset is repeatable; selecting the same example never downloads a tokenizer.
    assert template_capacity(template) == capacity


def test_custom_and_local_examples_do_not_invent_server_limits():
    assert template_capacity(CONNECTION_TEMPLATES[0]) is None
    assert template_capacity(CONNECTION_TEMPLATES[3]) is None
    assert capacity_policy({"model": "private/unknown"}, custom_endpoint=True)["unknown_reason"]
