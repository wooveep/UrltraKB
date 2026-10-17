"""A failed fragment never regenerates healthy prefixes or the remaining scope."""

import copy

import pytest
from pageindex.errors import IndexQualityError
from pageindex.index import page_index as pi
from pageindex.index.body_fragments import BodyFragments
from pageindex.index.physical_sections import PhysicalNavigationError

from tests.test_body_recovery_progress import proposal


class Log:
    def info(self, value):
        pass


def fragment_fixture():
    pages = [
        ("4.1 父章节\n4.1.1 已通过\n原文。\n", 0),
        ("4.1.2 待修复\n原文。\n4.2 下一章\n4.2.1 其余正文\n", 0),
        ("4.3 后续章节\n4.3.1 后续正文\n", 0),
    ]
    first = [proposal("4.1", "父章节", 1), proposal("4.1.1", "已通过", 1)]
    second = [proposal("4.1.2", "待修复", 2), proposal("4.2", "下一章", 2)]
    return pages, first, second


def handler(pages, saved, verify=lambda items, known: None):
    def retained(key, value=None):
        if value is not None:
            saved[key] = copy.deepcopy(value)
        return copy.deepcopy(saved.get(key))

    return BodyFragments(pages, 1, (1, 0), (4, 0), 4, "fixture", Log(), retained, verify)


def test_fragment_failure_prevents_generation_of_later_groups(monkeypatch):
    pages, _, _ = fragment_fixture()
    calls = []
    monkeypatch.setattr(pi, "page_list_to_group_text", lambda contents, lengths: contents)
    monkeypatch.setattr(pi, "count_tokens", lambda *args: 0)

    def initial(*args, **kwargs):
        calls.append("initial")
        return [proposal("9.9", "不存在的标题", 1)]

    monkeypatch.setattr(pi, "generate_toc_init", initial)
    monkeypatch.setattr(pi, "generate_toc_continue", lambda *args, **kwargs: calls.append("later"))
    with pytest.raises(PhysicalNavigationError, match="native parent"):
        pi.process_no_toc(pages, model="fixture", logger=Log(), fragment_handler=handler(pages, {}))
    assert calls == ["initial"]


def test_repair_uses_native_parent_end_before_next_sibling_is_generated(monkeypatch):
    pages, first, second = fragment_fixture()
    saved, repairs = {}, []
    h = handler(pages, saved)
    h.accept(first, "first")

    def rebuild(subset, **kwargs):
        repairs.append((subset, kwargs))
        return [*first, second[0]]

    monkeypatch.setattr(pi, "process_no_toc", rebuild)
    bad = [{**second[0], "title": "错误标题"}]
    h.accept(bad, "second")
    assert len(repairs) == 1
    subset, args = repairs[0]
    assert len(subset) == 2
    assert args["boundary"].end == (2, pages[1][0].index("4.2 下一章"))
    assert args["repair_context"]["failed_section"] == "4.1.2"
    assert [n["structure"] for n in h.result()] == ["4.1", "4.1.1", "4.1.2"]
    # The raw response remains available for continuation prompts, while the
    # verified fragment reuses its repaired canonical navigation on resume.
    resumed = handler(pages, saved)
    resumed.accept(resumed.load_fragment("first"), "first")
    assert resumed.load_fragment("second") == bad
    resumed.accept(bad, "second")
    assert resumed.result() == h.result()
    assert len(repairs) == 1


def test_completed_fragment_is_reused_after_later_transport_failure(monkeypatch):
    pages, first, second = fragment_fixture()
    saved, generated = {}, []
    broken = True
    monkeypatch.setattr(pi, "page_list_to_group_text", lambda contents, lengths: contents[:2])
    monkeypatch.setattr(pi, "count_tokens", lambda *args: 0)

    def initial(*args, **kwargs):
        generated.append("first")
        return first

    def continuation(*args, **kwargs):
        generated.append("second")
        if broken:
            raise RuntimeError("retained delivery unavailable")
        return second

    monkeypatch.setattr(pi, "generate_toc_init", initial)
    monkeypatch.setattr(pi, "generate_toc_continue", continuation)
    with pytest.raises(RuntimeError):
        pi.process_no_toc(
            pages, model="fixture", logger=Log(), fragment_handler=handler(pages, saved)
        )
    broken = False
    result = pi.process_no_toc(
        pages, model="fixture", logger=Log(), fragment_handler=handler(pages, saved)
    )
    assert generated == ["first", "second", "second"]
    assert len(result) == 4


def test_tampered_fragment_does_not_reuse_its_role_verdicts():
    pages, first, _ = fragment_fixture()
    saved, known_counts = {}, []
    h = handler(pages, saved)
    h.accept(first, "first")
    saved[h.key("first")]["entries"][0]["title"] = "伪造标题"
    resumed = handler(pages, saved, lambda items, known: known_counts.append(len(known)))
    resumed.accept(resumed.load_fragment("first"), "first")
    assert known_counts == [0]
    assert resumed.result() == h.result()


def test_rejected_repair_is_retained_as_proposal_never_verified(monkeypatch):
    pages, first, second = fragment_fixture()
    saved, sends = {}, []

    def rejected(items, known):
        for item in items:
            if item["structure"] == "4.1.2":
                error = IndexQualityError("rejected role")
                error.failed_entry = item
                raise error

    def rebuild(*args, **kwargs):
        sends.append(1)
        return [*first, second[0]]

    monkeypatch.setattr(pi, "process_no_toc", rebuild)
    for _ in range(2):
        h = handler(pages, saved, rejected)
        h.accept(h.load_fragment("first") or first, "first")
        with pytest.raises(PhysicalNavigationError, match="no progress"):
            h.accept(second, "second")
    assert len(sends) == 1
    assert not any(
        v.get("state") == "verified" and any(n["structure"] == "4.1.2" for n in v["entries"])
        for v in saved.values()
    )


@pytest.mark.parametrize("delivered", [None, {}, ["wrong shape"], [{"title": 1}]])
def test_malformed_fragment_is_terminal_without_more_generation(delivered):
    pages, _, _ = fragment_fixture()
    with pytest.raises(PhysicalNavigationError, match="entry list"):
        handler(pages, {}).accept(delivered, "first")
