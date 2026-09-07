"""Concurrent public operation-log writes retain every completed entry."""

from concurrent.futures import ThreadPoolExecutor

from openkb.log import append_log


def test_concurrent_log_writers_do_not_overwrite_entries(kb_dir):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: append_log(kb_dir / "wiki", "test", f"entry-{i:02d}"), range(40)))
    content = (kb_dir / "wiki/log.md").read_text()
    assert all(content.count(f"entry-{i:02d}") == 1 for i in range(40))
