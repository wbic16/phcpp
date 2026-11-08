#!/usr/bin/env python3
"""phcpp_ir.py — phcpp front-end v1: shared IR, C emitter, RA ledger.

Both front-end tracks (tools/phcpp_track1.py, the hand-rolled subset
parser; tools/phcpp_track2.py, the Clang-AST adapter) lower C++23RA
to the SAME intermediate representation defined here, and this module
turns IR into C + a C++23RA feature ledger. The emitted C is a pure
function of the IR: no track identity, timestamp, or ordering accident
reaches the bytes, which is what makes the differential corpus a
byte-level arbiter rather than a behavioural one.

Lowering conventions are docs/DESIGN.md SS3 as mechanised by v1. Where
the mechanical rule normalises something the hand-lowered v0 slice did
mixed (linkage of generated functions), the mechanical rule is: every
generated function is `static` except `main`. Heap allocation goes
through per-class helper functions (__new_T / __delete_T) in plain C
— no GNU statement expressions — so the lowered C stays inside what
phcc is receipted to compile.

Refusals are loud and named: front ends raise Refuse(code, detail)
and the corpus matches codes across tracks.
"""
import hashlib

# ------------------------------------------------------------ refusals
class Refuse(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

# ------------------------------------------------------------ RA constructs
# The v1 construct registry (docs/ABI-REVISIONS.md SS2): each construct
# carries the revision that introduced its current lowering. v1
# introduces every construct at revision 1; the mechanism (per-symbol
# minimal revision = max over the symbol's own construct set) is what
# the receipts exercise.
CONSTRUCT_REVISIONS = {
    "mangling.readable": 1,      # readable name forms (DESIGN SS3)
    "namespace.flatten": 1,      # namespaces ride the mangling
    "layout.struct": 1,          # member order / base flattening / bool->int
    "vtable.shape": 1,           # dtor slot first, declaration order
    "ctor-dtor.explicit": 1,     # explicit init/destroy call discipline
    "calling.explicit-this": 1,  # member fns take self first
    "calling.byvalue-out": 1,    # class-by-value return via hidden out-param
    "template.instantiation": 1, # one member per instantiation
    "reference.pointer": 1,      # references lower to pointers
    "lambda.closureless": 1,     # non-capturing lambda -> plain function
}

def stamp_of(constructs):
    if not constructs:
        return "r0"
    return "r%d" % max(CONSTRUCT_REVISIONS[c] for c in constructs)

# ------------------------------------------------------------ types
# Type tuples:
#   ("builtin", name)   void bool char int uint long float double
#   ("class", qname)    qname = tuple of name parts, e.g. ("geo","Circle")
#   ("ptr", T) ("ref", T) ("const", T) ("array", T, n) ("fnptr", ret, (T,...))
BUILTIN_C = {"void": "void", "bool": "int", "char": "char", "int": "int",
             "uint": "unsigned int", "long": "long", "float": "float",
             "double": "double"}

def T_builtin(n): return ("builtin", n)
def T_class(q): return ("class", tuple(q))
def T_ptr(t): return ("ptr", t)
def T_ref(t): return ("ref", t)
def T_const(t): return ("const", t)
def T_array(t, n): return ("array", t, n)
def T_fnptr(r, ps): return ("fnptr", r, tuple(ps))

def strip_const(t):
    return t[1] if t[0] == "const" else t

def base_type(t):
    while t[0] in ("const", "ref"):
        t = t[1]
    return t

def is_class_t(t):
    return strip_const(t)[0] == "class"

def class_qname(t):
    return strip_const(t)[1]

def pointee(t):
    t = strip_const(t)
    return t[1] if t[0] in ("ptr", "ref") else None

def has_ref(params):
    return any(strip_const(p[1])[0] == "ref" or p[1][0] == "ref"
               for p in params)

def mangle_type(t):
    t = strip_const(t)
    k = t[0]
    if k == "builtin":
        return t[1] if t[1] != "uint" else "uint"
    if k == "class":
        return "_".join(t[1])
    if k == "ptr":
        return mangle_type(t[1]) + "_ptr"
    if k == "ref":
        return mangle_type(t[1]) + "_ref"
    if k == "array":
        return mangle_type(t[1]) + "_arr"
    if k == "fnptr":
        return "fnptr"
    raise AssertionError(t)

def cname(qname):
    return "_".join(qname)

# ------------------------------------------------------------ C declarator
def render_type(t, name=""):
    """Render a C declaration of `name` with type t: 'int *x', etc."""
    return _decl(t, name)

def _decl(t, name):
    k = t[0]
    if k == "builtin":
        return f"{BUILTIN_C[t[1]]} {name}".strip()
    if k == "class":
        return f"struct {cname(t[1])} {name}".strip()
    if k == "const":
        inner = t[1]
        if inner[0] == "ptr":
            return _decl(inner, name)       # top-level const on a pointer: dropped
        if inner[0] in ("builtin", "class"):
            return "const " + _decl(inner, name)
        return _decl(inner, name)
    if k == "ptr":
        inner = t[1]
        if inner[0] in ("fnptr", "array"):
            return _decl(inner, f"(*{name})")
        return _decl(inner, f"*{name}")
    if k == "ref":
        return _decl(("ptr", t[1]), name)
    if k == "array":
        return _decl(t[1], f"{name}[{t[2]}]")
    if k == "fnptr":
        params = ", ".join(_decl(p, "") for p in t[2]) or "void"
        return _decl(t[1], f"(*{name})({params})")
    raise AssertionError(t)

def type_str(t):
    return _decl(t, "")

# ------------------------------------------------------------ TU + semantics
class TU:
    def __init__(self, cpp_name):
        self.cpp_name = cpp_name
        self.classes = []
        self.class_by_qname = {}
        self.funcs = []
        self.globals = []
        self.uses_printf = False
        self.uses_new = False

    def add_class(self, c):
        self.classes.append(c)
        self.class_by_qname[c["qname"]] = c

    def lookup_class(self, qname):
        return self.class_by_qname.get(tuple(qname))

class Semantics:
    """Derived class facts, computed once from the IR (shared by both
    tracks, so the facts cannot diverge)."""
    def __init__(self, tu):
        self.tu = tu
        self._poly = {}
        self._slots = {}
        self._fields = {}
        self._abstract = {}
        self._needs_init = {}
        self._needs_destroy = {}

    def cls(self, q):
        return self.tu.class_by_qname[tuple(q)]

    def bases_chain(self, q):
        out = []
        c = self.cls(q)
        while c is not None and c.get("base"):
            out.append(c["base"])
            c = self.cls(c["base"])
        return out

    def is_derived(self, q, base):
        return tuple(base) in [tuple(b) for b in self.bases_chain(q)]

    def polymorphic(self, q):
        q = tuple(q)
        if q in self._poly:
            return self._poly[q]
        c = self.cls(q)
        poly = bool(any(m.get("virtual") for m in c["methods"]) or
                    (c.get("dtor") is not None and
                     c["dtor"].get("virtual")) or
                    (c.get("base") and self.polymorphic(c["base"])))
        self._poly[q] = poly
        return poly

    @staticmethod
    def _sig(m):
        return (m["name"], tuple(strip_const(p[1]) for p in m["params"]),
                bool(m.get("const")))

    def slots(self, q):
        """Ordered virtual slots: [destroy] + virtuals in declaration
        order from the root down; overrides replace in place."""
        q = tuple(q)
        if q in self._slots:
            return self._slots[q]
        c = self.cls(q)
        if c.get("base") and self.polymorphic(c["base"]):
            slots = [dict(s) for s in self.slots(c["base"])]
        else:
            slots = [{"name": "~dtor", "ret": T_builtin("void"),
                      "params": [], "const": False, "pure": False,
                      "owner": q}]
        for m in c["methods"]:
            sig = self._sig(m)
            hit = None
            for s in slots:
                if s["name"] != "~dtor" and self._sig(s) == sig:
                    hit = s
                    break
            # Once virtual, always virtual: a method matching an
            # inherited slot IS an override even without the keyword.
            if hit is None and not m.get("virtual"):
                continue
            entry = {"name": m["name"], "ret": m["ret"],
                     "params": list(m["params"]),
                     "const": bool(m.get("const")),
                     "pure": bool(m.get("pure")), "owner": q}
            if hit is not None:
                hit.update(entry)
            else:
                slots.append(entry)
        self._slots[q] = slots
        return slots

    def abstract(self, q):
        q = tuple(q)
        if q in self._abstract:
            return self._abstract[q]
        a = self.polymorphic(q) and any(s["pure"] for s in self.slots(q))
        self._abstract[q] = a
        return a

    def layout_fields(self, q):
        q = tuple(q)
        if q in self._fields:
            return self._fields[q]
        c = self.cls(q)
        out = []
        if c.get("base"):
            out += self.layout_fields(c["base"])
        out += [(f["name"], f["type"]) for f in c["fields"]
                if not f.get("static")]
        self._fields[q] = out
        return out

    def check_layout(self, q):
        names = [n for n, _ in self.layout_fields(q)]
        if len(names) != len(set(names)):
            raise Refuse("field-shadowing",
                         f"a field name repeats in the flattened layout "
                         f"of {'::'.join(q)}")

    def find_field(self, q, name):
        for cq in [tuple(q)] + self.bases_chain(q):
            c = self.cls(cq)
            for f in c["fields"]:
                if f["name"] == name and not f.get("static"):
                    return cq, f
        return None, None

    def find_static_field(self, q, name):
        for cq in [tuple(q)] + self.bases_chain(q):
            c = self.cls(cq)
            for f in c["fields"]:
                if f["name"] == name and f.get("static"):
                    return cq, f
        return None, None

    def needs_init(self, q):
        q = tuple(q)
        if q in self._needs_init:
            return self._needs_init[q]
        c = self.cls(q)
        v = bool(c["ctors"]) or self.polymorphic(q)
        if not v and c.get("base"):
            v = self.needs_init(c["base"])
        if not v:
            v = any(is_class_t(f["type"]) and
                    self.needs_init(class_qname(f["type"]))
                    for f in c["fields"] if not f.get("static"))
        self._needs_init[q] = v
        return v

    def needs_destroy(self, q):
        q = tuple(q)
        if q in self._needs_destroy:
            return self._needs_destroy[q]
        c = self.cls(q)
        v = c.get("dtor") is not None or self.polymorphic(q)
        if not v and c.get("base"):
            v = self.needs_destroy(c["base"])
        if not v:
            v = any(is_class_t(f["type"]) and
                    self.needs_destroy(class_qname(f["type"]))
                    for f in c["fields"] if not f.get("static"))
        self._needs_destroy[q] = v
        return v

    def default_ctor(self, q):
        c = self.cls(q)
        for ct in c["ctors"]:
            if all(p[2] is not None for p in ct["params"]):
                return ct
        return None

    def pick_ctor(self, q, nargs):
        """Ctor callable with nargs arguments (defaults fill the rest)."""
        c = self.cls(q)
        for ct in c["ctors"]:
            ps = ct["params"]
            if len(ps) >= nargs and all(p[2] is not None for p in ps[nargs:]):
                return ct
        return None

    def pick_ctor_for(self, q, args):
        """Type-aware constructor selection: among arity-compatible
        constructors, prefer exact type matches, then derived-to-base
        and scalar conversions. Returns None when nothing fits."""
        def score(pt, at):
            pb, ab = base_type(pt), base_type(at)
            if pb == ab:
                return 0
            if is_class_t(pb) and is_class_t(ab):
                if self.is_derived(class_qname(ab), class_qname(pb)):
                    return 1
                return None
            if pb[0] in ("int", "uint", "long", "float", "double",
                         "char", "bool") and \
                    ab[0] in ("int", "uint", "long", "float", "double",
                              "char", "bool"):
                return 2
            if pb[0] == "ptr" and ab[0] == "ptr":
                return 2
            if pb[0] == "ptr" and ab[0] == "array":
                return 2    # array-to-pointer decay (string literals)
            return None
        best = None
        for ct in self.cls(q)["ctors"]:
            ps = ct["params"]
            if len(ps) < len(args) or \
                    not all(p[2] is not None for p in ps[len(args):]):
                continue
            total = 0
            ok = True
            for p, a in zip(ps, args):
                sc = score(p[1], a.get("t", T_builtin("void")))
                if sc is None:
                    ok = False
                    break
                total += sc
            if ok and (best is None or total < best[0]):
                best = (total, ct)
        return best[1] if best else None

    def is_copy_init(self, q, args):
        """A single argument of exactly the constructed class is a
        copy-initialisation (the implicit memberwise copy; user copy
        constructors are outside the subset)."""
        return (len(args) == 1 and is_class_t(args[0].get("t", T_builtin("void")))
                and class_qname(args[0]["t"]) == tuple(q))

# ============================================================ emitter
class Item:
    def __init__(self, kind, name, anchor, constructs):
        self.kind = kind        # struct|vtable-struct|func|object
        self.name = name        # C name
        self.key = f"{kind}:{name}"
        self.anchor = anchor    # source line of the originating decl
        self.constructs = set(constructs)
        self.dep_keys = set()   # Item keys that must be emitted first
        self.lines = []

class Emitter:
    def __init__(self, tu):
        self.tu = tu
        self.sem = Semantics(tu)
        self.items = []
        self.func_names = {}
        self.method_names = {}
        self.ctor_names = {}
        self.used_new = {}      # qname -> set of ctor arg-counts used
        self.used_delete = set()
        self._plan_names()

    # ---------- naming ----------
    def _plan_names(self):
        def suffix(params):
            if not params:
                return "__void"
            return "__" + "_".join(mangle_type(p[1]) for p in params)
        groups = {}
        for f in self.tu.funcs:
            groups.setdefault(f["qname"], []).append(f)
        for q, fs in groups.items():
            keys = {(f["ret"], tuple(p[1] for p in f["params"])) for f in fs}
            multi = len(keys) > 1
            for f in fs:
                nm = cname(q)
                if multi:
                    nm += suffix(f["params"])
                self.func_names[id(f)] = nm
        for c in self.tu.classes:
            q = c["qname"]
            groups = {}
            for m in c["methods"]:
                groups.setdefault(m["name"], []).append(m)
            for name, ms in groups.items():
                keys = {(m["ret"], tuple(p[1] for p in m["params"]),
                         bool(m.get("const"))) for m in ms}
                multi = len(keys) > 1
                for m in ms:
                    nm = f"{cname(q)}_{name}"
                    if multi:
                        nm += suffix(m["params"])
                    self.method_names[(q, id(m))] = nm
            cs = c["ctors"]
            multi = len(cs) > 1
            for ct in cs:
                nm = f"{cname(q)}_init"
                if multi:
                    nm += suffix(ct["params"])
                self.ctor_names[(q, id(ct))] = nm

    def func_cname(self, f): return self.func_names[id(f)]
    def method_cname(self, q, m): return self.method_names[(tuple(q), id(m))]
    def ctor_cname(self, q, ct): return self.ctor_names[(tuple(q), id(ct))]
    def destroy_name(self, q): return f"{cname(q)}_destroy"
    def default_init_name(self, q): return f"{cname(q)}_init"

    def vtable_struct_type(self, q):
        q = tuple(q)
        c = self.sem.cls(q)
        if c.get("base") and self.sem.polymorphic(c["base"]):
            own = [s["name"] for s in self.sem.slots(q)]
            base = [s["name"] for s in self.sem.slots(c["base"])]
            if own == base:
                return self.vtable_struct_type(c["base"])
        return f"{cname(q)}_vtable"

    def vtable_inst_name(self, q): return f"{cname(q)}_vtable"

    # ---------- plumbing ----------
    def add_item(self, kind, name, anchor, constructs):
        it = Item(kind, name, anchor, constructs)
        self.items.append(it)
        return it

    def emit(self):
        for q in self.tu.class_by_qname:
            self.sem.check_layout(q)
        self.class_items = {}
        for c in self.tu.classes:
            self._emit_class_skeleton(c)
        for c in self.tu.classes:
            self._emit_class_members(c)
        for f in self.tu.funcs:
            self._emit_func(f)
        for g in self.tu.globals:
            self._emit_global(g)
        self._emit_new_delete_helpers()
        order = self._topo()
        out = ["/* lowered by phcpp front-end v1 from "
               f"{self.tu.cpp_name} —\n"
               " * mechanical lowering (docs/DESIGN.md SS3 conventions);\n"
               " * provenance + RA ledger: see this phext's ledger "
               "scroll. */\n"]
        inc = []
        if self.tu.uses_printf:
            inc.append("#include <stdio.h>")
        if self.tu.uses_new:
            inc.append("#include <stdlib.h>")
        if inc:
            out.append("\n".join(inc) + "\n\n")
        for it in order:
            out.append("\n".join(it.lines) + "\n\n")
        return "".join(out).rstrip() + "\n", order

    def _topo(self):
        by_key = {}
        for it in self.items:
            assert it.key not in by_key, f"duplicate item {it.key}"
            by_key[it.key] = it
        done, placed, rest = [], set(), list(self.items)
        while rest:
            ready = [it for it in rest if it.dep_keys <= placed]
            if not ready:
                raise AssertionError("dependency cycle among emitted items")
            ready.sort(key=lambda it: (it.anchor, it.name))
            it = ready[0]
            done.append(it)
            placed.add(it.key)
            rest.remove(it)
        return done

    # ---------- classes ----------
    def _emit_class_skeleton(self, c):
        q = c["qname"]
        sem = self.sem
        items = {}
        if sem.polymorphic(q) and \
                self.vtable_struct_type(q) == f"{cname(q)}_vtable":
            cons = {"mangling.readable", "vtable.shape"}
            if len(q) > 1:
                cons.add("namespace.flatten")
            if c.get("template_inst"):
                cons.add("template.instantiation")
            it = self.add_item("vtable-struct", f"{cname(q)}_vtable",
                               c["line"], cons)
            lines = [f"struct {cname(q)}_vtable {{"]
            for i, s in enumerate(sem.slots(q)):
                if i == 0:
                    lines.append("    void (*destroy)(void *self);")
                else:
                    selfp = "const void *self" if s["const"] else "void *self"
                    params = ", ".join([selfp] +
                                       [render_type(p[1], p[0]) for p in s["params"]])
                    lines.append(f"    {render_type(s['ret'], '')} "
                                 f"(*{s['name']})({params});")
            lines.append("};")
            it.lines = lines
            items["vtable-struct"] = it
        cons = {"mangling.readable", "layout.struct"}
        if len(q) > 1:
            cons.add("namespace.flatten")
        if c.get("template_inst"):
            cons.add("template.instantiation")
        it = self.add_item("struct", cname(q), c["line"], cons)
        lines = [f"struct {cname(q)} {{"]
        if sem.polymorphic(q):
            lines.append(f"    const struct {self.vtable_struct_type(q)} *vptr;")
        for fname, ftype in sem.layout_fields(q):
            lines.append(f"    {render_type(ftype, fname)};")
        if not sem.polymorphic(q) and not sem.layout_fields(q):
            lines.append("    char _empty;")
        lines.append("};")
        it.lines = lines
        items["struct"] = it
        for fname, ftype in sem.layout_fields(q):
            if is_class_t(ftype):
                dq = class_qname(ftype)
                if dq != q and dq in self.class_items:
                    it.dep_keys.add(f"struct:{cname(dq)}")
        self.class_items[q] = items

    def _base_cons(self, c):
        cons = {"mangling.readable"}
        if len(c["qname"]) > 1:
            cons.add("namespace.flatten")
        if c.get("template_inst"):
            cons.add("template.instantiation")
        return cons

    def _emit_class_members(self, c):
        q = c["qname"]
        sem = self.sem
        items = self.class_items[q]
        base_cons = self._base_cons(c)
        destroy_item = None
        if sem.needs_destroy(q):
            cons = set(base_cons) | {"ctor-dtor.explicit"}
            if sem.polymorphic(q):
                cons.add("vtable.shape")
            destroy_item = self.add_item("func", self.destroy_name(q),
                                         c["line"], cons)
            ctx = FnCtx(self, destroy_item, q, None)
            lines = [f"static void {self.destroy_name(q)}(void *self_) {{"]
            lines.append(f"    struct {cname(q)} *self = "
                         f"(struct {cname(q)} *)self_;")
            lines.append("    (void)self;")
            body = []
            dt = c.get("dtor")
            if dt is not None and dt["body"] != "default":
                body += self._emit_stmts(ctx, dt["body"], 1)
            for fname, ftype in reversed(sem.layout_fields(q)):
                owner, _f = sem.find_field(q, fname)
                if owner != q:
                    continue  # base members die with the base destroy
                if is_class_t(ftype) and sem.needs_destroy(class_qname(ftype)):
                    fq = class_qname(ftype)
                    ctx.dep_func(self.destroy_name(fq))
                    body.append(f"    {self.destroy_name(fq)}(&self->{fname});")
            if c.get("base") and sem.needs_destroy(c["base"]):
                ctx.dep_func(self.destroy_name(c["base"]))
                body.append(f"    {self.destroy_name(c['base'])}((void *)self);")
            lines += body
            lines.append("}")
            destroy_item.lines = lines
            destroy_item.dep_keys.add(f"struct:{cname(q)}")
            items["destroy"] = destroy_item
        init_items = []
        for ct in c["ctors"]:
            cons = set(base_cons) | {"ctor-dtor.explicit", "layout.struct"}
            if sem.polymorphic(q):
                cons.add("vtable.shape")
            if has_ref(ct["params"]):
                cons.add("reference.pointer")
            it = self.add_item("func", self.ctor_cname(q, ct), c["line"], cons)
            init_items.append(it)
            self._emit_ctor(c, ct, it)
        if not c["ctors"] and sem.needs_init(q):
            cons = set(base_cons) | {"ctor-dtor.explicit", "layout.struct"}
            if sem.polymorphic(q):
                cons.add("vtable.shape")
            it = self.add_item("func", self.default_init_name(q), c["line"], cons)
            init_items.append(it)
            self._emit_ctor(c, {"params": [], "inits": [], "body": []}, it)
        items["inits"] = init_items
        for m in c["methods"]:
            if m.get("body") is None:
                continue
            cons = set(base_cons) | {"layout.struct"}
            if not m.get("static"):
                cons.add("calling.explicit-this")
            if m.get("virtual"):
                cons.add("vtable.shape")
            if has_ref(m["params"]) or strip_const(m["ret"])[0] == "ref":
                cons.add("reference.pointer")
            if is_class_t(m["ret"]):
                cons.add("calling.byvalue-out")
            it = self.add_item("func", self.method_cname(q, m),
                               m.get("line", c["line"]), cons)
            self._emit_method(c, m, it)
        if sem.polymorphic(q) and not sem.abstract(q):
            cons = set(base_cons) | {"vtable.shape"}
            it = self.add_item("object", self.vtable_inst_name(q), c["line"], cons)
            vt = self.vtable_struct_type(q)
            entries = []
            for i, s in enumerate(sem.slots(q)):
                if i == 0:
                    entries.append(self.destroy_name(q))
                    it.dep_keys.add(f"func:{self.destroy_name(q)}")
                else:
                    owner = s["owner"]
                    _oq, mm = self._find_slot_method(owner, s)
                    entries.append(self.method_cname(owner, mm))
                    it.dep_keys.add(f"func:{self.method_cname(owner, mm)}")
            it.lines = [f"static const struct {vt} {self.vtable_inst_name(q)} = {{",
                        "    " + ", ".join(entries), "};"]
            items["vtable"] = it
        for f in c["fields"]:
            if f.get("static") and f.get("defined"):
                if is_class_t(f["type"]):
                    raise Refuse("global-class-object",
                                 f"static member {cname(q)}_{f['name']} has "
                                 f"class type; v1 has no global ctor mechanism")
                cons = set(base_cons) | {"layout.struct"}
                it = self.add_item("object", f"{cname(q)}_{f['name']}",
                                   c["line"], cons)
                ctx = FnCtx(self, it, None, None)
                init = ""
                if f.get("init") is not None:
                    init = " = " + ctx.expr(f["init"])
                it.lines = [f"{render_type(f['type'], cname(q) + '_' + f['name'])}{init};"]

    def _find_slot_method(self, owner_q, slot):
        c = self.sem.cls(owner_q)
        for m in c["methods"]:
            if self.sem._sig(m) == self.sem._sig(slot):
                return owner_q, m
        raise AssertionError("slot method not found")

    def _emit_ctor(self, c, ct, item):
        q = c["qname"]
        sem = self.sem
        ctx = FnCtx(self, item, q, None)
        params = ", ".join([f"struct {cname(q)} *self"] +
                           [render_type(p[1], p[0]) for p in ct["params"]])
        lines = [f"static void {item.name}({params}) {{"]
        body = []
        base = c.get("base")
        inits = {}
        for i in ct["inits"]:
            inits["base" if i[0] == "base" else i[1]] = i
        if base and sem.needs_init(base):
            args = inits["base"][2] if "base" in inits else []
            body += ctx.materialize_args(args, 1)
            bct = sem.pick_ctor_for(base, args)
            if bct is not None:
                bn = self.ctor_cname(base, bct)
                full = self._fill_args(ctx, bct["params"], args)
            elif args or sem.cls(base)["ctors"]:
                raise Refuse("no-matching-constructor",
                             f"base {cname(base)} of {cname(q)} with "
                             f"{len(args)} init argument(s)")
            else:
                bn = self.default_init_name(base)
                full = []
            ctx.dep_func(bn)
            argstr = ", ".join(["(void *)self"] + full)
            body.append(f"    {bn}({argstr});")
            body += ctx.cleanup_temps(1)
        if sem.polymorphic(q):
            if sem.abstract(q):
                body.append("    self->vptr = 0;")
            else:
                body.append(f"    self->vptr = &{self.vtable_inst_name(q)};")
                item.dep_keys.add(f"object:{self.vtable_inst_name(q)}")
        init_exprs = {i[1]: i[2] for i in ct["inits"] if i[0] == "field"}
        for fname, ftype in sem.layout_fields(q):
            owner, _f = sem.find_field(q, fname)
            if owner != q:
                continue  # base members initialised by the base ctor
            if fname in init_exprs:
                args = init_exprs[fname]
                if is_class_t(ftype):
                    fq = class_qname(ftype)
                    if sem.is_copy_init(fq, args):
                        body += ctx.assign_to(args[0],
                                              f"self->{fname}", 1)
                        continue
                    body += ctx.materialize_args(args, 1)
                    fct = sem.pick_ctor_for(fq, args)
                    if fct is None:
                        if sem.cls(fq)["ctors"]:
                            raise Refuse("no-matching-constructor",
                                         f"member {fname} of {cname(q)}")
                        # ctor-less member class: one argument is a
                        # copy-init; none is default-init.
                        if len(args) == 1:
                            body += ctx.assign_to(args[0],
                                                  f"self->{fname}", 1)
                        elif len(args) == 0 and sem.needs_init(fq):
                            fn = self.default_init_name(fq)
                            ctx.dep_func(fn)
                            body.append(f"    {fn}(&self->{fname});")
                        else:
                            raise Refuse("no-matching-constructor",
                                         f"member {fname} of {cname(q)}")
                    else:
                        fn = self.ctor_cname(fq, fct)
                        ctx.dep_func(fn)
                        full = self._fill_args(ctx, fct["params"], args)
                        argstr = ", ".join([f"&self->{fname}"] + full)
                        body.append(f"    {fn}({argstr});")
                    body += ctx.cleanup_temps(1)
                else:
                    if len(args) != 1:
                        raise Refuse("no-matching-constructor",
                                     f"scalar member {fname} of {cname(q)} "
                                     f"takes exactly one init argument")
                    body.append(f"    self->{fname} = {ctx.expr(args[0])};")
            elif is_class_t(ftype) and sem.needs_init(class_qname(ftype)):
                fq = class_qname(ftype)
                fct = sem.default_ctor(fq)
                if fct is None and sem.cls(fq)["ctors"]:
                    raise Refuse("no-matching-constructor",
                                 f"member {fname} of {cname(q)}: "
                                 f"{cname(fq)} has no default ctor")
                fn = self.ctor_cname(fq, fct) if fct else self.default_init_name(fq)
                ctx.dep_func(fn)
                body.append(f"    {fn}(&self->{fname});")
        body += self._emit_stmts(ctx, ct["body"], 1)
        lines += body
        lines.append("}")
        item.lines = lines
        item.dep_keys.add(f"struct:{cname(q)}")

    def _fill_args(self, ctx, params, args):
        out = []
        for i, p in enumerate(params):
            if i < len(args):
                out.append(ctx.conv(args[i], p[1]))
            else:
                out.append(ctx.expr(p[2]))
        return out

    def _emit_method(self, c, m, item):
        q = c["qname"]
        ctx = FnCtx(self, item, q, m)
        ret = m["ret"]
        params = []
        prologue = []
        if not m.get("static"):
            # A method occupying a vtable slot takes void *self_ and
            # casts to the concrete type on entry (the hand slice's
            # SS3 convention) so slot entries type-check exactly.
            occupies_slot = bool(m.get("virtual")) or (
                self.sem.polymorphic(q) and any(
                    s["owner"] == q and Semantics._sig(s) ==
                    Semantics._sig(m)
                    for s in self.sem.slots(q) if s["name"] != "~dtor"))
            if occupies_slot:
                voidp = "const void *self_" if m.get("const") \
                    else "void *self_"
                params.append(voidp)
                cself = "const " if m.get("const") else ""
                prologue.append(
                    f"    {cself}struct {cname(q)} *self = "
                    f"({cself}struct {cname(q)} *)self_;")
                prologue.append("    (void)self;")
            else:
                selft = ("ptr", ("const", ("class", q))) if m.get("const") \
                    else ("ptr", ("class", q))
                params.append(render_type(selft, "self"))
        params += [render_type(p[1], p[0]) for p in m["params"]]
        if is_class_t(ret):
            params.insert(0, f"struct {cname(class_qname(ret))} *__ret")
            retc = "void"
        else:
            retc = render_type(ret, "")
        lines = [f"static {retc} {item.name}({', '.join(params) or 'void'}) {{"]
        lines += prologue
        lines += self._emit_stmts(ctx, m["body"], 1, ret_type=ret)
        lines.append("}")
        item.lines = lines
        item.dep_keys.add(f"struct:{cname(q)}")

    # ---------- free functions / globals ----------
    def _emit_func(self, f):
        cons = {"mangling.readable"}
        if len(f["qname"]) > 1:
            cons.add("namespace.flatten")
        if f.get("template_inst"):
            cons.add("template.instantiation")
        if f.get("lambda"):
            cons.add("lambda.closureless")
        if has_ref(f["params"]) or strip_const(f["ret"])[0] == "ref":
            cons.add("reference.pointer")
        if is_class_t(f["ret"]):
            cons.add("calling.byvalue-out")
        it = self.add_item("func", self.func_cname(f), f.get("line", 1), cons)
        ctx = FnCtx(self, it, None, f)
        ret = f["ret"]
        params = [render_type(p[1], p[0]) for p in f["params"]]
        if is_class_t(ret):
            params.insert(0, f"struct {cname(class_qname(ret))} *__ret")
            retc = "void"
        elif f["qname"] == ("main",):
            retc = "int"
        else:
            retc = render_type(ret, "")
        static = "" if f["qname"] == ("main",) else "static "
        lines = [f"{static}{retc} {it.name}({', '.join(params) or 'void'}) {{"]
        lines += self._emit_stmts(ctx, f["body"], 1, ret_type=ret)
        lines.append("}")
        it.lines = lines

    def _emit_global(self, g):
        if is_class_t(g["type"]):
            raise Refuse("global-class-object",
                         f"global {cname(g['qname'])} has class type; "
                         f"v1 has no global ctor mechanism")
        cons = {"mangling.readable"}
        if len(g["qname"]) > 1:
            cons.add("namespace.flatten")
        it = self.add_item("object", cname(g["qname"]), g.get("line", 1), cons)
        ctx = FnCtx(self, it, None, None)
        init = ""
        if g.get("init") is not None:
            init = " = " + ctx.expr(g["init"])
        it.lines = [f"{render_type(g['type'], cname(g['qname']))}{init};"]

    def _emit_new_delete_helpers(self):
        for q in sorted(self.used_new):
            c = self.sem.cls(q)
            for nargs in sorted(self.used_new[q]):
                ct = self.sem.pick_ctor(q, nargs)
                if ct is None and (nargs or c["ctors"]):
                    raise Refuse("no-matching-constructor",
                                 f"new {cname(q)} with {nargs} args")
                fn = self.ctor_cname(q, ct) if ct else self.default_init_name(q)
                cons = {"mangling.readable", "layout.struct"}
                if len(q) > 1:
                    cons.add("namespace.flatten")
                it = self.add_item("func", f"__new_{cname(q)}_{nargs}",
                                   c["line"], cons)
                params = [render_type(p[1], p[0]) for p in ct["params"]] if ct else []
                fwd = ", ".join(p[0] for p in ct["params"]) if ct else ""
                call = f"{fn}(__p" + (f", {fwd}" if fwd else "") + ");"
                it.lines = [
                    f"static struct {cname(q)} *__new_{cname(q)}_{nargs}"
                    f"({', '.join(params) or 'void'}) {{",
                    f"    struct {cname(q)} *__p = "
                    f"(struct {cname(q)} *)malloc(sizeof(struct {cname(q)}));",
                    f"    {call}",
                    "    return __p;",
                    "}"]
                it.dep_keys.add(f"struct:{cname(q)}")
                it.dep_keys.add(f"func:{fn}")
        for q in sorted(self.used_delete):
            cons = {"mangling.readable", "layout.struct"}
            if len(q) > 1:
                cons.add("namespace.flatten")
            if self.sem.polymorphic(q):
                cons.add("vtable.shape")
            it = self.add_item("func", f"__delete_{cname(q)}",
                               self.sem.cls(q)["line"], cons)
            if self.sem.polymorphic(q):
                body = ["    if (__p) {",
                        f"        ((struct {cname(q)} *)__p)->vptr->destroy(__p);",
                        "        free(__p);",
                        "    }"]
            elif self.sem.needs_destroy(q):
                body = ["    if (__p) {",
                        f"        {self.destroy_name(q)}(__p);",
                        "        free(__p);",
                        "    }"]
                it.dep_keys.add(f"func:{self.destroy_name(q)}")
            else:
                body = ["    free(__p);"]
            it.lines = [f"static void __delete_{cname(q)}(void *__p) {{"] + \
                body + ["}"]
            it.dep_keys.add(f"struct:{cname(q)}")

    # ---------- statements ----------
    def _emit_stmts(self, ctx, stmts, indent, ret_type=None):
        out = []
        pad = "    " * indent
        ctx.push_scope()
        for s in stmts:
            out += self._emit_stmt(ctx, s, indent, ret_type)
        out += ctx.pop_scope_destroy_lines(indent)
        return out

    def _destroy_lines(self, ctx, entries, indent):
        pad = "    " * indent
        out = []
        for name, q, count in entries:
            dn = self.destroy_name(q)
            ctx.dep_func(dn)
            if count is None:
                out.append(f"{pad}{dn}(&{name});")
            else:
                out.append(f"{pad}{{ int __di; for (__di = {count} - 1; "
                           f"__di >= 0; __di--) {dn}(&{name}[__di]); }}")
        return out

    def _emit_stmt(self, ctx, s, indent, ret_type):
        pad = "    " * indent
        k = s["k"]
        if k == "group":
            lines = []
            for sub in s["ss"]:
                lines += self._emit_stmt(ctx, sub, indent, ret_type)
            return lines
        if k == "block":
            lines = [f"{pad}{{"]
            lines += self._emit_stmts(ctx, s["ss"], indent + 1, ret_type)
            lines.append(f"{pad}}}")
            return lines
        if k == "decl":
            return self._emit_decl(ctx, s, indent)
        if k == "expr":
            return ctx.full_expr(s["e"], indent)
        if k == "return":
            lines = self._destroy_lines(ctx, ctx.all_live_destroys(), indent)
            e = s.get("e")
            if e is None:
                lines.append(f"{pad}return;")
                return lines
            if ret_type is not None and is_class_t(ret_type):
                lines += ctx.assign_to(e, "__ret", indent, deref=True)
                lines.append(f"{pad}return;")
                return lines
            if ret_type is not None and strip_const(ret_type)[0] == "ref":
                pre = ctx.collect_temps(e, indent)
                lines += pre
                lines.append(f"{pad}return {ctx.expr(e, want_addr=True)};")
                lines += ctx.cleanup_temps(indent)
                return lines
            lines += ctx.full_expr(e, indent, prefix="return ", suffix=";",
                                   target=ret_type)
            return lines
        if k == "if":
            lines = ctx.full_expr(s["c"], indent, prefix="if (", suffix=") {",
                                  cond=True)
            lines += self._emit_stmts(ctx, s["t"], indent + 1, ret_type)
            if s.get("e"):
                lines.append(f"{pad}}} else {{")
                lines += self._emit_stmts(ctx, s["e"], indent + 1, ret_type)
            lines.append(f"{pad}}}")
            return lines
        if k == "while":
            lines = ctx.full_expr(s["c"], indent, prefix="while (", suffix=") {",
                                  cond=True)
            ctx.push_loop_mark()
            lines += self._emit_stmts(ctx, s["b"], indent + 1, ret_type)
            ctx.pop_loop_mark()
            lines.append(f"{pad}}}")
            return lines
        if k == "dowhile":
            lines = [f"{pad}do {{"]
            ctx.push_loop_mark()
            lines += self._emit_stmts(ctx, s["b"], indent + 1, ret_type)
            ctx.pop_loop_mark()
            lines += ctx.full_expr(s["c"], indent, prefix="} while (",
                                   suffix=");", cond=True)
            return lines
        if k == "for":
            lines = []
            ctx.push_scope()
            if s.get("init"):
                lines += self._emit_stmt(ctx, s["init"], indent, ret_type)
            for part in (s.get("c"), s.get("inc")):
                if part is not None:
                    acc = []
                    ctx._walk_temps(part, acc)
                    if acc:
                        raise Refuse("temporary-in-condition",
                                     "v1 subset: temporaries may not "
                                     "appear in if/while/for conditions")
            cond = ctx.expr(s["c"]) if s.get("c") else ""
            inc = ctx.expr(s["inc"]) if s.get("inc") else ""
            lines.append(f"{pad}for (; {cond}; {inc}) {{")
            ctx.push_loop_mark()
            lines += self._emit_stmts(ctx, s["b"], indent + 1, ret_type)
            ctx.pop_loop_mark()
            lines.append(f"{pad}}}")
            lines += self._destroy_lines(ctx, ctx.pop_scope_entries(), indent)
            return lines
        if k == "rangefor":
            rt = s["range"]["t"]
            elem = rt[1]
            lines = [f"{pad}{{ int __ri;",
                     f"{pad}for (__ri = 0; __ri < {rt[2]}; __ri++) {{"]
            ctx.push_loop_mark()
            ctx.push_scope()
            declared = strip_const(s["type"]) if s.get("type") else elem
            if declared[0] == "ref":
                # a reference loop variable binds to the element
                # itself: a pointer in C, no copy, no destruction
                lines.append(
                    f"{'    ' * (indent + 1)}"
                    f"{render_type(T_ptr(elem), s['name'])} = "
                    f"&{ctx.expr(s['range'])}[__ri];")
            else:
                decl = {"k": "decl", "name": s["name"], "type": elem,
                        "init": {"form": "expr",
                                 "e": {"k": "index", "t": elem,
                                       "a": s["range"],
                                       "i": {"k": "var",
                                             "t": T_builtin("int"),
                                             "name": "__ri"}}}}
                lines += self._emit_stmt(ctx, decl, indent + 1, ret_type)
            for st in s["b"]:
                lines += self._emit_stmt(ctx, st, indent + 1, ret_type)
            lines += self._destroy_lines(ctx, ctx.pop_scope_entries(),
                                         indent + 1)
            ctx.pop_loop_mark()
            lines.append(f"{pad}}}")
            lines.append(f"{pad}}}")
            return lines
        if k == "break":
            lines = self._destroy_lines(ctx, ctx.loop_destroys(), indent)
            lines.append(f"{pad}break;")
            return lines
        if k == "continue":
            lines = self._destroy_lines(ctx, ctx.loop_destroys(), indent)
            lines.append(f"{pad}continue;")
            return lines
        raise AssertionError(f"stmt kind {k}")

    def _emit_decl(self, ctx, s, indent):
        pad = "    " * indent
        t = s["type"]
        name = s["name"]
        init = s.get("init")
        lines = []
        bt = base_type(t)
        if bt[0] == "array" and is_class_t(bt[1]):
            q = class_qname(bt[1])
            lines.append(f"{pad}{render_type(t, name)};")
            if init is not None and init["form"] == "list":
                items = init["e"]["items"]
                for idx, ie in enumerate(items):
                    if ie["k"] == "construct" and \
                            not self.sem.is_copy_init(q, ie["args"]):
                        fn = self.ctor_cname(
                            q, self.sem.pick_ctor_for(q, ie["args"]))
                        ctx.dep_func(fn)
                        argstr = ", ".join([f"&{name}[{idx}]"] +
                                           [ctx.expr(a) for a in ie["args"]])
                        lines.append(f"{pad}{fn}({argstr});")
                    else:
                        lines += ctx.assign_to(ie, f"{name}[{idx}]", indent)
            elif self.sem.needs_init(q):
                fct = self.sem.default_ctor(q)
                if fct is None and self.sem.cls(q)["ctors"]:
                    raise Refuse("no-matching-constructor",
                                 f"array of {cname(q)} needs a default ctor")
                fn = self.ctor_cname(q, fct) if fct else self.default_init_name(q)
                ctx.dep_func(fn)
                lines.append(f"{pad}{{ int __ai; for (__ai = 0; __ai < {bt[2]}; "
                             f"__ai++) {fn}(&{name}[__ai]); }}")
            if self.sem.needs_destroy(q):
                ctx.register_destroy(name, q, bt[2])
            return lines
        if is_class_t(t):
            q = class_qname(t)
            lines.append(f"{pad}{render_type(t, name)};")
            if init is not None and init["form"] == "ctor" and \
                    self.sem.is_copy_init(q, init["args"]):
                lines += ctx.assign_to(init["args"][0], name, indent)
                if self.sem.needs_destroy(q):
                    ctx.register_destroy(name, q, None)
                return lines
            if init is not None and init["form"] == "ctor":
                args = init["args"]
                fct = self.sem.pick_ctor_for(q, args)
                decl_idx = len(lines) - 1
                lines += ctx.materialize_args(args, indent)
                if fct is None:
                    if self.sem.cls(q)["ctors"]:
                        raise Refuse("no-matching-constructor",
                                     f"{cname(q)} with {len(args)} args")
                    lines[decl_idx] = (
                        f"{pad}{render_type(t, name)} = "
                        f"{ctx.aggregate_init(q, args, indent)};")
                    lines += ctx.cleanup_temps(indent)
                    if self.sem.needs_destroy(q):
                        ctx.register_destroy(name, q, None)
                    return lines
                fn = self.ctor_cname(q, fct)
                ctx.dep_func(fn)
                full = self._fill_args(ctx, fct["params"], args)
                argstr = ", ".join([f"&{name}"] + full)
                lines.append(f"{pad}{fn}({argstr});")
                lines += ctx.cleanup_temps(indent)
            elif init is not None and init["form"] == "list" and \
                    (self.sem.cls(q)["ctors"] or self.sem.needs_init(q)):
                # brace-init of a class with constructors (or an
                # init-needing layout) is construction, not an
                # aggregate assignment
                items = init["e"]["items"]
                fct = self.sem.pick_ctor_for(q, items)
                if fct is None:
                    raise Refuse("no-matching-constructor",
                                 f"{cname(q)} with {len(items)} args")
                lines += ctx.materialize_args(items, indent)
                fn = self.ctor_cname(q, fct)
                ctx.dep_func(fn)
                full = self._fill_args(ctx, fct["params"], items)
                argstr = ", ".join([f"&{name}"] + full)
                lines.append(f"{pad}{fn}({argstr});")
                lines += ctx.cleanup_temps(indent)
            elif init is not None and init["form"] == "list":
                initc = ctx.expr(init["e"])
                lines[-1] = f"{pad}{render_type(t, name)} = {initc};"
            elif init is not None and init["form"] == "expr":
                lines += ctx.assign_to(init["e"], name, indent)
            elif self.sem.needs_init(q):
                fct = self.sem.default_ctor(q)
                if fct is None and self.sem.cls(q)["ctors"]:
                    raise Refuse("no-matching-constructor",
                                 f"{cname(q)} has no default ctor")
                fn = self.ctor_cname(q, fct) if fct else self.default_init_name(q)
                ctx.dep_func(fn)
                lines.append(f"{pad}{fn}(&{name});")
            if self.sem.needs_destroy(q):
                ctx.register_destroy(name, q, None)
            return lines
        if init is not None and init["form"] == "expr":
            e = init["e"]
            if strip_const(t)[0] == "ref":
                lines.append(f"{pad}{render_type(t, name)} = "
                             f"{ctx.expr(e, want_addr=True)};")
            elif e["k"] == "lambda":
                fn = ctx.lambda_func(e)
                lines.append(f"{pad}{render_type(t, name)} = {fn};")
            else:
                lines += ctx.collect_temps(e, indent)
                lines.append(f"{pad}{render_type(t, name)} = {ctx.expr(e)};")
                lines += ctx.cleanup_temps(indent)
            return lines
        if init is not None and init["form"] == "list":
            lines.append(f"{pad}{render_type(t, name)} = {ctx.expr(init['e'])};")
            return lines
        lines.append(f"{pad}{render_type(t, name)};")
        return lines

# ------------------------------------------------------------ function context
class FnCtx:
    def __init__(self, em, item, class_q, func):
        self.em = em
        self.sem = em.sem
        self.item = item
        self.class_q = class_q
        self.func = func
        self.scopes = []        # list of [(name, qname, count)]
        self.loop_marks = []    # scope-stack depths at loop-body entry
        self.temp_counter = 0
        self.temps = []

    def dep_func(self, c_name):
        self.item.dep_keys.add(f"func:{c_name}")

    def dep_object(self, c_name):
        self.item.dep_keys.add(f"object:{c_name}")

    # ----- destruction bookkeeping -----
    def push_scope(self):
        self.scopes.append([])

    def register_destroy(self, name, q, count):
        self.scopes[-1].append((name, q, count))

    def pop_scope_entries(self):
        return list(reversed(self.scopes.pop()))

    def pop_scope_destroy_lines(self, indent):
        entries = self.pop_scope_entries()
        return self.em._destroy_lines(self, entries, indent)

    def all_live_destroys(self):
        out = []
        for sc in reversed(self.scopes):
            out += list(reversed(sc))
        return out

    def push_loop_mark(self):
        self.loop_marks.append(len(self.scopes))

    def pop_loop_mark(self):
        self.loop_marks.pop()

    def loop_destroys(self):
        if not self.loop_marks:
            return []
        mark = self.loop_marks[-1]
        out = []
        for sc in reversed(self.scopes[mark:]):
            out += list(reversed(sc))
        return out

    # ----- temporaries -----
    def _is_temp(self, e):
        return e["k"] == "construct" or \
            (e["k"] == "call" and is_class_t(e["t"]))

    def _walk_temps(self, e, acc):
        if self._is_temp(e):
            acc.append(e)
            return
        for v in e.values():
            if isinstance(v, dict) and "k" in v:
                self._walk_temps(v, acc)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, dict) and "k" in x:
                        self._walk_temps(x, acc)

    def collect_temps(self, e, indent=0):
        acc = []
        self._walk_temps(e, acc)
        lines = []
        self.temps = []
        for te in acc:
            lines += self._materialize(te, indent)
        return lines

    def _materialize(self, te, indent):
        pad = "    " * indent
        lines = []
        inner = []
        for a in (te["args"] if te["k"] == "construct" else te["args"]):
            self._walk_temps(a, inner)
        for ie in inner:
            lines += self._materialize(ie, indent)
        n = self.temp_counter
        self.temp_counter += 1
        nm = f"__tmp{n}"
        te["_tmp"] = nm
        q = class_qname(te["t"])
        lines.append(f"{pad}struct {cname(q)} {nm};")
        if te["k"] == "construct":
            fct = self.sem.pick_ctor_for(q, te["args"])
            if self.sem.is_copy_init(q, te["args"]):
                lines += self.assign_to(te["args"][0], nm, indent)
            elif fct is None:
                if self.sem.cls(q)["ctors"]:
                    raise Refuse("no-matching-constructor",
                                 f"temporary of {cname(q)}")
                lines[-1] = (f"{pad}struct {cname(q)} {nm} = "
                             f"{self.aggregate_init(q, te['args'], indent)};")
            else:
                fn = self.em.ctor_cname(q, fct)
                self.dep_func(fn)
                full = self.em._fill_args(self, fct["params"], te["args"])
                argstr = ", ".join([f"&{nm}"] + full)
                lines.append(f"{pad}{fn}({argstr});")
        else:
            lines.append(f"{pad}{self._call_str(te, target=f'&{nm}')};")
        if self.sem.needs_destroy(q):
            self.temps.append((nm, q))
        return lines

    def cleanup_temps(self, indent):
        pad = "    " * indent
        lines = []
        for nm, q in reversed(self.temps):
            dn = self.em.destroy_name(q)
            self.dep_func(dn)
            lines.append(f"{pad}{dn}(&{nm});")
        self.temps = []
        return lines

    def full_expr(self, e, indent, prefix="", suffix=";", cond=False,
                  target=None):
        if cond:
            acc = []
            self._walk_temps(e, acc)
            if acc:
                raise Refuse("temporary-in-condition",
                             "v1 subset: temporaries may not appear in "
                             "if/while/for conditions")
        lines = self.collect_temps(e, indent)
        pad = "    " * indent
        lines.append(f"{pad}{prefix}{self.conv(e, target)}{suffix}")
        lines += self.cleanup_temps(indent)
        return lines

    def materialize_args(self, args, indent):
        """Materialise temporaries nested in an argument list;
        pair with cleanup_temps after the consuming call."""
        lines = []
        self.temps = []
        acc = []
        for a in args:
            self._walk_temps(a, acc)
        for te in acc:
            lines += self._materialize(te, indent)
        return lines

    def collect_arg_temps(self, e, indent):
        """Materialise temporaries nested in e's arguments (e itself
        is being constructed/called into a target, so it is not
        materialised)."""
        return self.materialize_args(e.get("args", []), indent)

    def aggregate_init(self, q, args, indent):
        """A construct of a class with NO declared constructors is
        aggregate initialisation: field values in layout order."""
        vals = [self.conv(a, ftype)
                for a, (_fn, ftype) in
                zip(args, self.sem.layout_fields(q))]
        if len(args) > len(vals):
            raise Refuse("no-matching-constructor",
                         f"too many aggregate elements for {cname(q)}")
        return "{ " + (", ".join(vals) if vals else "0") + " }"

    def assign_to(self, e, name, indent, deref=False):
        pad = "    " * indent
        tgt = f"*{name}" if deref else name
        addr = name if deref else f"&{name}"
        if e["k"] == "construct":
            q = class_qname(e["t"])
            if self.sem.is_copy_init(q, e["args"]):
                return self.assign_to(e["args"][0], name, indent, deref)
            fct = self.sem.pick_ctor_for(q, e["args"])
            lines = self.collect_arg_temps(e, indent)
            if fct is None:
                if self.sem.cls(q)["ctors"]:
                    raise Refuse("no-matching-constructor",
                                 f"construction of {cname(q)}")
                lines.append(f"{pad}{tgt} = "
                             f"{self.aggregate_init(q, e['args'], indent)};")
                lines += self.cleanup_temps(indent)
                return lines
            fn = self.em.ctor_cname(q, fct)
            self.dep_func(fn)
            full = self.em._fill_args(self, fct["params"], e["args"])
            argstr = ", ".join([addr] + full)
            lines.append(f"{pad}{fn}({argstr});")
            lines += self.cleanup_temps(indent)
            return lines
        if e["k"] == "call" and is_class_t(e["t"]):
            lines = self.collect_arg_temps(e, indent)
            lines.append(f"{pad}{self._call_str(e, target=addr)};")
            lines += self.cleanup_temps(indent)
            return lines
        lines = self.collect_temps(e, indent)
        lines.append(f"{pad}{tgt} = {self.expr(e)};")
        lines += self.cleanup_temps(indent)
        return lines

    def lambda_func(self, e):
        for f in self.em.tu.funcs:
            if id(f) == e.get("_func_id"):
                fn = self.em.func_cname(f)
                self.dep_func(fn)
                return fn
        raise AssertionError("lambda func not registered")

    # ----- expressions -----
    def expr(self, e, want_addr=False):
        if "_tmp" in e:
            return f"&{e['_tmp']}" if want_addr else e["_tmp"]
        k = e["k"]
        if k in ("int", "float", "str", "char"):
            return e["v"]
        if k == "bool":
            return "1" if e["v"] else "0"
        if k == "nullptr":
            return "((void *)0)"
        if k == "var":
            if e.get("ref"):
                return e["name"] if want_addr else f"(*{e['name']})"
            return f"&{e['name']}" if want_addr else e["name"]
        if k == "gvar":
            return f"&{cname(e['qname'])}" if want_addr else cname(e["qname"])
        if k == "smember":
            nm = f"{cname(e['class'])}_{e['name']}"
            return f"&{nm}" if want_addr else nm
        if k == "this":
            return "self"
        if k == "member":
            return self._member(e, want_addr)
        if k == "index":
            a = self.expr(e["a"])
            return f"{a}[{self.expr(e['i'])}]"
        if k == "call":
            return self._call_str(e)
        if k == "bin":
            return f"({self.expr(e['l'])} {e['op']} {self.expr(e['r'])})"
        if k == "un":
            op = e["op"]
            if op == "addr":
                return self.expr(e["e"], want_addr=True)
            if op == "deref":
                return f"(*{self.expr(e['e'])})"
            if e.get("post"):
                return f"({self.expr(e['e'])}{op})"
            return f"({op}{self.expr(e['e'])})"
        if k == "assign":
            if e["op"] == "=":
                return (f"({self.expr(e['l'])} = "
                        f"{self.conv(e['r'], e['l']['t'])})")
            return f"({self.expr(e['l'])} {e['op']} {self.expr(e['r'])})"
        if k == "cond":
            if want_addr:
                return (f"({self.expr(e['c'])} ? "
                        f"{self.expr(e['a'], want_addr=True)} : "
                        f"{self.expr(e['b'], want_addr=True)})")
            return (f"({self.expr(e['c'])} ? {self.expr(e['a'])} : "
                    f"{self.expr(e['b'])})")
        if k == "cast":
            return f"({render_type(e['type'], '')})({self.expr(e['e'])})"
        if k == "sizeof":
            return f"sizeof({render_type(e['type'], '')})"
        if k == "new":
            q = class_qname(e["type"])
            self.em.used_new.setdefault(q, set()).add(len(e["args"]))
            self.em.tu.uses_new = True
            argstr = ", ".join(self.expr(a) for a in e["args"])
            return f"__new_{cname(q)}_{len(e['args'])}({argstr})"
        if k == "delete":
            t = base_type(e["e"]["t"])
            if t[0] == "ptr":
                t = base_type(t[1])
            q = class_qname(t)
            self.em.used_delete.add(q)
            self.em.tu.uses_new = True
            return f"__delete_{cname(q)}({self.expr(e['e'])})"
        if k == "construct":
            if "_tmp" in e:
                return e["_tmp"]
            raise AssertionError("construct expr outside temp context")
        if k == "initlist":
            lt = strip_const(e["t"]) if e.get("t") else None
            if lt is not None and lt[0] == "array":
                return "{ " + ", ".join(self.conv(x, lt[1])
                                         for x in e["items"]) + " }"
            return "{ " + ", ".join(self.expr(x) for x in e["items"]) + " }"
        if k == "lambda":
            return self.lambda_func(e)
        raise AssertionError(f"expr kind {k}")

    def _member(self, e, want_addr):
        o = e["o"]
        if o["k"] == "this":
            core = f"self->{e['f']}"
            return f"&{core}" if want_addr else core
        ot = strip_const(o["t"])
        if ot[0] == "class":
            core = f"{self.expr(o)}.{e['f']}"
        elif ot[0] == "ptr":
            core = f"{self.expr(o)}->{e['f']}"
        elif ot[0] == "ref":
            core = f"{self.expr(o, want_addr=True)}->{e['f']}"
        elif ot[0] == "array":
            core = f"{self.expr(o)}[0].{e['f']}"
        else:
            raise AssertionError(f"member on {ot}")
        return f"&({core})" if want_addr else core

    def _obj_ptr(self, o):
        ot = strip_const(o["t"])
        if ot[0] == "class":
            if o["k"] == "this":
                return "self"
            return self.expr(o, want_addr=True)
        return self.expr(o)

    def _call_str(self, e, target=None):
        callee = e["callee"]
        args = e["args"]
        if callee[0] == "free":
            q = callee[1]
            if q == ("printf",):
                argstr = ", ".join(self.expr(a) for a in args)
                return f"printf({argstr})"
            f = callee[2]
            fn = self.em.func_cname(f)
            self.dep_func(fn)
            parts = []
            if is_class_t(f["ret"]):
                parts.append(target if target is not None else "&__unused_ret")
            parts += self._call_args(f["params"], args)
            return f"{fn}({', '.join(parts)})"
        if callee[0] == "method":
            _, o, owner, m, dispatch = callee
            parts = []
            if is_class_t(m["ret"]):
                parts.append(target if target is not None else "&__unused_ret")
            if dispatch:
                self.item.constructs.add("vtable.shape")
                slot_obj = self._obj_ptr(o)
                parts.append(slot_obj)
                parts += self._call_args(m["params"], args)
                return f"({slot_obj})->vptr->{m['name']}({', '.join(parts)})"
            fn = self.em.method_cname(owner, m)
            self.dep_func(fn)
            if not m.get("static"):
                op = self._obj_ptr(o)
                ot = strip_const(o["t"])
                if ot[0] == "ref":
                    ot = strip_const(ot[1])
                if ot[0] == "ptr":
                    ot = strip_const(ot[1])
                if ot[0] == "class" and ot[1] != tuple(owner):
                    op = f"({render_type(('ptr', T_class(owner)), '')})({op})"
                parts.append(op)
            parts += self._call_args(m["params"], args)
            return f"{fn}({', '.join(parts)})"
        if callee[0] == "smethod":
            _, owner, m = callee
            fn = self.em.method_cname(owner, m)
            self.dep_func(fn)
            parts = []
            if is_class_t(m["ret"]):
                parts.append(target if target is not None
                             else "&__unused_ret")
            parts += self._call_args(m["params"], args)
            return f"{fn}({', '.join(parts)})"
        if callee[0] == "expr":
            fnexpr = self.expr(callee[1])
            parts = [self._arg(a, None) for a in args]
            return f"({fnexpr})({', '.join(parts)})"
        raise AssertionError(callee)

    def _call_args(self, params, args):
        out = []
        for i, p in enumerate(params):
            if i < len(args):
                out.append(self._arg(args[i], p[1]))
            else:
                out.append(self.expr(p[2]))
        return out

    def conv(self, e, target_t):
        """Render e, inserting the (layout-prefix) cast when a
        derived-class pointer flows into a base-class pointer slot —
        the cast the C++ upcast implies, made explicit in C."""
        s = self.expr(e)
        if target_t is None:
            return s
        et, tt = strip_const(e["t"]), strip_const(target_t)
        if et[0] == "ref":
            et = strip_const(et[1])
        if et == tt or tt[0] == "ref":
            return s
        if et[0] == "ptr" and tt[0] == "ptr":
            ei, ti = strip_const(et[1]), strip_const(tt[1])
            if ei != ti and ei[0] == "class" and ti[0] == "class" and \
                    self.sem.is_derived(ei[1], ti[1]):
                return f"({render_type(tt, '')})({s})"
        return s

    def _arg(self, a, ptype):
        if a["k"] == "lambda":
            return self.lambda_func(a)
        if ptype is not None and strip_const(ptype)[0] == "ref":
            if a["k"] == "var" and a.get("ref"):
                return a["name"]
            return self.expr(a, want_addr=True)
        return self.conv(a, ptype)

# ------------------------------------------------------------ ledger
def build_ledger(tu, members, segment_info):
    """members: emitter items in emission order. segment_info: list of
    (coord_str, seg_name, sha256) aligned 1:1 with members."""
    sym_rows = []
    mem_rows = []
    required = set()
    for it, (coord, seg_name, sha) in zip(members, segment_info):
        cons = sorted(it.constructs)
        required |= set(cons)
        mem_rows.append(f"{it.name}\tcoord={coord}\tsegment={seg_name}\t"
                        f"sha256={sha}\tconstructs={','.join(cons)}\t"
                        f"stamp={stamp_of(it.constructs)}")
        if it.kind in ("func", "object"):
            sym_rows.append(f"{it.name}\tmember={coord}\t"
                            f"constructs={','.join(cons)}\t"
                            f"stamp={stamp_of(it.constructs)}")
    registry = ",".join(f"{k}={v}" for k, v in
                        sorted(CONSTRUCT_REVISIONS.items()))
    lines = ["phcpp-ledger 1",
             f"tu: {tu.cpp_name}",
             f"constructs-registry: {registry}",
             "symbols:"]
    lines += sym_rows
    lines.append("members:")
    lines += mem_rows
    lines.append("required: " + ",".join(sorted(required)))
    return "\n".join(lines) + "\n"

def ledger_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()
