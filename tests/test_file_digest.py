from ultrakb.file_digest import hash_file


def test_hash_file_produces_64_char_hex(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("hello world")
    digest = hash_file(f)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_file_deterministic(tmp_path):
    f = tmp_path / "data.txt"
    f.write_text("deterministic content")
    assert hash_file(f) == hash_file(f)


def test_hash_file_different_content(tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("content A")
    f2.write_text("content B")
    assert hash_file(f1) != hash_file(f2)
