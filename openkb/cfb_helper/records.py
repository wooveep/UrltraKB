"""Attested native helper metadata, validated before a subprocess can start."""

from typing import Literal

from openkb.source_records import Digest, Record, RelativePath


class HelperManifest(Record):
    platform: Literal["linux", "win32"]
    version: Literal["openkb-cfb 1.0.0 cfb 0.15.0"]
    binary: Literal["openkb-cfb", "openkb-cfb.exe"]
    sha256: Digest
    toolchain: Literal["1.95.0"]
    source: dict[RelativePath, Digest]
    dependencies: Digest
    source_archive: Digest
