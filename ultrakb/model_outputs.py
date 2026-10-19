"""Outcome facts for files accepted by a shared native model output writer."""

from dataclasses import dataclass


@dataclass
class ModelOutputs:
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
