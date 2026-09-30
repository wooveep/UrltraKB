# olefile 0.47 dependency review

The compound-object reader directly requires `olefile==0.47` to distinguish a
complete CFB document, its counted Ole10Native/Package file, and a child storage
that still requires reconstruction. It never activates an embedded application.
No oletools runtime dependency is installed.

The [PyPI 0.47 release metadata](https://pypi.org/pypi/olefile/0.47/json) and wheel
were inspected. There are no runtime dependencies; the optional `tests` extra
contains pytest and pytest-cov and is not selected. This is a pure Python wheel,
with no native build or install hooks. The wheel SHA256 is
`543c7da2a7adadf21214938bb79c83ea12b473a4b6ee4ad4bf854e7715e13d1f`;
the source archive SHA256 is
`599383381a0bf3dfbd932ca0ca6515acd174ed48870cbf7fee123d698c192c1c`.
Both distribution artifacts are locked by `uv.lock`. The entire BSD/PIL license
from the wheel metadata is retained at `openkb/pending/olefile-LICENSE.txt`.

Compound streams are opened with `DEFECT_INCORRECT`, read under shared byte/time
budgets, checked against their declared lengths, and decoded only at supported
file boundaries. Unrecognized bytes remain private-object diagnostics. Damaged
objects do not suppress later valid package parts.

Small real Package fixtures are from
[oletools commit ec10260989dbc48b9109e3d05d22beee19cf2333](https://github.com/decalage2/oletools/tree/ec10260989dbc48b9109e3d05d22beee19cf2333/tests/test-data/oleobj).
Their source URLs and SHA256 values are in `tests/fixtures/office/package-fixtures.json`;
the original license is kept alongside them. Only the benign `embedded-simple-2007`
text fixtures are included. Their recovered text is asserted against independent
literal contents, not the extractor's own serialization.
