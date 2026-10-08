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
    # A word merely embedded inside a Latin product token is not a name hint.
    assert resolve_product_question(views, "What are the deployment rules?").status == "unscoped"


def test_typographic_product_names_bind_consistently_but_semantic_suffixes_do_not():
    from openkb.product_identity import confirmed_product
    from openkb.view_records import Product

    product = Product(product_id="a" * 32, name="CNware WinStack")
    assert confirmed_product((product,), "ＣＮｗａｒｅ—WinStack") == product
    assert confirmed_product((product,), "CNware WinStack 虚拟化云平台") is None
    duplicate = Product(product_id="b" * 32, name="CNware-WinStack")
    with pytest.raises(ValueError, match="identity"):
        confirmed_product((product, duplicate), "cnware winstack")


@pytest.mark.parametrize("name", ["CNware WinStack", "Acme Ledger", "北辰资料管理"])
def test_exact_confirmed_name_does_not_expand_to_longer_product_name(name):
    from openkb.product_identity import resolve_product_question
    from openkb.view_records import KnowledgeView

    views = tuple(
        KnowledgeView(
            view_id=str(i) * 32,
            product_id=str(i) * 32,
            product=label,
            applicable_versions=("9.4.0",),
        )
        for i, label in enumerate((name, name + " Extended"), 1)
    )
    result = resolve_product_question(views, name + " V9.4.0 如何配置？")
    assert result.status == "resolved" and result.product_ids == {"1" * 32}


def test_alias_cannot_capture_another_canonical_product_name(kb_dir, monkeypatch):
    from test_query_views import _import_rule

    from openkb.application.products import confirm_product_aliases, list_products

    for name in ("Product A", "Product B"):
        _import_rule(kb_dir, monkeypatch, name, "1.0", "Fact.", product=name)
    a = next(p for p in list_products(kb_dir) if p.name == "Product A")
    with pytest.raises(ValueError, match="alias"):
        confirm_product_aliases(kb_dir, a.product_id, ("Product-B",))


def test_existing_sources_move_by_explicit_identity_review_and_keep_history(kb_dir, monkeypatch):
    from test_query_views import _import_rule

    from openkb.application.products import list_products, retire_product_identity
    from openkb.application.query_views import resolve_query_views
    from openkb.application.version_review import (
        resume_version_review,
        review_source_version,
        supplement_version_reviews,
    )
    from openkb.view_records import SourceMetadata

    first = _import_rule(
        kb_dir,
        monkeypatch,
        "install",
        "9.4.0",
        "Install rule.",
        product="CNware WinStack",
        family="install",
    )
    other = _import_rule(
        kb_dir,
        monkeypatch,
        "ops",
        "9.4.0",
        "Ops rule.",
        product="CNware WinStack Platform",
        family="ops",
    )
    products = {p.name: p for p in list_products(kb_dir)}
    target, old = products["CNware WinStack"], products["CNware WinStack Platform"]
    with pytest.raises(ValueError, match="Live sources"):
        retire_product_identity(kb_dir, old.product_id, target.product_id)
    prior = resolve_query_views(kb_dir, "CNware WinStack Platform").views[0]
    review = review_source_version(kb_dir, other.source_id)
    ready = supplement_version_reviews(
        kb_dir,
        {review.review_id: SourceMetadata(product=target.name, product_id=target.product_id)},
    )[0]
    assert ready.metadata.applicable_versions == ("9.4.0",)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "litellm.completion",
        lambda **kwargs: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "description": "Ops",
                                "content": "Ops rule.",
                                "create": [],
                                "update": [],
                                "related": [],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        ),
    )
    result = resume_version_review(kb_dir, ready.review_id)
    assert result.status == "added", result.message
    retire_product_identity(kb_dir, old.product_id, target.product_id)
    selected = resolve_query_views(kb_dir, "CNware WinStack V9.4.0")
    assert len(selected.views) == 1
    assert set(selected.views[0].source_revision_ids) == {
        first.source_revision_id,
        other.source_revision_id,
    }
    assert prior.scope.wiki_dir.is_dir() and prior.product_id == old.product_id
    # A question with no product hint must not reintroduce retired identities.
    all_current = resolve_query_views(kb_dir, "What are the installation and operations rules?")
    assert {view.product_id for view in all_current.views} == {target.product_id}
    historical = resolve_query_views(kb_dir, scope=prior.scope)
    assert historical.views[0].product_id == old.product_id
