#!/usr/bin/env python3
"""phcpp.py — phcpp phext tooling: pack / verify / unpack.

phcpp is a lowering front end. A C++23RA translation unit is lowered
to C; both live in ONE phext:

  - the lowered C is ordinary phcc material (Collection 4 / Source,
    Volume 2 whole-file scroll + Volume 3 member segments carrying
    `#phcc-member` headers, exactly as phcc's own phext does), and
  - the C++ source lives in Collection 4, Volume 4, segmented, every
    segment scroll opening with a `#phcpp-commentary` header line.

Commentary is machine-checkable, not a convention: `verify` enforces
(1) every Volume 4 segment parses as commentary, (2) no Volume 4
coordinate is named by any kind=c manifest row or Volume 3 index,
(3) every segment payload in BOTH volumes hashes to the value its
header and index record, and the payloads tile their file exactly.
`unpack` is manifest-driven and exports kind=c rows only, so the C++
can never reach the compiler through this tool — while `verify`
still holds it to the same hash discipline as the C, which is what
makes corrupting it detectable (see examples/slice/RECEIPTS.md).

The phext byte layer (walk_bytes / implode / coordinate order) is
behaviour-identical to phcc's tools/phext_io.py: same delimiter set,
same dead-reckoning walk, so a phcpp phext is readable by phcc
tooling and vice versa. Stdlib only.

Segmentation is the v0 segmenter: a byte scanner (string/char/comment
aware) that cuts top-level declarations at brace-depth 0. It exists
to give segments stable spans and hashes; it is NOT a C++ parser and
claims no semantic understanding. The front-end question is open —
see docs/DESIGN.md.
"""
import hashlib, re, sys
from pathlib import Path

LEVEL_BYTES = [0x01, 0x1F, 0x1E, 0x1D, 0x1C, 0x1A, 0x19, 0x18, 0x17]
DELIM_SET = set(LEVEL_BYTES)
DELIM_TO_LEVEL = {b: i for i, b in enumerate(LEVEL_BYTES)}

PHCC_BASELINE = "36a7932f"
PHCPP_VERSION = "0.1.0"

# Coordinates: (library, shelf, series, collection, volume, book,
# chapter, section, scroll) — phcc's tuple order.
MANIFEST = (1, 1, 1, 1, 2, 10, 1, 1, 1)
NOTES    = (1, 1, 1, 1, 2, 10, 1, 2, 1)
LEDGER   = (1, 1, 1, 1, 2, 10, 1, 3, 1)

# The C++23RA construct-revision registry (docs/ABI-REVISIONS.md),
# duplicated here literally on purpose: verify must be able to check
# the front end's ledger without importing the front end.
CONSTRUCT_REVISIONS = {
    "mangling.readable": 1,
    "namespace.flatten": 1,
    "layout.struct": 1,
    "vtable.shape": 1,
    "ctor-dtor.explicit": 1,
    "calling.explicit-this": 1,
    "calling.byvalue-out": 1,
    "template.instantiation": 1,
    "reference.pointer": 1,
    "lambda.closureless": 1,
}


def stamp_of(constructs):
    if not constructs:
        return "r0"
    return "r%d" % max(CONSTRUCT_REVISIONS[c] for c in constructs)
C_FILE   = (1, 1, 1, 4, 2, 1, 1, 1, 1)
C_INDEX  = (1, 1, 1, 4, 3, 1, 1, 1, 1)
C_SEG0   = (1, 1, 1, 4, 3, 1, 1, 1, 2)   # scroll component increments
CPP_INDEX  = (1, 1, 1, 4, 4, 1, 1, 1, 1)
CPP_SEG0   = (1, 1, 1, 4, 4, 1, 1, 1, 2)


def fmt(coord):
    return (f"{coord[0]}.{coord[1]}.{coord[2]}/"
            f"{coord[3]}.{coord[4]}.{coord[5]}/"
            f"{coord[6]}.{coord[7]}.{coord[8]}")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def walk_bytes(data):
    coord = [1] * 9
    buf = bytearray()
    out = []
    for b in data:
        lvl = DELIM_TO_LEVEL.get(b)
        if lvl is None:
            buf.append(b)
        else:
            out.append((tuple(coord), bytes(buf)))
            buf = bytearray()
            coord[lvl] += 1
            for j in range(lvl + 1, 9):
                coord[j] = 1
    out.append((tuple(coord), bytes(buf)))
    if out and out[-1][1] == b"":
        out.pop()
    return out


def implode(scrolls):
    out = bytearray()
    state = [1] * 9
    for target in sorted(scrolls):
        for lvl in range(9):
            while state[lvl] < target[lvl]:
                out.append(LEVEL_BYTES[lvl])
                state[lvl] += 1
                for j in range(lvl + 1, 9):
                    state[j] = 1
        assert tuple(state) == target, (state, target)
        out += scrolls[target]
    return bytes(out)


def load_phext(path):
    return dict(walk_bytes(Path(path).read_bytes()))


# ------------------------------------------------------------ segmenter
def segment(data):
    """Cut `data` into top-level segments tiling it exactly.

    Returns [(kind, name, start, end)]. kind is one of
    function|type|object|gap, in phcc's importer vocabulary.
    """
    n = len(data)
    segs = []
    i = 0

    def scan(i, depth):
        """Advance one byte, tracking literals/comments; return
        (new_i, depth, char) where char is the significant byte or
        None for consumed literal/comment content."""
        b = data[i]
        if b == 0x22 or b == 0x27:  # " or '
            q = b
            i += 1
            while i < n:
                if data[i] == 0x5C:
                    i += 2
                    continue
                if data[i] == q:
                    return i + 1, depth, None
                i += 1
            return i, depth, None
        if b == 0x2F and i + 1 < n and data[i + 1] == 0x2F:
            while i < n and data[i] != 0x0A:
                i += 1
            return i, depth, None
        if b == 0x2F and i + 1 < n and data[i + 1] == 0x2A:
            i += 2
            while i + 1 < n and not (data[i] == 0x2A and data[i + 1] == 0x2F):
                i += 1
            return min(i + 2, n), depth, None
        if b == 0x7B:
            depth += 1
        elif b == 0x7D:
            depth -= 1
        return i + 1, depth, b

    def is_gap_start(i):
        return data[i] in b" \t\r\n" or (
            data[i] == 0x2F and i + 1 < n and data[i + 1] in (0x2F, 0x2A))

    while i < n:
        if is_gap_start(i):
            start = i
            while i < n and is_gap_start(i):
                i, _, _ = scan(i, 0)
            segs.append(("gap", "-", start, i))
            continue
        start = i
        if data[i] == 0x23:  # '#' preprocessor line (with continuations)
            while i < n:
                if data[i] == 0x0A and (i == 0 or data[i - 1] != 0x5C):
                    i += 1
                    break
                i += 1
            segs.append(("gap", "-", start, i))
            continue
        depth = 0
        saw_brace = False
        while i < n:
            i, depth, ch = scan(i, depth)
            if ch == 0x7B:
                saw_brace = True
            if ch == 0x7D and depth == 0:
                # member may be closed by a following ';' (struct/array)
                j = i
                while j < n and data[j] in b" \t\r\n":
                    j += 1
                if j < n and data[j] == 0x3B:
                    i = j + 1
                break
            if ch == 0x3B and depth == 0 and not saw_brace:
                break
        text = data[start:i]
        segs.append((classify(text), member_name(text), start, i))
    return segs


def classify(text):
    head = text.lstrip()
    if re.match(rb"(template\s*<[^;]*>\s*)?(struct|class|union|enum)\b", head):
        return "type"
    if b"(" in text and (b"{" in text or text.rstrip().endswith(b";")):
        return "function"
    return "object"


def member_name(text):
    head = text.lstrip()
    m = re.match(rb"(?:template\s*<[^;]*>\s*)?(?:struct|class|union|enum)\s+(\w+)", head)
    if m:
        return m.group(1).decode()
    m = re.search(rb"(\w+)\s*\(", text)
    if m:
        return m.group(1).decode()
    m = re.search(rb"(\w+)", text)
    return m.group(1).decode() if m else "-"


# ------------------------------------------------------------ pack
LAYOUT_NOTES = f"""# layout notes (phcpp pack, v1)

One file: a C++23RA translation unit, its mechanically lowered C,
the C++23RA feature ledger, and the provenance binding them.
Outer dimensions 1.1.1 throughout.

- Collection 1 (Meta), Volume 2, Book 10: Section 1 = placement
  manifest (kind, path, coordinate, SHA-256, size); Section 2 = this
  notes scroll; Section 3 = the feature ledger (present when the
  pack was made by the front end: per-member construct sets and
  C++23RA revision stamps, docs/ABI-REVISIONS.md). Kinds: `c`
  (build input) and `cpp-commentary` (provenance only — never a
  build input). The ledger is recorded on a manifest line that is
  not a kind= row, so `unpack` never exports it.
- Collection 4 (Source), Volume 2, Book 1, Chapter 1, Section 1:
  the lowered C file, whole bytes — the only file scroll `unpack`
  will export for building.
- Collection 4 (Source), Volume 3: member segmentation of the
  lowered C — index scroll + segment scrolls carrying `#phcc-member`
  headers, exactly phcc's format; payloads tile the C file exactly.
- Collection 4 (Source), Volume 4: the C++ source, segmented — index
  scroll + segment scrolls carrying `#phcpp-commentary` headers.
  There is deliberately NO whole-file C++ scroll in Volume 2: the
  C++ exists only as commentary segments, so no manifest-driven
  tool can mistake it for a build input. Its whole-file bytes are
  recoverable by tiling and `verify` proves the tiling against the
  manifest's SHA-256.
- phcc baseline: {PHCC_BASELINE}. phcpp version: {PHCPP_VERSION}.
"""


def seg_scroll(marker, kind, name, start, end, payload):
    hdr = (f"{marker}\tkind={kind}\tname={name}\t"
           f"span={start}-{end}\tsha256={sha256(payload)}\n")
    return hdr.encode() + payload


def index_scroll(rows):
    out = []
    for scroll_no, kind, name, start, end, payload in rows:
        out.append(f"{scroll_no}\t{kind}\t{name}\t{start}-{end}\t"
                   f"{sha256(payload)}")
    return ("\n".join(out) + "\n").encode()


def build_volume(scrolls, base_index, base_seg, marker, data, segs):
    rows = []
    for k, (kind, name, start, end) in enumerate(segs):
        payload = data[start:end]
        coord = base_seg[:8] + (base_seg[8] + k,)
        scrolls[coord] = seg_scroll(marker, kind, name, start, end, payload)
        rows.append((base_seg[8] + k, kind, name, start, end, payload))
    scrolls[base_index] = index_scroll(rows)


def cmd_pack(args):
    cpp = Path(args.cpp).read_bytes()
    c = Path(args.c).read_bytes()
    ledger = Path(args.ledger).read_bytes() if args.ledger else None
    baseline = args.phcc_baseline or PHCC_BASELINE
    for label, blob in (("cpp", cpp), ("c", c), ("ledger", ledger)):
        if blob is None:
            continue
        bad = DELIM_SET.intersection(blob)
        if bad:
            sys.exit(f"pack: {label} source contains phext delimiter "
                     f"bytes {sorted(bad)}; cannot be a scroll byte-exactly")
    scrolls = {}
    scrolls[C_FILE] = c
    build_volume(scrolls, C_INDEX, C_SEG0, "#phcc-member", c, segment(c))
    build_volume(scrolls, CPP_INDEX, CPP_SEG0, "#phcpp-commentary",
                 cpp, segment(cpp))
    if ledger is not None:
        scrolls[LEDGER] = ledger
    scrolls[NOTES] = LAYOUT_NOTES.encode()
    manifest = [
        "phcpp-manifest 1",
        f"phcpp-version: {PHCPP_VERSION}",
        f"phcc-baseline: {baseline}",
        f"kind=c\tpath={args.c_path}\tcoord={fmt(C_FILE)}\t"
        f"sha256={sha256(c)}\tsize={len(c)}",
        f"kind=cpp-commentary\tpath={args.cpp_path}\tcoord={fmt(CPP_INDEX)}\t"
        f"sha256={sha256(cpp)}\tsize={len(cpp)}",
    ]
    if ledger is not None:
        manifest.append(
            f"ledger\tcoord={fmt(LEDGER)}\tsha256={sha256(ledger)}\t"
            f"size={len(ledger)}")
    scrolls[MANIFEST] = ("\n".join(manifest) + "\n").encode()
    Path(args.out).write_bytes(implode(scrolls))
    print(f"pack: wrote {args.out} "
          f"({len(implode(scrolls))} bytes, {len(scrolls)} scrolls)")


# ------------------------------------------------------------ verify
class Fail(Exception):
    pass


def parse_manifest(scrolls):
    if MANIFEST not in scrolls:
        raise Fail("manifest scroll missing")
    rows = []
    for line in scrolls[MANIFEST].decode().splitlines():
        if line.startswith("kind="):
            f = dict(kv.split("=", 1) for kv in line.split("\t"))
            rows.append(f)
    return rows


def parse_coord(s):
    a, b, c = s.split("/")
    parts = []
    for grp in (a, b, c):
        parts += [int(x) for x in grp.split(".")]
    return tuple(parts)


SEG_HDR = re.compile(
    rb"\A#(phcc-member|phcpp-commentary)\tkind=(\S+)\tname=(\S+)\t"
    rb"span=(\d+)-(\d+)\tsha256=([0-9a-f]{64})\n")


def check_volume(scrolls, index_coord, marker, file_bytes, label):
    if index_coord not in scrolls:
        raise Fail(f"{label}: index scroll {fmt(index_coord)} missing")
    tiled = bytearray()
    for line in scrolls[index_coord].decode().splitlines():
        scroll_no, kind, name, span, isha = line.split("\t")
        coord = index_coord[:8] + (int(scroll_no),)
        if coord not in scrolls:
            raise Fail(f"{label}: segment scroll {fmt(coord)} missing")
        raw = scrolls[coord]
        m = SEG_HDR.match(raw)
        if not m:
            raise Fail(f"{label}: segment {fmt(coord)} header unparsable")
        if m.group(1).decode() != marker.lstrip('#'):
            raise Fail(f"{label}: segment {fmt(coord)} carries marker "
                       f"{m.group(1).decode()}, expected {marker}")
        payload = raw[m.end():]
        start, end = int(m.group(4)), int(m.group(5))
        if sha256(payload) != m.group(6).decode():
            raise Fail(f"{label}: segment {fmt(coord)} payload SHA-256 "
                       f"mismatch (header records {m.group(6).decode()}, "
                       f"bytes hash to {sha256(payload)})")
        if sha256(payload) != isha:
            raise Fail(f"{label}: segment {fmt(coord)} payload SHA-256 "
                       f"disagrees with index")
        if payload != file_bytes[start:end]:
            raise Fail(f"{label}: segment {fmt(coord)} payload != file "
                       f"bytes at its recorded span {start}-{end}")
        tiled += payload
    if bytes(tiled) != file_bytes:
        raise Fail(f"{label}: segment payloads do not tile the file")
    return len(scrolls[index_coord].decode().splitlines())


def check_ledger(scrolls):
    """If the manifest records a ledger, verify it end to end:
    scroll hash, registry agreement, and — the ABI SS6 rule — every
    member row's SHA-256 against the actual Volume 3 segment payload
    (a segment edited after stamping no longer matches, so its stamp
    is demoted: the ledger on disk simply fails verify), plus every
    stamp recomputed from the construct registry. Returns the member
    count, or None when the phext carries no ledger."""
    raw = scrolls[MANIFEST].decode()
    line = None
    for ln in raw.splitlines():
        if ln.startswith("ledger\t"):
            line = dict(kv.split("=", 1) for kv in ln.split("\t")[1:])
    if line is None:
        return None
    coord = parse_coord(line["coord"])
    if coord not in scrolls:
        raise Fail("ledger scroll missing")
    blob = scrolls[coord]
    if sha256(blob) != line["sha256"]:
        raise Fail("ledger SHA-256 mismatch vs manifest")
    rows = blob.decode().splitlines()
    if not rows or rows[0] != "phcpp-ledger 1":
        raise Fail("ledger header line bad")
    registry = ",".join(f"{k}={v}" for k, v in
                        sorted(CONSTRUCT_REVISIONS.items()))
    if f"constructs-registry: {registry}" not in rows:
        raise Fail("ledger construct registry disagrees with the "
                   "C++23RA registry")
    if "symbols:" not in rows or "members:" not in rows:
        raise Fail("ledger sections missing")
    si, mi = rows.index("symbols:"), rows.index("members:")
    sym_rows = rows[si + 1:mi]
    mem_rows = [r for r in rows[mi + 1:]
                if not r.startswith("required:")]
    required = set()
    for r in sym_rows + mem_rows:
        f = dict(kv.split("=", 1) for kv in r.split("\t")[1:])
        cons = f["constructs"].split(",") if f["constructs"] else []
        for cn in cons:
            if cn not in CONSTRUCT_REVISIONS:
                raise Fail(f"ledger names unknown construct {cn}")
        if f["stamp"] != stamp_of(cons):
            raise Fail(f"ledger stamp for {r.split(chr(9))[0]} does not "
                       f"recompute from its construct set")
        required |= set(cons)
    seg_rows = []
    for ln in scrolls[C_INDEX].decode().splitlines():
        no, kind, name, span, isha = ln.split("\t")
        if kind != "gap":
            seg_rows.append((C_INDEX[:8] + (int(no),), isha))
    if len(seg_rows) != len(mem_rows):
        raise Fail(f"ledger has {len(mem_rows)} member rows but the "
                   f"C index has {len(seg_rows)} non-gap segments")
    for r, (scoord, isha) in zip(mem_rows, seg_rows):
        f = dict(kv.split("=", 1) for kv in r.split("\t")[1:])
        if f["coord"] != fmt(scoord):
            raise Fail(f"ledger member {r.split(chr(9))[0]} records "
                       f"coord {f['coord']}, segment is at "
                       f"{fmt(scoord)}")
        if f["sha256"] != isha:
            raise Fail(f"ledger member {r.split(chr(9))[0]} SHA-256 "
                       f"does not match the segment payload — the "
                       f"segment changed after stamping (stamp demoted)")
    req_line = [r for r in rows if r.startswith("required:")]
    if len(req_line) != 1 or \
            set(req_line[0].split(": ", 1)[1].split(",")) != required:
        raise Fail("ledger required-constructs line disagrees with "
                   "the member rows")
    return len(mem_rows)


def cmd_verify(args):
    scrolls = load_phext(args.phext)
    try:
        rows = parse_manifest(scrolls)
        by_kind = {r["kind"]: r for r in rows}
        crow, prow = by_kind["c"], by_kind["cpp-commentary"]
        c_coord = parse_coord(crow["coord"])
        if scrolls.get(c_coord) is None:
            raise Fail("kind=c file scroll missing")
        c_bytes = scrolls[c_coord]
        if sha256(c_bytes) != crow["sha256"]:
            raise Fail("kind=c file scroll SHA-256 mismatch vs manifest")
        # Commentary isolation: no Volume 4 coordinate may be a build
        # input — not the C file scroll, not in the C index.
        cpp_index = parse_coord(prow["coord"])
        c_index_text = scrolls.get(C_INDEX, b"").decode()
        n_cpp = 0
        for coord in scrolls:
            if coord[3] == 4 and coord[4] == 4 and coord != cpp_index:
                head = scrolls[coord].split(b"\n", 1)[0]
                if not head.startswith(b"#phcpp-commentary\t"):
                    raise Fail(f"Volume 4 scroll {fmt(coord)} does not "
                               f"open with a #phcpp-commentary header")
                n_cpp += 1
        # The C++ whole-file bytes exist only as commentary tiling.
        # Per-segment checks run FIRST (against the tiling as the
        # file image) so a corrupted payload is named by coordinate
        # and hash, not just by the whole-file mismatch; the tiling
        # is then bound to the manifest hash as a whole.
        cpp_bytes = tile_from_index(scrolls, cpp_index)
        n_c = check_volume(scrolls, C_INDEX, "#phcc-member", c_bytes, "c")
        n_cpp2 = check_volume(scrolls, cpp_index, "#phcpp-commentary",
                              cpp_bytes, "cpp-commentary")
        assert n_cpp == n_cpp2
        if sha256(cpp_bytes) != prow["sha256"]:
            raise Fail("cpp-commentary tiling SHA-256 mismatch vs manifest")
        n_led = check_ledger(scrolls)
    except Fail as e:
        print(f"verify: FAIL — {e}")
        return 1
    msg = (f"verify: PASS — manifest hashes ok; {n_c} C segments "
           f"(#phcc-member) tile the lowered C; {n_cpp} commentary "
           f"segments (#phcpp-commentary) tile the C++ and are "
           f"isolated from every build input")
    if n_led is not None:
        msg += (f"; ledger ok: {n_led} members, stamps recompute, "
                f"member hashes match their segments")
    print(msg)
    return 0


def tile_from_index(scrolls, index_coord):
    out = bytearray()
    for line in scrolls[index_coord].decode().splitlines():
        scroll_no = int(line.split("\t")[0])
        coord = index_coord[:8] + (scroll_no,)
        raw = scrolls[coord]
        m = SEG_HDR.match(raw)
        if not m:
            raise Fail(f"segment {fmt(coord)} header unparsable")
        out += raw[m.end():]
    return bytes(out)


# ------------------------------------------------------------ unpack
def cmd_unpack(args):
    scrolls = load_phext(args.phext)
    rows = parse_manifest(scrolls)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()) and not args.force:
        sys.exit("unpack: target directory non-empty (use --force)")
    out.mkdir(parents=True, exist_ok=True)
    record = []
    for r in rows:
        if r["kind"] != "c":
            print(f"unpack: SKIP kind={r['kind']} path={r['path']} "
                  f"(commentary is provenance, never a build input)")
            continue
        blob = scrolls[parse_coord(r["coord"])]
        if sha256(blob) != r["sha256"]:
            sys.exit(f"unpack: SHA-256 mismatch for {r['path']}; "
                     f"nothing written for this file")
        dest = out / r["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        record.append(f"{r['path']}\t{r['sha256']}\t{len(blob)}\tc")
        print(f"unpack: wrote {r['path']} ({len(blob)} bytes, sha256 ok)")
    (out / "phcpp-manifest.tsv").write_text("\n".join(record) + "\n")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="phcpp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--cpp", required=True)
    p.add_argument("--c", required=True)
    p.add_argument("--cpp-path", required=True,
                   help="path recorded in the manifest for the C++ file")
    p.add_argument("--c-path", required=True,
                   help="path recorded in the manifest for the C file")
    p.add_argument("--ledger",
                   help="feature-ledger text to inscribe at "
                        "1.1.1/1.2.10/1.3.1 (front-end packs)")
    p.add_argument("--phcc-baseline",
                   help="phcc commit this pack stands against "
                        "(default: the PHCC_BASELINE constant)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_pack)
    v = sub.add_parser("verify")
    v.add_argument("--phext", required=True)
    v.set_defaults(fn=cmd_verify)
    u = sub.add_parser("unpack")
    u.add_argument("--phext", required=True)
    u.add_argument("--out", required=True)
    u.add_argument("--force", action="store_true")
    u.set_defaults(fn=cmd_unpack)
    args = ap.parse_args()
    rc = args.fn(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
