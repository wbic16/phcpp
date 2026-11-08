# PATCHES.md — phcpp's divergence record

## Baseline relationship

phcpp is **not** a fork of phcc's tree and carries no phcc source.
It is a lowering front end whose back end is phcc at a named commit:

- **Back-end baseline:** wbic16/phcc @ `36a7932f` — the Wave D7
  publication tip (PATCHES F1–F39 + U1; supersedes `d0659ba4`, the
  gate-green tip named at seed). The baseline *relationship* is
  unchanged — a named commit, no carried phcc source; the named
  commit advanced with front-end v1 because the corpus receipts
  are issued against the phcc that actually builds the lowered C.
  Every phext phcpp packs records this baseline in its manifest; a
  phcpp receipt is valid only against the baseline it names, and
  goes STALE when the baseline moves until re-issued. The seed
  slice's committed phext still names `d0659ba4` and stands
  against it; it is not repacked.
- **Format baseline:** the phext byte format and layout conventions
  of phcc's `tools/phext_io.py` / `tools/phcc_migrate.py` (delimiter
  set, coordinate order, `#phcc-member` segment headers, Collection
  1 = Meta / Collection 4 = Source phasing). phcpp's one format
  *addition* is the `#phcpp-commentary` segment header and Source
  Volume 4 — an extension in unused space, not a divergence: phcc
  tooling reads phcpp phexts without modification, and no phcc
  scroll changes meaning.

## Language naming + ABI directive

The dialect phcpp compiles is named **C++23RA** (Will, 2026-11-07,
task 32.6.10/7.6.11/6.6.9): a fork of C++23 supporting **revisioned
ABIs** — per-construct ABI revisions with per-symbol minimal-revision
stamps, so libraries need not recompile when new language features
land unless they use them. Model: `docs/ABI-REVISIONS.md`; the
ledger + stamps are implemented in front-end v1 (P1/P2 below);
link-time enforcement via phcc F28 remains a later step.

## phcpp's own patches

phcpp has no upstream of its own yet: the seed is original work
(the layout, `tools/phcpp.py`, the slice). If the front-end ruling
(docs/DESIGN.md §4) adopts an existing open front end, this file
becomes that fork's complete per-file divergence record in the same
format phcc uses — one row per carried delta, with reason and
receipt — from the day the first upstream file lands, not after.

## Patch table

| ID | File(s) | Delta vs baseline | Reason | Receipt |
|---|---|---|---|---|
| — | — | *(no carried divergence at seed)* | — | — |
| P1 | `tools/phcpp.py` | Front-end v1: docstring names the dialect **C++23RA** (was "C++23"); `PHCPP_VERSION` 0.1.0; baseline constant advances to `36a7932f` (see above); pack gains `--ledger` + `--phcc-baseline`; a ledger scroll (`1.1.1/1.2.10/1.3.1`) is written into the phext and named in the manifest without a `kind=` (never a build input); `verify` recomputes every ledger stamp from an independent registry copy and re-hashes member segments (stamp demotion on edit, ABI-REVISIONS §6). | The ABI directive's ledger becomes mechanism; verify stays the contract. | Seed slice phext still verifies PASS under the extended tool; corpus packs verify on both tracks (`examples/corpus/RECEIPTS.md`). |
| P2 | `tools/phcpp_ir.py` *(new)* | The shared IR, semantics (layout, vtable slots, type-aware ctor selection, copy-init, aggregate init), the single C emitter, and the ledger builder both tracks feed. | One lowering model behind both tracks: the emitter is a pure function of the IR, so track identity cannot leak into the bytes. | Corpus: C, ledger, phext byte-identical across tracks on all 10 positive TUs. |
| P3 | `tools/phcpp_track1.py` *(new)* | Track 1: the hand-rolled subset parser (declarations first, bodies second, template bodies re-parsed per instantiation; lazy class-template member instantiation by fixpoint prune). Anything outside the v1 subset is refused with a named error. | DESIGN §4 Option A, under the hard fail-loud rule. | Corpus positives lower + run byte-identical to `g++ -std=c++23` under phcc; refusal corpus refused by name. |
| P4 | `tools/phcpp_track2.py` *(new)* | Track 2: the Clang-AST adapter (clang 18 JSON dump, TU on stdin; prescan refusals; clang's pre-foldings — implicit init synthesis, conversion-operator calls, elided copies — normalised to the shared IR). | DESIGN §4 Option B prototype, same lowering model. | Corpus: byte-identical to track 1 on every positive TU; ledger agreement per TU (the §4 pre-folding caveat does not bite on this corpus). |
| P5 | `tools/phcpp_front.py`, `tools/run_corpus.py` *(new)* | The front-end driver (`lower`/`pack`/`stamps`, named-refusal exit contract) and the differential corpus runner that issues `examples/corpus/RECEIPTS.md`. | One entry point + one receipts machine for both tracks. | `examples/corpus/RECEIPTS.md` (generated). |
| P6 | `examples/corpus/` *(new)* | The differential corpus: 10 positive TUs spanning the v1 subset + 11 refusal TUs, with generated receipts (incl. per-symbol C++23RA stamp tables). | DESIGN §5: a feature is "in" when the corpus carries its receipt. | 21/21 pass, 2026-11-08, aletheia-core, phcc @ `36a7932f`. |
