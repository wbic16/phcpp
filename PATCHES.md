# PATCHES.md — phcpp's divergence record

## Baseline relationship

phcpp is **not** a fork of phcc's tree and carries no phcc source.
It is a lowering front end whose back end is phcc at a named commit:

- **Back-end baseline:** wbic16/phcc @ `d0659ba4` — the gate-green
  tip (PATCHES F1–F24; publication gate re-verified green on this
  commit, all eleven projects, clean trees). Every phext phcpp packs
  records this baseline in its manifest; a phcpp receipt is valid
  only against the baseline it names, and goes STALE when the
  baseline moves until re-issued.
- **Format baseline:** the phext byte format and layout conventions
  of phcc's `tools/phext_io.py` / `tools/phcc_migrate.py` (delimiter
  set, coordinate order, `#phcc-member` segment headers, Collection
  1 = Meta / Collection 4 = Source phasing). phcpp's one format
  *addition* is the `#phcpp-commentary` segment header and Source
  Volume 4 — an extension in unused space, not a divergence: phcc
  tooling reads phcpp phexts without modification, and no phcc
  scroll changes meaning.

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
| — | — | *(empty: no carried divergence at seed)* | — | — |
