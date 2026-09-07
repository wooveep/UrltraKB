"""Validate journal targets before allowing a writer to publish new files."""

import pytest

from openkb.locks import kb_ingest_lock
from openkb.mutation import mutation_scope


@pytest.mark.parametrize("existing", [False, True])
def test_mutation_rejects_external_targets_before_business(kb_dir, tmp_path, existing):
    outside = tmp_path.parent / f"{tmp_path.name}-external.md"
    if existing:
        outside.write_text("external content")
    try:
        with kb_ingest_lock(kb_dir / ".openkb"), pytest.raises(ValueError):
            with mutation_scope(kb_dir, [outside], operation="probe"):
                pytest.fail("An external target reached business execution")
        assert not list((kb_dir / ".openkb/journal").glob("*.json"))
        assert not outside.exists() or outside.read_text() == "external content"
    finally:
        outside.unlink(missing_ok=True)
