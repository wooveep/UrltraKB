"""Explicit offline experiment limits; these are not production defaults."""

import yaml

OFFLINE_PROCESSING = {
    "request_timeout": 5,
    "stage_timeout": 30,
    "document_timeout": 60,
    "cleanup_timeout": 5,
    "max_attempts": 2,
    "max_requests": 100,
    "max_tokens": 1000000,
    "concurrency": 5,
    "context_tokens": 128000,
    "output_tokens": 4096,
}


def configure_processing(kb):
    path = kb / ".openkb/config.yaml"
    config = yaml.safe_load(path.read_text()) or {}
    config.setdefault("processing", OFFLINE_PROCESSING)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
