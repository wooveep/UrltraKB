"""Confirmed aliases share identity without widening queries or rewriting history."""

import json

import pytest

pytest_plugins = ("test_workbook_import",)


def test_aliases_bind_three_families_to_one_view(kb_dir, three_sheets, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.products import confirm_product_aliases, list_products
    from openkb.application.views import list_views
    from openkb.view_records import SourceMetadata

    first = import_document(
        kb_dir,
        three_sheets,
        metadata=SourceMetadata(
            product="CNware WinStack", applicable_versions=("9.4.0",), family="operations"
        ),
    )
    product = list_products(kb_dir)[0]
    confirm_product_aliases(
        kb_dir, product.product_id, ("WinStack", "CNware-WinStack", "CNware WinStack 虚拟化云平台")
    )
    for name, family in (
        ("CNware-WinStack", "installation"),
        ("CNware WinStack 虚拟化云平台", "best practices"),
    ):
        path = three_sheets.with_name(f"{family}.xlsx")
        path.write_bytes(three_sheets.read_bytes())
        result = import_document(
            kb_dir,
            path,
            metadata=SourceMetadata(product=name, applicable_versions=("V9.4.0",), family=family),
        )
        assert result.status == "added"
        assert {u.view_id for u in result.units} == {first.units[0].view_id}
    assert len(list_products(kb_dir)) == 1
    assert len([v for v in list_views(kb_dir) if not v.unknown_source_id and v.product_id]) == 1
    assert len(list((kb_dir / ".openkb/catalog/families").glob("*.json"))) == 3


def test_short_product_hint_cannot_fall_back_to_all_products(kb_dir, monkeypatch):
    from test_query_views import _import_rule

    from openkb.application.products import confirm_product_aliases, list_products
    from openkb.application.query_views import resolve_query_views

    for product in ("CNware WinStack", "CNware WinSphere"):
        _import_rule(kb_dir, monkeypatch, product, "9.4.0", "Source fact.", product=product)
    for short in ("WinStack", "WinSphere"):
        unresolved = resolve_query_views(kb_dir, f"{short} V9.4.0 如何安装？")
        assert not unresolved.views and unresolved.missing
    for product in list_products(kb_dir):
        confirm_product_aliases(kb_dir, product.product_id, (product.name.split()[-1],))
    for short in ("WinStack", "WinSphere"):
        selected = resolve_query_views(kb_dir, f"{short} V9.4.0 如何安装？")
        assert len(selected.views) == 1 and not selected.missing
        assert selected.views[0].product == f"CNware {short}"
    assert len(resolve_query_views(kb_dir, "V9.4.0 有哪些部署要求？").views) == 2
    assert len(resolve_query_views(kb_dir, "比较 WinStack 与 WinSphere V9.4.0").views) == 2


def test_alias_confirmation_is_audited_idempotent_and_collision_safe(kb_dir, monkeypatch):
    from test_query_views import _import_rule

    from openkb.application.products import confirm_product_aliases, list_products

    for name in ("A", "B"):
        _import_rule(kb_dir, monkeypatch, name, "1.0", "Fact.", product=name)
    a, b = sorted(list_products(kb_dir), key=lambda item: item.name)
    confirm_product_aliases(kb_dir, a.product_id, ("Alias",))
    before = list((kb_dir / ".openkb/catalog/product-confirmations").glob("*.json"))
    confirm_product_aliases(kb_dir, a.product_id, (" alias ",))
    assert list((kb_dir / ".openkb/catalog/product-confirmations").glob("*.json")) == before
    saved = json.loads(before[0].read_text())
    assert saved["product_id"] == a.product_id and saved["aliases"] == ["Alias"]
    with pytest.raises(ValueError, match="alias"):
        confirm_product_aliases(kb_dir, b.product_id, ("ALIAS",))


def test_unconfirmed_short_name_inside_chinese_product_cannot_expand():
    from openkb.product_identity import resolve_product_question
    from openkb.view_records import KnowledgeView

    views = tuple(
        KnowledgeView(
            view_id=str(i) * 32,
            product_id=str(i) * 32,
            product=name,
            applicable_versions=("9.4.0",),
        )
        for i, name in enumerate(("云宏WinStack虚拟化云平台", "CNwareWinSphere"), 1)
    )
    for name in ("WinStack", "WinSphere"):
        result = resolve_product_question(views, name + " V9.4.0")
        assert result.status == "unresolved" and not result.product_ids
