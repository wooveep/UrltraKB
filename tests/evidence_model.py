"""Call genuine source readers for provenance delivery regressions."""


async def read_fixture(agent, path="sources/fixture.md"):
    from test_answer_evidence_regression import invoke

    return await invoke(agent, "read_file", path=path)
