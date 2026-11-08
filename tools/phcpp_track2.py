#!/usr/bin/env python3
"""phcpp_track2.py — phcpp front-end v1, Track 2: Clang-AST adapter.

Reads clang 18's JSON AST dump (-Xclang -ast-dump=json) for the same
C++23RA v1 subset Track 1 parses, and builds the SAME shared IR
(tools/phcpp_ir.py), so the emitter, the phext layout and the feature
ledger are byte-identical machinery for both tracks.

Method: index every node by id (with parent links), translate only
main-file declarations, in two phases mirroring Track 1 (signatures
for all classes/functions first, then bodies). clang hands us the
instantiated template specialisations directly; their readable names
are synthesised with the same mangling rules Track 1 uses, so an
instantiation is the same IR class/function on both tracks.

Recorded caveat (DESIGN SS4): clang may pre-fold constructs before
the ledger sees them (e.g. value-returning lambdas arrive with an
inferred return type, so Track 1's explicit-->-type rule cannot be
checked here). The differential corpus is the arbiter of how much
that matters; known asymmetries are reported, not hidden.

Float/char literals are recovered from the source text by offset:
clang's JSON prints APFloat values rounded, which would silently
change constants.
"""
import copy
import json
import re
import subprocess
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from phcpp_ir import (TU, Refuse, T_builtin, T_class, strip_const,
                      base_type, is_class_t, class_qname, Semantics,
                      cname, mangle_type)

WRAPPER_EXPRS = {"ImplicitCastExpr", "MaterializeTemporaryExpr",
                 "CXXBindTemporaryExpr", "ExprWithCleanups", "ParenExpr"}

class ClangTU:
    def __init__(self, ast, src, cpp_name):
        self.ast = ast
        self.src = src
        self.tu = TU(cpp_name)
        self.byid = {}
        self.parent = {}
        self._index(ast, None)
        self.aliases = {}          # qname -> IR type
        self.records = {}          # qname -> record node (definition)
        self.spec_records = {}     # qname -> specialization node
        self.spec_nodes = []       # specialization nodes, unindexed
        self.func_by_id = {}       # decl id -> FuncIR
        self.method_by_id = {}     # decl id -> (owner qname, MethodIR)
        self.ctor_by_id = {}       # decl id -> (owner qname, CtorIR)
        self.global_by_id = {}     # decl id -> GlobalIR
        self.sfield_by_id = {}     # decl id -> (owner qname, field name)
        self.class_ir = {}         # qname -> ClassIR
        self.pending_bodies = []   # (kind, owner_q, obj, node)
        self.lambda_n = 0
        self.cur_class = None
        self.scopes = []           # id -> (name, ir_type, is_ref)

    # ---------------- indexing ----------------
    def _index(self, node, parent):
        if isinstance(node, dict):
            if set(node.keys()) <= {"id", "kind", "name"}:
                # an inline reference stub (referencedDecl & friends):
                # not a tree child — never a parent, never the
                # canonical node for its id
                return
            nid = node.get("id")
            if nid:
                if nid not in self.byid:
                    self.byid[nid] = node
                    self.parent[nid] = parent
            for v in node.values():
                self._index(v, node)
        elif isinstance(node, list):
            for v in node:
                self._index(v, parent)

    def in_main(self, node):
        # The TU is always fed to clang on stdin, so main-file
        # locations carry file "<stdin>"; anything else is foreign.
        loc = node.get("loc") or {}
        if "includedFrom" in loc:
            return False
        if "file" in loc and loc["file"] != "<stdin>":
            return False
        for k in ("spellingLoc", "expansionLoc"):
            sub = loc.get(k)
            if isinstance(sub, dict):
                if "includedFrom" in sub:
                    return False
                if "file" in sub and sub["file"] != "<stdin>":
                    return False
        return "offset" in loc or "line" in loc

    def line_of(self, node):
        loc = node.get("loc") or {}
        if "line" in loc:
            return loc["line"]
        rng = node.get("range") or {}
        b = rng.get("begin") or {}
        if "line" in b:
            return b["line"]
        off = b.get("offset", loc.get("offset"))
        if off is not None:
            # expression nodes (LambdaExpr above all) may carry only
            # an offset; the line is exact because we fed the source
            return self.src.count("\n", 0, off) + 1
        return 1

    def ns_of(self, node):
        parts = []
        # instantiations sit at TU level in the tree but carry their
        # semantic context in parentDeclContextId — start there
        ctx = node.get("parentDeclContextId")
        p = self.byid.get(ctx) if ctx else None
        if p is None:
            p = self.parent.get(node.get("id"))
        while p is not None:
            if p.get("kind") == "NamespaceDecl":
                parts.append(p.get("name"))
            p = self.parent.get(p.get("id"))
        return tuple(reversed([x for x in parts if x]))

    def record_of(self, node):
        p = self.parent.get(node.get("id"))
        while p is not None:
            if p.get("kind") in ("CXXRecordDecl",
                                 "ClassTemplateSpecializationDecl"):
                return p
            p = self.parent.get(p.get("id"))
        return None

    # ---------------- type strings ----------------
    def canon(self, t):
        if t[0] == "const" and t[1][0] == "ptr":
            return t[1]
        if t[0] == "ptr":
            return ("ptr", self.canon(t[1]))
        if t[0] == "ref":
            return ("ref", self.canon(t[1]))
        if t[0] == "array":
            return ("array", self.canon(t[1]), t[2])
        if t[0] == "const":
            return ("const", self.canon(t[1]))
        return t

    def parse_type(self, s):
        s = s.strip()
        s = re.sub(r"\s*noexcept\s*$", "", s)
        s = re.sub(r"\s*noexcept\s*(?=\()", " ", s)
        # array suffix(es): "T [N]"
        m = re.search(r"\[\s*(\d+)\s*\]\s*$", s)
        if m:
            inner = self.parse_type(s[:m.start()])
            return self.canon(("array", strip_const(inner)
                               if inner[0] == "array" else inner,
                               int(m.group(1))))
        # function pointer: "ret (*)(params)" / "ret (*name)(params)"
        m = re.match(r"^(.*)\(\s*\*\s*\)\s*\((.*)\)$", s, re.S)
        if m:
            ret = self.parse_type(m.group(1))
            params = [self.parse_type(a)
                      for a in self.split_types(m.group(2))
                      if a.strip() and a.strip() != "void"]
            return self.canon(("fnptr", ret, tuple(params)))
        if s.startswith("const "):
            # const binds to the base type, inside any pointer/ref
            # the rest of the string adds: const char * is a pointer
            # to const char, not a const pointer to char.
            inner = self.parse_type(s[6:])
            return self.canon(self.apply_const(inner))
        if s.endswith(" *"):
            base = self.parse_type(s[:-2])
            return self.canon(("ptr", base))
        if s.endswith("*const"):
            return self.parse_type(s[:-5])
        if s.endswith(" &"):
            return self.canon(("ref", self.parse_type(s[:-2])))
        if s.endswith(" &&"):
            raise Refuse("rvalue-reference",
                         f"type {s}: rvalue references are outside "
                         f"the subset")
        if s.endswith("&") and not s.endswith("&&"):
            return self.canon(("ref", self.parse_type(s[:-1])))
        base = {"void": "void", "bool": "bool", "char": "char",
                "int": "int", "unsigned int": "uint",
                "unsigned": "uint", "long": "long",
                "unsigned long": "long", "float": "float",
                "double": "double", "long int": "long"}
        if s in base:
            return T_builtin(base[s])
        if s in ("short", "unsigned short", "long long",
                 "unsigned long long", "signed char", "unsigned char",
                 "wchar_t", "char16_t", "char32_t"):
            raise Refuse("parse-error",
                         f"type {s} is outside the v1 type set")
        name = s
        if name.startswith("struct "):
            name = name[7:]
        elif name.startswith("class "):
            name = name[6:]
        if name.startswith("std::"):
            raise Refuse("stl-facility",
                         f"{name} is outside the v1 subset")
        if "<" in name:
            base_name, argstr = name[:name.index("<")], \
                name[name.index("<") + 1:name.rindex(">")]
            args = [self.parse_type(a) for a in self.split_types(argstr)]
            parts = tuple(base_name.split("::"))
            iq = parts[:-1] + (parts[-1] + "__" + "_".join(
                mangle_type(a) for a in args),)
            self.ensure_class(iq)
            return T_class(iq)
        parts = tuple(name.split("::"))
        if len(parts) == 1 and parts in self.aliases:
            return self.aliases[parts]
        for cut in range(len(parts), 0, -1):
            cand = parts[:cut]
            if cand in self.aliases:
                return self.aliases[cand]
        if len(parts) == 1:
            hits = [k for k in self.aliases if k[-1] == parts[0]]
            if len(hits) == 1:
                return self.aliases[hits[0]]
            if len(hits) > 1:
                raise Refuse("parse-error",
                             f"ambiguous unqualified alias {s!r}")
        q = parts
        if q in self.records or q in self.spec_records or \
                q in self.class_ir:
            self.ensure_class(q)
            return T_class(q)
        if len(parts) == 1:
            # unqualified name: resolve by unique suffix across the
            # TU (the adapter does not thread the use-site namespace
            # into type parsing; ambiguity refuses loudly)
            hits = [k for k in list(self.records) +
                    list(self.spec_records) + list(self.class_ir)
                    if k[-1] == parts[0]]
            if len(hits) == 1:
                self.ensure_class(hits[0])
                return T_class(hits[0])
            if len(hits) > 1:
                raise Refuse("parse-error",
                             f"ambiguous unqualified type {s!r}")
        raise Refuse("parse-error", f"unknown type {s!r}")

    def apply_const(self, t):
        if t[0] in ("ptr", "ref"):
            return (t[0], self.apply_const(t[1]))
        if t[0] == "const":
            return t
        return ("const", t)

    def split_types(self, s):
        out, depth, cur = [], 0, ""
        for ch in s:
            if ch in "<([":
                depth += 1
            elif ch in ">)]":
                depth -= 1
            if ch == "," and depth == 0:
                out.append(cur)
                cur = ""
            else:
                cur += ch
        if cur.strip():
            out.append(cur)
        return [o.strip() for o in out if o.strip()]

    def fn_type_parts(self, type_str):
        """Split a function type string: ret, [param type strings], const."""
        s = type_str.strip()
        is_const = False
        m = re.search(r"\)\s*(const)?\s*(noexcept)?\s*$", s)
        if m and m.group(1):
            is_const = True
        i = s.index("(")
        ret = s[:i].strip()
        depth = 0
        j = i
        while True:
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        params = self.split_types(s[i + 1:j])
        if params == ["void"]:
            params = []
        return ret, params, is_const

    # ---------------- class registry ----------------
    def collect_records(self, node):
        if isinstance(node, dict):
            k = node.get("kind")
            if k in ("CXXRecordDecl", "ClassTemplateSpecializationDecl") \
                    and node.get("completeDefinition") and \
                    node.get("name") and \
                    self.in_main(node) and not node.get("isImplicit"):
                ns = self.ns_of(node)
                if k == "ClassTemplateSpecializationDecl":
                    # template arguments are parsed LATER, in
                    # index_specs: clang emits specialisations before
                    # some of the records their arguments name
                    self.spec_nodes.append(node)
                else:
                    q = ns + (node["name"],)
                    self.records[q] = node
            for v in node.values():
                self.collect_records(v)
        elif isinstance(node, list):
            for v in node:
                self.collect_records(v)

    def ensure_class(self, q):
        q = tuple(q)
        if q in self.class_ir:
            return self.class_ir[q]
        if q in self.records:
            return self.translate_record(self.records[q], q, None)
        if q in self.spec_records:
            node = self.spec_records[q]
            return self.translate_record(
                node, q, {"template": node["_tq"],
                          "args": node["_targs"]})
        raise Refuse("parse-error",
                     f"class {'::'.join(q)} not found in the AST")

    def translate_record(self, node, q, template_inst):
        if node.get("tagUsed") == "union":
            raise Refuse("union", f"{q[-1]}: unions are outside the "
                         f"subset")
        c = {"qname": q, "base": None, "base_type": None, "fields": [],
             "methods": [], "ctors": [], "dtor": None,
             "line": self.line_of(node), "template_inst": template_inst,
             "tparams": []}
        self.class_ir[q] = c
        self.tu.add_class(c)
        bases = node.get("bases") or []
        if len(bases) > 1:
            raise Refuse("multiple-inheritance",
                         f"{q[-1]}: multiple bases are outside the "
                         f"subset")
        if bases:
            b = bases[0]
            if b.get("isVirtual"):
                raise Refuse("virtual-inheritance",
                             f"{q[-1]}: virtual bases are outside the "
                             f"subset")
            bt_s = b["type"].get("desugaredQualType") or \
                b["type"]["qualType"]
            try:
                bt = self.parse_type(bt_s)
            except Refuse as r:
                if r.code != "parse-error":
                    raise
                # the base name as written may be unqualified: resolve
                # by unique suffix across the record registry
                hits = [cand for cand in
                        list(self.records) + list(self.spec_records)
                        if cand[-1] == bt_s.split("::")[-1].split("<")[0]]
                if len(hits) != 1:
                    raise
                bt = T_class(self.ensure_class(hits[0])) \
                    if "<" not in bt_s else self.parse_type(bt_s)
            if not is_class_t(bt):
                raise Refuse("parse-error", f"base of {q[-1]} not a class")
            c["base_type"] = bt
            c["base"] = class_qname(bt)
        for ch in node.get("inner", []):
            k = ch.get("kind")
            if k == "FieldDecl":
                if ch.get("isBitField"):
                    raise Refuse("parse-error", "bitfields are outside "
                                 "the subset")
                ft = self.parse_type(ch["type"]["qualType"])
                if strip_const(ft)[0] == "ref":
                    raise Refuse("reference-field",
                                 f"{q[-1]}::{ch['name']}: reference "
                                 f"fields are outside the subset")
                c["fields"].append({"name": ch["name"], "type": ft,
                                    "static": False, "defined": False,
                                    "init": None})
            elif k == "VarDecl":
                # in-class static data member declaration
                ft = self.parse_type(ch["type"]["qualType"])
                f = {"name": ch["name"], "type": ft, "static": True,
                     "defined": False, "init": None}
                c["fields"].append(f)
                self.sfield_by_id[ch["id"]] = (q, ch["name"])
            elif k == "CXXMethodDecl":
                self.translate_method_decl(c, ch)
            elif k == "CXXConstructorDecl":
                self.translate_ctor_decl(c, ch)
            elif k == "CXXDestructorDecl":
                self.translate_dtor_decl(c, ch)
            elif k == "CXXConversionDecl":
                raise Refuse("operator-overload",
                             f"{q[-1]}: conversion operators are "
                             f"outside the subset")
            elif k == "FriendDecl":
                raise Refuse("friend", f"{q[-1]}: friends are outside "
                             f"the subset")
            elif k in ("CXXRecordDecl", "ClassTemplateSpecializationDecl"):
                if not ch.get("isImplicit"):
                    raise Refuse("nested-class",
                                 f"{q[-1]}: nested classes are outside "
                                 f"the subset")
            elif k == "EnumDecl":
                raise Refuse("enum", "enums are outside the subset")
            elif k in ("TypedefDecl", "TypeAliasDecl"):
                pass  # member aliases: not in the v1 subset's use
        return c

    def method_params(self, node):
        params = []
        for ch in node.get("inner", []):
            if ch.get("kind") == "ParmVarDecl":
                default = None
                if ch.get("inner"):
                    default = ch["inner"][0]
                t = self.parse_type(ch["type"]["qualType"])
                params.append((ch.get("name", ""), t, default))
                self._param_nodes = getattr(self, "_param_nodes", {})
        return params

    def register_params(self, node, fnlike):
        for ch in node.get("inner", []):
            if ch.get("kind") == "ParmVarDecl" and ch.get("name"):
                t = fnlike_param(fnlike, ch.get("name"))
                self.scopes[-1][ch["id"]] = (ch["name"], t,
                                             strip_const(t)[0] == "ref")

    def translate_method_decl(self, c, ch):
        if ch.get("isImplicit"):
            return
        name = ch["name"]
        if name.startswith("operator"):
            raise Refuse("operator-overload",
                         f"{c['qname'][-1]}::{name}: operator overloads "
                         f"are outside the subset")
        ret_s, _ps, is_const = self.fn_type_parts(ch["type"]["qualType"])
        params = []
        for pch in ch.get("inner", []):
            if pch.get("kind") == "ParmVarDecl":
                default = None
                if pch.get("inner"):
                    default = ("node", pch["inner"][0])
                params.append((pch.get("name", ""),
                               self.parse_type(pch["type"]["qualType"]),
                               default))
        m = {"name": name, "ret": self.parse_type(ret_s),
             "params": params,
             "virtual": bool(ch.get("virtual")),
             "static": ch.get("storageClass") == "static",
             "const": is_const, "pure": bool(ch.get("pure")),
             "body": None, "line": self.line_of(ch), "node": ch}
        c["methods"].append(m)
        self.method_by_id[ch["id"]] = (c["qname"], m)
        body = self.body_of(ch)
        if body is not None:
            self.pending_bodies.append(("method", c["qname"], m, ch))
        elif ch.get("explicitlyDefaulted"):
            m["body"] = []

    def translate_ctor_decl(self, c, ch):
        if ch.get("isImplicit"):
            return
        if ch.get("isCopy") or ch.get("isMove"):
            raise Refuse("user-copy-ctor",
                         f"{c['qname'][-1]}: user-declared copy/move "
                         f"constructors are outside the subset "
                         f"(copies are memberwise in v1)")
        params = []
        for pch in ch.get("inner", []):
            if pch.get("kind") == "ParmVarDecl":
                default = None
                if pch.get("inner"):
                    default = ("node", pch["inner"][0])
                params.append((pch.get("name", ""),
                               self.parse_type(pch["type"]["qualType"]),
                               default))
        if len(params) == 1:
            pt = strip_const(params[0][1])
            if pt[0] == "ref" and is_class_t(strip_const(pt[1])) and \
                    class_qname(strip_const(pt[1])) == c["qname"]:
                raise Refuse("user-copy-ctor",
                             f"{c['qname'][-1]}: user-declared copy "
                             f"constructors are outside the subset")
        ct = {"params": params, "inits": [], "body": None,
              "line": self.line_of(ch), "node": ch}
        c["ctors"].append(ct)
        self.ctor_by_id[ch["id"]] = (c["qname"], ct)
        if self.body_of(ch) is not None:
            self.pending_bodies.append(("ctor", c["qname"], ct, ch))
        elif ch.get("explicitlyDefaulted"):
            ct["body"] = []

    def translate_dtor_decl(self, c, ch):
        if ch.get("isImplicit"):
            return
        dt = {"virtual": bool(ch.get("virtual")), "body": None,
              "line": self.line_of(ch), "node": ch}
        c["dtor"] = dt
        if self.body_of(ch) is not None:
            self.pending_bodies.append(("dtor", c["qname"], dt, ch))
        elif ch.get("explicitlyDefaulted"):
            dt["body"] = "default"

    @staticmethod
    def body_of(node):
        for ch in node.get("inner", []):
            if ch.get("kind") == "CompoundStmt":
                return ch
        return None

    def prescan_refusals(self, node):
        """Whole-dump scans for constructs clang forms happily but
        the subset refuses, wherever they hide (function bodies,
        template patterns, nested contexts)."""
        if isinstance(node, dict):
            k = node.get("kind")
            if k in ("RequiresExpr", "ConceptSpecializationExpr") and \
                    self.in_main(node):
                raise Refuse("concept-requires",
                             "concepts/requires are outside the subset")
            if k in ("FunctionTemplateDecl", "ClassTemplateDecl",
                     "FunctionDecl", "CXXMethodDecl",
                     "CXXConstructorDecl") and self.in_main(node):
                # a trailing requires-clause is serialised as a bare
                # expression child of the declaration (clang 18 JSON
                # has no RequiresExpr node in this position)
                for ch in node.get("inner", []):
                    ck = ch.get("kind", "")
                    if ck.endswith("Expr") or ck.endswith("Operator"):
                        raise Refuse(
                            "concept-requires",
                            "concepts/requires are outside the subset")
            if k == "TemplateTypeParmDecl" and \
                    node.get("typeConstraint") is not None and \
                    self.in_main(node):
                raise Refuse("concept-requires",
                             "concepts/requires are outside the subset")
            if k == "EnumDecl" and self.in_main(node) and \
                    not node.get("isImplicit"):
                raise Refuse("enum", "enums are outside the subset")
            if k == "CoroutineBodyStmt" and self.in_main(node):
                raise Refuse("coroutine",
                             "coroutines are outside the subset")
            for v in node.values():
                self.prescan_refusals(v)
        elif isinstance(node, list):
            for v in node:
                self.prescan_refusals(v)

    # ---------------- top level ----------------
    def index_specs(self):
        """Index collected specialisations by their instance qname;
        runs only after every record is collected, because clang
        emits specialisations ahead of records they name."""
        for node in self.spec_nodes:
            ns = self.ns_of(node)
            args = [self.parse_type(a["type"]["qualType"])
                    for a in node.get("inner", [])
                    if a.get("kind") == "TemplateArgument"]
            q = ns + (node["name"] + "__" + "_".join(
                mangle_type(a) for a in args),)
            node["_targs"] = args
            node["_tq"] = ns + (node["name"],)
            self.spec_records[q] = node

    def translate(self):
        self.prescan_refusals(self.ast)
        self.collect_records(self.ast)
        self.index_specs()
        tu_node = self.ast
        self.walk_context(tu_node.get("inner", []), ())
        # out-of-line member definitions arrive as later declarations;
        # they were attached during walk_context. Bodies now:
        self.translate_bodies()
        # resolve deferred default-arg nodes and printf flag
        return self.tu

    def walk_context(self, children, ns):
        for ch in children:
            if not isinstance(ch, dict):
                continue
            k = ch.get("kind")
            if k == "NamespaceDecl":
                if self.in_main(ch) or True:
                    self.walk_context(ch.get("inner", []),
                                      ns + (ch["name"],))
                continue
            if not self.in_main(ch):
                continue
            if k in ("CXXRecordDecl", "ClassTemplateSpecializationDecl"):
                if ch.get("completeDefinition") and \
                        not ch.get("isImplicit"):
                    self.ensure_class(ns + (ch["name"],) if
                                      k == "CXXRecordDecl" else
                                      self.spec_qname(ch, ns))
                continue
            if k == "FunctionTemplateDecl":
                for inner in ch.get("inner", []):
                    if inner.get("kind") == "FunctionDecl" and \
                            inner.get("mangledName"):
                        self.translate_func_template_inst(ch, inner, ns)
                continue
            if k == "FunctionDecl":
                self.translate_function(ch, ns)
                continue
            if k in ("CXXMethodDecl", "CXXConstructorDecl",
                     "CXXDestructorDecl"):
                self.attach_outofline(ch)
                continue
            if k == "VarDecl":
                self.translate_toplevel_var(ch, ns)
                continue
            if k in ("TypedefDecl", "TypeAliasDecl"):
                if ns and ns[0] == "std":
                    continue  # library internals are not user aliases
                t = ch["type"].get("desugaredQualType") or \
                    ch["type"]["qualType"]
                self.aliases[ns + (ch["name"],)] = self.parse_type(t)
                continue
            if k == "EnumDecl":
                raise Refuse("enum", "enums are outside the subset")
            if k == "LinkageSpecDecl":
                raise Refuse("linkage-spec",
                             "extern blocks are outside the subset")

    def spec_qname(self, node, ns):
        args = [self.parse_type(a["type"]["qualType"])
                for a in node.get("inner", [])
                if a.get("kind") == "TemplateArgument"]
        return ns + (node["name"] + "__" + "_".join(
            mangle_type(a) for a in args),)

    def translate_func_template_inst(self, tdecl, node, ns):
        args = [self.parse_type(a["type"]["qualType"])
                for a in node.get("inner", [])
                if a.get("kind") == "TemplateArgument"]
        name = node["name"]
        q = ns + (name + "__" + "_".join(mangle_type(a) for a in args),)
        ret_s, _ps, _c = self.fn_type_parts(node["type"]["qualType"])
        params = []
        for pch in node.get("inner", []):
            if pch.get("kind") == "ParmVarDecl":
                params.append((pch.get("name", ""),
                               self.parse_type(pch["type"]["qualType"]),
                               None))
        f = {"qname": q, "ret": self.parse_type(ret_s),
             "params": params, "body": None,
             "line": self.line_of(tdecl),
             "template_inst": {"template": ns + (name,),
                               "args": args},
             "node": node}
        self.tu.funcs.append(f)
        self.func_by_id[node["id"]] = f
        if self.body_of(node) is not None:
            self.pending_bodies.append(("func", None, f, node))

    def translate_function(self, ch, ns):
        if ch.get("isImplicit"):
            return
        name = ch["name"]
        if name.startswith("operator"):
            raise Refuse("operator-overload",
                         f"{name}: operator overloads are outside "
                         f"the subset")
        q = ns + (name,)
        ret_s, _ps, _c = self.fn_type_parts(ch["type"]["qualType"])
        params = []
        for pch in ch.get("inner", []):
            if pch.get("kind") == "ParmVarDecl":
                default = None
                if pch.get("inner"):
                    default = ("node", pch["inner"][0])
                params.append((pch.get("name", ""),
                               self.parse_type(pch["type"]["qualType"]),
                               default))
        f = {"qname": q, "ret": self.parse_type(ret_s),
             "params": params, "body": None, "line": self.line_of(ch),
             "node": ch}
        # redeclaration chains: keep one FuncIR per function
        prev = ch.get("previousDecl")
        if prev and prev in self.func_by_id:
            f = self.func_by_id[prev]
            f["params"] = params
            f["ret"] = self.parse_type(ret_s)
        else:
            self.tu.funcs.append(f)
        self.func_by_id[ch["id"]] = f
        if self.body_of(ch) is not None:
            self.pending_bodies.append(("func", None, f, ch))

    def attach_outofline(self, ch):
        prev = ch.get("previousDecl")
        body = self.body_of(ch)
        if prev and prev in self.method_by_id:
            owner, m = self.method_by_id[prev]
            if body is not None:
                self.pending_bodies.append(("method", owner, m, ch))
            return
        if prev and prev in self.ctor_by_id:
            owner, ct = self.ctor_by_id[prev]
            if body is not None:
                self.pending_bodies.append(("ctor", owner, ct, ch))
            return
        if ch.get("kind") == "CXXDestructorDecl":
            # name is "~Class": resolve through the namespace context
            q = self.ns_of(ch) + (ch["name"].lstrip("~"),)
            if q in self.class_ir:
                cir = self.class_ir[q]
                if cir.get("dtor") is not None and body is not None:
                    self.pending_bodies.append(
                        ("dtor", q, cir["dtor"], ch))
                    return
        raise Refuse("parse-error",
                     f"out-of-line definition of {ch.get('name')} "
                     f"could not be attached")

    def translate_toplevel_var(self, ch, ns):
        name = ch["name"]
        parent_ctx = ch.get("parentDeclContextId")
        if parent_ctx is not None:
            rec = self.byid.get(parent_ctx)
            if rec is None:
                raise Refuse("parse-error", "static member def without "
                             "a record")
            q = None
            for cand, node2 in list(self.records.items()) + \
                    list(self.spec_records.items()):
                if node2 is rec:
                    q = cand
                    break
            if q is None:
                raise Refuse("parse-error", "static member def for "
                             "unknown class")
            cir = self.ensure_class(q)
            for f in cir["fields"]:
                if f["name"] == name and f.get("static"):
                    f["defined"] = True
                    if ch.get("inner"):
                        f["init"] = ("node", ch["inner"][0])
                        self.pending_bodies.append(
                            ("static-init", q, f, ch))
                    return
            raise Refuse("parse-error",
                         f"static member {name} not declared in class")
        if ch.get("storageClass") == "extern":
            return
        t = self.parse_type(ch["type"]["qualType"])
        g = {"qname": ns + (name,), "type": t, "init": None,
             "line": self.line_of(ch), "node": ch}
        self.tu.globals.append(g)
        self.global_by_id[ch["id"]] = g
        if ch.get("previousDecl"):
            self.global_by_id[ch["previousDecl"]] = g
        if ch.get("inner"):
            g["init"] = ("node", ch["inner"][0])
            self.pending_bodies.append(("global-init", None, g, ch))

    # ---------------- bodies ----------------
    def translate_bodies(self):
        i = 0
        while i < len(self.pending_bodies):
            kind, owner_q, obj, node = self.pending_bodies[i]
            i += 1
            self.scopes = [{}]
            if kind == "static-init":
                f = obj
                f["init"] = self.translate_expr(node["inner"][0])
                continue
            if kind == "global-init":
                g = obj
                g["init"] = self.translate_expr(node["inner"][0])
                continue
            # register params (+ defaults are converted lazily)
            self.cur_class = owner_q
            fnlike = obj
            scope = {}
            for pch in node.get("inner", []):
                if pch.get("kind") == "ParmVarDecl" and pch.get("name"):
                    pname = pch["name"]
                    pt = None
                    for (n2, t2, _d) in fnlike.get("params", []):
                        if n2 == pname:
                            pt = t2
                            break
                    if pt is None:
                        pt = self.parse_type(pch["type"]["qualType"])
                    scope[pch["id"]] = (pname, pt,
                                        strip_const(pt)[0] == "ref")
            self.scopes = [scope]
            if kind == "ctor":
                obj["inits"] = self.translate_ctor_inits(node)
            body = self.body_of(node)
            obj["body"] = self.translate_block(body)
            self.cur_class = None
        # convert ("node", x) defaults in signatures to IR exprs
        self.convert_defaults()

    def convert_defaults(self):
        def conv_params(params):
            out = []
            for (n, t, d) in params:
                if isinstance(d, tuple) and d and d[0] == "node":
                    self.scopes = [{}]
                    self.cur_class = None
                    d = self.translate_expr(d[1])
                out.append((n, t, d))
            return out
        for f in self.tu.funcs:
            f["params"] = conv_params(f["params"])
        for c in self.tu.classes:
            for m in c["methods"]:
                m["params"] = conv_params(m["params"])
            for ct in c["ctors"]:
                ct["params"] = conv_params(ct["params"])

    def translate_ctor_inits(self, node):
        inits = []
        cir = self.class_ir[self.cur_class]
        for ch in node.get("inner", []):
            if ch.get("kind") != "CXXCtorInitializer":
                continue
            args = [self.translate_expr(a) for a in ch.get("inner", [])]
            def unwrap_construct(args, target_q):
                # clang delivers a base/member init as one construct
                # node wrapping the ctor arguments; the IR carries
                # the argument list itself
                if len(args) == 1 and args[0].get("k") == "construct" \
                        and tuple(class_qname(args[0]["t"])) == \
                        tuple(target_q):
                    return args[0]["args"]
                return args
            def implicit_default(args, target_q):
                # clang inserts a zero-argument construction of the
                # base/member's own class for implicit default
                # initialisation; the emitter's default-init path
                # reproduces it, and the subset parser never sees it
                return (len(args) == 1 and args[0].get("k") ==
                        "construct" and not args[0].get("args") and
                        tuple(class_qname(args[0]["t"])) ==
                        tuple(target_q))
            if "anyInit" in ch:
                fname = ch["anyInit"]["name"]
                ftype = None
                for fd in cir["fields"]:
                    if fd["name"] == fname:
                        ftype = fd["type"]
                if ftype is not None and is_class_t(base_type(ftype)):
                    fq = class_qname(base_type(ftype))
                    args = unwrap_construct(args, fq)
                    if implicit_default(args, fq):
                        continue
                inits.append(("field", fname, args))
            elif "baseInit" in ch:
                bt_s = ch["baseInit"].get("desugaredQualType") or \
                    ch["baseInit"]["qualType"]
                try:
                    bt = self.parse_type(bt_s)
                except Refuse as r:
                    if r.code != "parse-error":
                        raise
                    hits = [cand for cand in
                            list(self.records) + list(self.spec_records)
                            if cand[-1] == bt_s.split("::")[-1]]
                    if len(hits) != 1:
                        raise
                    bt = T_class(hits[0])
                args = unwrap_construct(args, class_qname(bt))
                if implicit_default(args, class_qname(bt)):
                    continue
                inits.append(("base", class_qname(bt)[-1], args))
            elif ch.get("isDelegating"):
                raise Refuse("delegating-constructor",
                             "delegating constructors are outside "
                             "the subset")
        return inits

    # ---------------- statements ----------------
    def translate_block(self, node):
        out = []
        self.scopes.append({})
        for ch in node.get("inner", []):
            st = self.translate_stmt(ch)
            if st is not None:
                out.append(st)
        self.scopes.pop()
        return out

    def translate_stmt(self, node):
        if not isinstance(node, dict) or not node.get("kind"):
            return {"k": "block", "ss": []}
        k = node["kind"]
        if k == "CompoundStmt":
            return {"k": "block", "ss": self.translate_block(node)}
        if k == "DeclStmt":
            ss = []
            for ch in node.get("inner", []):
                ss.append(self.translate_var_decl(ch))
            if len(ss) == 1:
                return ss[0]
            # a multi-declarator declaration statement does NOT open
            # a scope in C++ ("group" emits inline, track 1 parity)
            return {"k": "group", "ss": ss}
        if k == "ReturnStmt":
            e = None
            if node.get("inner"):
                e = self.translate_expr(node["inner"][0])
            return {"k": "return", "e": e}
        if k == "IfStmt":
            return self.translate_if(node)
        if k == "WhileStmt":
            inn = [c for c in node.get("inner", [])
                   if isinstance(c, dict) and c.get("kind")]
            cond, body = inn[0], inn[1]
            return {"k": "while", "c": self.translate_expr(cond),
                    "b": [self.translate_stmt(body)]}
        if k == "DoStmt":
            inn = [c for c in node.get("inner", [])
                   if isinstance(c, dict) and c.get("kind")]
            body, cond = inn[0], inn[1]
            return {"k": "dowhile", "c": self.translate_expr(cond),
                    "b": [self.translate_stmt(body)]}
        if k == "ForStmt":
            return self.translate_for(node)
        if k == "CXXForRangeStmt":
            return self.translate_rangefor(node)
        if k == "BreakStmt":
            return {"k": "break"}
        if k == "ContinueStmt":
            return {"k": "continue"}
        if k == "CXXTryStmt":
            raise Refuse("exceptions", "try/catch is outside the subset")
        if k == "SwitchStmt":
            raise Refuse("switch-statement",
                         "switch is outside the v1 subset")
        if k == "GotoStmt":
            raise Refuse("goto-statement", "goto is outside the subset")
        if k == "NullStmt":
            return {"k": "block", "ss": []}
        return {"k": "expr", "e": self.translate_expr(node)}

    def translate_if(self, node):
        inn = [c for c in node.get("inner", [])
               if isinstance(c, dict) and c.get("kind")]
        init = None
        if inn and inn[0]["kind"] in ("DeclStmt", "NullStmt"):
            init = inn.pop(0)
        cond = self.translate_expr(inn[0])
        then = self.translate_stmt(inn[1])
        els = None
        if len(inn) > 2:
            els = self.translate_stmt(inn[2])
        node_ir = {"k": "if", "c": cond, "t": [then],
                   "e": [els] if els is not None else None}
        if init is not None and init["kind"] == "DeclStmt":
            d = self.translate_stmt(init)
            return {"k": "block", "ss": [d, node_ir]}
        return node_ir

    def translate_for(self, node):
        # clang ForStmt: exactly five slots [init, cond-var, cond,
        # inc, body]; an empty slot is an {} placeholder.
        inn = node.get("inner", [])
        parts = [c if isinstance(c, dict) and c.get("kind") else None
                 for c in inn]
        while len(parts) < 5:
            parts.append(None)
        init_node, condvar, cond_node, inc_node, body_node = parts[:5]
        if condvar is not None:
            raise Refuse("parse-error",
                         "for-loop condition declarations are outside "
                         "the subset")
        init_ir = None
        if init_node is not None:
            if init_node["kind"] == "DeclStmt":
                init_ir = self.translate_stmt(init_node)
            else:
                init_ir = {"k": "expr",
                           "e": self.translate_expr(init_node)}
        cond = self.translate_expr(cond_node) if cond_node else None
        inc = self.translate_expr(inc_node) if inc_node else None
        body = self.translate_stmt(body_node) if body_node else None
        return {"k": "for", "init": init_ir, "c": cond, "inc": inc,
                "b": [body] if body is not None else []}

    def translate_rangefor(self, node):
        inn = node.get("inner", [])
        range_decl = None
        loop_decl = None
        body = None
        for c in inn:
            if not isinstance(c, dict):
                continue
            if c.get("kind") == "DeclStmt":
                for v in c.get("inner", []):
                    if v.get("name", "").startswith("__range"):
                        if v.get("inner"):
                            range_decl = v["inner"][0]
                    elif not v.get("name", "").startswith("__"):
                        loop_decl = v
            elif c.get("kind") not in ("BinaryOperator", "UnaryOperator",
                                       "DeclStmt"):
                body = c
        if range_decl is None or loop_decl is None:
            raise Refuse("parse-error", "range-for shape not recognised")
        rng = self.translate_expr(range_decl)
        name = loop_decl["name"]
        ty = self.parse_type(loop_decl["type"]["qualType"])
        self.scopes.append({loop_decl["id"]: (name, ty,
                                              strip_const(ty)[0] == "ref")})
        b = [self.translate_stmt(body)] if body is not None else []
        self.scopes.pop()
        return {"k": "rangefor", "name": name, "type": ty,
                "range": rng, "b": b}

    def translate_var_decl(self, ch):
        if ch.get("kind") == "FunctionDecl":
            raise Refuse("parse-error",
                         "local function declarations are outside "
                         "the subset")
        name = ch["name"]
        if ch.get("storageClass") == "static":
            raise Refuse("static-local-variable",
                         "static locals are outside the v1 subset")
        # A lambda-bound variable's clang type is the closure type;
        # its IR type is the lambda's function-pointer type.
        e0 = ch["inner"][0] if ch.get("inner") else None
        probe = e0
        while isinstance(probe, dict) and \
                probe.get("kind") in WRAPPER_EXPRS:
            probe = probe["inner"][0]
        if probe is not None and probe.get("kind") == "LambdaExpr":
            lam = self.translate_expr(e0)
            t = lam["t"]
            self.scopes[-1][ch["id"]] = (name, t, False)
            return {"k": "decl", "name": name, "type": t,
                    "init": {"form": "expr", "e": lam}}
        t = self.parse_type(ch["type"]["qualType"])
        init = None
        if ch.get("inner"):
            e0u = e0
            while e0u.get("kind") in WRAPPER_EXPRS:
                e0u = e0u["inner"][0]
            if e0u.get("kind") == "CXXConstructExpr":
                args = [self.translate_expr(a)
                        for a in e0u.get("inner", [])]
                if args or e0u.get("isListInitialization"):
                    init = {"form": "ctor", "args": args}
                else:
                    init = None
            elif e0u.get("kind") == "InitListExpr":
                items = [self.translate_expr(a)
                         for a in e0u.get("inner", [])]
                init = {"form": "list",
                        "e": {"k": "initlist", "t": t, "items": items}}
            else:
                init = {"form": "expr", "e": self.translate_expr(e0)}
        self.scopes[-1][ch["id"]] = (name, t, strip_const(t)[0] == "ref")
        return {"k": "decl", "name": name, "type": t, "init": init}

    # ---------------- expressions ----------------
    def lookup_local(self, nid):
        for sc in reversed(self.scopes):
            if nid in sc:
                return sc[nid]
        return None

    def src_literal(self, node, charset):
        # clang 18's JSON range for a literal does not point at the
        # token start (observed: a constant +2 into the token, for
        # tokens of several lengths), so recover the literal as the
        # maximal run of literal characters containing the offset.
        rng = node.get("range") or {}
        b = rng.get("begin") or {}
        off = b.get("offset")
        if off is None:
            off = (node.get("loc") or {}).get("offset")
        if off is None or off >= len(self.src):
            return None
        start = off
        while start > 0 and self.src[start - 1] in charset:
            start -= 1
        end = off
        while end < len(self.src) and self.src[end] in charset:
            end += 1
        return self.src[start:end]

    def translate_expr(self, node):
        if not isinstance(node, dict):
            raise Refuse("parse-error", "empty expression node")
        k = node.get("kind")
        if k in WRAPPER_EXPRS:
            return self.translate_expr(node["inner"][0])
        if k == "CXXDefaultArgExpr":
            # call sites substitute the parameter's default before
            # translating arguments; reaching here means a default
            # argument outside a call's argument list
            raise Refuse("parse-error",
                         "default argument outside a call site")
        handler = getattr(self, "expr_" + (k or ""), None)
        if handler is None:
            raise Refuse("parse-error",
                         f"expression kind {k} is outside the subset")
        return handler(node)

    def etype(self, node):
        t = (node.get("type") or {}).get("qualType")
        if t is None:
            raise Refuse("parse-error", "expression without a type")
        return self.parse_type(t)

    def expr_IntegerLiteral(self, node):
        return {"k": "int", "v": node["value"], "t": self.etype(node)}

    def expr_FloatingLiteral(self, node):
        txt = self.src_literal(node, "0123456789.eE+-xXabcdefABCDEF")
        ty = self.etype(node)
        if txt is None:
            txt = node["value"]
        if ty == T_builtin("float") and not txt.lower().endswith("f"):
            pass
        return {"k": "float", "v": txt, "t": ty}

    def expr_CharacterLiteral(self, node):
        txt = self.src_literal(node, "'\\abfnrtv0xuU0123456789abcdefABCDEF")
        if txt is None or not txt.startswith("'"):
            txt = f"'{chr(node['value'])}'"
        return {"k": "char", "v": txt, "t": T_builtin("char")}

    def expr_StringLiteral(self, node):
        v = node.get("value")
        if v is None:
            txt = self.src_literal(node, '"\\0123456789abcdefABCDEF'
                                   'abcdefghijklmnopqrstuvwxyz'
                                   'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                                   ' \t!#$%&()*+,-./:;<=>?@[]^_`{|}~')
            v = txt
        elif not v.startswith('"'):
            v = json.dumps(v)
        return {"k": "str", "v": v,
                "t": ("ptr", ("const", T_builtin("char")))}

    def expr_CXXBoolLiteralExpr(self, node):
        return {"k": "bool", "v": bool(node["value"]),
                "t": T_builtin("bool")}

    def expr_CXXNullPtrLiteralExpr(self, node):
        return {"k": "nullptr", "t": ("ptr", T_builtin("void"))}

    def expr_CXXThisExpr(self, node):
        return {"k": "this", "t": self.etype(node)}

    def expr_DeclRefExpr(self, node):
        ref = node.get("referencedDecl") or {}
        rid = ref.get("id")
        rk = ref.get("kind")
        if rk in ("VarDecl", "ParmVarDecl"):
            loc = self.lookup_local(rid)
            if loc is not None:
                return {"k": "var", "name": loc[0], "t": loc[1],
                        "ref": loc[2]}
        t = self.etype(node)
        if rk == "VarDecl":
            if rid in self.global_by_id:
                g = self.global_by_id[rid]
                return {"k": "gvar", "qname": g["qname"], "t": t}
            if rid in self.sfield_by_id:
                owner, fname = self.sfield_by_id[rid]
                return {"k": "smember", "class": owner, "name": fname,
                        "t": t}
            # a static member referenced through its definition node
            tgt = self.byid.get(rid)
            if tgt is not None and tgt.get("parentDeclContextId"):
                rec = self.byid.get(tgt["parentDeclContextId"])
                if rec is not None:
                    for cand, n2 in list(self.records.items()) + \
                            list(self.spec_records.items()):
                        if n2 is rec:
                            return {"k": "smember", "class": cand,
                                    "name": tgt["name"], "t": t}
            raise Refuse("parse-error",
                         f"unresolved variable {ref.get('name')}")
        if rk == "ParmVarDecl":
            loc = self.lookup_local(rid)
            if loc is not None:
                return {"k": "var", "name": loc[0], "t": loc[1],
                        "ref": loc[2]}
            raise Refuse("parse-error", "unresolved parameter")
        if rk == "FunctionDecl":
            fnode = self.byid.get(rid)
            q = self.func_qname_of(fnode)
            return {"k": "func", "qname": q}
        if rk == "EnumConstantDecl":
            raise Refuse("enum", "enum constants are outside the subset")
        if rk == "CXXMethodDecl":
            raise Refuse("parse-error",
                         "method reference outside a call")
        raise Refuse("parse-error", f"DeclRefExpr to {rk}")

    def func_qname_of(self, fnode):
        if fnode is None:
            raise Refuse("parse-error", "unknown function reference")
        if fnode["id"] in self.func_by_id:
            return self.func_by_id[fnode["id"]]["qname"]
        prev = fnode.get("previousDecl")
        if prev and prev in self.func_by_id:
            return self.func_by_id[prev]["qname"]
        ns = self.ns_of(fnode)
        return ns + (fnode["name"],)

    def expr_MemberExpr(self, node):
        mid = node.get("referencedMemberDecl")
        mdecl = self.byid.get(mid)
        obj = self.translate_expr(node["inner"][0])
        if mdecl is not None and mdecl.get("kind") == "FieldDecl":
            return {"k": "member", "o": obj, "f": node["name"],
                    "t": self.etype(node)}
        if mdecl is not None and mdecl.get("kind") == "VarDecl":
            rec = self.record_of(mdecl)
            q = None
            for cand, n2 in list(self.records.items()) + \
                    list(self.spec_records.items()):
                if n2 is rec:
                    q = cand
                    break
            return {"k": "smember", "class": q, "name": node["name"],
                    "t": self.etype(node)}
        if mdecl is not None and mdecl.get("kind") == "CXXMethodDecl":
            return {"k": "mfunc", "o": obj, "name": node["name"],
                    "qualified": None, "_mid": mid}
        raise Refuse("parse-error", "member expression not recognised")

    def translate_call_args(self, raw_args, params):
        """Translate call arguments; a CXXDefaultArgExpr carries no
        expression in clang's JSON — the default lives on the
        parameter, whose IR default expression is substituted."""
        out = []
        for i, a in enumerate(raw_args):
            if a.get("kind") == "CXXDefaultArgExpr":
                if params is None or i >= len(params) or \
                        params[i][2] is None:
                    raise Refuse("parse-error",
                                 "default argument not recoverable")
                d = params[i][2]
                if isinstance(d, tuple) and d[0] == "node":
                    # deferred default: a raw clang node; defaults in
                    # the subset are scope-free (literals, globals,
                    # static members), so translate it here
                    d = self.translate_expr(d[1])
                out.append(copy.deepcopy(d))
            else:
                out.append(self.translate_expr(a))
        return out

    def expr_CallExpr(self, node):
        inn = node.get("inner", [])
        callee_node = inn[0]
        raw_args = inn[1:]
        ce = callee_node
        while ce.get("kind") in WRAPPER_EXPRS:
            ce = ce["inner"][0]
        if ce.get("kind") == "DeclRefExpr":
            ref = ce.get("referencedDecl") or {}
            fnode = self.byid.get(ref.get("id"))
            if fnode is not None and fnode.get("kind") in (
                    "VarDecl", "ParmVarDecl"):
                # call through a function-pointer variable
                callee = self.translate_expr(callee_node)
                args = self.translate_call_args(raw_args, None)
                return self.build_indirect_call(callee, args, node)
            return self.build_func_call(fnode, ref, raw_args, node)
        callee = self.translate_expr(callee_node)
        args = self.translate_call_args(raw_args, None)
        return self.build_indirect_call(callee, args, node)

    def build_func_call(self, fnode, ref, raw_args, node):
        if fnode is None:
            raise Refuse("parse-error", "call to unknown function")
        if fnode.get("kind") == "FunctionTemplateDecl":
            # a call through a template-id references the template
            # decl in some dumps; judge it by its pattern (this is
            # how std::move arrives)
            for inner in fnode.get("inner", []):
                if inner.get("kind") == "FunctionDecl":
                    fnode = inner
                    break
        if fnode.get("kind") == "CXXMethodDecl":
            # qualified static call: Class::method(...)
            owner, m = self.method_by_id.get(fnode["id"], (None, None))
            if m is None and fnode.get("previousDecl"):
                owner, m = self.method_by_id.get(
                    fnode["previousDecl"], (None, None))
            if m is None:
                raise Refuse("parse-error",
                             "qualified method call not translated")
            if not m.get("static"):
                raise Refuse("parse-error",
                             "qualified call of a non-static method "
                             "needs an object")
            return {"k": "call", "callee": ("smethod", owner, m),
                    "args": self.translate_call_args(raw_args,
                                                     m["params"]),
                    "t": m["ret"]}
        name = fnode["name"]
        fns = self.ns_of(fnode)
        if name == "printf" and (fns == ("std",) or fns == ()):
            self.tu.uses_printf = True
            return {"k": "call", "callee": ("free", ("printf",), None),
                    "args": self.translate_call_args(raw_args, None),
                    "t": T_builtin("int")}
        if fns and fns[0] == "std":
            if name == "move":
                raise Refuse("move-semantics",
                             "std::move is outside the subset")
            raise Refuse("stl-facility",
                         f"std::{name} is outside the v1 subset")
        fid = fnode["id"]
        f = self.func_by_id.get(fid)
        if f is None and fnode.get("previousDecl"):
            f = self.func_by_id.get(fnode["previousDecl"])
        if f is None:
            raise Refuse("parse-error",
                         f"call to untranslated function {name}")
        return {"k": "call", "callee": ("free", f["qname"], f),
                "args": self.translate_call_args(raw_args, f["params"]),
                "t": f["ret"]}

    def build_indirect_call(self, callee, args, node):
        if callee["k"] == "var" or callee["k"] == "member" or \
                callee["k"] == "gvar":
            tt = strip_const(callee["t"])
            if tt[0] == "ref":
                tt = strip_const(tt[1])
            if tt[0] == "fnptr":
                return {"k": "call", "callee": ("expr", callee),
                        "args": args, "t": tt[1]}
        raise Refuse("parse-error", "indirect call shape not recognised")

    def member_qualified(self, mexpr, mname):
        """A qualified member call (obj->Base::m()) suppresses
        virtual dispatch in C++. clang's JSON carries no qualifier
        field on MemberExpr; its range begins at the arrow/dot and
        spans through the member name, so a qualified call is
        exactly one whose range text contains '::' + the name."""
        rng = mexpr.get("range") or {}
        b = (rng.get("begin") or {}).get("offset")
        e = rng.get("end") or {}
        if b is None or not mname:
            return False
        end_off = e.get("offset", b) + e.get("tokLen", len(mname))
        seg = self.src[b:end_off]
        return ("::" + mname) in seg

    def expr_CXXMemberCallExpr(self, node):
        inn = node.get("inner", [])
        mexpr = inn[0]
        raw_args = inn[1:]
        mid = mexpr.get("referencedMemberDecl")
        mdecl0 = self.byid.get(mid)
        qualified = self.member_qualified(
            mexpr, (mdecl0 or {}).get("name", ""))
        mdecl = self.byid.get(mid)
        obj_node = mexpr["inner"][0]
        obj = self.translate_expr(obj_node)
        ot = strip_const(obj["t"])
        if ot[0] == "ref":
            ot = strip_const(ot[1])
        if ot[0] == "ptr":
            ot = strip_const(ot[1])
        # a call to a lambda's conversion operator IS the
        # lambda-to-function-pointer conversion: clang spells
        # `IntFn f = <lambda>` (and `= sq`) as a member call to the
        # conversion operator on the closure object
        if mdecl is not None and (
                mdecl.get("kind") == "CXXConversionDecl" or
                (mdecl.get("name") or "").startswith("operator ") or
                "(*)" in (mdecl.get("type") or {}).get("qualType", "")):
            if obj["k"] == "lambda":
                # the object translation already lowered the lambda
                return obj
            probe = obj_node
            while probe.get("kind") in WRAPPER_EXPRS:
                probe = probe["inner"][0]
            if probe.get("kind") == "LambdaExpr":
                return self.translate_expr(probe)
            if strip_const(obj["t"])[0] == "fnptr":
                return obj
            raise Refuse("parse-error",
                         "conversion-operator call shape not "
                         "recognised")
        # lambda called through its variable: object is the closure
        otype = (obj_node.get("type") or {}).get("qualType", "")
        if otype.startswith("(lambda"):
            tt = strip_const(obj["t"])
            if tt[0] == "fnptr":
                return {"k": "call", "callee": ("expr", obj),
                        "args": self.translate_call_args(raw_args, None),
                        "t": tt[1]}
            raise Refuse("parse-error", "lambda object not lowered to "
                         "a function pointer")
        if ot[0] != "class":
            raise Refuse("parse-error", "member call on non-class")
        q = ot[1]
        if mdecl is None:
            raise Refuse("parse-error", "member call without a decl")
        if mdecl.get("storageClass") == "static":
            owner, m = self.method_by_id.get(mid, (None, None))
            if m is None:
                raise Refuse("parse-error", "static method not "
                             "translated")
            return {"k": "call", "callee": ("smethod", owner, m),
                    "args": self.translate_call_args(raw_args,
                                                     m["params"]),
                    "t": m["ret"]}
        owner, m = self.method_by_id.get(mid, (None, None))
        if m is None:
            # an override decl not separately translated: find by name
            cir = self.ensure_class(q)
            for cand_q in [q] + Semantics(self.tu).bases_chain(q):
                for mm in self.class_ir[cand_q]["methods"]:
                    if mm["name"] == mdecl["name"]:
                        owner, m = cand_q, mm
                        break
                if m is not None:
                    break
        if m is None:
            raise Refuse("parse-error",
                         f"method {mdecl.get('name')} not translated")
        return {"k": "call",
                "callee": ("method", obj, owner, m,
                           bool(mdecl.get("virtual")) and not qualified),
                "qualified": qualified,
                "args": self.translate_call_args(raw_args, m["params"]),
                "t": m["ret"]}

    def expr_CXXOperatorCallExpr(self, node):
        # calling a lambda through its variable is an operator()
        # call on the closure in clang's AST; the closure has
        # already lowered to a function pointer, so this is an
        # indirect call. Genuine operator overloads refuse.
        inn = node.get("inner", [])
        if len(inn) >= 2:
            probe = inn[0]
            while probe.get("kind") in WRAPPER_EXPRS:
                probe = probe["inner"][0]
            if probe.get("kind") == "DeclRefExpr":
                ref = probe.get("referencedDecl") or {}
                decl = self.byid.get(ref.get("id"))
                if decl is not None and \
                        decl.get("name") == "operator()":
                    obj = self.translate_expr(inn[1])
                    tt = strip_const(obj["t"])
                    if tt[0] == "ref":
                        tt = strip_const(tt[1])
                    if tt[0] == "fnptr":
                        args = self.translate_call_args(inn[2:], None)
                        return {"k": "call", "callee": ("expr", obj),
                                "args": args, "t": tt[1]}
        raise Refuse("operator-overload",
                     "operator calls are outside the v1 subset")

    def expr_CompoundAssignOperator(self, node):
        inn = node["inner"]
        return {"k": "assign", "op": node["opcode"],
                "l": self.translate_expr(inn[0]),
                "r": self.translate_expr(inn[1]),
                "t": self.etype(node)}

    def expr_BinaryOperator(self, node):
        op = node["opcode"]
        l = self.translate_expr(node["inner"][0])
        r = self.translate_expr(node["inner"][1])
        if op in ("=", "+=", "-=", "*=", "/=", "%="):
            return {"k": "assign", "op": op, "l": l, "r": r, "t": l["t"]}
        if op == ",":
            raise Refuse("parse-error",
                         "the comma operator is outside the subset")
        return {"k": "bin", "op": op, "l": l, "r": r,
                "t": self.etype(node)}

    def expr_UnaryOperator(self, node):
        op = node["opcode"]
        e = self.translate_expr(node["inner"][0])
        if op == "&":
            return {"k": "un", "op": "addr", "e": e,
                    "t": ("ptr", e["t"])}
        if op == "*":
            pt = strip_const(e["t"])
            if pt[0] == "ref":
                pt = strip_const(pt[1])
            return {"k": "un", "op": "deref", "e": e, "t": pt[1]}
        node_ir = {"k": "un", "op": op, "e": e, "t": self.etype(node)}
        if node.get("isPostfix"):
            node_ir["post"] = True
        return node_ir

    def expr_ConditionalOperator(self, node):
        c, a, b = (self.translate_expr(x) for x in node["inner"])
        return {"k": "cond", "c": c, "a": a, "b": b,
                "t": self.etype(node)}

    def expr_ArraySubscriptExpr(self, node):
        a = self.translate_expr(node["inner"][0])
        i = self.translate_expr(node["inner"][1])
        return {"k": "index", "a": a, "i": i, "t": self.etype(node)}

    def expr_CXXConstructExpr(self, node):
        t = self.etype(node)
        args = [self.translate_expr(a) for a in node.get("inner", [])]
        if len(args) == 1 and strip_const(args[0]["t"]) == strip_const(t):
            return args[0]  # implicit copy: memberwise in v1
        return {"k": "construct", "type": t, "args": args, "t": t}

    def expr_CXXTemporaryObjectExpr(self, node):
        t = self.etype(node)
        args = [self.translate_expr(a) for a in node.get("inner", [])]
        return {"k": "construct", "type": t, "args": args, "t": t}

    def expr_CXXNewExpr(self, node):
        t = self.etype(node)
        args = []
        for ch in node.get("inner", []):
            if ch.get("kind") == "CXXConstructExpr":
                args = [self.translate_expr(a)
                        for a in ch.get("inner", [])]
            else:
                raise Refuse("array-new-delete",
                             "array/placement new is outside the "
                             "subset")
        inner_t = strip_const(t)
        if inner_t[0] != "ptr" or not is_class_t(strip_const(inner_t[1])):
            raise Refuse("parse-error", "new of non-class type")
        return {"k": "new", "type": strip_const(inner_t[1]),
                "args": args, "t": t}

    def expr_CXXDeleteExpr(self, node):
        return {"k": "delete",
                "e": self.translate_expr(node["inner"][0]),
                "t": T_builtin("void")}

    def expr_CXXStaticCastExpr(self, node):
        return {"k": "cast", "type": self.etype(node),
                "e": self.translate_expr(node["inner"][0]),
                "t": self.etype(node)}

    expr_CXXReinterpretCastExpr = expr_CXXStaticCastExpr
    expr_CXXConstCastExpr = expr_CXXStaticCastExpr
    expr_CStyleCastExpr = expr_CXXStaticCastExpr

    def expr_CXXFunctionalCastExpr(self, node):
        # Type(args) / Type{args} is construction, not a cast, when
        # the operand is a constructor expression or an init list
        # for a class — the subset parser reads it the same way.
        inner = node["inner"][0]
        probe = inner
        while probe.get("kind") in WRAPPER_EXPRS:
            probe = probe["inner"][0]
        if probe.get("kind") in ("CXXConstructExpr",
                                 "CXXTemporaryObjectExpr"):
            return self.translate_expr(inner)
        t = self.etype(node)
        if probe.get("kind") == "InitListExpr" and is_class_t(t):
            args = [self.translate_expr(a)
                    for a in probe.get("inner", [])]
            return {"k": "construct", "type": t, "args": args, "t": t}
        return self.expr_CXXStaticCastExpr(node)

    def expr_CXXDynamicCastExpr(self, node):
        raise Refuse("rtti-dynamic-cast",
                     "dynamic_cast is outside the subset")

    def expr_CXXTypeidExpr(self, node):
        raise Refuse("rtti-typeid", "typeid is outside the subset")

    def expr_CXXThrowExpr(self, node):
        raise Refuse("exceptions", "throw is outside the subset")

    def expr_InitListExpr(self, node):
        items = [self.translate_expr(a) for a in node.get("inner", [])]
        return {"k": "initlist", "t": self.etype(node), "items": items}

    def expr_CXXScalarValueInitExpr(self, node):
        t = self.etype(node)
        if is_class_t(t):
            return {"k": "construct", "type": t, "args": [], "t": t}
        return {"k": "int", "v": "0", "t": t}

    def expr_SizeOfExpr(self, node):
        # sizeof with a type operand: clang emits the type inside the
        # node's "argType" for the type form; expression form otherwise.
        arg_type = node.get("argType")
        if arg_type:
            return {"k": "sizeof",
                    "type": self.parse_type(arg_type["qualType"]),
                    "t": T_builtin("long")}
        return {"k": "sizeofexpr",
                "e": self.translate_expr(node["inner"][0]),
                "t": T_builtin("long")}

    expr_UnaryExprOrTypeTraitExpr = expr_SizeOfExpr

    def expr_LambdaExpr(self, node):
        closure = node["inner"][0]
        for ch in closure.get("inner", []):
            if ch.get("kind") == "FieldDecl":
                raise Refuse("capturing-lambda",
                             "v1 subset: only non-capturing lambdas "
                             "lower")
        op = None
        for ch in closure.get("inner", []):
            if ch.get("kind") == "CXXMethodDecl" and \
                    ch.get("name") == "operator()":
                op = ch
                break
        if op is None:
            raise Refuse("parse-error", "lambda without operator()")
        ret_s, _ps, _c = self.fn_type_parts(op["type"]["qualType"])
        params = []
        for pch in op.get("inner", []):
            if pch.get("kind") == "ParmVarDecl":
                params.append((pch.get("name", ""),
                               self.parse_type(pch["type"]["qualType"]),
                               None))
        if ret_s == "auto":
            # clang spells a lambda call operator's type with the
            # placeholder 'auto' even when a trailing return type is
            # written; recover the written type from the source, and
            # refuse a value-returning lambda without one — the same
            # rule the subset parser applies (track parity).
            b = (node.get("range") or {}).get("begin", {}).get("offset")
            seg = ""
            if b is not None:
                brace = self.src.find("{", b)
                seg = self.src[b:brace if brace >= 0 else b + 200]
            if "->" in seg:
                ret = self.parse_type(seg.split("->", 1)[1].strip())
            else:
                raise Refuse("lambda-return-type",
                             "value-returning lambda without an "
                             "explicit -> return type")
        else:
            ret = self.parse_type(ret_s)
        name = f"__lambda_{self.lambda_n}"
        self.lambda_n += 1
        f = {"qname": (name,), "ret": ret, "params": params,
             "body": None, "line": self.line_of(node), "lambda": True,
             "node": op}
        self.tu.funcs.append(f)
        # translate the body now, with its params in scope
        saved_scopes = self.scopes
        scope = {}
        for pch in op.get("inner", []):
            if pch.get("kind") == "ParmVarDecl" and pch.get("name"):
                pt = self.parse_type(pch["type"]["qualType"])
                scope[pch["id"]] = (pch["name"], pt,
                                    strip_const(pt)[0] == "ref")
        self.scopes = [scope]
        saved_class = self.cur_class
        self.cur_class = None
        body_node = self.body_of(op)
        if body_node is None and len(node.get("inner", [])) > 1:
            body_node = node["inner"][1]
        f["body"] = self.translate_block(body_node)
        self.scopes = saved_scopes
        self.cur_class = saved_class
        fty = ("fnptr", ret, tuple(p[1] for p in params))
        return {"k": "lambda", "t": fty, "_func_id": id(f),
                "line": self.line_of(node)}

def fnlike_param(fnlike, name):
    for (n, t, _d) in fnlike.get("params", []):
        if n == name:
            return t
    raise Refuse("parse-error", f"param {name} not in signature")

def lower_source(src, cpp_name, clangxx="clang++"):
    proc = subprocess.run(
        [clangxx, "-std=c++23", "-x", "c++", "-Xclang", "-ast-dump=json",
         "-fsyntax-only", "-"],
        input=src, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Refuse("clang-error",
                     "clang++ rejected the TU: " +
                     proc.stderr.strip().split("\n")[0][:200])
    ast = json.loads(proc.stdout)
    ct = ClangTU(ast, src, cpp_name)
    tu = ct.translate()
    fix_dispatch(tu)
    return tu

def fix_dispatch(tu):
    """Recompute virtual dispatch on method calls from the final
    class hierarchy: a call dispatches iff the resolved method's
    signature occupies a vtable slot of the object's static type
    (the same rule the emitter uses for slot occupancy)."""
    sem = Semantics(tu)

    def visit(e):
        if isinstance(e, dict):
            if e.get("k") == "call" and \
                    isinstance(e.get("callee"), tuple) and \
                    e["callee"][0] == "method":
                _, obj, owner, m, _d = e["callee"]
                ot = strip_const(obj["t"])
                if ot[0] == "ref":
                    ot = strip_const(ot[1])
                if ot[0] == "ptr":
                    ot = strip_const(ot[1])
                disp = False
                if ot[0] == "class" and not e.get("qualified"):
                    sig = Semantics._sig(m)
                    disp = any(Semantics._sig(s) == sig
                               for s in sem.slots(ot[1])
                               if s["name"] != "~dtor")
                e["callee"] = ("method", obj, owner, m, disp)
            for v in e.values():
                visit(v)
        elif isinstance(e, list):
            for v in e:
                visit(v)

    def visit_fn(f):
        if f.get("body"):
            visit(f["body"])
        for (_n, _t, d) in f.get("params", []):
            if isinstance(d, dict):
                visit(d)

    for f in tu.funcs:
        visit_fn(f)
    for c in tu.classes:
        for m in c["methods"]:
            if m.get("body"):
                visit(m["body"])
            for (_n, _t, d) in m.get("params", []):
                if isinstance(d, dict):
                    visit(d)
        for ct in c["ctors"]:
            if ct.get("body"):
                visit(ct["body"])
            for (_k, _n, args) in ct.get("inits", []):
                visit(args)
        dt = c.get("dtor")
        if dt and isinstance(dt.get("body"), list):
            visit(dt["body"])
    for g in tu.globals:
        if isinstance(g.get("init"), dict):
            visit(g["init"])

if __name__ == "__main__":
    src = open(sys.argv[1]).read()
    tu = lower_source(src, sys.argv[1].rsplit("/", 1)[-1])
    from phcpp_ir import Emitter
    text, members = Emitter(tu).emit()
    sys.stdout.write(text)
