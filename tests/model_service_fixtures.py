"""Shared genuine SDK service fixtures, independent of collected test modules."""

from contextlib import contextmanager

import pytest


@contextmanager
def running_model_service(monkeypatch, live_llm):
    """Observe genuine SDK traffic through the shared budgeted DeepSeek gateway."""
    live_llm.install(monkeypatch)
    service = live_llm._proxy
    service.credentials = live_llm.credentials
    service.requests.clear()
    service.records.clear()
    service.intervals.clear()
    service.delay = 0
    try:
        yield service
    finally:
        service.delay = 0


@pytest.fixture
def model_service(monkeypatch, live_llm):
    with running_model_service(monkeypatch, live_llm) as service:
        yield service
