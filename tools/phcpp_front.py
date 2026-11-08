#!/usr/bin/env python3
"""phcpp_front.py — phcpp front-end v1 driver: both tracks, one
lowering model, one phext layout.

  lower  --track 1|2 --cpp F.cpp --c-out F.c --ledger-out F.ledger
  pack   --track 1|2 --cpp F.cpp --out F.phext
  stamps --track 1|2 --cpp F.cpp     (print the per-symbol stamps)

Track 1 (tools/phcpp_track1.py) is the hand-rolled subset parser;
Track 2 (tools/phcpp_track2.py) is the Clang-AST adapter. Both build
the shared IR of tools/phcpp_ir.py, so a TU lowered by either track
produces the same C, the same phext and the same C++23RA feature
ledger — the differential corpus (tools/run_corpus.py) receipts how
far that sameness actually holds.

Refusals are loud and named: an out-of-subset TU exits 3 with
`phcpp: REFUSE <code>: <detail>`; an unparseable TU exits 4; a clang
failure exits 5. Nothing is ever silently mis-lowered.
"""
import sys
from pathlib import Path
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from phcpp_ir import Refuse, Emitter, build_ledger
import phcpp as packtool


def lower(track, cpp_path):
    src = Path(cpp_path).read_text()
    name = Path(cpp_path).name
    if track == 1:
        import phcpp_track1
        tu = phcpp_track1.lower_source(src, name)
    else:
        import phcpp_track2
        tu = phcpp_track2.lower_source(src, name)
    em = Emitter(tu)
    c_text, members = em.emit()
    c_bytes = c_text.encode()
    seg_info = []
    n_nongap = 0
    for k, (kind, nm, s, e) in enumerate(packtool.segment(c_bytes)):
        if kind == "gap":
            continue
        coord = packtool.C_SEG0[:8] + (packtool.C_SEG0[8] + k,)
        seg_info.append((packtool.fmt(coord), nm,
                         packtool.sha256(c_bytes[s:e])))
        n_nongap += 1
    if n_nongap != len(members):
        raise Refuse("segment-misalignment",
                     f"emitter produced {len(members)} members but the "
                     f"segmenter cut {n_nongap} non-gap segments of "
                     f"the lowered C")
    ledger = build_ledger(tu, members, seg_info)
    return tu, c_text, ledger, members


def cmd_lower(args):
    _tu, c_text, ledger, _m = lower(args.track, args.cpp)
    Path(args.c_out).write_text(c_text)
    Path(args.ledger_out).write_text(ledger)
    print(f"lower: track {args.track} {args.cpp} -> {args.c_out} "
          f"({len(c_text)} bytes) + ledger ({len(ledger)} bytes)")
    return 0


def cmd_pack(args):
    import subprocess
    import tempfile
    _tu, c_text, ledger, _m = lower(args.track, args.cpp)
    stem = Path(args.cpp).stem
    with tempfile.TemporaryDirectory() as td:
        c_path = Path(td) / (stem + ".c")
        led_path = Path(td) / (stem + ".ledger")
        c_path.write_text(c_text)
        led_path.write_text(ledger)
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "phcpp.py"),
             "pack", "--cpp", args.cpp, "--c", str(c_path),
             "--cpp-path", Path(args.cpp).name,
             "--c-path", stem + ".c",
             "--ledger", str(led_path), "--out", args.out],
            capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        return r.returncode


def cmd_stamps(args):
    _tu, _c, ledger, _m = lower(args.track, args.cpp)
    in_sym = False
    for line in ledger.splitlines():
        if line == "symbols:":
            in_sym = True
            continue
        if line == "members:":
            break
        if in_sym:
            print(line)
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="phcpp-front")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lower", "pack", "stamps"):
        p = sub.add_parser(name)
        p.add_argument("--track", type=int, choices=(1, 2),
                       required=True)
        p.add_argument("--cpp", required=True)
        if name == "lower":
            p.add_argument("--c-out", required=True)
            p.add_argument("--ledger-out", required=True)
        if name == "pack":
            p.add_argument("--out", required=True)
    args = ap.parse_args()
    fn = {"lower": cmd_lower, "pack": cmd_pack,
          "stamps": cmd_stamps}[args.cmd]
    try:
        return fn(args)
    except Refuse as r:
        if r.code in ("parse-error",):
            print(f"phcpp: PARSE-ERROR: {r.detail}", file=sys.stderr)
            return 4
        if r.code in ("clang-error",):
            print(f"phcpp: CLANG-ERROR: {r.detail}", file=sys.stderr)
            return 5
        print(f"phcpp: REFUSE {r.code}: {r.detail}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
