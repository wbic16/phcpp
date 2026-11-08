#!/usr/bin/env python3
"""phcpp_track1.py — phcpp front-end v1, Track 1: hand-rolled subset parser.

Parses the C++23RA v1 subset (docs/DESIGN.md SS8) directly from source
text into the shared IR (tools/phcpp_ir.py). Two phases: declarations
first (the full registry — classes, signatures, template patterns),
then bodies, parsed from captured token spans with the complete
registry in scope. Template bodies are parsed per instantiation under
a type-substitution environment, so dependent names resolve against
the concrete types — exactly where a subset parser can be honest.

Anything outside the subset is refused by name (Refuse codes are the
shared registry with Track 2); anything unparseable is a loud parse
error. Nothing is ever silently mis-lowered.
"""
import re
import sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from phcpp_ir import (TU, Refuse, T_builtin, T_class, T_ptr, T_ref,
                      T_array, T_fnptr, strip_const, base_type,
                      is_class_t, class_qname, Semantics, cname,
                      mangle_type)

# ------------------------------------------------------------ lexer
MULTI = ["::", "->", "++", "--", "+=", "-=", "*=", "/=", "%=", "==",
         "!=", "<=", ">=", "&&", "||", "<<", ">>", "&=", "|=", "^="]
SINGLE = set("+-*/%<>=!&|^~?:;,.()[]{}#")

class Tok:
    __slots__ = ("kind", "text", "line")
    def __init__(self, kind, text, line):
        self.kind, self.text, self.line = kind, text, line
    def __repr__(self):
        return f"<{self.kind} {self.text!r} @{self.line}>"

def lex(src):
    toks = []
    includes = []
    i, n, line = 0, len(src), 1
    bol = True
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
            bol = True
            continue
        if ch in " \t\r":
            i += 1
            continue
        if bol and ch == "#":
            j = src.find("\n", i)
            if j < 0:
                j = n
            text = src[i:j].strip()
            if text.startswith("#include"):
                includes.append(text[len("#include"):].strip())
            elif text == "#pragma once":
                pass
            else:
                raise Refuse("preprocessor-directive", text)
            i = j
            continue
        bol = False
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise Refuse("parse-error", f"unterminated comment @{line}")
            line += src.count("\n", i, j)
            i = j + 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            toks.append(Tok("str", src[i:j + 1], line))
            i = j + 1
            continue
        if ch == "'":
            j = i + 1
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            toks.append(Tok("char", src[i:j + 1], line))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isalnum() or src[j] in "._"):
                if src[j] in "eE" and j + 1 < n and src[j + 1] in "+-":
                    j += 1
                j += 1
            toks.append(Tok("num", src[i:j], line))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(Tok("id", src[i:j], line))
            i = j
            continue
        matched = None
        for m in MULTI:
            if src.startswith(m, i):
                matched = m
                break
        if matched:
            toks.append(Tok("punct", matched, line))
            i += len(matched)
            continue
        if ch in SINGLE:
            toks.append(Tok("punct", ch, line))
            i += 1
            continue
        raise Refuse("parse-error", f"unexpected character {ch!r} @{line}")
    toks.append(Tok("eof", "", line))
    return toks, includes

REFUSE_KEYWORDS = {
    "try": "exceptions", "catch": "exceptions", "throw": "exceptions",
    "typeid": "rtti-typeid", "enum": "enum", "union": "union",
    "friend": "friend", "concept": "concept-requires",
    "requires": "concept-requires", "co_await": "coroutine",
    "co_return": "coroutine", "co_yield": "coroutine",
}

class Parser:
    def __init__(self, src, cpp_name):
        self.toks, self.includes = lex(src)
        # The most specific refusal wins: coroutine keywords are
        # recognised in the raw text before header discipline.
        for w in ("co_await", "co_return", "co_yield"):
            if re.search(rf"\b{w}\b", src):
                raise Refuse("coroutine",
                             f"{w}: coroutines are outside the subset")
        STL_HEADERS = {"<vector>", "<string>", "<map>", "<set>",
                       "<unordered_map>", "<unordered_set>", "<list>",
                       "<deque>", "<algorithm>", "<memory>",
                       "<iostream>", "<sstream>", "<array>", "<tuple>",
                       "<optional>", "<variant>", "<functional>"}
        for inc in self.includes:
            if inc in STL_HEADERS:
                raise Refuse("stl-facility",
                             f"#include {inc}: STL facilities are "
                             f"outside the v1 subset")
            if inc != "<cstdio>":
                raise Refuse("include-outside-subset",
                             f"#include {inc}: only <cstdio> is in the "
                             f"v1 subset")
        self.pos = 0
        self.tu = TU(cpp_name)
        self.ns = ()
        self.aliases = {}             # (ns_path, name) -> type
        self.func_templates = {}      # qname -> pattern FuncIR
        self.class_templates = {}     # qname -> pattern ClassIR
        self.inst_funcs = {}          # (qname, args-key) -> FuncIR
        self.inst_classes = {}        # (qname, args-key) -> qname
        self.pending_bodies = []      # (kind, owner_q, obj, span, init, subst)
        self.lambda_n = 0

    # ---------- token plumbing ----------
    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def at(self, text):
        return self.peek().text == text

    def at_id(self, word=None):
        t = self.peek()
        return t.kind == "id" and (word is None or t.text == word)

    def eat(self, text):
        if self.at(text):
            self.pos += 1
            return True
        return False

    def expect(self, text):
        t = self.next()
        if t.text != text:
            raise Refuse("parse-error",
                         f"expected {text!r}, got {t.text!r} @{t.line}")
        return t

    def expect_gt(self):
        """Consume one '>' of a template close, splitting a '>>' token."""
        if self.at(">"):
            self.next()
            return
        if self.at(">>"):
            t = self.peek()
            self.toks[self.pos] = Tok("punct", ">", t.line)
            self.toks.insert(self.pos + 1, Tok("punct", ">", t.line))
            self.next()
            return
        t = self.peek()
        raise Refuse("parse-error", f"expected '>', got {t.text!r} @{t.line}")

    def kw_check(self):
        t = self.peek()
        if t.kind == "id" and t.text in REFUSE_KEYWORDS:
            raise Refuse(REFUSE_KEYWORDS[t.text],
                         f"keyword {t.text} @{t.line}")

    def capture_block(self):
        start = self.pos
        self.expect("{")
        depth = 1
        while depth:
            t = self.next()
            if t.kind == "eof":
                raise Refuse("parse-error", "unterminated block")
            if t.text == "{":
                depth += 1
            elif t.text == "}":
                depth -= 1
        return (start, self.pos)

    def capture_expr_span(self):
        """Capture tokens up to the top-level ';' (consumed)."""
        start = self.pos
        depth = 0
        while True:
            t = self.peek()
            if t.kind == "eof":
                raise Refuse("parse-error", "unterminated expression")
            if t.text in ("(", "[", "{"):
                depth += 1
            elif t.text in (")", "]", "}"):
                depth -= 1
            elif t.text == ";" and depth == 0:
                end = self.pos
                self.next()
                return (start, end)
            self.next()

    # ---------- resolution ----------
    def resolve_class(self, parts):
        parts = tuple(parts)
        if not parts:
            return None
        if parts in self.tu.class_by_qname or parts in self.class_templates:
            return parts
        for cut in range(len(self.ns), -1, -1):
            cand = self.ns[:cut] + parts
            if cand in self.tu.class_by_qname or \
                    cand in self.class_templates:
                return cand
        return None

    def resolve_alias(self, parts):
        parts = tuple(parts)
        for cut in range(len(self.ns), -1, -1):
            cand = self.ns[:cut] + parts
            if (cand[:-1], cand[-1]) in self.aliases:
                return self.aliases[(cand[:-1], cand[-1])]
        return None

    def canonical(self, t):
        if t[0] == "const" and t[1][0] == "ptr":
            return t[1]
        if t[0] == "ptr":
            return ("ptr", self.canonical(t[1]))
        if t[0] == "ref":
            return ("ref", self.canonical(t[1]))
        if t[0] == "array":
            return ("array", self.canonical(t[1]), t[2])
        if t[0] == "const":
            return ("const", self.canonical(t[1]))
        return t

    # ---------- names ----------
    def parse_qualified_name(self):
        """Returns (parts, end_positions): end_positions[k] is the token
        index just after the k-th part (before any following '::')."""
        parts, ends = [], []
        if self.at("::"):
            self.next()
        while self.peek().kind == "id":
            parts.append(self.next().text)
            ends.append(self.pos)
            if self.at("::"):
                self.next()
                continue
            break
        return parts, ends

    # ---------- types ----------
    def parse_type(self, subst=None):
        is_const = self.eat("const")
        t = self.peek()
        if t.kind == "id" and t.text in REFUSE_KEYWORDS:
            self.kw_check()
        if subst and t.kind == "id" and t.text in subst and \
                self.peek(1).text != "::":
            self.next()
            base = subst[t.text]
            if is_const:
                base = ("const", base)
            return self._type_postfix(base)
        if t.text == "unsigned":
            self.next()
            if self.at_id("int"):
                self.next()
                base = T_builtin("uint")
            elif self.at_id("long"):
                self.next()
                base = T_builtin("long")
            elif self.at_id("char"):
                self.next()
                base = T_builtin("char")
            else:
                base = T_builtin("uint")
            if is_const:
                base = ("const", base)
            return self._type_postfix(base)
        if t.text in ("void", "bool", "char", "int", "long", "float",
                      "double"):
            self.next()
            base = T_builtin(t.text)
            if is_const:
                base = ("const", base)
            return self._type_postfix(base)
        if t.text == "short":
            raise Refuse("parse-error", "short is outside the v1 type set")
        # named type: backtrack to the longest resolving prefix
        start = self.pos
        parts, ends = self.parse_qualified_name()
        if not parts:
            raise Refuse("parse-error", f"type expected @{t.line}")
        if parts[0] == "std":
            if parts == ["std", "printf"]:
                raise Refuse("parse-error", "printf is not a type")
            raise Refuse("stl-facility",
                         f"std::{'::'.join(parts[1:])} is outside the "
                         f"v1 subset")
        base = None
        for k in range(len(parts), 0, -1):
            prefix = tuple(parts[:k])
            if subst and len(prefix) == 1 and prefix[0] in subst:
                base = subst[prefix[0]]
                self.pos = ends[k - 1]
                break
            alias = self.resolve_alias(prefix)
            if alias is not None:
                base = alias
                self.pos = ends[k - 1]
                break
            q = self.resolve_class(prefix)
            if q is not None:
                self.pos = ends[k - 1]
                if self.at("<"):
                    args = self.parse_template_args(subst)
                    base = T_class(self.instantiate_class(q, args))
                elif q in self.class_templates:
                    raise Refuse("parse-error",
                                 f"class template {'::'.join(q)} needs "
                                 f"arguments @{t.line}")
                else:
                    base = T_class(q)
                break
        if base is None:
            self.pos = start
            raise Refuse("parse-error",
                         f"unknown type {'::'.join(parts)} @{t.line}")
        if is_const:
            base = ("const", base)
        return self._type_postfix(base)

    def _type_postfix(self, base):
        while True:
            if self.at("*"):
                self.next()
                self.eat("const")
                base = ("ptr", base)
            elif self.at("&"):
                self.next()
                base = ("ref", base)
            elif self.at("&&"):
                raise Refuse("rvalue-reference",
                             "rvalue references are outside the subset")
            else:
                return self.canonical(base)

    def parse_template_args(self, subst=None):
        self.expect("<")
        args = []
        while True:
            args.append(self.parse_type(subst))
            if self.eat(","):
                continue
            break
        self.expect_gt()
        return args

    def subst_type(self, t, subst):
        if not subst:
            return t
        if t[0] == "tparam":
            return subst[t[1]]
        if t[0] == "ptr":
            return ("ptr", self.subst_type(t[1], subst))
        if t[0] == "ref":
            return ("ref", self.subst_type(t[1], subst))
        if t[0] == "const":
            return ("const", self.subst_type(t[1], subst))
        if t[0] == "array":
            return ("array", self.subst_type(t[1], subst), t[2])
        if t[0] == "fnptr":
            return ("fnptr", self.subst_type(t[1], subst),
                    tuple(self.subst_type(p, subst) for p in t[2]))
        return t

    # ---------- template instantiation ----------
    def instantiate_class(self, q, args):
        key = (q, tuple(args))
        if key in self.inst_classes:
            return self.inst_classes[key]
        pat = self.class_templates[q]
        subst = dict(zip(pat["tparams"], args))
        inst_qname = q[:-1] + (q[-1] + "__" + "_".join(
            mangle_type(a) for a in args),)
        c = {"qname": inst_qname, "base": None, "base_type": None,
             "fields": [], "methods": [], "ctors": [], "dtor": None,
             "line": pat["line"],
             "template_inst": {"template": q, "args": list(args)},
             "tparams": []}
        self.inst_classes[key] = inst_qname
        self.tu.add_class(c)
        if pat.get("base_type") is not None:
            bt = self.subst_type(pat["base_type"], subst)
            c["base_type"] = bt
            if is_class_t(bt):
                c["base"] = class_qname(bt)
        for f in pat["fields"]:
            nf = dict(f)
            nf["type"] = self.subst_type(f["type"], subst)
            c["fields"].append(nf)
        for m in pat["methods"]:
            nm = dict(m)
            nm["ret"] = self.subst_type(m["ret"], subst)
            nm["params"] = [(n, self.subst_type(t, subst), d)
                            for (n, t, d) in m["params"]]
            c["methods"].append(nm)
            if m.get("body_span") is not None:
                self.pending_bodies.append(
                    ("method", inst_qname, nm, m["body_span"], None, subst))
        for ct in pat["ctors"]:
            nct = dict(ct)
            nct["params"] = [(n, self.subst_type(t, subst), d)
                             for (n, t, d) in ct["params"]]
            c["ctors"].append(nct)
            if ct.get("body_span") is not None:
                self.pending_bodies.append(
                    ("ctor", inst_qname, nct, ct["body_span"],
                     ct.get("init_span"), subst))
        if pat.get("dtor"):
            nd = dict(pat["dtor"])
            c["dtor"] = nd
            if nd.get("body_span") is not None:
                self.pending_bodies.append(
                    ("dtor", inst_qname, nd, nd["body_span"], None, subst))
        return inst_qname

    def instantiate_func(self, q, texpr_args, args, subst):
        pat = self.func_templates[q]
        if texpr_args:
            targs = texpr_args
        else:
            deduced = {}
            for p, a in zip(pat["params"], args):
                self.deduce(p[1], a["t"], pat["tparams"], deduced)
            targs = []
            for tp in pat["tparams"]:
                if tp not in deduced:
                    raise Refuse("no-matching-function",
                                 f"cannot deduce {tp} for {cname(q)}")
                targs.append(deduced[tp])
        key = (q, tuple(targs))
        if key in self.inst_funcs:
            return self.inst_funcs[key]
        s2 = dict(zip(pat["tparams"], targs))
        inst_qname = q[:-1] + (q[-1] + "__" + "_".join(
            mangle_type(a) for a in targs),)
        f = {"qname": inst_qname,
             "ret": self.subst_type(pat["ret"], s2),
             "params": [(n, self.subst_type(t, s2), d)
                        for (n, t, d) in pat["params"]],
             "body": None, "line": pat["line"],
             "template_inst": {"template": q, "args": list(targs)},
             "body_span": pat["body_span"]}
        self.inst_funcs[key] = f
        self.tu.funcs.append(f)
        if pat["body_span"] is not None:
            saved_pos, saved_ns = self.pos, self.ns
            self.ns = q[:-1]
            env = Env(self, None, f)
            self.pos = pat["body_span"][0]
            f["body"] = self.parse_body_block(env, s2)
            self.pos, self.ns = saved_pos, saved_ns
        return f

    def deduce(self, pt, at, tparams, out):
        if pt[0] == "tparam":
            out[pt[1]] = strip_const(at)
            return
        if pt[0] == "const":
            self.deduce(pt[1], strip_const(at), tparams, out)
            return
        if pt[0] in ("ptr", "ref"):
            inner = strip_const(at)
            if inner[0] in ("ptr", "ref"):
                self.deduce(pt[1], inner[1], tparams, out)

    # ---------- declarators / params ----------
    def parse_declarator(self, base):
        stars = 0
        while self.at("*"):
            self.next()
            self.eat("const")
            stars += 1
        if self.at("&"):
            self.next()
            if self.at("&"):
                raise Refuse("rvalue-reference", "rvalue reference")
            base = ("ref", base)
        if self.at("("):
            self.next()
            self.expect("*")
            name = self.next().text
            self.expect(")")
            self.expect("(")
            params = self.parse_param_types()
            self.expect(")")
            ty = base
            for _ in range(stars):
                ty = ("ptr", ty)
            return name, self.canonical(("fnptr", ty, tuple(params)))
        name_tok = self.next()
        if name_tok.kind != "id":
            raise Refuse("parse-error",
                         f"declarator name expected @{name_tok.line}")
        ty = base
        for _ in range(stars):
            ty = ("ptr", ty)
        if self.at("["):
            self.next()
            n = self.next()
            if n.kind != "num":
                raise Refuse("parse-error", "array size must be a literal")
            self.expect("]")
            ty = ("array", ty, int(n.text))
        return name_tok.text, self.canonical(ty)

    def parse_param_types(self):
        types = []
        if self.at(")"):
            return types
        if self.at_id("void") and self.peek(1).text == ")":
            self.next()
            return types
        while True:
            types.append(self.parse_type())
            if self.peek().kind == "id":
                self.next()
                if self.at("["):
                    self.next()
                    self.next()
                    self.expect("]")
            if self.eat(","):
                continue
            break
        return types

    def parse_params(self, subst=None):
        self.expect("(")
        params = []
        if self.at(")"):
            self.next()
            return params
        if self.at_id("void") and self.peek(1).text == ")":
            self.next()
            self.expect(")")
            return params
        while True:
            ty = self.parse_type(subst)
            name = ""
            default = None
            while self.at("*"):
                self.next()
                self.eat("const")
                ty = ("ptr", ty)
            if self.at("&"):
                self.next()
                if self.at("&"):
                    raise Refuse("rvalue-reference", "rvalue reference")
                ty = ("ref", ty)
            if self.peek().kind == "id":
                name = self.next().text
            if self.at("["):
                self.next()
                n = self.next()
                self.expect("]")
                if n.kind == "num":
                    ty = ("ptr", strip_const(ty))
            if self.at("="):
                self.next()
                default = self.parse_expr(subst, None)
            params.append((name, self.canonical(ty), default))
            if self.eat(","):
                continue
            break
        self.expect(")")
        return params

    # ---------- top level ----------
    def parse(self):
        while self.peek().kind != "eof":
            self.parse_toplevel()
        self.parse_pending_bodies()
        return self.tu

    def parse_toplevel(self):
        self.kw_check()
        t = self.peek()
        if t.text == "namespace":
            self.next()
            name = self.next().text
            self.expect("{")
            saved = self.ns
            self.ns = self.ns + (name,)
            while not self.at("}"):
                self.parse_toplevel()
            self.expect("}")
            self.eat(";")
            self.ns = saved
            return
        if t.text == "template":
            self.parse_template_decl()
            return
        if t.text in ("struct", "class"):
            self.parse_class(None, None)
            if not self.at(";"):
                raise Refuse("parse-error",
                             "class definition followed by declarators "
                             "is outside the subset")
            self.expect(";")
            return
        if t.text == "typedef":
            self.next()
            ty = self.parse_type()
            name, ty = self.parse_declarator(ty)
            self.aliases[(self.ns, name)] = ty
            self.expect(";")
            return
        if t.text == "using":
            self.next()
            if self.at("namespace"):
                raise Refuse("using-namespace",
                             "using-directives are outside the subset")
            name = self.next().text
            if not self.eat("="):
                raise Refuse("using-declaration",
                             "only type aliases (using X = T) are in "
                             "the subset")
            self.aliases[(self.ns, name)] = self.parse_type()
            self.expect(";")
            return
        if t.text == "extern":
            raise Refuse("linkage-spec",
                         "extern blocks are outside the subset")
        if t.text == "operator":
            raise Refuse("operator-overload",
                         "operator declarations are outside the subset")
        self.parse_external_decl()

    def parse_template_decl(self):
        line = self.next().line
        self.expect("<")
        tparams = []
        while True:
            if self.at_id("typename") or self.at_id("class"):
                self.next()
            tparams.append(self.next().text)
            if self.eat(","):
                continue
            break
        self.expect_gt()
        subst = {tp: ("tparam", tp) for tp in tparams}
        if self.peek().text in ("struct", "class"):
            self.parse_class(tparams, subst)
            self.expect(";")
            return
        ret = self.parse_type(subst)
        parts, _ = self.parse_qualified_name()
        q = self.ns + tuple(parts)
        params = self.parse_params(subst)
        f = {"qname": q, "ret": ret, "params": params, "body": None,
             "line": line, "tparams": tparams, "body_span": None}
        if self.at("{"):
            f["body_span"] = self.capture_block()
        else:
            self.expect(";")
        self.func_templates[q] = f

    # ---------- classes ----------
    def parse_class(self, tparams, subst):
        kw = self.next()
        line = kw.line
        name_tok = self.next()
        if name_tok.kind != "id":
            raise Refuse("parse-error", f"class name expected @{line}")
        name = name_tok.text
        q = self.ns + (name,)
        self.eat("final")
        base_q = None
        base_type = None
        if self.at(":"):
            self.next()
            while True:
                if self.at_id("virtual"):
                    raise Refuse("virtual-inheritance",
                                 f"{name}: virtual bases are outside "
                                 f"the subset")
                self.eat("public") or self.eat("protected") or \
                    self.eat("private")
                if self.at_id("virtual"):
                    raise Refuse("virtual-inheritance",
                                 f"{name}: virtual bases are outside "
                                 f"the subset")
                bt = self.parse_type(subst)
                if not is_class_t(bt):
                    raise Refuse("parse-error", f"base of {name} not a class")
                base_type = bt
                base_q = class_qname(bt)
                if self.at(","):
                    raise Refuse("multiple-inheritance",
                                 f"{name}: multiple bases are outside "
                                 f"the subset")
                break
        c = {"qname": q, "base": base_q, "base_type": base_type,
             "fields": [], "methods": [], "ctors": [], "dtor": None,
             "line": line, "template_inst": None,
             "tparams": tparams or []}
        if tparams:
            c["pattern"] = True
            self.class_templates[q] = c
        else:
            self.tu.add_class(c)
        self.expect("{")
        self.parse_class_body(c, subst)
        self.expect("}")
        return c

    def parse_class_body(self, c, subst):
        q = c["qname"]
        pattern = bool(c.get("pattern"))
        while not self.at("}"):
            self.kw_check()
            t = self.peek()
            if t.text in ("public", "private", "protected") and \
                    self.peek(1).text == ":":
                self.next()
                self.next()
                continue
            if t.text in ("struct", "class"):
                raise Refuse("nested-class",
                             f"{q[-1]}: nested classes are outside "
                             f"the subset")
            if t.text == "template":
                raise Refuse("member-template",
                             f"{q[-1]}: member templates are outside "
                             f"the subset")
            if t.text == "operator":
                raise Refuse("operator-overload",
                             f"{q[-1]}: operator overloads are outside "
                             f"the subset")
            if t.text == "using":
                self.next()
                nm = self.next().text
                self.expect("=")
                self.aliases[(q, nm)] = self.parse_type(subst)
                self.expect(";")
                continue
            self.eat("explicit")
            is_static = self.eat("static")
            is_virtual = self.eat("virtual")
            if self.at("~"):
                self.next()
                self.next()
                self.parse_params(subst)
                dt = {"virtual": is_virtual, "body": None, "line": t.line,
                      "body_span": None}
                if self.at("="):
                    self.next()
                    if self.at_id("default"):
                        self.next()
                        dt["body"] = "default"
                        self.expect(";")
                    else:
                        raise Refuse("parse-error",
                                     "only = default destructors are "
                                     "supported")
                elif self.at("{"):
                    dt["body_span"] = self.capture_block()
                    dt["body"] = "span"
                    if not pattern:
                        self.pending_bodies.append(
                            ("dtor", q, dt, dt["body_span"], None, None))
                else:
                    self.expect(";")
                c["dtor"] = dt
                continue
            if self.at_id(q[-1]) and self.peek(1).text == "(" and \
                    not is_static:
                self.next()
                params = self.parse_params(subst)
                if len(params) == 1 and \
                        strip_const(params[0][1])[0] == "ref" and \
                        is_class_t(strip_const(params[0][1])[1]) and \
                        class_qname(strip_const(params[0][1])[1]) == q:
                    raise Refuse("user-copy-ctor",
                                 f"{q[-1]}: user-declared copy "
                                 f"constructors are outside the subset "
                                 f"(copies are memberwise in v1)")
                ct = {"params": params, "inits": [], "body": None,
                      "line": t.line, "body_span": None, "init_span": None}
                if self.at(":"):
                    ct["init_span"] = self.capture_init_list()
                if self.at("="):
                    self.next()
                    self.next()
                    ct["body"] = []
                    self.expect(";")
                elif self.at("{"):
                    ct["body_span"] = self.capture_block()
                    ct["body"] = "span"
                    if not pattern:
                        self.pending_bodies.append(
                            ("ctor", q, ct, ct["body_span"],
                             ct["init_span"], None))
                else:
                    self.expect(";")
                c["ctors"].append(ct)
                continue
            ty = self.parse_type(subst)
            if self.at_id("operator"):
                raise Refuse("operator-overload",
                             f"{q[-1]}: operator overloads are outside "
                             f"the subset")
            name, ty = self.parse_declarator(ty)
            if self.at("("):
                params = self.parse_params(subst)
                is_const = self.eat("const")
                self.eat("override")
                self.eat("final")
                m = {"name": name, "ret": ty, "params": params,
                     "virtual": is_virtual, "static": is_static,
                     "const": is_const, "pure": False, "body": None,
                     "line": t.line, "body_span": None}
                if self.at("="):
                    self.next()
                    if self.peek().text == "0":
                        self.next()
                        m["pure"] = True
                        m["virtual"] = True
                        self.expect(";")
                    elif self.at_id("default"):
                        self.next()
                        m["body"] = []
                        self.expect(";")
                    else:
                        raise Refuse("parse-error", "unsupported = clause")
                elif self.at("{"):
                    m["body_span"] = self.capture_block()
                    m["body"] = "span"
                    if not pattern:
                        self.pending_bodies.append(
                            ("method", q, m, m["body_span"], None, None))
                else:
                    self.expect(";")
                c["methods"].append(m)
            else:
                if self.at("=") or self.at("{"):
                    raise Refuse("default-member-initializer",
                                 f"{q[-1]}::{name}: NSDMI is outside "
                                 f"the subset")
                self.expect(";")
                c["fields"].append({"name": name, "type": ty,
                                    "static": is_static, "defined": False,
                                    "init": None})

    def capture_init_list(self):
        start = self.pos
        self.expect(":")
        depth = 0
        while True:
            t = self.peek()
            if t.kind == "eof":
                raise Refuse("parse-error", "unterminated init list")
            if t.text == "{" and depth == 0:
                break
            if t.text in ("(", "["):
                depth += 1
            elif t.text in (")", "]"):
                depth -= 1
            self.next()
        return (start, self.pos)

    # ---------- external declarations ----------
    def parse_external_decl(self):
        line = self.peek().line
        # out-of-line ctor/dtor: Name::Name(...) / Name::~Name(...)
        t0, t1 = self.peek(0), self.peek(1)
        if t0.kind == "id" and t1.text == "::":
            t2 = self.peek(2)
            if t2.kind == "id" and t2.text == t0.text and \
                    self.peek(3).text in ("(", ":"):
                cls_q = self.resolve_class([t0.text])
                if cls_q is not None:
                    self.parse_outofline_ctor(cls_q)
                    return
            if t2.text == "~" and self.peek(3).kind == "id" and \
                    self.peek(3).text == t0.text:
                cls_q = self.resolve_class([t0.text])
                if cls_q is not None:
                    self.parse_outofline_dtor(cls_q)
                    return
        ty = self.parse_type()
        if self.at_id("operator"):
            raise Refuse("operator-overload",
                         "operator overloads are outside the subset")
        parts, _ = self.parse_qualified_name()
        if len(parts) >= 2:
            cls_q = self.resolve_class(parts[:-1])
            if cls_q is not None:
                self.parse_member_def(cls_q, ty, parts[-1], line)
                return
        if not parts:
            raise Refuse("parse-error", f"declaration expected @{line}")
        q = self.ns + tuple(parts)
        if self.at("("):
            params = self.parse_params()
            f = {"qname": q, "ret": ty, "params": params, "body": None,
                 "line": line, "body_span": None}
            if self.at("{"):
                f["body_span"] = self.capture_block()
                f["body"] = "span"
                self.pending_bodies.append(
                    ("func", None, f, f["body_span"], None, None))
            else:
                self.expect(";")
            self.tu.funcs.append(f)
            return
        # global variable (possibly with ctor-form/paren init)
        g = {"qname": q, "type": ty, "init": None, "line": line}
        if self.at("("):
            self.next()
            span = self.capture_until(")")
            g["init_form"] = "ctorargs"
            g["init_span"] = span
            self.pending_bodies.append(
                ("global-init", None, g, None, None, None))
        elif self.at("="):
            self.next()
            g["init_span"] = self.capture_expr_span()
            g["init_form"] = "expr"
            self.pending_bodies.append(
                ("global-init", None, g, None, None, None))
        else:
            self.expect(";")
        self.tu.globals.append(g)

    def capture_until(self, close):
        start = self.pos
        depth = 0
        while True:
            t = self.peek()
            if t.kind == "eof":
                raise Refuse("parse-error", "unterminated list")
            if t.text in ("(", "[", "{"):
                depth += 1
            elif t.text in (")", "]", "}"):
                if depth == 0 and t.text == close:
                    end = self.pos
                    self.next()
                    self.expect(";") if close == ")" and False else None
                    return (start, end)
                depth -= 1
            self.next()

    def parse_member_def(self, cls_q, ret_ty, mname, line):
        c = self.tu.class_by_qname[cls_q]
        if self.at("("):
            params = self.parse_params()
            is_const = self.eat("const")
            init_span = None
            if self.at(":"):
                init_span = self.capture_init_list()
            for m in c["methods"]:
                if m["name"] == mname and \
                        len(m["params"]) == len(params) and \
                        all(strip_const(a[1]) == strip_const(b[1])
                            for a, b in zip(m["params"], params)):
                    if self.at("{"):
                        m["body_span"] = self.capture_block()
                        m["body"] = "span"
                        self.pending_bodies.append(
                            ("method", cls_q, m, m["body_span"], None, None))
                    else:
                        self.expect(";")
                    return
            raise Refuse("parse-error",
                         f"out-of-line definition of undeclared method "
                         f"{mname}")
        for f in c["fields"]:
            if f["name"] == mname and f.get("static"):
                f["defined"] = True
                if self.eat("="):
                    f["init_span"] = self.capture_expr_span()
                    self.pending_bodies.append(
                        ("static-init", cls_q, f, None, None, None))
                else:
                    self.expect(";")
                return
        raise Refuse("parse-error",
                     f"out-of-line definition of unknown member {mname}")

    def parse_outofline_ctor(self, cls_q):
        line = self.peek().line
        self.next()  # class name
        self.expect("::")
        self.next()  # ctor name
        params = self.parse_params()
        c = self.tu.class_by_qname[cls_q]
        ct = None
        for cand in c["ctors"]:
            if len(cand["params"]) == len(params) and \
                    all(strip_const(a[1]) == strip_const(b[1])
                        for a, b in zip(cand["params"], params)):
                ct = cand
                break
        if ct is None:
            ct = {"params": params, "inits": [], "body": None,
                  "line": line, "body_span": None, "init_span": None}
            c["ctors"].append(ct)
        if self.at(":"):
            ct["init_span"] = self.capture_init_list()
        if self.at("{"):
            ct["body_span"] = self.capture_block()
            ct["body"] = "span"
            self.pending_bodies.append(
                ("ctor", cls_q, ct, ct["body_span"], ct["init_span"], None))
        else:
            self.expect(";")

    def parse_outofline_dtor(self, cls_q):
        self.next()
        self.expect("::")
        self.expect("~")
        self.next()
        self.parse_params()
        c = self.tu.class_by_qname[cls_q]
        dt = c.get("dtor")
        if dt is None:
            dt = {"virtual": False, "body": None, "line": self.peek().line,
                  "body_span": None}
            c["dtor"] = dt
        if self.at("{"):
            dt["body_span"] = self.capture_block()
            dt["body"] = "span"
            self.pending_bodies.append(
                ("dtor", cls_q, dt, dt["body_span"], None, None))
        else:
            self.expect(";")

    # ---------- phase B ----------
    def parse_pending_bodies(self):
        self.sem = Semantics(self.tu)
        i = 0
        while i < len(self.pending_bodies):
            kind, owner_q, obj, span, init_span, subst = \
                self.pending_bodies[i]
            i += 1
            saved_ns = self.ns
            if owner_q is not None:
                self.ns = owner_q[:-1]
            elif obj.get("qname"):
                self.ns = obj["qname"][:-1]
            if kind == "static-init":
                if obj.get("init_span") is not None:
                    env = Env(self, None, None)
                    self.pos = obj["init_span"][0]
                    obj["init"] = self.parse_expr(subst, env)
            elif kind == "global-init":
                env = Env(self, None, None)
                if obj.get("init_form") == "ctorargs":
                    self.pos = obj["init_span"][0]
                    args = self.parse_args(env, subst)
                    obj["init"] = {"k": "construct", "type": obj["type"],
                                   "args": args, "t": obj["type"]}
                elif obj.get("init_span") is not None:
                    self.pos = obj["init_span"][0]
                    obj["init"] = self.parse_expr(subst, env)
            else:
                env = Env(self, owner_q, obj)
                if kind == "ctor" and init_span is not None:
                    self.pos = init_span[0]
                    obj["inits"] = self.parse_init_list(owner_q, env, subst)
                if span is not None:
                    self.pos = span[0]
                    obj["body"] = self.parse_body_block(env, subst)
            self.ns = saved_ns

    def parse_init_list(self, owner_q, env, subst):
        self.expect(":")
        inits = []
        while True:
            parts, _ = self.parse_qualified_name()
            name = parts[-1]
            args = []
            if self.at("("):
                self.next()
                args = self.parse_args(env, subst)
                self.expect(")")
            elif self.at("{"):
                self.next()
                args = self.parse_args(env, subst, brace=True)
                self.expect("}")
            c = self.tu.class_by_qname[owner_q]
            if name == owner_q[-1]:
                raise Refuse("delegating-constructor",
                             "delegating constructors are outside the "
                             "subset")
            if c.get("base") and name == c["base"][-1]:
                inits.append(("base", name, args))
            else:
                inits.append(("field", name, args))
            if self.eat(","):
                continue
            break
        return inits

    def parse_args(self, env, subst, brace=False):
        args = []
        close = "}" if brace else ")"
        if self.at(close):
            return args
        while True:
            args.append(self.parse_expr(subst, env))
            if self.eat(","):
                continue
            break
        return args

    def parse_body_block(self, env, subst):
        self.expect("{")
        stmts = self.parse_stmts(env, subst)
        self.expect("}")
        return stmts

    # ---------- statements ----------
    def parse_stmts(self, env, subst):
        out = []
        env.push()
        while not self.at("}"):
            out.append(self.parse_stmt(env, subst))
        env.pop()
        return out

    def parse_stmt(self, env, subst):
        self.kw_check()
        t = self.peek()
        if t.text == "{":
            self.next()
            ss = self.parse_stmts(env, subst)
            self.expect("}")
            return {"k": "block", "ss": ss}
        if t.text == "if":
            self.next()
            self.expect("(")
            c = self.parse_expr(subst, env)
            self.expect(")")
            then = [self.parse_stmt(env, subst)]
            els = None
            if self.at_id("else"):
                self.next()
                els = [self.parse_stmt(env, subst)]
            return {"k": "if", "c": c, "t": then, "e": els}
        if t.text == "while":
            self.next()
            self.expect("(")
            c = self.parse_expr(subst, env)
            self.expect(")")
            return {"k": "while", "c": c,
                    "b": [self.parse_stmt(env, subst)]}
        if t.text == "do":
            self.next()
            body = [self.parse_stmt(env, subst)]
            if not self.at_id("while"):
                raise Refuse("parse-error", "do without while")
            self.next()
            self.expect("(")
            c = self.parse_expr(subst, env)
            self.expect(")")
            self.expect(";")
            return {"k": "dowhile", "c": c, "b": body}
        if t.text == "for":
            return self.parse_for(env, subst)
        if t.text == "return":
            self.next()
            e = None
            if not self.at(";"):
                e = self.parse_expr(subst, env)
            self.expect(";")
            return {"k": "return", "e": e}
        if t.text == "break":
            self.next()
            self.expect(";")
            return {"k": "break"}
        if t.text == "continue":
            self.next()
            self.expect(";")
            return {"k": "continue"}
        if t.text == "switch":
            raise Refuse("switch-statement",
                         "switch is outside the v1 subset")
        if t.text == "goto":
            raise Refuse("goto-statement", "goto is outside the v1 subset")
        if t.text == ";":
            self.next()
            return {"k": "block", "ss": []}
        save = self.pos
        decl = self.try_parse_decl(env, subst)
        if decl is not None:
            return decl
        self.pos = save
        e = self.parse_expr(subst, env)
        self.expect(";")
        return {"k": "expr", "e": e}

    def try_parse_decl(self, env, subst):
        save = self.pos
        t = self.peek()
        is_auto = False
        base = None
        try:
            if self.at_id("const") and self.peek(1).text == "auto":
                self.next()
                self.next()
                is_auto = True
            elif t.text == "auto":
                self.next()
                is_auto = True
            else:
                base = self.parse_type(subst)
            if is_auto:
                if self.peek().kind != "id":
                    return None
                name = self.next().text
                ty = None
            else:
                name, ty = self.parse_declarator(base)
            if not (self.at("(") or self.at("=") or self.at(";") or
                    self.at("{") or self.at(",")):
                return None
        except Refuse as r:
            if r.code == "parse-error":
                self.pos = save
                return None
            raise
        stmts = []
        while True:
            init = None
            if self.at("("):
                if is_auto:
                    raise Refuse("parse-error",
                                 "auto with ctor-form init is unsupported")
                self.next()
                args = self.parse_args(env, subst)
                self.expect(")")
                init = {"form": "ctor", "args": args}
            elif self.at("{"):
                self.next()
                e = {"k": "initlist", "t": ty,
                     "items": self.parse_args(env, subst, brace=True)}
                self.expect("}")
                init = {"form": "list", "e": e}
            elif self.at("="):
                self.next()
                if self.at("{"):
                    self.next()
                    e = {"k": "initlist", "t": ty,
                         "items": self.parse_args(env, subst, brace=True)}
                    self.expect("}")
                    init = {"form": "list", "e": e}
                else:
                    e = self.parse_expr(subst, env)
                    if is_auto:
                        if e["k"] != "lambda":
                            raise Refuse("auto-nonlambda",
                                         "v1 subset: auto declares lambda "
                                         "bindings only")
                        ty = e["t"]
                    init = {"form": "expr", "e": e}
            elif is_auto:
                raise Refuse("parse-error",
                             "auto declaration needs a lambda initializer")
            env.declare(name, ty)
            stmts.append({"k": "decl", "name": name, "type": ty,
                          "init": init})
            if self.eat(","):
                name, ty = self.parse_declarator(base)
                continue
            break
        self.expect(";")
        if len(stmts) == 1:
            return stmts[0]
        # a multi-declarator declaration statement does NOT open a
        # scope in C++; "group" emits inline in the enclosing scope
        return {"k": "group", "ss": stmts}

    def parse_for(self, env, subst):
        self.expect("for")
        self.expect("(")
        # range-for with auto: for ( [const] auto [&] name : expr )
        save = self.pos
        auto_decl = None
        p = self.pos
        self.eat("const")
        if self.at_id("auto"):
            self.next()
            is_ref = self.eat("&")
            if self.peek().kind == "id" and self.peek(1).text == ":":
                auto_decl = (self.next().text, is_ref)
            else:
                auto_decl = None
        if auto_decl is not None:
            self.expect(":")
            rng = self.parse_expr(subst, env)
            self.expect(")")
            rt = strip_const(rng["t"])
            if rt[0] != "array":
                raise Refuse("range-for-nonarray",
                             "v1 range-for is over arrays only")
            elem = rt[1]
            ty = ("ref", elem) if auto_decl[1] else elem
            env.push()
            env.declare(auto_decl[0], ty)
            body = [self.parse_stmt(env, subst)]
            env.pop()
            return {"k": "rangefor", "name": auto_decl[0], "type": ty,
                    "range": rng, "b": body}
        self.pos = save
        try:
            base = self.parse_type(subst)
            name, ty = self.parse_declarator(base)
            if self.at(":"):
                self.next()
                rng = self.parse_expr(subst, env)
                self.expect(")")
                rt = strip_const(rng["t"])
                if rt[0] != "array":
                    raise Refuse("range-for-nonarray",
                                 "v1 range-for is over arrays only")
                env.push()
                env.declare(name, ty)
                body = [self.parse_stmt(env, subst)]
                env.pop()
                return {"k": "rangefor", "name": name, "type": ty,
                        "range": rng, "b": body}
        except Refuse as r:
            if r.code != "parse-error":
                raise
        self.pos = save
        env.push()
        init = None
        if not self.at(";"):
            save2 = self.pos
            d = self.try_parse_decl(env, subst)
            if d is not None:
                init = d
            else:
                self.pos = save2
                e = self.parse_expr(subst, env)
                self.expect(";")
                init = {"k": "expr", "e": e}
        else:
            self.next()
        cond = None
        if not self.at(";"):
            cond = self.parse_expr(subst, env)
        self.expect(";")
        inc = None
        if not self.at(")"):
            inc = self.parse_expr(subst, env)
        self.expect(")")
        body = [self.parse_stmt(env, subst)]
        env.pop()
        return {"k": "for", "init": init, "c": cond, "inc": inc, "b": body}

    # ---------- expressions ----------
    def parse_expr(self, subst=None, env=None):
        return self.parse_assign(subst, env)

    def parse_assign(self, subst, env):
        lhs = self.parse_cond(subst, env)
        t = self.peek()
        if t.text in ("=", "+=", "-=", "*=", "/=", "%="):
            op = self.next().text
            rhs = self.parse_assign(subst, env)
            return {"k": "assign", "op": op, "l": lhs, "r": rhs,
                    "t": lhs["t"]}
        return lhs

    BINPREC = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6,
               "<": 7, ">": 7, "<=": 7, ">=": 7, "<<": 8, ">>": 8,
               "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}

    def promote(self, a, b):
        a, b = strip_const(a), strip_const(b)
        if a == b:
            return a
        order = {"bool": 0, "char": 1, "int": 2, "uint": 3, "long": 4,
                 "float": 5, "double": 6}
        if a[0] == "builtin" and b[0] == "builtin":
            return a if order.get(a[1], 0) >= order.get(b[1], 0) else b
        if a[0] == "ptr":
            return a
        if b[0] == "ptr":
            return b
        return a

    def parse_cond(self, subst, env):
        e = self.parse_binary(subst, env, 1)
        if self.at("?"):
            self.next()
            a = self.parse_expr(subst, env)
            self.expect(":")
            b = self.parse_assign(subst, env)
            tt = a["t"] if a["t"] == b["t"] else self.promote(a["t"], b["t"])
            return {"k": "cond", "c": e, "a": a, "b": b, "t": tt}
        return e

    def parse_binary(self, subst, env, minp):
        lhs = self.parse_unary(subst, env)
        while True:
            t = self.peek()
            p = self.BINPREC.get(t.text)
            if p is None or p < minp:
                return lhs
            op = self.next().text
            rhs = self.parse_binary(subst, env, p + 1)
            if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||"):
                tt = T_builtin("bool")
            elif strip_const(lhs["t"])[0] == "ptr" and op in ("+", "-"):
                tt = lhs["t"]
            else:
                tt = self.promote(lhs["t"], rhs["t"])
            lhs = {"k": "bin", "op": op, "l": lhs, "r": rhs, "t": tt}

    def parse_unary(self, subst, env):
        t = self.peek()
        if t.text in ("-", "+", "!", "~"):
            op = self.next().text
            e = self.parse_unary(subst, env)
            tt = T_builtin("bool") if op == "!" else e["t"]
            return {"k": "un", "op": op, "e": e, "t": tt}
        if t.text in ("++", "--"):
            op = self.next().text
            e = self.parse_unary(subst, env)
            return {"k": "un", "op": op, "e": e, "t": e["t"]}
        if t.text == "&":
            self.next()
            e = self.parse_unary(subst, env)
            return {"k": "un", "op": "addr", "e": e, "t": ("ptr", e["t"])}
        if t.text == "*":
            self.next()
            e = self.parse_unary(subst, env)
            pt = strip_const(e["t"])
            if pt[0] == "ref":
                pt = strip_const(pt[1])
            if pt[0] != "ptr":
                raise Refuse("parse-error", "deref of non-pointer")
            return {"k": "un", "op": "deref", "e": e, "t": pt[1]}
        if t.text == "new":
            return self.parse_new(subst, env)
        if t.text == "delete":
            self.next()
            if self.at("["):
                raise Refuse("array-new-delete",
                             "array new/delete are outside the subset")
            e = self.parse_unary(subst, env)
            self.tu.uses_new = True
            return {"k": "delete", "e": e, "t": T_builtin("void")}
        if t.text == "sizeof":
            self.next()
            if self.at("("):
                save = self.pos
                self.next()
                try:
                    ty = self.parse_type(subst)
                    self.expect(")")
                    return {"k": "sizeof", "type": ty,
                            "t": T_builtin("long")}
                except Refuse as r:
                    if r.code != "parse-error":
                        raise
                    self.pos = save
            e = self.parse_unary(subst, env)
            return {"k": "sizeofexpr", "e": e, "t": T_builtin("long")}
        return self.parse_postfix(subst, env)

    def parse_new(self, subst, env):
        self.expect("new")
        ty = self.parse_type(subst)
        if self.at("["):
            raise Refuse("array-new-delete",
                         "array new is outside the subset")
        args = []
        if self.at("("):
            self.next()
            args = self.parse_args(env, subst)
            self.expect(")")
        if not is_class_t(ty):
            raise Refuse("parse-error", "new of non-class type")
        self.tu.uses_new = True
        return {"k": "new", "type": strip_const(ty), "args": args,
                "t": ("ptr", strip_const(ty))}

    def parse_postfix(self, subst, env):
        e = self.parse_primary(subst, env)
        while True:
            t = self.peek()
            if t.text == "(":
                e = self.finish_call(e, subst, env)
            elif t.text == "{" and e["k"] == "ctorcall":
                self.next()
                args = self.parse_args(env, subst, brace=True)
                self.expect("}")
                e = {"k": "construct", "type": e["type"], "args": args,
                     "t": e["type"]}
            elif t.text == "." or t.text == "->":
                op = self.next().text
                parts, _ = self.parse_qualified_name()
                if not parts:
                    raise Refuse("parse-error", "member name expected")
                if len(parts) >= 2:
                    cls_q = self.resolve_class(parts[:-1])
                    if cls_q is None:
                        raise Refuse("parse-error",
                                     "unknown qualifier in member access")
                    e = {"k": "mfunc", "o": e, "name": parts[-1],
                         "qualified": cls_q}
                elif self.at("("):
                    e = {"k": "mfunc", "o": e, "name": parts[0],
                         "qualified": None}
                else:
                    e = self.member_access(e, parts[0], subst, env)
            elif t.text == "[":
                self.next()
                idx = self.parse_expr(subst, env)
                self.expect("]")
                at = strip_const(e["t"])
                if at[0] == "ref":
                    at = strip_const(at[1])
                elem = at[1] if at[0] in ("array", "ptr") else None
                if elem is None:
                    raise Refuse("parse-error", "subscript of non-array")
                e = {"k": "index", "a": e, "i": idx, "t": elem}
            elif t.text in ("++", "--"):
                op = self.next().text
                e = {"k": "un", "op": op, "e": e, "t": e["t"], "post": True}
            else:
                return e

    def member_access(self, obj, name, subst, env):
        ot = strip_const(obj["t"])
        if ot[0] == "ref":
            ot = strip_const(ot[1])
        if ot[0] == "ptr":
            ot = strip_const(ot[1])
        if ot[0] != "class":
            raise Refuse("parse-error", "member access on non-class type")
        q = ot[1]
        sem = Semantics(self.tu)
        owner, f = sem.find_field(q, name)
        if f is not None:
            if strip_const(f["type"])[0] == "ref":
                raise Refuse("reference-field",
                             "reference fields are outside the subset")
            return {"k": "member", "o": obj, "f": name, "t": f["type"]}
        owner, sf = sem.find_static_field(q, name)
        if sf is not None:
            return {"k": "smember", "class": owner, "name": name,
                    "t": sf["type"]}
        raise Refuse("parse-error", f"no member {name} in {'::'.join(q)}")

    def finish_call(self, callee_expr, subst, env):
        self.expect("(")
        args = self.parse_args(env, subst)
        self.expect(")")
        return self.build_call(callee_expr, args, subst, env)

    def build_call(self, callee_expr, args, subst, env):
        ck = callee_expr["k"]
        if ck == "func":
            q = callee_expr["qname"]
            texpr_args = callee_expr.get("targs")
            if q == ("printf",):
                self.tu.uses_printf = True
                return {"k": "call", "callee": ("free", ("printf",), None),
                        "args": args, "t": T_builtin("int")}
            if q in self.func_templates:
                f = self.instantiate_func(q, texpr_args, args, subst)
                return {"k": "call", "callee": ("free", f["qname"], f),
                        "args": args, "t": f["ret"]}
            cands = [f for f in self.tu.funcs if f["qname"] == q]
            f = self.resolve_overload(cands, args,
                                      f"function {cname(q)}")
            return {"k": "call", "callee": ("free", f["qname"], f),
                    "args": args, "t": f["ret"]}
        if ck == "smfunc":
            cls_q, name = callee_expr["class"], callee_expr["name"]
            sem = Semantics(self.tu)
            cands = []
            owner = None
            for cq in [cls_q] + sem.bases_chain(cls_q):
                ms = [m for m in sem.cls(cq)["methods"]
                      if m["name"] == name and m.get("static")]
                if ms:
                    cands, owner = ms, cq
                    break
            if not cands:
                raise Refuse("parse-error",
                             f"no static method {name} in "
                             f"{'::'.join(cls_q)}")
            m = self.resolve_overload(cands, args, f"static method {name}")
            return {"k": "call", "callee": ("smethod", owner, m),
                    "args": args, "t": m["ret"]}
        if ck == "mfunc":
            obj, name = callee_expr["o"], callee_expr["name"]
            qualified = callee_expr.get("qualified")
            ot = strip_const(obj["t"])
            if ot[0] == "ref":
                ot = strip_const(ot[1])
            if ot[0] == "ptr":
                ot = strip_const(ot[1])
            if ot[0] != "class":
                raise Refuse("parse-error", "method call on non-class")
            q = ot[1]
            sem = Semantics(self.tu)
            if qualified is not None:
                cands = [m for m in sem.cls(qualified)["methods"]
                         if m["name"] == name]
                owner = qualified
            else:
                cands, owner = [], None
                for cq in [q] + sem.bases_chain(q):
                    ms = [m for m in sem.cls(cq)["methods"]
                          if m["name"] == name]
                    if ms:
                        cands, owner = ms, cq
                        break
            if not cands:
                raise Refuse("no-matching-function",
                             f"no method {name} in {'::'.join(q)}")
            m = self.resolve_overload(cands, args, f"method {name}")
            for cq in [owner] + sem.bases_chain(owner):
                if any(mm is m for mm in sem.cls(cq)["methods"]):
                    owner = cq
                    break
            if m.get("static"):
                return {"k": "call", "callee": ("smethod", owner, m),
                        "args": args, "t": m["ret"]}
            dispatch = bool(m.get("virtual")) and qualified is None
            return {"k": "call",
                    "callee": ("method", obj, owner, m, dispatch),
                    "args": args, "t": m["ret"]}
        if ck == "ctorcall":
            ty = callee_expr["type"]
            return {"k": "construct", "type": ty, "args": args, "t": ty}
        tt = strip_const(callee_expr["t"])
        if tt[0] == "ref":
            tt = strip_const(tt[1])
        if tt[0] == "fnptr":
            return {"k": "call", "callee": ("expr", callee_expr),
                    "args": args, "t": tt[1]}
        raise Refuse("parse-error", "call of non-callable expression")

    def resolve_overload(self, cands, args, what):
        scored = []
        for f in cands:
            ps = f["params"]
            if len(ps) < len(args):
                continue
            if any(p[2] is None for p in ps[len(args):]):
                continue
            ranks = []
            ok = True
            for p, a in zip(ps, args):
                r = self.conversion_rank(a["t"], p[1])
                if r is None:
                    ok = False
                    break
                ranks.append(r)
            if ok:
                scored.append((ranks, f))
        if not scored:
            raise Refuse("no-matching-function",
                         f"no overload of {what} accepts {len(args)} "
                         f"argument(s)")
        scored.sort(key=lambda x: x[0])
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            raise Refuse("ambiguous-call", f"ambiguous overload of {what}")
        return scored[0][1]

    def conversion_rank(self, at, pt):
        at, pt = strip_const(at), strip_const(pt)
        if pt[0] == "ref":
            inner = strip_const(pt[1])
            if at == inner:
                return 0
            if at[0] == "class" and inner[0] == "class" and \
                    Semantics(self.tu).is_derived(at[1], inner[1]):
                return 1
            return None
        if at == pt:
            return 0
        if at[0] == "class" and pt[0] == "class":
            return None
        if at[0] == "ptr" and pt[0] == "ptr":
            ai, pi = strip_const(at[1]), strip_const(pt[1])
            if ai == pi:
                return 0
            if ai[0] == "class" and pi[0] == "class" and \
                    Semantics(self.tu).is_derived(ai[1], pi[1]):
                return 1
            if pi == ("builtin", "void"):
                return 2
            return None
        if at[0] == "fnptr" and pt[0] == "fnptr":
            return 0 if at == pt else None
        if at[0] == "builtin" and pt[0] == "builtin":
            if at[1] == pt[1]:
                return 0
            if (at[1], pt[1]) in (("char", "int"), ("bool", "int"),
                                  ("float", "double")):
                return 2
            return 3
        return None

    # ---------- primaries ----------
    def parse_primary(self, subst, env):
        t = self.peek()
        self.kw_check()
        if t.kind == "num":
            self.next()
            txt = t.text
            low = txt.lower()
            if low.endswith("f") and not low.startswith("0x"):
                return {"k": "float", "v": txt, "t": T_builtin("float")}
            if ("." in low or ("e" in low and not low.startswith("0x"))) \
                    and low[0].isdigit():
                return {"k": "float", "v": txt, "t": T_builtin("double")}
            if low.endswith("ul") or low.endswith("lu"):
                ty = T_builtin("long")
            elif low.endswith("l"):
                ty = T_builtin("long")
            elif low.endswith("u"):
                ty = T_builtin("uint")
            else:
                ty = T_builtin("int")
            return {"k": "int", "v": txt, "t": ty}
        if t.kind == "str":
            self.next()
            return {"k": "str", "v": t.text,
                    "t": ("ptr", ("const", T_builtin("char")))}
        if t.kind == "char":
            self.next()
            return {"k": "char", "v": t.text, "t": T_builtin("char")}
        if t.text == "(":
            self.next()
            save = self.pos
            try:
                ty = self.parse_type(subst)
                if self.at(")"):
                    self.next()
                    e = self.parse_unary(subst, env)
                    return {"k": "cast", "type": ty, "e": e, "t": ty}
            except Refuse as r:
                if r.code != "parse-error":
                    raise
            self.pos = save
            e = self.parse_expr(subst, env)
            self.expect(")")
            return e
        if t.text == "[":
            return self.parse_lambda(subst, env)
        if t.kind == "id":
            word = t.text
            if word in ("true", "false"):
                self.next()
                return {"k": "bool", "v": word == "true",
                        "t": T_builtin("bool")}
            if word == "nullptr":
                self.next()
                return {"k": "nullptr", "t": ("ptr", T_builtin("void"))}
            if word == "this":
                self.next()
                if env is None or env.class_q is None:
                    raise Refuse("parse-error", "this outside a method")
                return {"k": "this", "t": ("ptr", T_class(env.class_q))}
            if word in ("static_cast", "reinterpret_cast", "const_cast"):
                self.next()
                self.expect("<")
                ty = self.parse_type(subst)
                self.expect_gt()
                self.expect("(")
                e = self.parse_expr(subst, env)
                self.expect(")")
                return {"k": "cast", "type": ty, "e": e, "t": ty}
            if word == "dynamic_cast":
                raise Refuse("rtti-dynamic-cast",
                             "dynamic_cast is outside the subset")
            return self.parse_name_expr(subst, env)
        raise Refuse("parse-error", f"unexpected token {t.text!r} @{t.line}")

    def parse_lambda(self, subst, env):
        line = self.peek().line
        self.expect("[")
        captures = []
        if not self.at("]"):
            while True:
                tok = self.next()
                captures.append(tok.text)
                if self.eat(","):
                    continue
                break
        self.expect("]")
        if captures:
            raise Refuse("capturing-lambda",
                         "v1 subset: only non-capturing lambdas "
                         "([](...) { ... }) lower")
        params = self.parse_params(subst)
        ret = None
        if self.at("->"):
            self.next()
            ret = self.parse_type(subst)
        span = self.capture_block()
        f = {"qname": (f"__lambda_{self.lambda_n}",),
             "ret": ret if ret is not None else T_builtin("void"),
             "params": params, "body": None, "line": line, "lambda": True,
             "ret_declared": ret is not None}
        self.lambda_n += 1
        self.tu.funcs.append(f)
        lenv = Env(self, None, f)
        saved = self.pos
        self.pos = span[0]
        f["body"] = self.parse_body_block(lenv, subst)
        self.pos = saved
        if not f["ret_declared"]:
            def has_value_return(ss):
                for s in ss:
                    if s["k"] == "return" and s.get("e") is not None:
                        return True
                    for v in s.values():
                        if isinstance(v, list) and v and \
                                isinstance(v[0], dict) and "k" in v[0]:
                            if has_value_return(v):
                                return True
                return False
            if has_value_return(f["body"]):
                raise Refuse("lambda-return-type",
                             "a value-returning lambda needs an explicit "
                             "-> return type in the v1 subset")
        fty = ("fnptr", f["ret"], tuple(p[1] for p in params))
        return {"k": "lambda", "t": fty, "_func_id": id(f), "line": line}

    def parse_name_expr(self, subst, env):
        parts, _ = self.parse_qualified_name()
        if not parts:
            raise Refuse("parse-error", "expected a name")
        targs = None
        if self.at("<"):
            save = self.pos
            try:
                targs = self.parse_template_args(subst)
            except Refuse:
                self.pos = save
                targs = None
        if parts[0] == "std":
            if parts == ["std", "printf"]:
                return {"k": "func", "qname": ("printf",)}
            if parts == ["std", "move"]:
                raise Refuse("move-semantics",
                             "std::move / rvalue references are outside "
                             "the subset")
            raise Refuse("stl-facility",
                         f"std::{'::'.join(parts[1:])} is outside the "
                         f"v1 subset")
        if parts == ["printf"]:
            return {"k": "func", "qname": ("printf",)}
        if len(parts) >= 2:
            cls_q = self.resolve_class(parts[:-1])
            if cls_q is not None and cls_q not in self.class_templates:
                member = parts[-1]
                sem = Semantics(self.tu)
                owner, sf = sem.find_static_field(cls_q, member)
                if sf is not None and self.peek().text != "(":
                    return {"k": "smember", "class": owner, "name": member,
                            "t": sf["type"]}
                return {"k": "smfunc", "class": cls_q, "name": member}
        cls_q = self.resolve_class(parts)
        if cls_q is not None and cls_q in self.class_templates:
            if targs is None:
                raise Refuse("parse-error",
                             "class template needs explicit arguments")
            iq = self.instantiate_class(cls_q, targs)
            if self.peek().text in ("(", "{"):
                return {"k": "ctorcall", "type": T_class(iq)}
        if cls_q is not None and self.peek().text in ("(", "{"):
            return {"k": "ctorcall", "type": T_class(cls_q)}
        if len(parts) == 1 and env is not None:
            v = env.lookup(parts[0])
            if v is not None:
                return {"k": "var", "name": parts[0], "t": v[0],
                        "ref": v[1]}
            if env.class_q is not None:
                sem = Semantics(self.tu)
                owner, f = sem.find_field(env.class_q, parts[0])
                if f is not None:
                    if strip_const(f["type"])[0] == "ref":
                        raise Refuse("reference-field",
                                     "reference fields are outside the "
                                     "subset")
                    return {"k": "member",
                            "o": {"k": "this",
                                  "t": ("ptr", T_class(env.class_q))},
                            "f": parts[0], "t": f["type"]}
                owner, sf = sem.find_static_field(env.class_q, parts[0])
                if sf is not None:
                    return {"k": "smember", "class": owner,
                            "name": parts[0], "t": sf["type"]}
                for cq in [env.class_q] + sem.bases_chain(env.class_q):
                    ms = [m for m in sem.cls(cq)["methods"]
                          if m["name"] == parts[0]]
                    if ms:
                        if all(m.get("static") for m in ms):
                            return {"k": "smfunc", "class": env.class_q,
                                    "name": parts[0]}
                        return {"k": "mfunc",
                                "o": {"k": "this",
                                      "t": ("ptr",
                                            T_class(env.class_q))},
                                "name": parts[0], "qualified": None}
        fq = self.resolve_func_or_global(parts)
        if fq is not None:
            kind, obj = fq
            if kind == "global":
                return {"k": "gvar", "qname": obj["qname"],
                        "t": obj["type"]}
            return {"k": "func", "qname": obj["qname"], "targs": targs}
        for cut in range(len(self.ns), -1, -1):
            cand = self.ns[:cut] + tuple(parts)
            if cand in self.func_templates:
                return {"k": "func", "qname": cand, "targs": targs}
        raise Refuse("parse-error",
                     f"unresolved name {'::'.join(parts)}")

    def resolve_func_or_global(self, parts):
        for cut in range(len(self.ns), -1, -1):
            cand = self.ns[:cut] + tuple(parts)
            for g in self.tu.globals:
                if g["qname"] == cand:
                    return ("global", g)
            for f in self.tu.funcs:
                if f["qname"] == cand:
                    return ("func", f)
        return None

# ------------------------------------------------------------ environments
class Env:
    def __init__(self, parser, class_q, func):
        self.parser = parser
        self.class_q = class_q
        self.func = func
        self.scopes = [{}]
        if func is not None:
            for (n, t, d) in func.get("params", []):
                if n:
                    self.scopes[0][n] = (t, strip_const(t)[0] == "ref")

    def push(self):
        self.scopes.append({})

    def pop(self):
        self.scopes.pop()

    def declare(self, name, ty):
        is_ref = ty is not None and strip_const(ty)[0] == "ref"
        self.scopes[-1][name] = (ty, is_ref)

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        return None

def _prune_unused_template_methods(tu):
    """C++ instantiates only the member functions of a class
    template that are actually used; clang's AST (track 2) carries
    only those. Drop unused methods of template instances here too,
    so both tracks lower the same member set. A method survives if
    a kept body calls it or it occupies a vtable slot (it can be
    reached by dispatch). Constructors and destructors are kept
    whole in v1."""
    from phcpp_ir import Semantics
    inst = {tuple(c["qname"]) for c in tu.classes
            if c.get("template_inst")}
    if not inst:
        return
    sem = Semantics(tu)
    slot_sigs = {}
    for q in inst:
        slot_sigs[q] = {Semantics._sig(s) for s in sem.slots(q)
                        if s["name"] != "~dtor"}
    kept = {id(m) for c in tu.classes if tuple(c["qname"]) in inst
            for m in c["methods"]}

    def collect(e, used):
        if isinstance(e, dict):
            if e.get("k") == "call" and \
                    isinstance(e.get("callee"), tuple) and \
                    e["callee"][0] == "method":
                used.add(id(e["callee"][3]))
            for v in e.values():
                collect(v, used)
        elif isinstance(e, list):
            for v in e:
                collect(v, used)

    while True:
        used = set()
        for f in tu.funcs:
            collect(f.get("body") or [], used)
            for (_n, _t, d) in f.get("params", []):
                if isinstance(d, dict):
                    collect(d, used)
        for g in tu.globals:
            if isinstance(g.get("init"), dict):
                collect(g["init"], used)
        for c in tu.classes:
            q = tuple(c["qname"])
            for ct in c["ctors"]:
                collect(ct.get("body") or [], used)
            if c.get("dtor"):
                collect(c["dtor"].get("body") or [], used)
            for m in c["methods"]:
                if q not in inst or id(m) in kept:
                    collect(m.get("body") or [], used)
        new_kept = set()
        for c in tu.classes:
            q = tuple(c["qname"])
            if q not in inst:
                continue
            for m in c["methods"]:
                if id(m) in used or \
                        Semantics._sig(m) in slot_sigs[q]:
                    new_kept.add(id(m))
        if new_kept == kept:
            break
        kept = new_kept
    for c in tu.classes:
        if tuple(c["qname"]) in inst:
            c["methods"] = [m for m in c["methods"] if id(m) in kept]


def lower_source(src, cpp_name):
    p = Parser(src, cpp_name)
    tu = p.parse()
    _prune_unused_template_methods(tu)
    return tu

if __name__ == "__main__":
    src = open(sys.argv[1]).read()
    tu = lower_source(src, sys.argv[1].rsplit("/", 1)[-1])
    from phcpp_ir import Emitter
    text, members = Emitter(tu).emit()
    sys.stdout.write(text)
