# Slice receipts — phcpp v0 vertical slice

Run 2026-11-07 on aletheia-core (Linux x86_64), from clean clones.
Tctl 38.0 °C at rest, 58.8 °C peak during the phcc build — under the
75 °C pause line throughout.

**The lowering in this slice was done by hand** (see the header of
`lowered/slice.c`). These receipts prove the *handoff loop* and the
*commentary discipline*, not a mechanical lowerer — none exists yet.

## Artefacts

| File | SHA-256 |
|---|---|
| `slice.cpp` (C++23 source) | `913c1a1eed29f090e68308edf788779d130193338dec791166eb24d8c2d44cc8` |
| `lowered/slice.c` (hand-lowered C) | `dd16c96876b7aac7cc27f9d2a01462fb70086b5daa82e974c8a328980bff6b9d` |
| `slice.phext` (22,279 bytes, 63 scrolls) | `55ff6d57956276dafd833641aa44e2cd4ea89f233298b3284b18fa2ae9c546f7` |

## Baseline: phcc @ d0659ba4

```
$ git clone git@github.com:wbic16/phcc.git && git checkout d0659ba4
$ git rev-parse HEAD
d0659ba4ef6caa94c120ffd7366c75b3c44e4d67
$ ./configure && make -j8        # rc=0
$ ./tcc -version
tcc version 1.0.0 2026-11-07 HEAD@d0659ba4 (x86_64 Linux)
```

(An uninstalled build-tree phcc is invoked with `-B<phcc-tree>` —
its own mechanism for naming the library path; used in every phcc
command below.)

## Oracle: g++ -std=c++23

```
$ g++ -std=c++23 -O0 -o slice_gpp slice.cpp      # rc=0, g++ 13.3.0
$ ./slice_gpp                                    # rc=0
circle area 12.57
square area 9.00
add int 42
add double 3.5
tracer inner constructed
inside scope
tracer inner destroyed
after scope
$ sha256sum stdout
9bf40ef5de2ee2f50ae56ea3cd6ca10aed18818f8ce19a9228eeaf2ebe49e48c
```

The output shows all three chartered features doing observable
work: virtual dispatch through `Shape*` (two different areas),
`add<T>` instantiated for `int` and `double`, and `Tracer`'s
destructor firing at scope exit, between the two scope markers.

## The loop: pack → verify → unpack → phcc → run

Pack (run wherever; shown once — the committed `slice.phext` is its
output, hashes above):

```
$ python3 tools/phcpp.py pack \
    --cpp examples/slice/slice.cpp --c examples/slice/lowered/slice.c \
    --cpp-path examples/slice/slice.cpp --c-path examples/slice/lowered/slice.c \
    --out examples/slice/slice.phext
pack: wrote examples/slice/slice.phext (22279 bytes, 63 scrolls)
```

Verify, unpack, build, compare — on the ranch, against the phext as
committed:

```
$ python3 tools/phcpp.py verify --phext examples/slice/slice.phext
verify: PASS — manifest hashes ok; 43 C segments (#phcc-member) tile the lowered C; 15 commentary segments (#phcpp-commentary) tile the C++ and are isolated from every build input
$ python3 tools/phcpp.py unpack --phext examples/slice/slice.phext --out /tmp/phcpp-build
unpack: wrote examples/slice/lowered/slice.c (3348 bytes, sha256 ok)
unpack: SKIP kind=cpp-commentary path=examples/slice/slice.cpp (commentary is provenance, never a build input)
$ tcc -B<phcc-tree> -o slice_phcc /tmp/phcpp-build/examples/slice/lowered/slice.c   # rc=0
$ ./slice_phcc                                                                     # rc=0
$ cmp <(g++ oracle stdout) <(phcc stdout)
CMP: phcc stdout BYTE-IDENTICAL to g++ oracle
```

The unpacked tree contains exactly one source file — the lowered C.
The C++ exists in the phext only as 15 commentary segments in Source
Volume 4 (types Shape/Circle/Square/Tracer, function template `add`,
`main`, and gap segments), each hash-bound; the C as 43 segments in
Volume 3 plus its whole-file scroll in Volume 2.

## The negative: corrupt the commentary, watch two things not happen

In a *copy* of the phext, one commentary payload was edited — the
`main` segment at `1.1.1/4.4.1/1.1.15`, changing the C++ text
`Circle c(2.0)` to `Circle c(9.0)`. Had those bytes reached a
compiler, the first output line would read `circle area 254.47`.

```
$ python3 tools/phcpp.py verify --phext slice-corrupt.phext
verify: FAIL — cpp-commentary: segment 1.1.1/4.4.1/1.1.15 payload SHA-256 mismatch (header records c00e455df60c7cd8fe74c4290d7d1b3ef37d0f618f42f03bd4ba17fab4e6d30d, bytes hash to 0cde6b411736155cd4a9d5dafe5315ece509f73416fea9efbebd8b245517b0a0)
# rc=1 — provenance flags the edit, naming the segment and both hashes

$ python3 tools/phcpp.py unpack --phext slice-corrupt.phext --out /tmp/phcpp-build2
unpack: wrote examples/slice/lowered/slice.c (3348 bytes, sha256 ok)
unpack: SKIP kind=cpp-commentary path=examples/slice/slice.cpp (commentary is provenance, never a build input)
$ tcc -B<phcc-tree> -o slice_phcc2 .../slice.c && ./slice_phcc2
CMP: corrupted-phext build stdout BYTE-IDENTICAL to g++ oracle
CMP: phcc binaries byte-identical across corruption
```

Both halves of the claim, on one page: the commentary is *checked*
(verify catches a one-token edit and names it) and *inert* (the
build cannot see it — output and binary are bit-identical with the
corruption in place).

## Notes and limits

- g++ was run at `-O0`; the comparison claimed is the conformance
  triple (compile class, run exit, stdout bytes) against phcc's
  unoptimised codegen, which is phcc's only regime today.
- The phext's phext-format compatibility was exercised by
  construction: `tools/phcpp.py`'s byte layer is behaviour-identical
  to phcc's `phext_io` (same delimiters, same walk), and every
  segment rule here is enforced by `verify`, not by trust.
- A first run of this protocol caught two defects in my own
  harness, neither in phcpp's design: phcc invoked without `-B`
  (build-tree library path) and a `-B~/…` argument whose tilde bash
  does not expand mid-word. Both are recorded because receipts
  should say what actually ran; the commands above are the ones
  that produced these outputs.
