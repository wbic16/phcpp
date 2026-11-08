#!/usr/bin/env python3
"""run_corpus.py — the phcpp front-end v1 differential corpus.

For every positive TU in examples/corpus/:
  (a) lower with BOTH tracks; compile each track's C with phcc and
      run it — stdout must be byte-identical to the g++ -std=c++23
      oracle's stdout;
  (b) the two tracks' lowered C must be byte-identical;
  (c) the two tracks' C++23RA ledgers must be byte-identical, and
      both packs must be byte-identical phexts passing phcpp verify;
For every refusal TU in examples/corpus/refuse/ (expected code in
its first-line `// refuse:` comment, `T1/T2` when the detection
point legitimately differs): both tracks must refuse BY NAME with
the expected code (exit 3; exit 5 is clang's own named rejection,
expected only where the manifest says `clang-error`).

Run on a host with g++, clang++ and a phcc tree (PHCC_TREE env,
default ~/phcc-d7/tree). Writes examples/corpus/RECEIPTS.md.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "examples" / "corpus"
FRONT = ROOT / "tools" / "phcpp_front.py"
PACKTOOL = ROOT / "tools" / "phcpp.py"
PHCC_TREE = os.environ.get("PHCC_TREE",
                           str(Path.home() / "phcc-d7" / "tree"))
GXX = os.environ.get("GXX", "g++")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def oracle(cpp):
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "oracle"
        r = run([GXX, "-std=c++23", "-O0", "-o", str(exe), str(cpp)])
        if r.returncode != 0:
            return None, r.stderr.decode()[:300]
        r = run([str(exe)])
        return r.stdout, None


def lower(track, cpp, td):
    c_out = Path(td) / f"t{track}.c"
    led_out = Path(td) / f"t{track}.ledger"
    r = run([sys.executable, str(FRONT), "lower", "--track",
             str(track), "--cpp", str(cpp), "--c-out", str(c_out),
             "--ledger-out", str(led_out)])
    if r.returncode != 0:
        return None, None, r.returncode, r.stderr.decode().strip()
    return c_out.read_bytes(), led_out.read_bytes(), 0, ""


def phcc_run(c_bytes, td, tag):
    c_file = Path(td) / f"{tag}.c"
    c_file.write_bytes(c_bytes)
    exe = Path(td) / f"{tag}.exe"
    r = run([str(Path(PHCC_TREE) / "tcc"), f"-B{PHCC_TREE}",
             "-o", str(exe), str(c_file)])
    if r.returncode != 0:
        return None, "phcc build: " + r.stderr.decode()[:300]
    r = run([str(exe)])
    if r.returncode != 0:
        return None, f"phcc-built binary exited {r.returncode}"
    return r.stdout, None


def pack(track, cpp, td):
    out = Path(td) / f"t{track}.phext"
    r = run([sys.executable, str(FRONT), "pack", "--track", str(track),
             "--cpp", str(cpp), "--out", str(out)])
    if r.returncode != 0:
        return None, r.stderr.decode()[:200]
    v = run([sys.executable, str(PACKTOOL), "verify", "--phext",
             str(out)])
    if v.returncode != 0:
        return None, "verify: " + v.stdout.decode()[:200]
    return out.read_bytes(), None


def positive_case(cpp):
    rep = {"name": cpp.name, "checks": {}, "notes": []}
    ora, err = oracle(cpp)
    if ora is None:
        rep["checks"]["oracle"] = f"FAIL: {err}"
        return rep
    rep["oracle_sha"] = sha(ora)
    rep["checks"]["oracle"] = "ok"
    with tempfile.TemporaryDirectory() as td:
        lowered = {}
        for track in (1, 2):
            c_b, led_b, rc, serr = lower(track, cpp, td)
            if c_b is None:
                rep["checks"][f"track{track} lower"] = \
                    f"FAIL rc={rc}: {serr[:200]}"
                return rep
            rep["checks"][f"track{track} lower"] = "ok"
            out, err = phcc_run(c_b, td, f"t{track}")
            if out is None:
                rep["checks"][f"track{track} phcc run"] = f"FAIL: {err}"
                return rep
            rep["checks"][f"track{track} stdout == oracle"] = \
                "ok" if out == ora else \
                f"FAIL: got {sha(out)}, want {sha(ora)}"
            pk, err = pack(track, cpp, td)
            if pk is None:
                rep["checks"][f"track{track} pack+verify"] = \
                    f"FAIL: {err}"
                return rep
            rep["checks"][f"track{track} pack+verify"] = "ok"
            lowered[track] = (c_b, led_b, pk)
        rep["checks"]["C identical across tracks"] = \
            "ok" if lowered[1][0] == lowered[2][0] else "FAIL"
        rep["checks"]["ledger identical across tracks"] = \
            "ok" if lowered[1][1] == lowered[2][1] else "FAIL"
        rep["checks"]["phext identical across tracks"] = \
            "ok" if lowered[1][2] == lowered[2][2] else "FAIL"
        rep["ledger"] = lowered[1][1].decode()
    return rep


def refusal_case(cpp):
    first = cpp.read_text().splitlines()[0]
    spec = first.split("refuse:", 1)[1].strip()
    if "/" in spec and not spec.startswith("http"):
        t1_code, t2_code = spec.split("/", 1)
    else:
        t1_code = t2_code = spec
    rep = {"name": cpp.name, "checks": {}}
    for track, want in ((1, t1_code), (2, t2_code)):
        with tempfile.TemporaryDirectory() as td:
            _c, _l, rc, serr = lower(track, cpp, td)
        if want == "clang-error":
            ok = rc == 5
            got = f"rc={rc}"
        else:
            ok = rc == 3 and f"REFUSE {want}:" in serr
            got = serr.split("\n")[0][:120] if serr else f"rc={rc}"
        rep["checks"][f"track{track} refuses {want}"] = \
            "ok" if ok else f"FAIL: {got}"
    return rep


def main():
    positives = sorted(CORPUS.glob("[0-9]*.cpp"))
    refusals = sorted((CORPUS / "refuse").glob("*.cpp"))
    results = []
    n_fail = 0
    for cpp in positives:
        rep = positive_case(cpp)
        results.append(("positive", rep))
        bad = [k for k, v in rep["checks"].items() if v != "ok"]
        n_fail += 1 if bad else 0
        print(f"{rep['name']}: {'PASS' if not bad else 'FAIL ' + str(bad)}")
    for cpp in refusals:
        rep = refusal_case(cpp)
        results.append(("refusal", rep))
        bad = [k for k, v in rep["checks"].items() if v != "ok"]
        n_fail += 1 if bad else 0
        print(f"{rep['name']}: {'PASS' if not bad else 'FAIL ' + str(bad)}")
    write_receipts(results)
    print(f"\n{len(results) - n_fail}/{len(results)} cases pass; "
          f"receipts in examples/corpus/RECEIPTS.md")
    return 1 if n_fail else 0


def write_receipts(results):
    lines = []
    lines.append("# phcpp front-end v1 — differential corpus receipts")
    lines.append("")
    lines.append("Generated by tools/run_corpus.py. Oracle: "
                 "g++ -std=c++23. Lowering target: phcc (baseline in "
                 "tools/phcpp.py, PHCC_BASELINE). Checks per positive "
                 "TU: both tracks lower; phcc builds both; stdout "
                 "byte-identical to the oracle; C, ledger and packed "
                 "phext byte-identical across tracks; pack passes "
                 "phcpp verify. Refusals must be named, on both "
                 "tracks, with the recorded code (a `T1/T2` pair "
                 "marks a legitimate detection-point difference).")
    lines.append("")
    for kind, rep in results:
        lines.append(f"## {rep['name']} ({kind})")
        lines.append("")
        for k, v in rep["checks"].items():
            lines.append(f"- {k}: {v}")
        if kind == "positive" and "oracle_sha" in rep:
            lines.append(f"- oracle stdout sha256: {rep['oracle_sha']}")
        lines.append("")
        if kind == "positive" and "ledger" in rep:
            lines.append("Per-symbol C++23RA stamps (mechanism "
                         "receipt; all constructs are revision 1 in "
                         "v1, so stamps are uniformly r1 — what is "
                         "receipted is the per-symbol construct sets):")
            lines.append("")
            lines.append("```")
            in_sym = False
            for ln in rep["ledger"].splitlines():
                if ln == "symbols:":
                    in_sym = True
                    continue
                if ln == "members:":
                    break
                if in_sym:
                    lines.append(ln)
            lines.append("```")
            lines.append("")
    (CORPUS / "RECEIPTS.md").write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
