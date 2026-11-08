# phcpp — design

**phcpp provides phext support for C++23RA, using phcc as the baseline.**
The language phcpp compiles is **C++23RA** — the fork of the C++23
standard with revisioned ABIs (`docs/ABI-REVISIONS.md`); "C++23" in
this document means the upstream standard, "C++23RA" the dialect.
A C++23RA translation unit is *lowered* to C. The C is ordinary phcc
material — phcc scrolls — and is handed off to phcc, which does all
code generation. The C++23RA source rides in the same phext **marked as
commentary**: present, addressable, hash-bound provenance — and never
a build input. One phext holds both, so the C++23RA a human wrote and the
C the machine compiled can never drift apart without a hash noticing.

This document is the v0 design. Where v0 is a hand-driven slice
rather than a built mechanism, it says so.

## 1. The handoff architecture

```
 slice.cpp ──lower──▶ slice.c ──pack──▶ slice.phext ──unpack──▶ slice.c ──phcc──▶ binary
 (C++23RA)            (C)               (one file)      (kind=c only)   (baseline)
                 ▲                          │
                 └────── the same phext also carries slice.cpp,
                         segmented, as #phcpp-commentary scrolls
```

phcpp owns exactly three things: the lowering (C++23RA → C), the phext
layout below, and the tooling that enforces it (`tools/phcpp.py`:
pack / verify / unpack). phcc owns everything after the handoff:
parsing C, code generation, linking. phcpp carries no fork of phcc's
tree; its baseline is a phcc *commit* (`36a7932f`, the Wave D7
publication tip; `d0659ba4` at seed) named in every phext it packs
and in PATCHES.md.

## 2. Phext layout

A phcpp project phext mirrors phcc's own layout (Collection 1 =
Meta, Collection 4 = Source in the 6D UML phasing; byte format
identical to phcc's `phext_io`, so phcc tooling reads phcpp phexts
and vice versa) and adds one volume:

| Address | Holds |
|---|---|
| `1.1.1/1.2.10/1.1.1` | Placement manifest: per file — kind, path, coordinate, SHA-256, size. Kinds: `c`, `cpp-commentary`. A pack made by the front end also names the **feature ledger** scroll here (coordinate + SHA-256 + size, no `kind=` — so `unpack` never exports it). |
| `1.1.1/1.2.10/1.2.1` | Layout notes scroll (what pack wrote, in words). |
| `1.1.1/1.2.10/1.3.1` | **Feature ledger** (front-end v1, §8): the C++23RA per-construct revision ledger for the lowered TU — construct registry line, per-symbol construct sets with minimal-revision stamps, per-member rows whose SHA-256s are the Volume 3 segment hashes (an edit to a member segment demotes its stamp on re-verify, per ABI-REVISIONS §6). |
| Collection 4, Volume 2 | Lowered C, whole-file scrolls — the only file scrolls `unpack` exports. Placed as phcc places C: Book = module, Chapter = component, Section = file. |
| Collection 4, Volume 3 | Member segmentation of the lowered C: index scroll + segment scrolls with `#phcc-member` headers (kind/name/span/SHA-256), payloads tiling each C file exactly. Identical in form to phcc's Volume 3, so phcc's member tooling (analyze, the statement tier) works on lowered C unchanged. |
| Collection 4, Volume 4 | **The C++23RA source, segmented**: index scroll + segment scrolls, each opening with a `#phcpp-commentary` header line (kind/name/span/SHA-256), payloads tiling each C++23RA file exactly. |

### The commentary marking — chosen design

Every C++23RA segment scroll's first line is exactly

```
#phcpp-commentary\tkind=<kind>\tname=<name>\tspan=<start>-<end>\tsha256=<payload sha256>
```

and the C++23RA has **no whole-file scroll anywhere** — it exists only as
Volume 4 commentary segments. This follows the house precedent for
machine-checkable one-line markers (`#phcc-member`,
`#phcc-dedup-ref <coord> <sha256>`, `#phcc-stmt-ref …`): a scroll's
class is decidable from its first line, exactly, never heuristically.

Why this design, against the alternatives:

- **A dedicated C++ phase/collection** (e.g. a new Collection for
  "C++23RA source") was rejected: the 6D UML model fixes Collection =
  SDLC phase, and the C++23RA *is* Source-phase material — it is the
  source of truth the C was lowered from. Exiling it to another
  collection would also hide it from Source-phase tooling (census,
  statements) for no benefit; the isolation the design needs is not
  "elsewhere", it is "never compiled", and that is a property of the
  unpack path, not of geography.
- **A marker comment inside the C++23RA text** (e.g. wrapping the C++23RA in
  C comments inside a C file scroll) was rejected: it would make the
  C++23RA a build input the moment anything mis-parses, and silent
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

- **ABI identity is part of the lowering model.** The mangling
  form, the vtable shape, and the layout rules above are
  ABI-affecting constructs, and C++23RA stamps each lowered symbol
  with the minimal revision of the constructs its own lowering
  depends on — so a construct's revision can move without forcing
  the world to recompile. The full model (feature ledger,
  name-stability rule, link-time enforcement via symbol
  versioning) is `docs/ABI-REVISIONS.md`. As of front-end v1 (§8)
  the ledger and the per-symbol stamps are mechanism, emitted with
  every lowered TU and recomputed by `verify`; link-time
  enforcement (phcc F28 symbol versioning) remains a later step —
  v1 stamps and records, it does not yet enforce.

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

## 4. The front-end question — RULED (Will, 2026-11-08)

Nothing in §§1–3 answers how C++23RA text becomes an AST to lower
from. That is the programme's biggest open decision, and this
document's job is to price the options, not to smuggle one in.

**Ruling: run both tracks.** Will ruled the dual-track
recommendation below into programme fact on 2026-11-08. Front-end
v1 (§8) implements both tracks behind the same lowering model and
phext layout, and the differential corpus arbitrates them: on the
v1 corpus the two tracks' lowered C, ledgers, and packed phexts are
byte-identical for every TU in the subset, and refusals agree by
name. The option pricing below is kept as the record of the
decision.

**Option A — hand-rolled subset parser, grown by slices.** Full
control; phext-native from the first commit; the lowering stays a
small, auditable object in the phcc style. The price is the C++23
grammar itself: among the hardest in production languages —
context-sensitive lexing (the `<` of templates vs less-than),
two-phase name lookup, the most-vexing-parse family, SFINAE and
concepts. A *complete* from-scratch C++23RA front end is a multi-year
object for a team. A subset parser is viable **only** under the
programme's standing discipline: it must fail loudly, by
construction, on anything outside its grammar — never guess. The
subset worth having first is the systems-code core: the C language,
classes, templates, namespaces-as-mangling — plausibly most of what
the gate's C projects would look like as C++, and none of it
sufficient for arbitrary C++23RA.

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
corpus (same C++23RA program through both tracks; lowered C compared
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
  exit status to `g++ -std=c++23`'s build of the C++23RA — the
  conformance programme's (compile class, run exit, stdout bytes)
  triple, with every divergence classified (a) phcpp defect,
  (b) intentional/documented, (c) undefined/implementation-defined.
  A receipt with an unclassified divergence is not a receipt.
- **The gate is external and report-only**, exactly as phcc's: a
  C++23RA corpus, lowered and run through the triple, verdict recorded
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
this design. What does not: a C++23RA parser, a mechanical lowerer, a
library, a corpus beyond one program. The slice's lowering was done
**by hand** and is labelled as such in its source, its phext's
layout notes, and its receipts — it proves the handoff loop and the
commentary discipline end-to-end, and it is the template the first
mechanical lowerer will be graded against.

Front-end v1 (§8) supersedes the "what does not" list: two
mechanical lowerers and a 21-case differential corpus now exist.
The seed slice's phext stands as packed at seed time, against the
seed baseline it names; it is not repacked by v1.

## 8. Front-end v1 (2026-11-08)

### 8.1 Architecture

Both tracks build one shared intermediate representation
(`tools/phcpp_ir.py`); one emitter produces the C, the phext member
segmentation, and the feature ledger as a pure function of the IR —
no track identity survives into the bytes.

- **Track 1** (`tools/phcpp_track1.py`): a hand-rolled subset
  parser. Declarations are collected first (classes, templates,
  signatures), bodies are parsed against the complete registry, and
  template bodies are re-parsed per instantiation under a
  substitution environment.
- **Track 2** (`tools/phcpp_track2.py`): an adapter over clang 18's
  JSON AST dump (`clang++ -std=c++23 -x c++ -Xclang -ast-dump=json
  -fsyntax-only -`, the TU fed on stdin; main-file nodes are
  separated from the header closure by location discipline). A
  prescan refuses whole construct families wherever they hide in
  the dump; translation proper is two-phase like track 1.
- **Driver** (`tools/phcpp_front.py`): `lower | pack | stamps
  --track 1|2`. Named refusals exit 3 printing `phcpp: REFUSE
  <code>: <detail>`; parse errors exit 4; clang failures exit 5.
  A pack asserts the ledger's member rows align 1:1, in order, with
  the segmenter's non-gap segments of the emitted C.
- **Corpus runner** (`tools/run_corpus.py`): writes
  `examples/corpus/RECEIPTS.md`.

### 8.2 The v1 subset

In (anything else is refused by name, §8.3):

- `#include <cstdio>` only; `printf` and `std::printf`.
- Namespaces, including nested; `typedef` / `using` type aliases.
- Classes/structs with single public inheritance; data members;
  static data members with out-of-line definitions; static member
  functions; member functions defined in-class or out-of-line;
  constructors with member-init lists (including a base-ctor
  entry), `explicit`; destructors, including virtual and
  `= default`; virtual functions (pure, `override`).
- Function templates and class templates, implicit instantiation
  only; class-template member definitions in-class only.
- Free functions; overloading by arity and by argument type;
  constructor selection is likewise type-aware.
- References (parameters, locals, returns); default arguments
  whose expressions are literals or globals.
- Non-capturing lambdas — bound to an `auto` or function-pointer
  variable, or passed directly as a call argument; a
  value-returning lambda needs an explicit `->` return type.
- `new` / `delete` on single objects; temporaries, destroyed at
  the end of the enclosing full expression; copy-initialisation
  via the implicit memberwise copy (user copy/move constructors
  are refused, §8.3).
- Aggregate initialisation of classes with no declared
  constructors, no base, and no polymorphism.
- Range-`for` over arrays (by value, or by `const T&` — a
  reference loop variable binds to the element, no copy);
  classic `for` / `while` / `do-while`; `if`/`else`;
  `break` / `continue`.

### 8.3 Refused by name (the v1 refusal registry)

`exceptions`, `rtti-dynamic-cast`, `rtti-typeid`,
`multiple-inheritance`, `virtual-inheritance`,
`capturing-lambda`, `lambda-return-type`, `stl-facility`,
`include-outside-subset`, `operator-overload`,
`move-semantics`, `rvalue-reference`, `coroutine`,
`concept-requires`, `enum`, `union`, `friend`,
`nested-class`, `user-copy-ctor`, `reference-field`,
`global-class-object` (no global constructor mechanism in v1),
`no-matching-constructor`, `no-matching-function`,
`ambiguous-call`, `temporary-in-condition`, `switch-statement`,
`goto-statement`, `static-local-variable`, `field-shadowing`,
`array-new-delete`, `delegating-constructor`,
`default-member-initializer`, `member-template`,
`range-for-nonarray`, `using-declaration`, `using-namespace`,
`linkage-spec`, `preprocessor-directive`, `auto-nonlambda`.
A refusal is a first-class output: never a silent mis-lowering.

Two refusal entries are legitimately track-split, and the corpus
records them as pairs: `move-semantics` (track 1 reaches the
`<utility>` include first: `include-outside-subset`; track 2 sees
`std::move` itself) and `coroutine` (track 1 keywords;
track 2's clang forms no AST, so `clang-error`). The detection
point differs; the refusal does not.

### 8.4 Mechanised lowering conventions (delta on §3)

- Every generated function is `static` except `main`. `bool`
  lowers to `int`.
- A virtual method occupying a vtable slot takes `void *self_` /
  `const void *self_` and casts to the concrete type in its
  prologue (the hand slice's convention), so vtable instances
  type-check exactly. The vtable *struct type* is the nearest
  ancestor's whose slot list is unchanged; the vtable *instance*
  is `<Class>_vtable`.
- Class-by-value returns lower through a hidden `__ret`
  out-parameter (construct `calling.byvalue-out`). `new` /
  `delete` lower through per-class helpers `__new_<C>_<nargs>` /
  `__delete_<C>` in plain C.
- Where one name has more than one distinct signature, **all** its
  overloads are mangled with an `__<argtypes>` suffix (`__void`
  for none). Pointers mangle `_ptr`, references `_ref`; `const`
  does not mangle; namespaces flatten with `_`.
- A derived→base pointer flow inserts the layout-prefix cast the
  C++ upcast implies, made explicit.
- Class-template instantiation is lazy, as in C++: only member
  functions a kept body calls — or that occupy a vtable slot —
  are emitted. Track 1 reaches this by a fixpoint prune over its
  own IR; track 2 inherits it from clang's instantiation. This
  was the corpus's sharpest asymmetry before it was closed.
- Temporaries created by a constructor's init list are destroyed
  at the end of that constructor call in v1; C++ destroys them at
  the end of the enclosing full expression. Unobservable in the
  subset unless a member constructor stores a pointer to the
  temporary — recorded here as a known approximation.
- Constructor selection is type-aware (exact match, then
  derived→base, then scalar/array decay). The one exception is
  `new`: `__new_<C>_<nargs>` helpers select by arity, so two
  same-arity constructors of different types are not yet
  distinguished at a `new` site.

### 8.5 The ledger as mechanism

Per ABI-REVISIONS §6, each lowered TU's ledger is stored per
member, alongside the member segmentation: the ledger scroll
(§2) carries the construct registry, one row per symbol (its
construct set and its minimal-revision stamp — the maximum
revision among the constructs its own lowering depends on), one
row per emitted member binding that member's Volume 3 segment
SHA-256, and the TU's required-revision union. `phcpp verify`
recomputes every stamp from an independent copy of the registry
table and re-hashes every member segment: edit a segment and its
stamp demotes at verify time. In v1 every construct is revision
1, so stamps are uniformly `r1` — what the corpus receipts is the
per-symbol construct sets and the recomputation machinery, not
yet a revision move.

Construct registry (all revision 1 in v1): `mangling.readable`,
`namespace.flatten`, `layout.struct`, `vtable.shape`,
`ctor-dtor.explicit`, `calling.explicit-this`,
`calling.byvalue-out`, `template.instantiation`,
`reference.pointer`, `lambda.closureless`.

### 8.6 Track asymmetries found by the corpus, and their verdicts

- **Ledger fidelity (the §4 caveat):** on this corpus the caveat
  does not bite. Both tracks produce byte-identical ledgers for
  every TU — clang's pre-folding (implicit base/member
  initialisers, conversion-operator calls for lambda→fnptr,
  elided copies delivered as construct nodes) is normalised in
  the adapter to the same IR the subset parser builds, and the
  corpus is the arbiter that caught each case. The caveat stays
  on record for wider language surface: it is a statement about
  clang, retired per-construct only by corpus receipts.
- A qualified member call (`a->Animal::legs()`) suppresses
  dispatch in C++; clang's JSON exposes no qualifier on
  `MemberExpr`, so track 2 reads it from the source text the
  node's range spans (the range begins at the arrow and contains
  `::name` exactly for qualified calls). Track 1 knows from its
  grammar. Both emit the direct base call; receipted in
  `04_inherit.cpp`.
- clang spells a lambda call operator's type with the placeholder
  `auto` even when a trailing return type is written; track 2
  recovers the written type from the source, and refuses a
  value-returning lambda without one — track 1's rule, same code
  (`lambda-return-type`).

### 8.7 Receipts

`examples/corpus/RECEIPTS.md`: 10 positive TUs spanning the
subset + 11 refusal TUs. **21/21 pass** (2026-11-08, aletheia-core,
phcc baseline `36a7932f`): for every positive TU, both tracks'
lowered C compiles under phcc and runs byte-identical to
`g++ -std=c++23`; C, ledger, and packed phext are byte-identical
across tracks; every pack passes `phcpp verify`; every refusal
TU is refused by name on both tracks.
