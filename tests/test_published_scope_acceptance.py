"""Published-page acceptance is separate from generating a correctly framed draft."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from native_usage_fixtures import read_requests

from ultrakb import frontmatter
from ultrakb.agent.create_assembly import assemble_contributions
from ultrakb.application.page_scope_validation import (
    PublishedScopeExpectation,
    verify_published_scopes,
)
from ultrakb.application.pages import read_page, save_page
from ultrakb.content_blocks import markdown_layout
from ultrakb.desktop.verification_data import publish_pages
from ultrakb.locks import atomic_write_text

FIXTURE = Path(__file__).parent / "fixtures" / "published-scope-pages.json"


def test_published_scopes_are_detected_and_manual_fix_keeps_evidence(kb_dir):
    cases = json.loads(FIXTURE.read_text("utf-8"))
    expectations = [PublishedScopeExpectation.model_validate(c["expected"]) for c in cases]
    changes = {}
    for case in cases:
        changes[case["expected"]["path"]] = case["published_before"].encode()
    for expected in expectations:
        for link in expected.link_targets:
            if link.startswith("wiki:[["):
                target = link.removeprefix("wiki:[[").removesuffix("]]").split("|", 1)[0]
                page = kb_dir / "wiki" / (target.split("#", 1)[0] + ".md")
                if not page.exists():
                    changes[page.relative_to(kb_dir / "wiki").as_posix()] = (
                        b"Existing linked knowledge.\n"
                    )
    publish_pages(kb_dir, changes)
    report = verify_published_scopes(kb_dir / "wiki", expectations)
    assert not report["ok"]
    assert len(report["pages"]) == 2
    assert all("missing_whole_scope" in p["issues"] for p in report["pages"])
    for case in cases:
        path = case["expected"]["path"]
        reference = Path(path).with_suffix("").as_posix()
        previous = read_page(kb_dir, reference)
        content = assemble_contributions(case["contributions"], case["windows"])
        before_body = frontmatter.split(case["published_before"])[1]
        content += before_body[case["retained_tail_start"] :]
        saved = save_page(kb_dir, reference, content, version=previous.version)
        assert saved.status == "saved"
        published = read_page(kb_dir, reference)
        assert published.body == saved.page.body
        assert all(value["content"].strip() in published.body for value in case["contributions"])
        assert before_body[case["retained_tail_start"] :] in published.body
    migrated = verify_published_scopes(kb_dir / "wiki", expectations)
    assert migrated["ok"], migrated
    assert all(page["ok"] for page in migrated["pages"])
    assert read_requests(kb_dir) == []


@pytest.mark.parametrize(
    "damage,issue",
    [
        ("whole", "wrong_whole_scope"),
        ("local", "wrong_section_scope:1"),
        ("missing_section", "wrong_section_count"),
        ("duplicate_number", "wrong_section_number:2"),
        ("quoted", "literal_blocks_changed"),
        ("quoted_crlf", "literal_blocks_changed"),
        ("image", "references_changed"),
        ("framing_in_code", "missing_whole_scope"),
    ],
)
def test_published_acceptance_rejects_wrong_scopes_and_changed_evidence(kb_dir, damage, issue):
    case = json.loads(FIXTURE.read_text("utf-8"))[0]
    expected = PublishedScopeExpectation.model_validate(case["expected"])
    before_body = frontmatter.split(case["published_before"])[1]
    content = assemble_contributions(case["contributions"], case["windows"])
    content += before_body[case["retained_tail_start"] :]
    if damage == "whole":
        changed = content.replace("物理页 105–127", "物理页 105–118", 1)
    elif damage == "local":
        changed = content.replace("物理页 105–118", "物理页 105–127", 1)
    elif damage == "missing_section":
        changed = content.replace("**来源分节 2 / 2**", "## Unscoped continuation")
    elif damage == "duplicate_number":
        changed = content.replace("**来源分节 2 / 2**", "**来源分节 1 / 2**")
    elif damage in {"quoted", "quoted_crlf"}:
        tokens, offsets = markdown_layout(content)
        code = next(token for token in tokens if token.type == "fence")
        if damage == "quoted":
            start = offsets[code.map[0] + 1]
            changed = content[:start] + "changed_value=999\n" + content[start:]
        else:
            start, stop = (offsets[line] for line in code.map)
            changed = content[:start] + content[start:stop].replace("\n", "\r\n") + content[stop:]
    elif damage == "image":
        target = next(
            link.removeprefix("image:")
            for link in expected.link_targets
            if link.startswith("image:")
        )
        changed = content.replace(target, "sources/images/wrong-image.png")
    else:
        fence = "`" * (1 + max(len(run) for run in re.findall(r"`+", content)))
        changed = f"{fence}markdown\n{content}\n{fence}\n"
    assert changed != content
    page = kb_dir / "wiki" / expected.path
    atomic_write_text(page, changed)
    before = page.read_bytes()
    report = verify_published_scopes(kb_dir / "wiki", [expected])
    assert not report["ok"]
    assert issue in report["pages"][0]["issues"]
    assert page.read_bytes() == before


def test_scope_acceptance_cli_reports_failure_and_success_without_publishing(kb_dir, tmp_path):
    case = json.loads(FIXTURE.read_text("utf-8"))[1]
    manifest = tmp_path / "scope-expectations.json"
    manifest.write_text(json.dumps([case["expected"]]), encoding="utf-8")
    wiki = kb_dir / "wiki"
    page = wiki / case["expected"]["path"]
    atomic_write_text(page, case["published_before"])
    output = tmp_path.parent / (tmp_path.name + "-acceptance.json")
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts/verify_published_scopes.py"),
        "--wiki",
        str(wiki),
        "--manifest",
        str(manifest),
        "--output",
        str(output),
    ]
    environment = {**os.environ, "PYTHON_DOTENV_DISABLED": "1"}
    previous = page.read_bytes()
    rejected = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert not json.loads(output.read_text())["ok"]
    assert page.read_bytes() == previous
    body = frontmatter.split(case["published_before"])[1]
    candidate = assemble_contributions(case["contributions"], case["windows"])
    atomic_write_text(page, candidate + body[case["retained_tail_start"] :])
    previous = page.read_bytes()
    accepted = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert json.loads(output.read_text())["ok"]
    assert page.read_bytes() == previous
    forbidden = subprocess.run(
        [*command[:-1], str(kb_dir / ".ultrakb/overwrite.json")],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert forbidden.returncode == 2
    assert not (kb_dir / ".ultrakb/overwrite.json").exists()


def test_scope_acceptance_refuses_missing_and_escaping_pages(kb_dir):
    case = json.loads(FIXTURE.read_text("utf-8"))[0]
    expected = PublishedScopeExpectation.model_validate(case["expected"])
    report = verify_published_scopes(kb_dir / "wiki", [expected])
    assert report["pages"][0]["issues"] == ["missing_page"]
    with pytest.raises(ValueError, match="relative wiki"):
        PublishedScopeExpectation.model_validate({**case["expected"], "path": "../outside.md"})
    with pytest.raises(ValueError, match="distinct"):
        verify_published_scopes(kb_dir / "wiki", [expected, expected])
