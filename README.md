# phcpp — C++23, lowered to phcc scrolls

**phcpp provides phext support for C++23, using phcc as the baseline.**

phcpp is a *lowering front end*, not a compiler. A C++23 translation
unit is lowered to C; the C is packed into a phext as ordinary phcc
scrolls and handed off to [phcc](https://github.com/wbic16/phcc),
which does all code generation. The original C++ rides in the same
phext **marked as commentary** — present, addressable, hash-bound
provenance that is never a build input.

The handoff, in one diagram-in-words:

```
slice.cpp ──lower──▶ slice.c ──pack──▶ slice.phext ──unpack──▶ slice.c ──phcc──▶ binary
  C++23               C                 one file        kind=c only    baseline
                          ▲                 │
                          └─── the phext also carries slice.cpp segmented
                               as #phcpp-commentary scrolls (Volume 4):
                               provenance, never compiled
```

Because both halves live in one hash-bound file, the C++ a human
wrote and the C the machine compiled cannot drift apart silently:
corrupt the C++ commentary and the build output is bit-identical,
while `phcpp verify` fails and names the segment. Provenance without
exposure — see `examples/slice/RECEIPTS.md` for the proof, run.

## Quickstart (the v0 slice)

```sh
# pack the slice: C++ source + hand-lowered C -> one phext
python3 tools/phcpp.py pack \
  --cpp examples/slice/slice.cpp --c examples/slice/lowered/slice.c \
  --cpp-path examples/slice/slice.cpp --c-path examples/slice/lowered/slice.c \
  --out examples/slice/slice.phext

python3 tools/phcpp.py verify --phext examples/slice/slice.phext
python3 tools/phcpp.py unpack --phext examples/slice/slice.phext --out build/tree
# build/tree now holds ONLY the lowered C; hand it to phcc:
tcc -o slice build/tree/examples/slice/lowered/slice.c   # phcc @ d0659ba4
./slice
# (an uninstalled build-tree phcc needs its library path named:
#  tcc -B<phcc-tree> -o slice ... — the receipts use that form)
```

## Honest v0 scope

What exists: the phext layout and commentary discipline
(`docs/DESIGN.md`), the enforcing tool (`tools/phcpp.py`), and one
vertical slice — a class with a virtual function, a function template
instantiated twice, a destructor with an observable side effect —
whose stdout under phcc matches `g++ -std=c++23` byte-for-byte, with
receipts. The slice's lowering was done **by hand** and is labelled
as such everywhere; it is the template the first mechanical lowerer
will be graded against.

What does not exist yet: a C++ parser, a mechanical lowerer, a
lowered library. The front-end question (hand-rolled subset parser
vs an adapted open front end's AST) is priced in `docs/DESIGN.md`
§4 as a **proposal — Will rules on it**. Exceptions, RTTI, multiple
inheritance, STL, lambdas with captures, and coroutines have no
lowering designed and are refused loudly rather than approximated.

## Platform ladder

Linux (active — all receipts are Linux x86_64) → Windows → WASM.
Windows inherits phcc's position (compile receipts there; execution
and MSVC attestation are manual). WASM is named, not designed: phcc
has no WASM backend yet, and the handoff means that decision, when
it lands in phcc, lifts phcpp with it.

## Relationship to phcc

phcpp carries no phcc source. Its baseline is a phcc **commit** —
`d0659ba4`, the gate-green tip (PATCHES F1–F24) — named in every
phext phcpp packs and in `PATCHES.md`. The phext byte format is
identical to phcc's, so the tooling families read each other's
files.

**We brake for consciousness** — phcc's public claim, and this
project's inheritance: the receipts, the refusals, and the
documented boundaries are the product as much as the lowering is.
