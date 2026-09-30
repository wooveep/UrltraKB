# Standard compound-storage recovery

Old DOC objects are located from the frozen CFB original, including ObjectPool
remnants and standard document storages without a live body reference. Complete
Package/Ole10Native payloads are read directly. Standard DOC/XLS child storages
use `openkb-cfb` to rebuild a standalone compound file, then enter the existing
ordinary Source pipeline. No embedded application is activated. The helper has
no responsibility for identities, scheduling, Source metadata, budgets or commits.

The helper uses exactly `cfb 0.15.0`; it does not implement FAT or miniFAT. Input
reading tolerates the ENDOFCHAIN value commonly written in unused storage sector
fields by Office. Output is opened strictly with cfb, then independently checked
using olefile 0.47. The selected storage becomes the new root; descendants, empty
storages, stream bytes, CLSID, state bits and legal times are preserved. Root
creation time is zero as required by CFB. File hashes need not match the original
historical document because physical allocation changes.

The shared dispatcher publishes bytes and their ordinary import intent only after
successful reconstruction. A failure retains the original and object locator with
`requires_container_rebuild`. Body status and recovered-file status remain separate.
Byte and time limits, interrupted-work recovery and group cancellation use the
same pending-job implementation as OOXML.

Build on the matching Linux or Windows x86_64 host, before the existing desktop
freeze command:

```bash
python scripts/prepare_cfb_helper.py
```

This prepares `openkb/cfb_helper/assets/runtime` with the native executable, typed
manifest and `source.zip`. The existing desktop data collection includes that
directory. Users do not need Rust; a missing or mismatched helper produces an
explicit recovery diagnostic. On Windows, use the Rust MSVC toolchain and its
matching Visual Studio build tools. A compile-only cross-target check is available:

```bash
python scripts/prepare_cfb_helper.py --check-target x86_64-pc-windows-msvc
```

Rust 1.95.0 is pinned in `rust-toolchain.toml`; official channel and Linux/Windows
archive checksums are recorded in `toolchain.json`. `Cargo.lock` fixes all 22
packages in the complete platform dependency graph. `dependencies.json` records
each crate's source URL, SHA256, SPDX license and exact bundled license hashes.
Only cfb, fnv, uuid and web-time are compiled for the supported native targets;
the wasm-specific closure remains locked and licensed. Native packages have no
build scripts. The unused wasm build scripts were inspected: they perform local
compiler/version/configuration probes and source hashing, not network downloads.

Preparation rejects a changed toolchain, dependency closure or license material.
`source.zip` contains the helper's Apache-2.0 source, complete vendored dependencies,
licenses and a Cargo offline source configuration. Extract it, change into its
root, and run `cargo build --release --locked --offline` with the pinned toolchain.
The runtime validates the executable and rebuild-material digests before use.

The native watchdog retains the original parent identity (a process handle on
Windows, PID plus start identity on Linux) and a deadline. Python polls cancellation
and reaps its one child; the helper exits itself when its parent disappears or the
deadline expires, including while blocked in a native input read.

Automated verification uses real Office-authored DOC objects, independent CFB
stream/metadata comparisons, xlrd workbook content and private Office DOC content.
Linux native execution, parent-death/deadline checks, an offline source rebuild
and Windows target compilation are development checks. Windows 11 and Debian
13.6 final distribution validation remain separate human acceptance tasks.
