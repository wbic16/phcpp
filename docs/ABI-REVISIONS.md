# C++23RA — revisioned ABIs

**Status: design. Nothing in this document is implemented.** The
seed (see `docs/DESIGN.md`) has no parser, no mechanical lowerer,
and no library; the ABI model below is a property the lowering is
being designed to have, stated now so the first lowerer is built
against it rather than retrofitted. Where a mechanism depends on
work still in flight elsewhere (phcc's F28), it says so.

**Naming.** The dialect phcpp compiles is **C++23RA** — C++23 with
**R**evisioned **A**BIs. C++23 is the upstream standard phcpp forks
from; C++23RA is the fork. Will's directive (2026-11-07, task
32.6.10/7.6.11/6.6.9), verbatim:

> fork C++23 to support revisioned ABIs so we don't have to
> recompile libraries when new language features come out — unless
> we're using the new language features

## 1. The problem this answers

Every C++ implementation couples ABI stability to the language as
a whole. When a standard library or compiler changes the lowering
of *one* construct — the C++11 `std::string` layout is the canonical
scars-and-all example — the ecosystem pays one of two prices: break
the ABI and recompile the world, or freeze the construct forever
and let the language route around its own history. The freeze price
is the one C++ mostly pays, and it is paid by everyone, including
the overwhelming majority of code that never touches the changed
construct.

C++23RA's claim: that coupling is an artifact of treating the ABI
as a property of the *language version*. It isn't one. An ABI is a
pile of individual lowering decisions, and each decision can carry
its own revision.

## 2. The core move: revision per construct

An **ABI-affecting construct** is any lowering decision that is
observable across a translation-unit boundary. The initial list —
expected to grow, and growth is fine, because growth is per-item:

- vtable shape and slot ordering for a class;
- struct/class layout rules (member order, alignment, empty-base
  and tail-padding reuse);
- the mangling form a symbol's name takes;
- the exception model (when one is designed — v0 has none);
- the RTTI model (likewise undesigned in v0);
- calling-convention details the lowering introduces (e.g. how a
  by-value aggregate or a destructor pair is passed).

Each construct carries **the revision that introduced its current
lowering**. The lowering keeps a **feature ledger**: a record, per
translation unit, of which constructs it used and at which
revision. The ledger is not a side file — it is provenance in the
phext, stored per member (see §6), computed as the lowering runs.

A language or library change under C++23RA is then a *ledger
event*, not a flag day: the changed construct's revision bumps;
every construct that did not change keeps its revision.

## 3. Per-symbol minimal revision, and the name-stability rule

Every lowered symbol is stamped with its **minimal revision**: the
maximum revision among the constructs *its own lowering actually
depends on*. A free function over plain structs depends on the
layout rules and the mangling form and nothing else; it does not
inherit the revision of a vtable change it never touches.

Two rules follow, and they are the whole guarantee:

1. **Name stability.** A lowered name changes only when its own
   lowering changes. If a symbol's constructs are all unrevised,
   its name — and therefore every reference to it, in every
   already-built library — is bit-identical to before.
2. **No-recompile.** A library whose symbols' minimal revisions
   are all satisfied by a consumer's toolchain needs no
   recompilation when the toolchain's *other* constructs move.
   "The language gained a feature" is not, by itself, an event
   any existing binary can observe.

This is deliberately stronger than a version number on a library:
the stamp is per symbol and derived from the ledger, not asserted
by the library's author.

## 4. The "unless" clause, mechanically

Will's clause — *unless we're using the new language features* —
is a checkable condition in this model, not a caveat:

- A TU **requires** exactly the revisions in its ledger. A library
  requires the union over its exported symbols (its other symbols'
  revisions are its own business).
- A consumer satisfies a library iff the consumer's toolchain
  provides every required construct at the required revision.
- Adopting a new language feature stamps only the symbols and TUs
  that use it. The rest of the program — and every library it
  links — is unrevised by construction, so nothing about them
  needs rebuilding, relinking, or re-testing on ABI grounds.
  (Behavioural re-testing is the project's own discipline; RA
  makes a claim about linkage, not about semantics.)

## 5. Link-time enforcement: revisions ride symbol versioning

A stamp nobody checks is documentation. RA's stamps are checked
at link time, by machinery that already exists in the ELF world
and is being implemented in phcc now:

- Revisions ride **ELF symbol versioning**: a library's required
  revisions are expressed in an RA version namespace
  (`CPPRA_<construct-set hash or sequence>`) attached to its
  symbols, the same mechanism glibc has used for decades and the
  same mechanism phcc's **F28** (Wave D2 of the phcc deficiency
  programme — in flight at the time of writing) implements on the
  consumption side: `name@VER` resolution and `.symver` authoring.
- The linker checks required-vs-provided the way it already checks
  versioned references: an unsatisfied revision requirement is a
  link error naming the symbol and the construct revision it
  needs — loud, specific, and at the earliest moment the mismatch
  is knowable.
- phcc is phcpp's linker, so this is not a dependency on a third
  party's roadmap: when F28 lands, the carrier exists; what RA
  adds is the namespace discipline and the ledger that fills it.

Until F28 lands and phcpp's lowerer exists, this section is a
design commitment, not a mechanism. It is labelled as such
wherever it is cited.

## 6. The phext's role: the ledger is provenance

In a conventional toolchain the information RA needs — which
constructs each object actually depends on — is computed during
compilation and thrown away. In phcpp it cannot be thrown away,
because the phext is the artifact:

- The ledger is stored **per member**, alongside the `#phcc-member`
  segment it describes, in the same hash-bound provenance
  discipline as everything else phcpp packs. Editing a segment
  demotes its stamp exactly as it demotes its receipts
  (`docs/DESIGN.md` §5): a stamp that no longer matches its member
  is STALE, not silently wrong.
- Revision changes are **auditable from the member graph**: when a
  construct's revision bumps, the set of members whose ledgers
  cite that construct is enumerable, so "what does this revision
  actually touch" is a query, not an archaeology project.
- Unchanged members keep everything: their lowered C, their build
  artifacts under phcc, their receipts and proofs. This is the
  amortization property the whole phext programme already relies
  on (verify once, inherit by reference), applied to ABI change:
  a revision is paid for by exactly the members it revises.

## 7. Prior art, stated plainly

RA is not the first attempt at this problem, and the differences
matter more than the similarities:

- **GCC's `__abi_tag`** — the closest existing art. Introduced for
  the C++11 `std::string`/`list` transition, it tags individual
  symbols (via the mangled name) so old-ABI and new-ABI variants
  coexist and link errors point at the mismatch. RA generalizes
  the tag from one library's one transition to a per-construct
  revision system covering the whole lowering — and derives the
  tag from a ledger instead of asking authors to annotate.
- **libstdc++'s dual ABI** (`_GLIBCXX_USE_CXX11_ABI`) — proves
  coexistence works, at the price of a whole-library compile-time
  switch: every consumer must pick the same side. RA's stamps are
  per symbol, so there is no side to pick.
- **glibc symbol versioning** — the carrier mechanism (§5), proven
  over decades. glibc versions *symbols as the library authors
  declare them*; RA versions *constructs as the lowering used
  them* — the declaration is computed, not maintained by hand.
- **libc++'s versioned inline namespaces** (`std::__1`) — ABI
  containment by namespacing: effective, but the version is the
  whole library's, so any ABI move re-namespaces everything.
- **Rust** — the anti-model, taken seriously: no stable ABI by
  design; generic code is monomorphized per crate and everything
  recompiles. Rust buys total implementation freedom with exactly
  the currency Will's directive refuses to spend. C++23RA exists
  because phcpp's context — libraries handed off as phext scrolls
  to a baseline compiler — can afford the ledger Rust never had a
  place to keep.
- **Swift's ABI stability** — a whole-language freeze, honoured
  since Swift 5. RA deliberately does **not** make that commitment:
  RA is per-construct and revisable forever. The point is not to
  stop the ABI from ever changing; it is to stop every change from
  being everyone's change.

## 8. Open questions

Design questions this document names but does not settle; each
blocks implementation of its piece, none blocks the model:

- **Type-layout revisioning.** If a layout rule revises, old-layout
  and new-layout objects of the "same" type coexist in one
  program. Do they become distinct types at the lowered level
  (distinct names, explicit conversion members generated by the
  lowerer), or is layout revision fenced to new types only? The
  distinct-types answer composes with §3's name rule; its cost is
  conversion surface. Unruled.
- **Vtable revisioning.** Same shape, sharper teeth: a vtable
  layout revision splits a class hierarchy's dispatch world.
  Whether cross-revision virtual calls are bridged (thunks) or
  refused is unruled.
- **The standard library's own stamps.** A lowered C++23RA library
  (none exists yet) is itself a consumer of constructs; whether
  its stamps are a special "system" namespace or ordinary RA
  stamps affects who may satisfy what. Unruled.
- **Front-end interaction.** The ledger is **front-end-agnostic by
  construction**: it records what the *lowering* did, so both
  tracks in `docs/DESIGN.md` §4 (hand-rolled subset parser,
  adapted open front end) emit the same ledger form, and the §4
  ruling — still Will's — neither changes this model nor is
  changed by it. One honest caveat: a front end that loses
  construct information before the lowering sees it (e.g. an AST
  that pre-folds a construct) would under-report its ledger; the
  differential corpus proposed in §4 is where that would surface.

## 9. What would make this real

In dependency order: phcc F28 lands (carrier) → a mechanical
lowerer exists for a first construct set (the ledger has something
to record) → the ledger ships in the phext layout as a layout
revision of phcpp's own → the first construct revision is taken,
deliberately, on a small surface, with receipts showing an
unrevised library linking unchanged. Until the last step runs, 
C++23RA's ABI guarantee is a design property — checked by reading,
not yet by receipts — and phcpp's documents will keep saying so.
