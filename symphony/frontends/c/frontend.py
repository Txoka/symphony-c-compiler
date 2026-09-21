"""C semantic analysis and conversion to the shared typed representation."""

import re
from pycparser import c_ast as c
from .parser import parse, strip_comments
from ...middle.model import (
    CompileError,
    Type,
    Record,
    INT,
    UINT,
    CHAR,
    BOOL,
    VOID,
    pointer,
    array,
    vla,
    common,
    Symbol,
    Node,
    Global,
    Function,
    Program,
    align_up,
)

def literal(text):
    s = re.sub("[uUlL]+$", "", text)
    if s.startswith(("0x", "0X")):
        return int(s, 16)
    if len(s) > 1 and s[0] == "0":
        return int(s, 8)
    return int(s, 10)


def literal_bytes(text, quote='"'):
    """Decode C escapes explicitly (Python's escape rules are not identical)."""
    data = bytearray()
    escapes = {
        "a": 7,
        "b": 8,
        "f": 12,
        "n": 10,
        "r": 13,
        "t": 9,
        "v": 11,
        chr(92): 92,
        "'": 39,
        '"': 34,
        "?": 63,
    }
    i = 0
    tokens = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i] != quote:
            raise CompileError("only ordinary single-byte literals are supported")
        tokens += 1
        i += 1
        while i < len(text) and text[i] != quote:
            ch = text[i]
            i += 1
            if ord(ch) != 92:
                value = ord(ch)
            else:
                if i == len(text):
                    raise CompileError("unterminated escape sequence")
                ch = text[i]
                i += 1
                if ch in escapes:
                    value = escapes[ch]
                elif ch in "01234567":
                    digits = ch
                    while i < len(text) and len(digits) < 3 and text[i] in "01234567":
                        digits += text[i]
                        i += 1
                    value = int(digits, 8)
                elif ch == "x":
                    start = i
                    while i < len(text) and text[i] in "0123456789abcdefABCDEF":
                        i += 1
                    if i == start:
                        raise CompileError("hexadecimal escape requires a digit")
                    value = int(text[start:i], 16)
                else:
                    raise CompileError(f"unsupported escape sequence: {chr(92)}{ch}")
            if value > 255:
                raise CompileError("literal character exceeds one byte")
            data.append(value)
        if i == len(text):
            raise CompileError("unterminated literal")
        i += 1
    if not tokens or (quote == "'" and tokens != 1):
        raise CompileError("invalid literal")
    return bytes(data)


def string_bytes(text):
    return literal_bytes(text) + bytes([0])


class Frontend:
    def __init__(self, namespace=""):
        self.scopes = [{}]
        self.typedefs = [{}]
        self.records = [{}]
        self.enum_tags = [{}]
        self.globals = []
        self.functions = []
        self.locals = []
        self.serial = 0
        self.loop_depth = 0
        self.break_depth = 0
        self.switch_depth = 0
        self.switch_types = []
        self.switch_vlas = []
        self.scope_serial = 0
        self.scope_ids = [0]
        self.scope_vlas = [False]
        self.labels = {}
        self.gotos = []
        self.return_type = VOID
        self.namespace = namespace

    def internal_key(self, name):
        return f"__tu_{self.namespace}_{name}" if self.namespace else name

    def fail(self, source, message):
        raise CompileError(f'{getattr(source, "coord", "")}: {message}')

    def new(self, name, type_, storage):
        self.serial += 1
        return Symbol(
            name,
            type_,
            storage,
            name if storage in ("global", "function") else f"{name}.{self.serial}",
        )

    def push_scope(self):
        self.scopes.append({})
        self.typedefs.append({})
        self.records.append({})
        self.enum_tags.append({})
        self.scope_serial += 1
        self.scope_ids.append(self.scope_serial)
        self.scope_vlas.append(False)
        return self.scope_serial

    def pop_scope(self):
        self.enum_tags.pop()
        self.records.pop()
        self.typedefs.pop()
        self.scopes.pop()
        self.scope_ids.pop()
        self.scope_vlas.pop()

    def check_jump_into_vla(self, source, kind):
        target_vlas = frozenset(
            scope_id for scope_id, has_vla in zip(self.scope_ids, self.scope_vlas) if has_vla
        )
        if not target_vlas <= self.switch_vlas[-1]:
            self.fail(source, f"switch {kind} enters the scope of a variably modified object")

    def resolve_gotos(self):
        for node, name, source_scopes, source_vlas in self.gotos:
            target = self.labels.get(name)
            if target is None:
                self.fail(node, f"undefined label {name}")
            target_scopes, target_vlas = target
            if not target_vlas <= source_vlas:
                self.fail(node, f"goto {name} enters the scope of a variably modified object")
            node.value = (name, source_scopes, target_scopes)

    def lookup(self, source):
        for scope in reversed(self.scopes):
            if source.name in scope:
                return scope[source.name]
        self.fail(source, f"undeclared identifier {source.name}")

    def lookup_type_name(self, name):
        for scope in reversed(self.typedefs):
            if name in scope:
                return scope[name]
        return None

    def apply_qualifiers(self, source, type_, qualifiers):
        unsupported = set(qualifiers or ()) - {"const"}
        if unsupported:
            self.fail(source, f"unsupported type qualifier: {sorted(unsupported)[0]}")
        return type_.qualified(*qualifiers) if qualifiers else type_

    def typename(self, t):
        if isinstance(t, (c.Decl, c.Typename, c.Typedef)):
            # pycparser mirrors declaration qualifiers onto ``Decl.quals`` as
            # well as placing them on the declarator node they actually
            # qualify.  Applying both makes ``const char *p`` a const pointer
            # instead of a mutable pointer to const char.
            return self.typename(t.type)
        if isinstance(t, c.TypeDecl):
            return self.apply_qualifiers(t, self.typename(t.type), t.quals)
        if isinstance(t, c.IdentifierType):
            names = t.names
            alias = self.lookup_type_name(names[0]) if len(names) == 1 else None
            if alias is not None:
                return alias
            if names == ["void"]:
                return VOID
            if names == ["_Bool"]:
                return BOOL
            if any(
                n not in ("signed", "unsigned", "char", "short", "int", "long")
                for n in names
            ):
                self.fail(t, "only integer, pointer and array types are supported")
            if names.count("long") > 1:
                self.fail(t, "64-bit long long is not yet supported")
            if (
                len(set(names)) != len(names)
                or ("signed" in names and "unsigned" in names)
                or (
                    "char" in names
                    and any(x in names for x in ("short", "int", "long"))
                )
                or ("short" in names and "long" in names)
            ):
                self.fail(t, "invalid integer type specifiers")
            size = 1 if "char" in names else 2 if "short" in names else 4
            signed = "unsigned" not in names and (
                "char" not in names or "signed" in names
            )
            return Type(size=size, signed=signed)
        if isinstance(t, c.PtrDecl):
            return self.apply_qualifiers(t, pointer(self.typename(t.type)), t.quals)
        if isinstance(t, c.ArrayDecl):
            base = self.typename(t.type)
            if base.size == 0 and not self.variably_modified(base):
                self.fail(t, "invalid array type")
            if t.dim is None:
                return array(base, 0)
            try:
                count = self.const_int(t.dim)
            except CompileError:
                return vla(base)
            if count < 0:
                self.fail(t, "invalid array type")
            return array(base, count)
        if isinstance(t, c.FuncDecl):
            params = []
            if t.args:
                for p in t.args.params:
                    if isinstance(p, c.EllipsisParam):
                        self.fail(p, "variadic functions are unsupported")
                    pt = self.typename(p).decay()
                    if pt.kind in ("struct", "union"):
                        self.fail(p, "aggregate parameters are not yet supported")
                    params.append(pt)
                if params == [VOID]:
                    params = []
            result = self.typename(t.type)
            if result.kind in ("array", "function", "union"):
                self.fail(t, "invalid function return type")
            return Type("function", 0, False, result, params=tuple(params))
        if isinstance(t, (c.Struct, c.Union)):
            kind = "union" if isinstance(t, c.Union) else "struct"
            tag = t.name or ""
            record = self.records[-1].get(tag) if tag and t.decls is not None else None
            if tag and t.decls is None:
                for scope in reversed(self.records):
                    if tag in scope:
                        record = scope[tag]
                        break
            if t.decls is None:
                if record is None:
                    record = Record(tag, kind)
                    self.records[-1][tag] = record
                if record.kind != kind:
                    self.fail(t, f"{tag} was previously declared as a {record.kind}")
                return Type(kind, 0, False, record=record)
            if record is None or (record.complete and not tag):
                record = Record(tag, kind)
                if tag:
                    self.records[-1][tag] = record
            elif record.kind != kind:
                self.fail(t, f"{tag} was previously declared as a {record.kind}")
            elif record.complete:
                self.fail(t, f"redefinition of struct {tag}")
            members = []
            offset = 0
            alignment = 1
            names = set()
            for declaration in t.decls:
                if declaration.bitsize is not None:
                    self.fail(declaration, "bit-fields are not yet supported")
                if declaration.name and declaration.name in names:
                    self.fail(declaration, "duplicate structure member")
                member_type = self.typename(declaration)
                if not declaration.name and member_type.kind not in ("struct", "union"):
                    self.fail(declaration, "anonymous member must be a structure or union")
                if member_type.kind in ("void", "function") or not member_type.size:
                    self.fail(declaration, "structure member requires a complete object type")
                offset = 0 if kind == "union" else align_up(offset, member_type.align)
                members.append((declaration.name, member_type, offset))
                if declaration.name:
                    names.add(declaration.name)
                offset = max(offset, member_type.size) if kind == "union" else offset + member_type.size
                alignment = max(alignment, member_type.align)
            record.members = tuple(members)
            record.alignment = alignment
            record.size = align_up(offset, alignment)
            record.complete = True
            return Type(kind, 0, False, record=record)
        if isinstance(t, c.Enum):
            tag = t.name or ""
            if t.values is None:
                if not any(tag in scope for scope in reversed(self.enum_tags)):
                    self.fail(t, f"unknown enum {tag}")
                return INT
            if tag and tag in self.enum_tags[-1]:
                self.fail(t, f"redefinition of enum {tag}")
            value = -1
            for enumerator in t.values.enumerators:
                if enumerator.name in self.scopes[-1]:
                    self.fail(enumerator, "duplicate enumerator")
                value = self.const_int(enumerator.value) if enumerator.value else value + 1
                self.scopes[-1][enumerator.name] = Symbol(
                    enumerator.name, INT, "enum", enumerator.name, value
                )
            if tag:
                self.enum_tags[-1][tag] = True
            return INT
        self.fail(t, f"unsupported type: {type(t).__name__}")

    def const_int(self, source):
        try:
            value = self.static_value(self.expr(source))
        except (ZeroDivisionError, ValueError):
            self.fail(source, "invalid integer constant expression")
        if not isinstance(value, int):
            self.fail(source, "integer constant expression required")
        return value

    def node(self, source, op, type_=VOID, children=None, value=None, lvalue=False):
        return Node(
            op, type_, children or [], value, str(getattr(source, "coord", "")), lvalue
        )

    def cast(self, n, t):
        if n.type == t:
            return n
        # C requires conversion to _Bool to produce precisely 0 or 1, rather
        # than merely truncating the low byte.
        if t.kind == "bool":
            return Node("bool_cast", t, [self.value(n)], location=n.location)
        return Node("cast", t, [n], location=n.location)

    def value(self, n):
        if n.type.kind in ("array", "vla", "function"):
            return Node("address", n.type.decay(), [n], location=n.location)
        return n

    @staticmethod
    def variably_modified(t):
        if t.kind == "vla":
            return True
        return t.base is not None and Frontend.variably_modified(t.base)

    @staticmethod
    def compatible_type(a, b):
        if a == b:
            return True
        if a.kind in ("array", "vla") and b.kind in ("array", "vla"):
            if a.kind == b.kind == "array" and a.count != b.count:
                return False
            return Frontend.compatible_type(a.base, b.base)
        return False

    def member_lookup(self, source, aggregate, name):
        """Resolve a direct or anonymously promoted aggregate member."""
        matches = []

        def visit(type_, base, direct_index=None):
            for index, (member_name, member_type, member_offset) in enumerate(
                type_.record.members
            ):
                root_index = index if direct_index is None else direct_index
                offset = base + member_offset
                if member_name == name:
                    matches.append((root_index, member_type, offset))
                elif member_name is None and member_type.kind in ("struct", "union"):
                    visit(member_type, offset, root_index)

        visit(aggregate, 0)
        if not matches:
            self.fail(source, f"{aggregate.kind} has no member {name}")
        if len(matches) != 1:
            self.fail(source, f"ambiguous member {name} through anonymous aggregates")
        return matches[0]

    def bind_vla_bounds(self, declarator, t):
        """Attach saved declaration-time bounds to a variably modified type."""
        if isinstance(declarator, (c.Decl, c.Typename, c.Typedef, c.TypeDecl)):
            return self.bind_vla_bounds(declarator.type, t)
        if isinstance(declarator, c.PtrDecl):
            base, bounds = self.bind_vla_bounds(declarator.type, t.base)
            return pointer(base), bounds
        if isinstance(declarator, c.ArrayDecl):
            base, bounds = self.bind_vla_bounds(declarator.type, t.base)
            if t.kind == "vla":
                expression = self.scalar(declarator.dim, self.expr(declarator.dim))
                if not expression.type.integer:
                    self.fail(declarator.dim, "variable array bound requires an integer")
                saved = self.new("__vla_bound", UINT, "local")
                self.locals.append(saved)
                reference = self.node(declarator.dim, "var", UINT, value=saved, lvalue=True)
                return vla(base, reference), [*bounds, (saved, self.cast(expression, UINT))]
            return array(base, t.count), bounds
        return t, []

    def runtime_size(self, source, t):
        if t.kind == "vla":
            if t.bound is None:
                self.fail(source, "variable array bound is unavailable in this context")
            element = self.runtime_size(source, t.base)
            return self.node(source, "checked_mul", UINT, [self.value(t.bound), element])
        if t.kind == "array" and self.variably_modified(t.base):
            element = self.runtime_size(source, t.base)
            return self.node(
                source,
                "checked_mul",
                UINT,
                [self.node(source, "const", UINT, value=t.count), element],
            )
        if not t.size:
            self.fail(source, "sizeof requires a complete object type")
        return self.node(source, "const", UINT, value=t.size)

    def scalar(self, source, n):
        n = self.value(n)
        if not n.type.integer and n.type.kind != "pointer":
            self.fail(source, "scalar expression required")
        return n

    def convert(self, source, n, t):
        n = self.value(n)
        if n.type.integer and t.integer:
            return self.cast(n, t)
        if t.kind == "pointer":
            if n.type.kind == "pointer" and (
                n.type == t
                or n.type.base.unqualified() == VOID
                or t.base.unqualified() == VOID
            ):
                if not n.type.base.qualifiers <= t.base.qualifiers:
                    self.fail(source, f"cannot discard qualifiers converting {n.type} to {t}")
                return self.cast(n, t)
            if (
                n.type.kind == "pointer"
                and self.compatible_type(
                    n.type.base.unqualified(), t.base.unqualified()
                )
                and n.type.base.qualifiers <= t.base.qualifiers
            ):
                return self.cast(n, t)
            if n.op == "const" and n.value == 0:
                return self.cast(n, t)
        self.fail(source, f"cannot convert {n.type} to {t}")

    def binary(self, source, op, left, right):
        a, b = self.value(left), self.value(right)
        if op in ("&&", "||"):
            return self.node(
                source,
                "binary",
                INT,
                [self.scalar(source, a), self.scalar(source, b)],
                op,
            )
        if op == "+" and b.type.kind == "pointer" and a.type.integer:
            a, b = b, a
        if op in ("+", "-") and a.type.kind == "pointer":
            if not a.type.base.size and not self.variably_modified(a.type.base):
                self.fail(source, "arithmetic on incomplete/function pointer")
            if b.type.integer:
                scale = (
                    self.runtime_size(source, a.type.base)
                    if self.variably_modified(a.type.base)
                    else a.type.base.size
                )
                if op == "-":
                    scale = (
                        self.node(source, "unary", UINT, [scale], "-")
                        if isinstance(scale, Node)
                        else -scale
                    )
                return self.node(
                    source,
                    "pointer_add",
                    a.type,
                    [a, self.cast(b, INT)],
                    scale,
                )
            if op == "-" and b.type == a.type:
                scale = (
                    self.runtime_size(source, a.type.base)
                    if self.variably_modified(a.type.base)
                    else a.type.base.size
                )
                return self.node(source, "pointer_diff", INT, [a, b], scale)
        if op in ("==", "!=", "<", "<=", ">", ">=") and (
            a.type.kind == "pointer" or b.type.kind == "pointer"
        ):
            t = a.type if a.type.kind == "pointer" else b.type
            a, b = self.convert(source, a, t), self.convert(source, b, t)
        else:
            t = common(a.type, b.type)
            if op in ("<<", ">>"):
                t = a.type.promote()
                a, b = self.cast(a, t), self.cast(b, b.type.promote())
            else:
                a, b = self.cast(a, t), self.cast(b, t)
        result = INT if op in ("==", "!=", "<", "<=", ">", ">=") else t
        return self.node(source, "binary", result, [a, b], op)

    def printf(self, source):
        args = source.args.exprs if source.args else []
        if not args or not isinstance(args[0], c.Constant) or args[0].type != "string":
            self.fail(source, "printf format must be a string literal")
        data = string_bytes(args[0].value)[:-1]
        tokens = []
        literal_start = 0
        argument = 1
        index = 0

        def flush(end):
            nonlocal literal_start
            if end > literal_start:
                tokens.append(("literal", literal_start, end - literal_start))
            literal_start = end

        while index < len(data):
            if data[index] != ord("%"):
                index += 1
                continue
            flush(index)
            if index + 1 == len(data):
                self.fail(source, "incomplete printf conversion")
            conversion = chr(data[index + 1])
            if conversion == "%":
                tokens.append(("literal", index, 1))
            elif conversion in "ducxs":
                if argument == len(args):
                    self.fail(source, "printf has too few arguments")
                value = self.value(self.expr(args[argument]))
                argument += 1
                if conversion == "s":
                    if value.type.kind != "pointer" or value.type.base.unqualified() != CHAR:
                        self.fail(source, "printf %s requires a char pointer")
                else:
                    if not value.type.integer:
                        self.fail(source, f"printf %{conversion} requires an integer")
                    value = self.cast(
                        value, INT if conversion == "d" else UINT
                    )
                tokens.append((conversion, value))
            else:
                self.fail(source, f"unsupported printf conversion %{conversion}")
            index += 2
            literal_start = index
        flush(len(data))
        if argument != len(args):
            self.fail(source, "printf has too many arguments")
        return self.node(
            source,
            "printf",
            INT,
            [self.value(self.expr(args[0]))]
            + [token[1] for token in tokens if token[0] != "literal"],
            tokens,
        )

    def expr(self, s):
        if isinstance(s, c.Constant):
            if s.type == "string":
                data = string_bytes(s.value)
                sym = self.new(
                    f"__string_{self.serial}", array(CHAR, len(data)), "global"
                )
                sym.key = self.internal_key(sym.key)
                self.globals.append(Global(sym, bytearray(data), section="rodata"))
                return self.node(s, "var", sym.type, value=sym, lvalue=True)
            if s.type == "char":
                data = literal_bytes(s.value, "'")
                if len(data) != 1:
                    self.fail(s, "only single-character constants are supported")
                v = data[0]
                return self.node(s, "const", INT, value=v)
            if "float" in s.type or "double" in s.type:
                self.fail(s, "floating point is unsupported")
            if "ll" in s.value.lower():
                self.fail(s, "64-bit literals are unsupported")
            v = literal(s.value)
            if v > 0xFFFFFFFF:
                self.fail(s, "integer literal exceeds 32 bits")
            unsigned = "u" in s.value.lower()
            if v > 0x7FFFFFFF and not unsigned:
                if s.value.startswith("0"):
                    unsigned = True
                else:
                    self.fail(
                        s,
                        "decimal literal needs unsupported 64-bit type; use a U suffix for unsigned values",
                    )
            return self.node(s, "const", UINT if unsigned else INT, value=v)
        if isinstance(s, c.ID):
            sym = self.lookup(s)
            if sym.storage == "enum":
                return self.node(s, "const", INT, value=sym.constant)
            return self.node(
                s, "var", sym.type, value=sym, lvalue=sym.storage != "function"
            )
        if isinstance(s, c.BinaryOp):
            return self.binary(s, s.op, self.expr(s.left), self.expr(s.right))
        if isinstance(s, c.ArrayRef):
            p = self.binary(s, "+", self.expr(s.name), self.expr(s.subscript))
            if p.type.kind != "pointer":
                self.fail(s, "array subscript requires a pointer")
            return self.node(s, "deref", p.type.base, [p], lvalue=True)
        if isinstance(s, c.StructRef):
            base = self.expr(s.name)
            if s.type == "->":
                address = self.value(base)
                if address.type.kind != "pointer" or address.type.base.kind not in ("struct", "union"):
                    self.fail(s, "-> requires a pointer to structure")
                structure = address.type.base
            else:
                if base.type.kind not in ("struct", "union") or not base.lvalue:
                    self.fail(s, ". requires a structure or union lvalue")
                structure = base.type
                address = self.node(s, "address", pointer(structure), [base])
            _, member_type, offset = self.member_lookup(
                s.field, structure, s.field.name
            )
            if "const" in structure.qualifiers:
                member_type = member_type.qualified("const")
            return self.node(
                s, "member", member_type, [address], offset, lvalue=True
            )
        if isinstance(s, c.UnaryOp):
            if s.op == "sizeof":
                t = (
                    self.typename(s.expr)
                    if isinstance(s.expr, c.Typename)
                    else self.expr(s.expr).type
                )
                return self.runtime_size(s, t)
            a = self.expr(s.expr)
            if s.op == "&":
                if not a.lvalue and a.type.kind != "function":
                    self.fail(s, "address-of requires an lvalue")
                return self.node(s, "address", pointer(a.type), [a])
            if s.op == "*":
                a = self.value(a)
                if a.type.kind != "pointer" or a.type.base == VOID:
                    self.fail(s, "dereference requires a non-void pointer")
                return self.node(
                    s, "deref", a.type.base, [a], lvalue=a.type.base.kind != "function"
                )
            if s.op in ("++", "--", "p++", "p--"):
                self.modifiable(s, a)
                self.scalar(s, a)
                if (
                    a.type.kind == "pointer"
                    and not a.type.base.size
                    and not self.variably_modified(a.type.base)
                ):
                    self.fail(s, "invalid pointer increment")
                step = (
                    self.runtime_size(s, a.type.base)
                    if a.type.kind == "pointer" and self.variably_modified(a.type.base)
                    else a.type.base.size if a.type.kind == "pointer" else 1
                )
                return self.node(s, "increment", a.type, [a], (s.op, step))
            a = self.scalar(s, a)
            if s.op == "!":
                return self.node(s, "unary", INT, [a], "!")
            if not a.type.integer:
                self.fail(s, "integer unary operand required")
            a = self.cast(a, a.type.promote())
            if s.op == "+":
                return a
            if s.op in ("-", "~"):
                return self.node(s, "unary", a.type, [a], s.op)
            self.fail(s, "unsupported unary operator")
        if isinstance(s, c.Cast):
            t, a = self.typename(s.to_type), self.value(self.expr(s.expr))
            if t == VOID:
                return self.cast(a, t)
            if t.kind not in ("int", "pointer") or a.type.kind not in (
                "int",
                "pointer",
            ):
                self.fail(s, "unsupported cast")
            return self.cast(a, t)
        if isinstance(s, c.Assignment):
            a, b = self.expr(s.lvalue), self.expr(s.rvalue)
            self.modifiable(s, a)
            if s.op == "=":
                if a.type.kind in ("struct", "union"):
                    if a.type != b.type:
                        self.fail(s, f"cannot convert {b.type} to {a.type}")
                    if not b.lvalue:
                        self.fail(s, "aggregate assignment requires an aggregate lvalue")
                    return self.node(s, "aggregate_assign", a.type, [a, b])
                b = self.convert(s, b, a.type)
            else:
                # Keep the lvalue as a single node; lower its address only once.
                probe = self.binary(s, s.op[:-1], a, b)
                self.convert(s, probe, a.type)
                return self.node(s, "compound_assign", a.type, [a, b], s.op[:-1])
            return self.node(s, "assign", a.type, [a, b])
        if isinstance(s, c.FuncCall):
            if isinstance(s.name, c.ID) and s.name.name == "printf":
                # printf is compiler-lowered, but it is still an ordinary C
                # library name and must have been declared (normally by
                # <stdio.h>) before use.
                self.lookup(s.name)
                return self.printf(s)
            fn = self.value(self.expr(s.name))
            if fn.type.kind != "pointer" or fn.type.base.kind != "function":
                self.fail(s, "call requires a function")
            ft = fn.type.base
            args = s.args.exprs if s.args else []
            if len(args) != len(ft.params):
                self.fail(s, f"expected {len(ft.params)} arguments, got {len(args)}")
            values = [self.convert(s, self.expr(a), t) for a, t in zip(args, ft.params)]
            if ft.base.kind == "struct":
                return self.node(s, "aggregate_call", ft.base, [fn] + values, lvalue=True)
            return self.node(s, "call", ft.base, [fn] + values)
        if isinstance(s, c.ExprList):
            values = [self.value(self.expr(a)) for a in s.exprs]
            return self.node(s, "comma", values[-1].type, values)
        if isinstance(s, c.TernaryOp):
            cond = self.scalar(s, self.expr(s.cond))
            a, b = self.value(self.expr(s.iftrue)), self.value(self.expr(s.iffalse))
            t = (
                a.type
                if a.type.kind == "pointer"
                else b.type if b.type.kind == "pointer" else common(a.type, b.type)
            )
            return self.node(
                s, "select", t, [cond, self.convert(s, a, t), self.convert(s, b, t)]
            )
        self.fail(s, f"unsupported expression: {type(s).__name__}")

    def modifiable(self, s, n):
        if (
            not n.lvalue
            or n.type.kind in ("array", "vla", "function", "void")
            or "const" in n.type.qualifiers
        ):
            self.fail(s, "modifiable lvalue required")

    def resolve_array(self, s, t):
        if t.kind == "array" and not t.count:
            if isinstance(s.init, c.InitList):
                t = array(t.base, len(s.init.exprs))
            elif isinstance(s.init, c.Constant) and s.init.type == "string":
                t = array(t.base, len(string_bytes(s.init.value)))
            else:
                self.fail(s, "incomplete arrays need an initializer")
        return t

    def vla_bound(self, source):
        """Type-check the outer VLA bound; it is evaluated at declaration time."""
        declarator = source.type
        if not isinstance(declarator, c.ArrayDecl) or declarator.dim is None:
            raise AssertionError("VLA declaration without an outer array bound")
        bound = self.scalar(declarator.dim, self.expr(declarator.dim))
        if not bound.type.integer:
            self.fail(declarator.dim, "variable array bound requires an integer")
        return self.cast(bound, UINT)

    @staticmethod
    def aggregate_type(t):
        return t.kind in ("array", "struct", "union")

    def designated_member(self, source, t, designators):
        """Resolve a C designator sequence to an aggregate subobject.

        The returned index is the first direct member/subscript selected.  It
        lets a following undesignated initializer resume at the next direct
        subobject, as required by C's aggregate-initializer rules.
        """
        offset = 0
        direct_index = None
        for designator in designators:
            if isinstance(designator, c.ID) and t.kind in ("struct", "union"):
                index, t, member_offset = self.member_lookup(
                    designator, t, designator.name
                )
                direct_index = index if direct_index is None else direct_index
                offset += member_offset
            elif isinstance(designator, c.Constant) and t.kind == "array":
                index = self.const_int(designator)
                if not 0 <= index < t.count:
                    self.fail(designator, "array designator is outside the array")
                direct_index = index if direct_index is None else direct_index
                offset += index * t.base.size
                t = t.base
            else:
                self.fail(designator, "invalid initializer designator")
        if direct_index is None:
            self.fail(source, "empty initializer designator")
        return direct_index, t, offset

    def consume_initializer(self, source, t, expressions, position):
        """Consume one possibly brace-elided aggregate initializer."""
        if position >= len(expressions):
            return [], position
        expression = expressions[position]
        if not self.aggregate_type(t) or isinstance(expression, c.InitList):
            return self.initializer(source, t, expression), position + 1
        if t.kind == "array" and isinstance(expression, c.Constant) and expression.type == "string":
            return self.initializer(source, t, expression), position + 1
        if t.kind == "union":
            _, member_type, member_offset = t.record.members[0]
            entries, position = self.consume_initializer(
                source, member_type, expressions, position
            )
            return [
                (member_offset + offset, type_, node) for offset, type_, node in entries
            ], position
        return self.consume_aggregate(source, t, expressions, position)

    def consume_aggregate(self, source, t, expressions, position=0):
        """Flatten one struct/array initializer, accepting omitted inner braces."""
        members = (
            [(member_type, offset) for _, member_type, offset in t.record.members]
            if t.kind == "struct"
            else [(t.base, index * t.base.size) for index in range(t.count)]
        )
        out = []
        member_index = 0
        while position < len(expressions):
            expression = expressions[position]
            if isinstance(expression, c.NamedInitializer):
                member_index, member_type, member_offset = self.designated_member(
                    source, t, expression.name
                )
                entries = self.initializer(source, member_type, expression.expr)
                position += 1
            else:
                if member_index >= len(members):
                    break
                member_type, member_offset = members[member_index]
                entries, position = self.consume_initializer(
                    source, member_type, expressions, position
                )
            out.extend(
                (member_offset + offset, type_, node) for offset, type_, node in entries
            )
            member_index += 1
        return out, position

    def aggregate_initializer(self, source, t, expressions):
        out, position = self.consume_aggregate(source, t, expressions)
        if position != len(expressions):
            self.fail(source, "too many aggregate initializers")
        return out

    def initializer(self, s, t, init):
        """Flatten aggregate initializers to typed scalar entries at byte offsets."""
        if t.kind == "array":
            if (
                isinstance(init, c.Constant)
                and init.type == "string"
                and t.base.size == 1
            ):
                data = string_bytes(init.value)
                if len(data) - 1 > t.count:
                    self.fail(s, "string initializer too long")
                return [
                    (i, t.base, self.node(init, "const", INT, value=b))
                    for i, b in enumerate(data[: t.count])
                ]
            if not isinstance(init, c.InitList):
                self.fail(s, "array requires a brace or string initializer")
            return self.aggregate_initializer(s, t, init.exprs)
        if t.kind in ("struct", "union"):
            if not isinstance(init, c.InitList):
                self.fail(s, f"{t.kind} requires a brace initializer")
            if t.kind == "union":
                if not init.exprs:
                    return []
                if len(init.exprs) != 1:
                    self.fail(s, "union initializer selects exactly one member")
                expression = init.exprs[0]
                member_index = 0
                if isinstance(expression, c.NamedInitializer):
                    member_index, member_type, member_offset = self.designated_member(
                        s, t, expression.name
                    )
                    expression = expression.expr
                else:
                    _, member_type, member_offset = t.record.members[member_index]
                return [
                    (member_offset + offset, type_, node)
                    for offset, type_, node in self.initializer(s, member_type, expression)
                ]
            return self.aggregate_initializer(s, t, init.exprs)
        if isinstance(init, c.InitList):
            if len(init.exprs) != 1:
                self.fail(s, "scalar initializer requires one value")
            init = init.exprs[0]
        return [(0, t, self.convert(s, self.expr(init), t))]

    def initialize_static_object(self, source, global_, init):
        if init is None:
            return
        for offset, type_, node in self.initializer(
            source, global_.symbol.type, init
        ):
            try:
                value = self.static_value(node)
            except (ZeroDivisionError, ValueError):
                self.fail(source, "invalid static initializer")
            if isinstance(value, tuple):
                if type_.size != 4:
                    self.fail(source, "address initializer needs a 32-bit destination")
                global_.relocations.append((offset, *value))
            else:
                global_.data[offset : offset + type_.size] = (
                    value & ((1 << (8 * type_.size)) - 1)
                ).to_bytes(type_.size, "big")

    def statement(self, s):
        if s is None:
            return Node("block")
        if isinstance(s, c.Compound):
            scope_id = self.push_scope()
            body = [self.statement(x) for x in s.block_items or []]
            self.pop_scope()
            return self.node(s, "block", children=body, value=scope_id)
        if isinstance(s, c.DeclList):
            return self.node(s, "block", children=[self.statement(x) for x in s.decls])
        if isinstance(s, c.Typedef):
            if s.name in self.typedefs[-1]:
                self.fail(s, "duplicate typedef")
            t = self.typename(s)
            t, bounds = self.bind_vla_bounds(s, t) if self.variably_modified(t) else (t, [])
            self.typedefs[-1][s.name] = t
            return self.node(s, "vla_bounds", value=bounds) if bounds else self.node(s, "block")
        if isinstance(s, c.Decl):
            if not s.name:
                self.typename(s)
                return self.node(s, "block")
            if any(storage != "static" for storage in s.storage):
                self.fail(
                    s,
                    "unsupported local storage specifier",
                )
            t = self.resolve_array(s, self.typename(s))
            t, vla_bounds = self.bind_vla_bounds(s, t) if self.variably_modified(t) else (t, [])
            dynamic_object = t.kind in ("array", "vla") and self.variably_modified(t)
            if self.variably_modified(t):
                self.scope_vlas[-1] = True
            if t.kind in ("void", "function") or (not t.size and not dynamic_object and t.kind != "pointer"):
                self.fail(s, "local variable requires a complete object type")
            if s.name in self.scopes[-1]:
                self.fail(s, "duplicate local declaration")
            if "static" in s.storage:
                self.serial += 1
                sym = Symbol(
                    s.name,
                    t,
                    "global",
                    self.internal_key(f"__static_{self.serial}_{s.name}"),
                )
                global_ = Global(sym, bytearray(t.size))
                self.globals.append(global_)
                self.initialize_static_object(s, global_, s.init)
                self.scopes[-1][s.name] = sym
                return self.node(s, "block")
            sym = self.new(s.name, t, "local")
            self.scopes[-1][s.name] = sym
            self.locals.append(sym)
            if dynamic_object and s.init is not None:
                self.fail(s, "variable-length arrays cannot have initializers")
            aggregate_initial = (
                self.expr(s.init)
                if t.kind in ("struct", "union") and s.init is not None and not isinstance(s.init, c.InitList)
                else None
            )
            if aggregate_initial is not None and (
                aggregate_initial.type != t or not aggregate_initial.lvalue
            ):
                self.fail(s, f"cannot convert {aggregate_initial.type} to {t}")
            entries = self.initializer(s, t, s.init) if s.init and aggregate_initial is None else None
            size = self.runtime_size(s, t) if dynamic_object else None
            declaration = self.node(s, "declare", value=(sym, entries, vla_bounds, size))
            if aggregate_initial is None:
                return declaration
            destination = self.node(s, "var", t, value=sym, lvalue=True)
            return self.node(
                s,
                "block",
                children=[
                    declaration,
                    self.node(
                        s,
                        "expression",
                        children=[self.node(s, "aggregate_assign", t, [destination, aggregate_initial])],
                    ),
                ],
            )
        if isinstance(s, c.Return):
            if self.return_type == VOID:
                if s.expr:
                    self.fail(s, "void function cannot return a value")
                children = []
            else:
                if not s.expr:
                    self.fail(s, "non-void function must return a value")
                value = self.expr(s.expr)
                if self.return_type.kind == "struct":
                    if value.type != self.return_type or not value.lvalue:
                        self.fail(s, f"cannot convert {value.type} to {self.return_type}")
                    children = [value]
                else:
                    children = [self.convert(s, value, self.return_type)]
            return self.node(s, "return", children=children)
        if isinstance(s, c.If):
            return self.node(
                s,
                "if",
                children=[
                    self.scalar(s, self.expr(s.cond)),
                    self.statement(s.iftrue),
                    self.statement(s.iffalse),
                ],
            )
        if isinstance(s, c.Switch):
            condition = self.scalar(s.cond, self.expr(s.cond))
            if not condition.type.integer:
                self.fail(s.cond, "switch condition requires an integer")
            condition = self.cast(condition, condition.type.promote())
            source_vlas = frozenset(
                scope_id for scope_id, has_vla in zip(self.scope_ids, self.scope_vlas) if has_vla
            )
            self.break_depth += 1
            self.switch_depth += 1
            self.switch_types.append(condition.type)
            self.switch_vlas.append(source_vlas)
            body = self.statement(s.stmt)
            self.switch_vlas.pop()
            self.switch_types.pop()
            self.switch_depth -= 1
            self.break_depth -= 1
            return self.node(s, "switch", children=[condition, body])
        if isinstance(s, (c.While, c.For, c.DoWhile)):
            self.loop_depth += 1
            self.break_depth += 1
            scope_id = self.push_scope()
            if isinstance(s, c.For):
                init = self.statement(s.init)
                cond = (
                    self.scalar(s, self.expr(s.cond))
                    if s.cond
                    else Node("const", INT, value=1)
                )
                step = self.statement(s.next)
                body = self.statement(s.stmt)
                n = self.node(s, "for", children=[init, cond, step, body], value=scope_id)
            else:
                n = self.node(
                    s,
                    "do" if isinstance(s, c.DoWhile) else "while",
                    children=[
                        self.scalar(s, self.expr(s.cond)),
                        self.statement(s.stmt),
                    ], value=scope_id,
                )
            self.pop_scope()
            self.loop_depth -= 1
            self.break_depth -= 1
            return n
        if isinstance(s, c.Break):
            if not self.break_depth:
                self.fail(s, "break outside loop or switch")
            return self.node(s, "break")
        if isinstance(s, c.Continue):
            if not self.loop_depth:
                self.fail(s, "continue outside loop")
            return self.node(s, "continue")
        if isinstance(s, c.Case):
            if not self.switch_depth:
                self.fail(s, "case outside switch")
            self.check_jump_into_vla(s, "case")
            value = self.normalize_constant(self.const_int(s.expr), self.switch_types[-1])
            return self.node(
                s, "case", children=[self.statement(x) for x in s.stmts], value=value
            )
        if isinstance(s, c.Default):
            if not self.switch_depth:
                self.fail(s, "default outside switch")
            self.check_jump_into_vla(s, "default")
            return self.node(s, "default", children=[self.statement(x) for x in s.stmts])
        if isinstance(s, c.Label):
            if s.name in self.labels:
                self.fail(s, f"duplicate label {s.name}")
            self.labels[s.name] = (tuple(self.scope_ids), frozenset(
                scope_id for scope_id, has_vla in zip(self.scope_ids, self.scope_vlas) if has_vla
            ))
            return self.node(s, "label", children=[self.statement(s.stmt)], value=s.name)
        if isinstance(s, c.Goto):
            node = self.node(s, "goto")
            self.gotos.append((
                node,
                s.name,
                tuple(self.scope_ids),
                frozenset(
                    scope_id for scope_id, has_vla in zip(self.scope_ids, self.scope_vlas) if has_vla
                ),
            ))
            return node
        if isinstance(s, c.EmptyStatement):
            return self.node(s, "block")
        return self.node(s, "expression", children=[self.expr(s)])

    @staticmethod
    def normalize_constant(value, type_):
        if isinstance(value, tuple):
            return value
        if type_.kind not in ("int", "bool", "pointer"):
            raise CompileError("constant requires an integer or pointer type")
        if type_.kind == "bool":
            return int(bool(value))
        bits = type_.size * 8
        value &= (1 << bits) - 1
        if type_.signed and value & (1 << (bits - 1)):
            value -= 1 << bits
        return value

    def static_value(self, n):
        """Evaluate typed constant expressions and symbolic address relocations.

        Apply target-width conversions at every operation, not just at the final
        data store. This matters for (signed char)255 and unsigned wraparound.
        """
        if n.op == "bool_cast":
            return int(bool(self.static_value(n.children[0])))
        if n.op == "cast":
            return self.normalize_constant(self.static_value(n.children[0]), n.type)
        if n.op == "const":
            return self.normalize_constant(n.value, n.type)
        if n.op == "address":
            value = n.children[0]
            if value.op == "var" and value.value.storage in ("global", "function"):
                return (value.value.key, 0)
            if value.op == "deref":
                return self.static_value(value.children[0])
        if n.op == "pointer_add":
            base = self.static_value(n.children[0])
            delta = self.static_value(n.children[1])
            if isinstance(base, tuple) and isinstance(delta, int):
                return (base[0], base[1] + delta * n.value)
        if n.op == "select":
            cond = self.static_value(n.children[0])
            if isinstance(cond, int):
                return self.static_value(n.children[1 if cond else 2])
        if n.op == "unary":
            value = self.static_value(n.children[0])
            if isinstance(value, int):
                value = {
                    "-": lambda: -value,
                    "~": lambda: ~value,
                    "!": lambda: int(not value),
                }[n.value]()
                return self.normalize_constant(value, n.type)
        if n.op == "binary":
            a = self.static_value(n.children[0])
            if n.value == "&&" and a == 0:
                return 0
            if n.value == "||" and isinstance(a, int) and a != 0:
                return 1
            b = self.static_value(n.children[1])
            if isinstance(a, int) and isinstance(b, int):

                def quotient():
                    return (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)

                operations = {
                    "+": lambda: a + b,
                    "-": lambda: a - b,
                    "*": lambda: a * b,
                    "/": quotient,
                    "%": lambda: a - quotient() * b,
                    "<<": lambda: a << b,
                    ">>": lambda: a >> b,
                    "&": lambda: a & b,
                    "|": lambda: a | b,
                    "^": lambda: a ^ b,
                    "==": lambda: int(a == b),
                    "!=": lambda: int(a != b),
                    "<": lambda: int(a < b),
                    "<=": lambda: int(a <= b),
                    ">": lambda: int(a > b),
                    ">=": lambda: int(a >= b),
                    "&&": lambda: int(bool(a) and bool(b)),
                    "||": lambda: int(bool(a) or bool(b)),
                }
                if n.value in ("<<", ">>") and not 0 <= b < 32:
                    raise CompileError(f"{n.location}: invalid constant shift count")
                return self.normalize_constant(operations[n.value](), n.type)
        raise CompileError(f"{n.location}: unsupported static constant initializer")

    def build(self, tree, require_main=True):
        definitions = {}
        for item in tree.ext:
            if isinstance(item, c.Typedef):
                self.typedefs[0][item.name] = self.typename(item)
                continue
            d = item.decl if isinstance(item, c.FuncDef) else item
            if isinstance(d, c.Decl) and not d.name:
                self.typename(d)
                continue
            if not isinstance(d, c.Decl):
                self.fail(d, "unsupported top-level declaration")
            t = (
                self.resolve_array(d, self.typename(d))
                if not isinstance(d.type, c.FuncDecl)
                else self.typename(d)
            )
            storage = "function" if t.kind == "function" else "global"
            if any(x not in ("static", "extern") for x in d.storage):
                self.fail(d, "unsupported storage specifier")
            old = self.scopes[0].get(d.name)
            if old and old.type != t:
                self.fail(d, "conflicting declaration")
            sym = old or self.new(d.name, t, storage)
            if old is None and "static" in d.storage:
                sym.key = self.internal_key(d.name)
            self.scopes[0][d.name] = sym
            if isinstance(item, c.FuncDef):
                if d.name in definitions:
                    self.fail(d, "duplicate function definition")
                definitions[d.name] = item
            elif storage == "global" and "extern" not in d.storage:
                if any(g.symbol.name == d.name for g in self.globals):
                    self.fail(d, "duplicate global definition")
                if not t.size:
                    self.fail(d, "global requires a complete object type")
                self.globals.append(Global(sym, bytearray(t.size)))
        # All global/function names are now available to initializers and bodies.
        for item in tree.ext:
            if isinstance(item, c.Decl) and item.init is not None:
                sym = self.scopes[0][item.name]
                g = next((g for g in self.globals if g.symbol == sym), None)
                if g is None:
                    self.fail(item, "extern initializers are unsupported")
                self.initialize_static_object(item, g, item.init)
        for name, item in definitions.items():
            sym = self.scopes[0][name]
            self.locals = []
            self.labels = {}
            self.gotos = []
            function_scope = self.push_scope()
            params = []
            declarations = item.decl.type.args.params if item.decl.type.args else []
            parameter_bounds = []
            for d, t in zip(declarations, sym.type.params):
                if not d.name:
                    self.fail(d, "definition parameters need names")
                if d.name in self.scopes[-1]:
                    self.fail(d, "duplicate parameter")
                raw = self.typename(d)
                if self.variably_modified(raw):
                    raw, bounds = self.bind_vla_bounds(d, raw)
                    parameter_bounds.extend(bounds)
                    t = raw.decay()
                p = self.new(d.name, t, "parameter")
                params.append(p)
                self.scopes[-1][d.name] = p
                if self.variably_modified(t):
                    self.scope_vlas[-1] = True
            self.return_type = sym.type.base
            body = self.node(
                item.body,
                "block",
                children=(
                    [self.node(item.body, "vla_bounds", value=parameter_bounds)]
                    if parameter_bounds else []
                ) + [self.statement(x) for x in item.body.block_items or []],
                value=function_scope,
            )
            self.resolve_gotos()
            self.functions.append(Function(sym, params, self.locals, body))
            self.pop_scope()
        main = self.scopes[0].get("main")
        if require_main:
            if not main or "main" not in definitions:
                raise CompileError("a definition of main is required")
            if main.type.params or main.type.base != INT:
                raise CompileError("entry point must be int main(void)")
        return Program(
            self.globals,
            self.functions,
            {symbol.key: symbol for symbol in self.scopes[0].values()},
        )


def typecheck(tree, require_main=True, namespace=""):
    return Frontend(namespace).build(tree, require_main=require_main)
