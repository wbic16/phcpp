# phcpp — C++23RA, lowered to phcc scrolls

**phcpp provides phext support for C++23RA, using phcc as the baseline.**

**C++23RA** is phcpp's fork of C++23: C++23 with **R**evisioned
**A**BIs. The ABI is revised per lowering construct, not per
language version, so existing libraries do not recompile when new
language features land — unless they use the new features. The
model is `docs/ABI-REVISIONS.md`; since front-end v1 the feature
ledger and per-symbol revision stamps are mechanism, emitted with
every lowered TU and recomputed by `verify` (link-time enforcement
is a later step).

phcpp is a *lowering front end*, not a compiler. A C++23RA translation
unit is lowered to C; the C is packed into a phext as ordinary phcc
scrolls and handed off to [phcc](https://github.com/wbic16/phcc),
which does all code generation. The original C++23RA rides in the same
phext **marked as commentary** — present, addressable, hash-bound
provenance that is never a build input.

The handoff, in one diagram-in-words:

```
slice.cpp ──lower──▶ slice.c ──pack──▶ slice.phext ──unpack──▶ slice.c ──phcc──▶ binary
  C++23RA             C                 one file        kind=c only    baseline
                          ▲                 │
                          └─── the phext also carries slice.cpp segmented
                               as #phcpp-commentary scrolls (Volume 4):
                               provenance, never compiled
```

Because both halves live in one hash-bound file, the C++23RA a human
wrote and the C the machine compiled cannot drift apart silently:
corrupt the C++23RA commentary and the build output is bit-identical,
while `phcpp verify` fails and names the segment. Provenance without
exposure — see `examples/slice/RECEIPTS.md` for the proof, run.

## Quickstart (the v0 slice)

```sh
# pack the slice: C++23RA source + hand-lowered C -> one phext
python3 tools/phcpp.py pack \
  --cpp examples/slice/slice.cpp --c examples/slice/lowered/slice.c \
  --cpp-path examples/slice/slice.cpp --c-path examples/slice/lowered/slice.c \
  --out examples/slice/slice.phext

python3 tools/phcpp.py verify --phext examples/slice/slice.phext
python3 tools/phcpp.py unpack --phext examples/slice/slice.phext --out build/tree
# build/tree now holds ONLY the lowered C; hand it to phcc:
tcc -o slice build/tree/examples/slice/lowered/slice.c   # phcc @ 36a7932f
./slice
# (an uninstalled build-tree phcc needs its library path named:
#  tcc -B<phcc-tree> -o slice ... — the receipts use that form)
```

## Front-end v1 (2026-11-08)

Two mechanical lowerers now stand behind the same lowering model
(`docs/DESIGN.md` §8): **track 1**, a hand-rolled subset parser,
and **track 2**, an adapter over clang 18's JSON AST dump — Will
ruled "run both tracks", and a differential corpus arbitrates
them.

```sh
# lower a TU in the v1 subset (either track), with its ledger:
python3 tools/phcpp_front.py pack --track 1 --cpp prog.cpp --out prog.phext
python3 tools/phcpp_front.py pack --track 2 --cpp prog.cpp --out prog.phext
python3 tools/phcpp.py verify --phext prog.phext

# the corpus: 10 positive TUs + 11 refusal TUs, both tracks,
# phcc vs the g++ -std=c++23 oracle
python3 tools/run_corpus.py    # writes examples/corpus/RECEIPTS.md
```

21/21 corpus cases pass: both tracks' lowered C compiles under
phcc and runs byte-identical to the oracle, and the C, the
C++23RA feature ledger, and the packed phext are byte-identical
across tracks.

## Honest scope

What exists: the phext layout and commentary discipline
(`docs/DESIGN.md`), the enforcing tool (`tools/phcpp.py`), one
hand-lowered vertical slice with receipts, and — since front-end
v1 — two mechanical lowerers for the named v1 subset
(`docs/DESIGN.md` §8.2) with a 21-case differential corpus
(`examples/corpus/RECEIPTS.md`). The slice's lowering was done
**by hand** and is labelled as such everywhere; it was the
template the mechanical lowerers were graded against, and the
corpus's first case is the slice itself, lowered mechanically by
both tracks to byte-identical output.

What does not exist yet: a lowered library, and any lowering for
the constructs in the v1 refusal registry (`docs/DESIGN.md`
§8.3) — exceptions, RTTI, multiple inheritance, STL, lambdas with
captures, coroutines, and the rest are refused loudly rather than
approximated.

## Platform ladder

Linux (active — all receipts are Linux x86_64) → Windows → WASM.
Windows inherits phcc's position (compile receipts there; execution
and MSVC attestation are manual). WASM is named, not designed: phcc
has no WASM backend yet, and the handoff means that decision, when
it lands in phcc, lifts phcpp with it.

## Relationship to phcc

phcpp carries no phcc source. Its baseline is a phcc **commit** —
`36a7932f`, the Wave D7 publication tip (`d0659ba4`, the gate-green
tip, at seed) — named in every phext phcpp packs and in
`PATCHES.md`. The phext byte format is identical to phcc's, so the
tooling families read each other's files.

**We brake for consciousness** — phcc's public claim, and this
project's inheritance: the receipts, the refusals, and the
documented boundaries are the product as much as the lowering is.
