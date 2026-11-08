# phcpp — design

**phcpp provides phext support for C++23, using phcc as the baseline.**
A C++23 translation unit is *lowered* to C. The C is ordinary phcc
material — phcc scrolls — and is handed off to phcc, which does all
code generation. The C++ source rides in the same phext **marked as
commentary**: present, addressable, hash-bound provenance — and never
a build input. One phext holds both, so the C++ a human wrote and the
C the machine compiled can never drift apart without a hash noticing.

This document is the v0 design. Where v0 is a hand-driven slice
rather than a built mechanism, it says so.

## 1. The handoff architecture

```
 slice.cpp ──lower──▶ slice.c ──pack──▶ slice.phext ──unpack──▶ slice.c ──phcc──▶ binary
 (C++23)              (C)               (one file)      (kind=c only)   (baseline)
                 ▲                          │
                 └────── the same phext also carries slice.cpp,
                         segmented, as #phcpp-commentary scrolls
```

phcpp owns exactly three things: the lowering (C++ → C), the phext
layout below, and the tooling that enforces it (`tools/phcpp.py`:
pack / verify / unpack). phcc owns everything after the handoff:
parsing C, code generation, linking. phcpp carries no fork of phcc's
tree; its baseline is a phcc *commit* (`d0659ba4`, the gate-green
tip) named in every phext it packs and in PATCHES.md.

## 2. Phext layout

A phcpp project phext mirrors phcc's own layout (Collection 1 =
Meta, Collection 4 = Source in the 6D UML phasing; byte format
identical to phcc's `phext_io`, so phcc tooling reads phcpp phexts
and vice versa) and adds one volume:

| Address | Holds |
|---|---|
| `1.1.1/1.2.10/1.1.1` | Placement manifest: per file — kind, path, coordinate, SHA-256, size. Kinds: `c`, `cpp-commentary`. |
| `1.1.1/1.2.10/1.2.1` | Layout notes scroll (what pack wrote, in words). |
| Collection 4, Volume 2 | Lowered C, whole-file scrolls — the only file scrolls `unpack` exports. Placed as phcc places C: Book = module, Chapter = component, Section = file. |
| Collection 4, Volume 3 | Member segmentation of the lowered C: index scroll + segment scrolls with `#phcc-member` headers (kind/name/span/SHA-256), payloads tiling each C file exactly. Identical in form to phcc's Volume 3, so phcc's member tooling (analyze, the statement tier) works on lowered C unchanged. |
| Collection 4, Volume 4 | **The C++ source, segmented**: index scroll + segment scrolls, each opening with a `#phcpp-commentary` header line (kind/name/span/SHA-256), payloads tiling each C++ file exactly. |

### The commentary marking — chosen design

Every C++ segment scroll's first line is exactly

```
#phcpp-commentary\tkind=<kind>\tname=<name>\tspan=<start>-<end>\tsha256=<payload sha256>
```

and the C++ has **no whole-file scroll anywhere** — it exists only as
Volume 4 commentary segments. This follows the house precedent for
machine-checkable one-line markers (`#phcc-member`,
`#phcc-dedup-ref <coord> <sha256>`, `#phcc-stmt-ref …`): a scroll's
class is decidable from its first line, exactly, never heuristically.

Why this design, against the alternatives:

- **A dedicated C++ phase/collection** (e.g. a new Collection for
  "C++ source") was rejected: the 6D UML model fixes Collection =
  SDLC phase, and the C++ *is* Source-phase material — it is the
  source of truth the C was lowered from. Exiling it to another
  collection would also hide it from Source-phase tooling (census,
  statements) for no benefit; the isolation the design needs is not
  "elsewhere", it is "never compiled", and that is a property of the
  unpack path, not of geography.
- **A marker comment inside the C++ text** (e.g. wrapping the C++ in
  C comments inside a C file scroll) was rejected: it would make the
  C++ a build input the moment anything mis-parses, and silent
  inclusion is the failure class this programme exists to kill
  (cf. F5's macro-eaten attributes, F23/F24's dead-branch leaks).
  Commentary must fail *closed*: unreachable by construction, not
  skipped by politeness.

### Machine-checkable rules (enforced by `phcpp verify`)

1. **Reachability.** A scroll is a build input iff it is named by a
   `kind=c` manifest row (Volume 2 file) or by a Volume 3 index via a
   `#phcc-member` header. `unpack` exports `kind=c` rows only and
   prints every commentary row it skips, by name.
2. **Isolation.** Every non-index Volume 4 scroll opens with
   `#phcpp-commentary\t`; no Volume 4 coordinate appears in any
   `kind=c` manifest row or in the Volume 3 index. Verified by
   exhaustive scan, not by sampling.
3. **Provenance.** Every segment payload in *both* volumes hashes to
   the SHA-256 in its own header and in its index, and the payloads
   tile their file exactly (concatenation = file bytes = the
   manifest's file SHA-256). Commentary is held to the *same* hash
   discipline as code — that is what makes editing it detectable.

Consequence, proven in `examples/slice/RECEIPTS.md`: corrupt a
commentary payload and the build output is bit-identical (the bytes
never reach a compiler), while `verify` fails, naming the segment
coordinate and both hashes. Provenance without exposure.

## 3. The lowering model (v0)

v0 conventions — each is a rule a lowerer (human today, mechanical
tomorrow) can apply and a reader can check in the phext:

- **Classes → structs + dispatch.** A polymorphic class becomes a C
  struct whose first member is a pointer to a vtable struct holding
  function pointers in declaration order, destructor slot first. A
  virtual call `s->m(...)` lowers to `s->vptr->m(s, ...)`. A
  non-polymorphic class becomes a plain struct; member functions
  become `Type_method` functions taking `self` first.
- **Construction/destruction are explicit.** A constructor becomes
  `Type_init`, a destructor `Type_destroy`; the lowerer emits calls
  at exactly the points C++ semantics run them (scope exit, in
  reverse construction order). RAII is a lowering-time discipline in
  v0, not a runtime mechanism.
- **Templates → instantiated members.** Each instantiation becomes
  one ordinary C function/struct in the lowered C, present in the
  phext as its own member — instantiation is *visible*, countable,
  and dedup-able by the existing census tooling, instead of happening
  inside a compiler's head.
- **Name mangling: readable v0 mangling, not Itanium.** Template
  instantiations are named `<template>__<arg types>` (`add__int`,
  `add__double`); member functions `Type_name`. The lowered C is
  meant to be *read in the phext* — it is a first-class scroll, not
  an intermediate to be hidden. The cost is stated plainly: v0-phcpp
  objects do not link against g++-compiled objects that expect
  Itanium-mangled names. Itanium support is a linking feature to be
  added when mixed-toolchain linking is a goal, not a v0 property.

### In / out for v0 — honestly

**In** (conventions defined; the slice exercises the first three):
single-inheritance polymorphic classes and virtual dispatch;
non-virtual member functions; constructors/destructors with the
explicit-call discipline; function templates and class templates by
instantiation; static data members as C objects; the C subset of
C++ unchanged (it lowers to itself).

**Out** (no lowering designed; a lowerer must refuse, loudly):
exceptions and stack unwinding (a setjmp/longjmp sketch exists in
TinyCC-lineage compilers' folklore but is *not* designed here);
RTTI and `dynamic_cast`; multiple and virtual inheritance;
namespaces as a semantic feature (they will ride the mangling when
mangling grows up); operator overloading beyond direct function
lowering; the STL and any library facility (no lowered library
exists yet); lambdas with captures; coroutines; move semantics
beyond ctor/dtor pairing; concepts *checking* (an instantiated
constrained template lowers fine — verifying the constraint is the
front end's job, §4).

## 4. The front-end question — proposal, Will rules

Nothing in §§1–3 answers how C++23 text becomes an AST to lower
from. That is the programme's biggest open decision, and this
document's job is to price the options, not to smuggle one in.

**Option A — hand-rolled subset parser, grown by slices.** Full
control; phext-native from the first commit; the lowering stays a
small, auditable object in the phcc style. The price is the C++23
grammar itself: among the hardest in production languages —
context-sensitive lexing (the `<` of templates vs less-than),
two-phase name lookup, the most-vexing-parse family, SFINAE and
concepts. A *complete* from-scratch C++23 front end is a multi-year
object for a team. A subset parser is viable **only** under the
programme's standing discipline: it must fail loudly, by
construction, on anything outside its grammar — never guess. The
subset worth having first is the systems-code core: the C language,
classes, templates, namespaces-as-mangling — plausibly most of what
the gate's C projects would look like as C++, and none of it
sufficient for arbitrary C++23.

**Option B — adapt an existing open front end's AST, fork-patch
discipline.** Clang's AST (via libclang or a Clang tool) is the
strong candidate: complete C++23, an AST designed for tools, Apache
LLVM licensing. The phcc playbook applies directly — carry upstream,
PATCHES.md as the complete divergence record, lower from the AST
rather than from text. The prices: a very large carried tree (the
phcc phext was 63 MB for 135K LOC; a Clang carry is a different
weight class entirely); the AST's view of templates is
post-instantiation only in part, so template provenance in the phext
needs design, not assumption; and Option B's completeness quietly
removes the pressure that makes Option A's subset honest. GCC's
front end was considered and set aside (GPLv3 plus a dump interface
never designed as a stable AST API).

**Recommendation (proposal — Will rules): dual-track.** Track 1,
now: Option A, slice-grown under a hard-fail grammar, because it
keeps phcpp self-contained and every one of its limits is a named,
loud refusal. Track 2, decision-gated: prototype the Clang-AST
adapter behind the same lowering model, and let a differential
corpus (same C++ program through both tracks; lowered C compared
behaviourally under phcc) arbitrate which track carries which
language surface. The phext layout and lowering model in this
document are deliberately front-end-agnostic: both tracks emit the
same scrolls, so the ruling can change without invalidating any
packed project.

## 5. Receipts and gate discipline — inherited from phcc

phcpp takes phcc's evidentiary rules whole:

- **Hash-bound scrolls.** Every source scroll (C and commentary
  alike) carries SHA-256 provenance; any edit demotes dependent
  receipts to STALE until re-issued. There is no silent edit.
- **The oracle is g++.** A lowering is correct *for a program* when
  phcc's build of the lowered C produces byte-identical stdout and
  exit status to `g++ -std=c++23`'s build of the C++ — the
  conformance programme's (compile class, run exit, stdout bytes)
  triple, with every divergence classified (a) phcpp defect,
  (b) intentional/documented, (c) undefined/implementation-defined.
  A receipt with an unclassified divergence is not a receipt.
- **The gate is external and report-only**, exactly as phcc's: a
  C++ corpus, lowered and run through the triple, verdict recorded
  as a scroll in the phext. phcpp's corpus starts as the slice and
  grows one accepted feature at a time — a feature is "in" when the
  corpus carries its receipt, not when the lowerer emits something
  plausible.
- **Clean-tree evidence.** Builds that constitute receipts run from
  clean trees configured under the tool under test. (Written down
  because phcc's programme paid for this lesson once, at F23.)

## 6. Platform ladder

Per Will's ruling (task 30.7.8/9.7.11/7.6.9): **Linux (active) →
Windows → WASM**, for phcc, phcpp, and phst alike.

- **Linux** is the only platform v0 builds or claims. The slice's
  receipts are Linux x86_64 receipts.
- **Windows** inherits phcc's position: cross compile+link receipts
  exist there, execution receipts and the MSVC attestation are
  Will's manual domain. phcpp adds nothing Windows-specific until
  phcc's Windows surface is attested — the lowering is
  platform-neutral text, so nothing in this design blocks it.
- **WASM** is named, not designed: phcc has no WASM backend today,
  so a phcpp WASM story is a backend decision in phcc first. The
  phext handoff means that decision, when it lands, lifts phcpp
  with it — lowered C is lowered C.

## 7. v0 scope, stated once

What exists in this seed: the layout, the commentary discipline and
its enforcing tool, one hand-lowered slice with full receipts, and
this design. What does not: a C++ parser, a mechanical lowerer, a
library, a corpus beyond one program. The slice's lowering was done
**by hand** and is labelled as such in its source, its phext's
layout notes, and its receipts — it proves the handoff loop and the
commentary discipline end-to-end, and it is the template the first
mechanical lowerer will be graded against.
