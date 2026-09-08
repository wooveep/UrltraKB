# Frozen Linux GCC runtime source mapping

Date: 2026-09-08. Scope: the two standalone libraries under `numpy.libs/` in
OpenKB commit `87bdcc7b6e3306b2f1981b80af5a705b258e0c35`. This does not assess
Windows static runtime objects or other distribution components.

**The two previously unresolved Linux runtime sources are now identified and
cached. No replacement or rebuild of these libraries is needed to establish
their source mapping.** Both exact upstream binary RPMs and their corresponding
source RPMs pass signature verification against the CentOS 7 official signing
key. Every executable/data section matches the shipped library; remaining
differences are ELF linking metadata and section-index renumbering.

## Exact source packages

| Shipped file | Verified upstream binary package | Corresponding source package |
| --- | --- | --- |
| `libgfortran-040039e1-0352e75f.so.5.0.0` | `libgfortran5-8.3.1-2.1.1.el7.x86_64.rpm` | `gcc-libraries-8.3.1-2.1.1.el7.src.rpm` |
| `libquadmath-96973f99-934c22de.so.0.0.0` | `libquadmath-4.8.5-44.el7.x86_64.rpm` | `gcc-4.8.5-44.el7.src.rpm` |

The binary RPMs explicitly name those source packages in their `SOURCERPM`
headers. Both source packages are available from the same official CentOS 7.9
archive and are retained whole, including the vendor `.spec` build recipe,
source archives and patches. The Fortran source RPM also contains GCC 7 inputs
used for a different subpackage; retaining the complete original RPM does not
assert that OpenKB ships `libgfortran.so.4`.

Primary downloads:

- [libgfortran5 binary RPM](https://vault.centos.org/7.9.2009/os/x86_64/Packages/libgfortran5-8.3.1-2.1.1.el7.x86_64.rpm)
  and [its source RPM](https://vault.centos.org/7.9.2009/os/Source/SPackages/gcc-libraries-8.3.1-2.1.1.el7.src.rpm).
- [libquadmath binary RPM](https://vault.centos.org/7.9.2009/os/x86_64/Packages/libquadmath-4.8.5-44.el7.x86_64.rpm)
  and [its source RPM](https://vault.centos.org/7.9.2009/os/Source/SPackages/gcc-4.8.5-44.el7.src.rpm).

| Archive | SHA-256 |
| --- | --- |
| `libgfortran5-8.3.1-2.1.1.el7.x86_64.rpm` | `f0bebfc9c53dbbd867f3e6a509afb3352a7a9bd2bee0f34f798f7d17b7ff9f66` |
| `gcc-libraries-8.3.1-2.1.1.el7.src.rpm` | `cf2a1d2c2e5a56eb2e495c2638e64769b73db07555e1f7ce34cbd9feff6891f5` |
| `libquadmath-4.8.5-44.el7.x86_64.rpm` | `72c210666a44092430a965f8319492faf4950959d56650067d0629ee71caa2ab` |
| `gcc-4.8.5-44.el7.src.rpm` | `642c4085f6af9565e39583e54a9d7a5d9e6b93dd8c8e9474aec8a892d6c7aa36` |

All four original RPM header-and-payload signatures produce `VALIDSIG` for
`6341AB2753D78A78A7C27BB124C6A8A7F4A80EB5`. That fingerprint matches the
[CentOS project's published CentOS 7 key](https://www.centos.org/keys/#centos-7).
Verification used a temporary GnuPG keyring and did not alter the user's keys.

## Binary correspondence

The unmodified library extracted from the Fortran binary RPM has SHA-256
`040039e193a33d4d4304a514f9e51441c9fe42549c8432a3588850c4517ecfde`.
The Quadmath library has SHA-256
`96973f995bad4e4b80eaa188b7bac60bd0df44b22ee67bd046b3aa4ecb9e34fd`.
These hashes also explain the first filename suffix introduced by wheel repair.

| Library | GNU Build ID, identical in RPM and frozen file | Frozen SHA-256 |
| --- | --- | --- |
| libgfortran | `5bbe74eb6855e0a2c043c0bec2f484bf3e9f14c0` | `c6090048eccc763522c12ef016f81da6b627cb3a044f55cf0479a839c41c0980` |
| libquadmath | `549b4c82347785459571c79239872ad31509dcf4` | `6ed5137f412781ad7863439fb543613f620b43c32b63292a0029246162f5bbc6` |

The `.text`, `.rodata`, relocation tables, data, unwind sections and Build ID
are byte-for-byte equal. Raw `.dynsym` changes are only section-index
renumbering: resolving those indices to section names produces identical
symbols, types, values and sizes for all 1,617 Fortran and 131 Quadmath symbols.
Other changed sections are `.dynstr` and `.dynamic`, plus Fortran's
`.gnu.version_r` after renaming its Quadmath dependency.

This matches the recorded wheel transformation: renamed SONAME/NEEDED names,
relocated dynamic metadata and Fortran's `$ORIGIN` RPATH. The fixed
[openblas-libs repair recipe](https://github.com/MacPython/openblas-libs/blob/2387cb313f653b694d864f2d1d6888a26d7b2ba1/ci-repair-wheel.sh)
runs `auditwheel repair` and sets that Fortran RPATH with `patchelf`.
The existing NumPy/OpenBLAS provenance records cover the subsequent wheel
repair stage. This finding comes from the actual binary RPM bytes, not from
NumPy's compiler version or an assumed manylinux image date.

## Original licenses, notices and build material

Fortran's exact binary RPM installs `COPYING3` and `COPYING.RUNTIME`.
The corresponding source's `libgfortran/runtime/main.c` states GPL version 3 or
later with the GCC Runtime Library Exception version 3.1. Preserve both full
texts and the original source-specific notices. This standalone library is
included in the source materials; there is no need to depend on a broad
exception claim to omit its source.

Quadmath's exact binary RPM installs `COPYING.LIB.libquadmath`, the full LGPL
2.1 text. Its corresponding source retains file-specific grants, including
LGPL version 2 or later in `libquadmath/quadmath.h` and permissive grants in
other original files. Use LGPL-2.1-or-later for the library-level distribution
entry and retain those original per-file terms. Do not label all Quadmath
files with the GCC Runtime Library Exception merely because it is shipped
inside a GCC source package. The primary license evidence is in the exact
[source RPM](https://vault.centos.org/7.9.2009/os/Source/SPackages/gcc-4.8.5-44.el7.src.rpm),
not an inferred aggregate RPM `License` string.

The local cache is `packaging/desktop/build/source-cache/gcc-runtime/`:

- `sources.json` records both mappings, all 37 and 141 source-RPM payload files
  with hashes, the two source recipes and inner source archive hashes.
- `binary-correspondence.json` records every ELF section's size and digest,
  the Build IDs, frozen paths and normalized dynamic-symbol checks.
- `rpm-signature-verification.json` preserves all four successful signature
  results. `RPM-GPG-KEY-CentOS-7` is the published public key.
- `gcc-runtime-license-notices.zip` preserves three original binary-package
  license texts and complete original source files containing copyright
  notices: 755 Fortran and 119 Quadmath files. It supplements the whole source
  RPMs, whose patches, build inputs and all remaining source files are also
  retained; it does not replace them.
- `extract-rpm.py`, `compare-elf.py`, and `verify-rpm-signatures.py` preserve the
  extraction, ELF comparison and signature-checking scripts respectively.
  They operate on data files and do not execute downloaded package scripts.

The license/notice ZIP has SHA-256
`9a010d80eb1b913225996e1c3da1726b3223a00d2a8d67c5318151a3f38e8e8e`.
The release assembler should include the two complete source RPMs and their
build recipes, this notice material, and the documented wheel-repair steps
in the actual component-to-source mapping. Completion of this narrow source
gap does not by itself establish that the entire release is ready to publish.

## Windows x64: the embedded GCC runtime exception

This separate assessment covers only
`_internal/numpy.libs/libscipy_openblas64_-63c857e738469261263c764a36be9436.dll`
in the same frozen `87bdcc7` program. Its SHA-256 is
`63c857e738469261263c764a36be9436ebdeaa272e340a828f42047a97131080`,
identical to the DLL in the official
`scipy_openblas64-0.3.31.188.0-py3-none-win_amd64.whl`. The wheel's SHA-256
`4958b7fb8dcc5b8312652764acc762f42e7f1cc7269fe5a561b0635ffd1a8601`
matches the [official PyPI publication record](https://pypi.org/pypi/scipy-openblas64/0.3.31.188.0/json).

The exact Windows wheel contains its own platform-specific license file.
It identifies OpenBLAS and LAPACK under their original BSD grants and
explicitly identifies the GCC runtime as statically linked into the DLL,
under GPL version 3 or later with GCC Runtime Library Exception 3.1. It
includes the original runtime declaration, complete GPLv3 text and complete
exception. That platform-specific text exactly contains the fixed source's
[LICENSE_win32.txt](https://github.com/MacPython/openblas-libs/blob/2387cb313f653b694d864f2d1d6888a26d7b2ba1/tools/LICENSE_win32.txt).
The material assembly should retain that original file and each component's
original notices; an overall AGPL distribution label does not replace them.

There is a concrete basis for treating this embedded contribution as covered
Target Code under the exception:

1. The exact OpenBLAS source commit is
   `4956446ca26d365f209bf729123349f19dd820b6`, and the packaging commit is
   `2387cb313f653b694d864f2d1d6888a26d7b2ba1`. Their complete sources and
   packaging patch are already cached under `source-cache/native/`.
2. The fixed [Windows workflow](https://github.com/MacPython/openblas-libs/blob/2387cb313f653b694d864f2d1d6888a26d7b2ba1/.github/workflows/windows.yml)
   and [build script](https://github.com/MacPython/openblas-libs/blob/2387cb313f653b694d864f2d1d6888a26d7b2ba1/tools/build_steps_windows.sh)
   compile BSD-licensed OpenBLAS/LAPACK source into native PE target code with
   ordinary GCC/GFortran and GNU build tools. The recipe introduces no
   proprietary GCC plugin or GCC intermediate-representation optimizer.
   The x64 link flags include `-lucrt -static -static-libgcc`.
3. The [published x64 ILP64 build job](https://github.com/MacPython/openblas-libs/actions/runs/23386193057/job/68033527746)
   completed successfully at that exact packaging commit. The original
   platform notice directly describes the resulting static runtime
   combination, and the wheel's DLL matches the shipped bytes.
4. Section 1 of the [original GCC Runtime Library Exception](https://www.gnu.org/licenses/gcc-exception-3.1.html)
   permits conveying covered runtime Target Code combined with Independent
   Modules under terms consistent with those modules' licenses when the
   Compilation Process is Eligible. The [FSF's accompanying FAQ](https://www.gnu.org/licenses/gcc-exception-3.1-faq.html)
   confirms that ordinary GCC compilation qualifies and distinguishes
   conveying a runtime library independently from conveying the covered
   combination. Its conditions do not turn on static versus dynamic linking.

For release-material assembly, the resulting assessment is to apply that
published exception to the GCC runtime contribution inside this exact DLL,
preserve the full original grant, and retain the OpenBLAS/LAPACK sources and
fixed packaging recipe. This is an inference grounded in the upstream
artifact and successful fixed build, not a claim to have recovered an
expired compiler log. An unknown precise vendor revision of the ordinary
compiler is not, by itself, a requirement to distribute the entire compiler
source or a reason to keep this specific source-material gap open. The
separately conveyed Linux libraries remain covered by the complete source
RPMs above.

### Quadmath is excluded from this Windows DLL's component list

The fixed build patch creates `exports/output.map` and enables section
garbage collection. The x64 workflow resolves `quadmath_snprintf` to
`snprintf`. The build script then fails if any `libquadmath.a(` archive member
appears in the linker map, if the map is missing, or if inspection errors.
The matching published job succeeded. Direct PE import inspection of the
exact DLL also finds only `KERNEL32.dll` and Windows UCRT API-set DLLs; it
finds no dynamic libquadmath, libgfortran or libgcc dependency. The exact
Windows platform notice lists no Quadmath component.

Together, these establish a reviewable basis to exclude Quadmath from this
Windows DLL's materials; NumPy's aggregate Linux/Windows notice does not
establish that the Windows DLL contains it. The original linker map and logs
have expired, so the static-exclusion evidence is the successful upstream
job and its mandatory fixed guard, not a new local linker-map verification.
This conclusion is confined to this DLL.

The independent assembly record is
`source-cache/gcc-runtime/windows-exception.json`; it does not modify the
Linux `sources.json`. Its `windows/` evidence directory preserves eleven
files: the original wheel license, original fixed source notice, full
exception, fixed workflow/script/patch and OpenBLAS commit declaration,
successful job metadata, PyPI hash record, direct PE inspection and the
official FAQ text. The original wheel license SHA-256 is
`7317b3a9eb806cc83a0cc6b1c7c2fbe6018eed556daa9996e6e3a727a2a5bfbe`.
This is a bounded assessment of the stated contribution, not a legal
guarantee for the whole distribution.
