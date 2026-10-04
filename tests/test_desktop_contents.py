"""Slim builds keep dynamic rendering inputs and share only identical entry points."""

import os

import pytest

from scripts.desktop_contents import ENTRYPOINTS, deduplicate_entrypoints, runtime_asset


def test_runtime_assets_keep_dynamic_bundles_fonts_and_licenses():
    for name in (
        "node",
        "NODE-LICENSE",
        "fonts/NotoSansCJKsc-Regular.otf",
        "node_modules/@mathjax/src/LICENSE",
        "node_modules/@mathjax/src/package.json",
        "node_modules/@mathjax/src/bundle/input/tex/extensions/mhchem.js",
        "node_modules/@mathjax/mathjax-newcm-font/svg/dynamic/greek.js",
        "node_modules/@mathjax/mathjax-newcm-font/mjs/svg.js",
    ):
        assert runtime_asset(name), name
    for folder in ("ts", "cjs", "mjs", "components", "tsconfig"):
        assert not runtime_asset(f"node_modules/@mathjax/src/{folder}/unused.js")


@pytest.mark.skipif(os.name != "posix", reason="Linux entrypoint sharing")
def test_entrypoint_sharing_is_idempotent_and_keeps_names_and_modes(tmp_path):
    for name in ENTRYPOINTS:
        path = tmp_path / name
        path.write_bytes(b"same executable")
        path.chmod(0o755)
    assert deduplicate_entrypoints(tmp_path) == 3 * len(b"same executable")
    assert len({(tmp_path / name).stat().st_ino for name in ENTRYPOINTS}) == 1
    assert all((tmp_path / name).stat().st_mode & 0o111 for name in ENTRYPOINTS)
    assert deduplicate_entrypoints(tmp_path) == 0


@pytest.mark.skipif(os.name != "posix", reason="Linux entrypoint sharing")
def test_distinct_entrypoint_payloads_or_modes_are_not_replaced(tmp_path):
    for index, name in enumerate(ENTRYPOINTS):
        path = tmp_path / name
        path.write_bytes(b"different" if index == 1 else b"same")
        path.chmod(0o700 if index == 2 else 0o755)
    assert deduplicate_entrypoints(tmp_path) == len(b"same")
    assert (tmp_path / ENTRYPOINTS[1]).read_bytes() == b"different"
    assert (tmp_path / ENTRYPOINTS[2]).stat().st_mode & 0o777 == 0o700
