"""Small CNKI inputs shared independently of test-module collection order."""

import pymupdf
import pytest


@pytest.fixture
def cnki_source(tmp_path):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Original CNKI content.")
        data = pdf.tobytes()
    key = b"FZHMEI"
    path = tmp_path / "中文 空格.KDH"
    path.write_bytes(
        b"KDH 2.00".ljust(254, b"\0")
        + bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))
    )
    return path
