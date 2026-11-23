#!/usr/bin/env python3
"""Win64 virtual-method count must use the full class pointer.

Creating a class mock or stub counts virtual methods in
Source/Delphi.Mocks.Utils.pas. These tests run that function as it is
written, on a Win64 class reference (a 64-bit VMT pointer), and require:

* the only method-table load is an 8-byte read of the vmtClassName slot
  at the original class pointer, and
* the returned count is

    (namePointer - (classPointer + vmtParent) - SizeOf(Pointer))
      div SizeOf(Pointer)

A class whose real count is not greater than the source threshold must
be rejected by TMock<T>.Create, TStub<T>.Create, and TAutoMock.Mock.
Bytes placed at the low 32 bits of the class pointer must not be read
and must not change that decision.

The tests fail while GetVirtualMethodCount casts the class reference
through Integer and loads a 4-byte Integer from the truncated address.
They pass once that cast is gone and the slot is read as a native pointer.

Run from the repository root:

    python3 Tests/Win64VirtualMethodCount/test_truncated_class_pointer.py

The expression evaluator accepts the current function and a native-width
repair of the same shape, including local variables, NativeInt or
NativeUInt, PNativeInt or PPointer or PByte, SizeOf(Pointer), and
integer div. Integer stays 32-bit. NativeInt and pointers stay 64-bit.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import faulthandler
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

faulthandler.disable()

REPO = Path(__file__).resolve().parents[2]
UTILS = REPO / "Source" / "Delphi.Mocks.Utils.pas"
VALIDATION = REPO / "Source" / "Delphi.Mocks.Validation.pas"
MOCKS = REPO / "Source" / "Delphi.Mocks.pas"
AUTOMOCK = REPO / "Source" / "Delphi.Mocks.AutoMock.pas"

# RAD Studio Athens / Sydney / Alexandria "Internal Data Formats (Delphi)":
# Win64 column of the virtual method table. Integer is 4 bytes on every
# platform in that document; a class reference on Win64 is an 8-byte VMT
# pointer; a pointer is 8 bytes. vmtClassName and vmtParent are the
# System.pas constants named in that table.
ATHENS_WIN64 = {
    "name": "athens-sydney-alexandria",
    "vmtClassName": -136,
    "vmtParent": -120,
}
# System.pas CPUX64 block quoted from the XE2-era compiler (the minimum
# compiler in Source/Delphi.Mocks.inc) by Marc Durdin, May 2012.
XE2_WIN64 = {
    "name": "xe2-cpux64",
    "vmtClassName": -112,
    "vmtParent": -96,
}
LAYOUTS = (ATHENS_WIN64, XE2_WIN64)

POINTER_SIZE = 8
INTEGER_BITS = 32
CLASS_POINTER = 0x00007FF600021000
PAGE = 4096

READER_C = r"""
#define _GNU_SOURCE
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>

static sigjmp_buf slot_jmp;

static void slot_fault(int sig) {
  (void)sig;
  siglongjmp(slot_jmp, 1);
}

static int read_bytes(uint64_t addr, void *dst, size_t n) {
  struct sigaction next;
  struct sigaction prev;
  memset(&next, 0, sizeof next);
  next.sa_handler = slot_fault;
  sigemptyset(&next.sa_mask);
  if (sigaction(SIGSEGV, &next, &prev) != 0)
    return -2;
  if (sigaction(SIGBUS, &next, 0) != 0) {
    sigaction(SIGSEGV, &prev, 0);
    return -2;
  }
  if (sigsetjmp(slot_jmp, 1) != 0) {
    sigaction(SIGSEGV, &prev, 0);
    sigaction(SIGBUS, &prev, 0);
    return -1;
  }
  memcpy(dst, (const void *)(uintptr_t)addr, n);
  sigaction(SIGSEGV, &prev, 0);
  sigaction(SIGBUS, &prev, 0);
  return 0;
}

int read_slot(uint64_t addr, size_t n, uint64_t *out) {
  unsigned char buf[8];
  size_t i;
  int rc;
  if (n == 0 || n > 8)
    return -2;
  memset(buf, 0, sizeof buf);
  rc = read_bytes(addr, buf, n);
  if (rc != 0)
    return rc;
  *out = 0;
  for (i = 0; i < n; i++)
    *out |= ((uint64_t)buf[i]) << (8 * i);
  return 0;
}
"""


class AccessViolation(Exception):
    def __init__(self, address, size):
        self.address = address
        self.size = size
        super().__init__(
            "access violation reading %d bytes at 0x%x" % (size, address)
        )


class SlotReader:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory(prefix="vmt-slot-")
        src = Path(self.dir.name) / "read_slot.c"
        so = Path(self.dir.name) / "read_slot.so"
        src.write_text(READER_C)
        subprocess.check_call(
            ["gcc", "-shared", "-fPIC", "-O0", "-o", str(so), str(src)],
            stdout=subprocess.DEVNULL,
        )
        self.lib = ctypes.CDLL(str(so))
        self.lib.read_slot.argtypes = [
            ctypes.c_uint64,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self.lib.read_slot.restype = ctypes.c_int
        self.reads = []

    def close(self):
        self.dir.cleanup()

    def read(self, address, size):
        address &= (1 << 64) - 1
        out = ctypes.c_uint64()
        rc = self.lib.read_slot(address, size, ctypes.byref(out))
        self.reads.append((address, size, rc))
        if rc == -1:
            raise AccessViolation(address, size)
        if rc != 0:
            raise OSError("read_slot failed at 0x%x size %d rc %d" % (address, size, rc))
        value = out.value
        if size == 4:
            return _sign32(value)
        if size == 8:
            return _sign64(value)
        return value


def _sign32(value):
    value &= 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def _sign64(value):
    value &= (1 << 64) - 1
    if value >= (1 << 63):
        value -= 1 << 64
    return value


def _wrap32(value):
    return _sign32(value)


def _wrap64(value):
    return _sign64(value)


class PageMap:
    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.libc.mmap.restype = ctypes.c_void_p
        self.libc.mmap.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_long,
        ]
        self.libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.libc.munmap.restype = ctypes.c_int
        self.spans = []

    def map(self, address, length):
        prot = 1 | 2  # PROT_READ | PROT_WRITE
        flags = 0x02 | 0x20 | 0x100000  # MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE
        ctypes.set_errno(0)
        got = self.libc.mmap(ctypes.c_void_p(address), length, prot, flags, -1, 0)
        if got in (None, -1) or (isinstance(got, int) and got == (1 << 64) - 1):
            err = ctypes.get_errno()
            raise OSError("mmap(0x%x, 0x%x) failed errno %d" % (address, length, err))
        if got != address:
            raise OSError("mmap returned 0x%x, wanted 0x%x" % (got, address))
        self.spans.append((address, length))

    def write(self, address, data):
        buf = ctypes.create_string_buffer(data)
        ctypes.memmove(address, buf, len(data))

    def close(self):
        for address, length in reversed(self.spans):
            self.libc.munmap(ctypes.c_void_p(address), length)
        self.spans.clear()


# --- Pascal expression evaluator (Win64 Delphi sizes) ---

_TYPES = {
    "integer": ("i32", None),
    "nativeint": ("i64", None),
    "nativeuint": ("u64", None),
    "int64": ("i64", None),
    "cardinal": ("u32", None),
    "pointer": ("ptr", None),
    "pbyte": ("pbyte", 1),
    "pinteger": ("pinteger", 4),
    "pnativeint": ("pnativeint", 8),
    "pnativeuint": ("pnativeuint", 8),
    "ppointer": ("ppointer", 8),
    "pcardinal": ("pcardinal", 4),
    "pint64": ("pint64", 8),
}


class Value:
    def __init__(self, kind, bits, width=None):
        self.kind = kind
        self.bits = bits
        self.width = width

    def __repr__(self):
        return "Value(%s, %s, %s)" % (self.kind, self.bits, self.width)


def _tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if ch == "{":
            end = text.find("}", i + 1)
            if end < 0:
                raise SyntaxError("unterminated comment")
            i = end + 1
            continue
        if ch == "(" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*)", i + 2)
            if end < 0:
                raise SyntaxError("unterminated comment")
            i = end + 2
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            low = word.lower()
            if low in ("div", "mod", "and", "or", "xor", "not", "shl", "shr"):
                tokens.append(("op", low))
            elif low in (
                "begin",
                "end",
                "var",
                "function",
                "result",
            ):
                tokens.append(("kw", low))
            else:
                tokens.append(("id", word))
            i = j
            continue
        if ch.isdigit() or (ch == "$" and i + 1 < n and text[i + 1].isalnum()):
            if ch == "$":
                j = i + 1
                while j < n and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                tokens.append(("num", int(text[i + 1 : j], 16)))
                i = j
                continue
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            tokens.append(("num", int(text[i:j])))
            i = j
            continue
        if text.startswith(":=", i):
            tokens.append(("op", ":="))
            i += 2
            continue
        if text.startswith("<>", i) or text.startswith("<=", i) or text.startswith(">=", i):
            tokens.append(("op", text[i : i + 2]))
            i += 2
            continue
        if ch == ":":
            tokens.append(("punc", ch))
            i += 1
            continue
        if ch in "+-*/^().,;=<>":
            tokens.append(("op" if ch in "+-*/^=<>" else "punc", ch))
            i += 1
            continue
        raise SyntaxError("unexpected character %r at %d" % (ch, i))
    return tokens


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    def peek(self):
        if self.i >= len(self.tokens):
            return None
        return self.tokens[self.i]

    def pop(self):
        tok = self.peek()
        if tok is None:
            raise SyntaxError("unexpected end")
        self.i += 1
        return tok

    def accept(self, kind, value=None):
        tok = self.peek()
        if tok is None:
            return None
        if tok[0] != kind:
            return None
        if value is not None and tok[1].lower() != value.lower():
            return None
        self.i += 1
        return tok

    def expect(self, kind, value=None):
        tok = self.accept(kind, value)
        if tok is None:
            raise SyntaxError("expected %s %s, got %s" % (kind, value, self.peek()))
        return tok


def _is_pointer_kind(kind):
    return kind in (
        "ptr",
        "pbyte",
        "pinteger",
        "pnativeint",
        "pnativeuint",
        "ppointer",
        "pcardinal",
        "pint64",
    )


def _as_int(value):
    if value.kind == "i32":
        return _wrap32(value.bits), 32
    if value.kind == "u32":
        return value.bits & 0xFFFFFFFF, 32
    if value.kind in ("i64", "u64") or _is_pointer_kind(value.kind):
        return value.bits, 64
    raise TypeError("not an integer: %s" % value.kind)


def _promote_pair(left, right):
    lv, lw = _as_int(left)
    rv, rw = _as_int(right)
    if lw == 64 or rw == 64:
        return _sign64(lv), _sign64(rv), 64
    return _sign32(lv), _sign32(rv), 32


class Evaluator:
    def __init__(self, reader, constants):
        self.reader = reader
        self.constants = {
            "vmtclassname": constants["vmtClassName"],
            "vmtparent": constants["vmtParent"],
        }
        self.env = {}

    def eval_body(self, body, args):
        self.env = {}
        for name, value in args.items():
            self.env[name.lower()] = value
        self.env["result"] = Value("i32", 0)
        parser = Parser(_tokenize(body))
        self._block(parser)
        return self.env["result"]

    def _block(self, parser):
        if parser.accept("kw", "var"):
            self._var_section(parser)
        parser.expect("kw", "begin")
        while not parser.accept("kw", "end"):
            self._statement(parser)
            parser.accept("punc", ";")

    def _var_section(self, parser):
        while True:
            tok = parser.peek()
            if tok is None or tok[0] == "kw":
                return
            names = [parser.expect("id")[1]]
            while parser.accept("punc", ","):
                names.append(parser.expect("id")[1])
            parser.expect("punc", ":")
            type_name = parser.expect("id")[1].lower()
            parser.accept("punc", ";")
            kind, width = _TYPES.get(type_name, ("i64", None))
            for name in names:
                self.env[name.lower()] = Value(kind, 0, width)

    def _statement(self, parser):
        tok = parser.peek()
        if tok and tok[0] in ("id", "kw") and tok[1].lower() == "result":
            parser.pop()
            name = "result"
        else:
            name = parser.expect("id")[1].lower()
        parser.expect("op", ":=")
        value = self._expr(parser)
        if name == "result":
            number, _width = _as_int(value)
            self.env["result"] = Value("i32", _wrap32(number))
        else:
            self.env[name] = value

    def _expr(self, parser):
        return self._comparison(parser)

    def _comparison(self, parser):
        left = self._additive(parser)
        tok = parser.peek()
        if tok and tok[0] == "op" and tok[1] in (">", "<", ">=", "<=", "=", "<>"):
            op = parser.pop()[1]
            right = self._additive(parser)
            lv, rv, _width = _promote_pair(left, right)
            if op == ">":
                ok = lv > rv
            elif op == "<":
                ok = lv < rv
            elif op == ">=":
                ok = lv >= rv
            elif op == "<=":
                ok = lv <= rv
            elif op == "=":
                ok = lv == rv
            else:
                ok = lv != rv
            return Value("i32", 1 if ok else 0)
        return left

    def _additive(self, parser):
        left = self._term(parser)
        while True:
            tok = parser.peek()
            if not tok or tok[0] != "op" or tok[1] not in ("+", "-"):
                return left
            op = parser.pop()[1]
            right = self._term(parser)
            left = self._add(left, right, op)

    def _term(self, parser):
        left = self._unary(parser)
        while True:
            tok = parser.peek()
            if not tok or tok[0] != "op" or tok[1] not in ("*", "div", "mod"):
                return left
            op = parser.pop()[1]
            right = self._unary(parser)
            lv, rv, width = _promote_pair(left, right)
            if op == "*":
                result = lv * rv
            elif op == "div":
                if rv == 0:
                    raise ZeroDivisionError("div by zero")
                result = int(lv / rv)
            else:
                if rv == 0:
                    raise ZeroDivisionError("mod by zero")
                result = lv - int(lv / rv) * rv
            if width == 32:
                left = Value("i32", _wrap32(result))
            else:
                left = Value("i64", _wrap64(result))

    def _unary(self, parser):
        tok = parser.peek()
        if tok and tok[0] == "op" and tok[1] in ("+", "-"):
            op = parser.pop()[1]
            value = self._unary(parser)
            if op == "+":
                return value
            number, width = _as_int(value)
            number = -_sign64(number) if width == 64 else -_sign32(number)
            if width == 32:
                return Value("i32", _wrap32(number))
            return Value("i64", _wrap64(number))
        return self._postfix(parser)

    def _postfix(self, parser):
        value = self._primary(parser)
        while parser.accept("op", "^"):
            value = self._deref(value)
        return value

    def _primary(self, parser):
        if parser.accept("punc", "("):
            value = self._expr(parser)
            parser.expect("punc", ")")
            return value
        if parser.accept("kw", "result"):
            return self.env["result"]
        tok = parser.peek()
        if tok and tok[0] == "num":
            parser.pop()
            return Value("i32", _wrap32(tok[1]))
        if tok and tok[0] == "id":
            name = parser.pop()[1]
            low = name.lower()
            if low == "sizeof":
                parser.expect("punc", "(")
                arg = parser.expect("id")[1].lower()
                parser.expect("punc", ")")
                if arg == "pointer":
                    return Value("i32", POINTER_SIZE)
                if arg in ("nativeint", "nativeuint", "int64"):
                    return Value("i32", 8)
                if arg == "integer":
                    return Value("i32", 4)
                raise SyntaxError("SizeOf(%s) is not modeled" % arg)
            if low in _TYPES and parser.accept("punc", "("):
                inner = self._expr(parser)
                parser.expect("punc", ")")
                return self._cast(low, inner)
            if low in self.constants and low not in self.env:
                return Value("i32", _wrap32(self.constants[low]))
            if low not in self.env:
                raise NameError(name)
            return self.env[low]
        raise SyntaxError("expected expression, got %s" % (tok,))

    def _cast(self, type_name, inner):
        kind, width = _TYPES[type_name]
        if kind == "i32":
            number, _width = _as_int(inner)
            return Value("i32", _wrap32(number))
        if kind == "u32":
            number, _width = _as_int(inner)
            return Value("u32", number & 0xFFFFFFFF)
        if kind == "i64":
            number, width_in = _as_int(inner)
            if width_in == 32:
                number = _sign32(number)
            return Value("i64", _sign64(number))
        if kind == "u64":
            number, width_in = _as_int(inner)
            if width_in == 32 and inner.kind == "i32":
                number = _sign32(number) & ((1 << 64) - 1)
            return Value("u64", number & ((1 << 64) - 1))
        # Pointer casts. A 32-bit Integer is sign-extended; a native
        # integer or pointer keeps all 64 bits.
        number, width_in = _as_int(inner)
        if width_in == 32:
            address = _sign32(number) & ((1 << 64) - 1)
        else:
            address = number & ((1 << 64) - 1)
        return Value(kind, address, width)

    def _add(self, left, right, op):
        sign = -1 if op == "-" else 1
        if left.kind == "pbyte" or (
            _is_pointer_kind(left.kind) and left.kind != "ptr" and right.kind in ("i32", "u32", "i64", "u64")
        ):
            delta, _width = _as_int(right)
            if right.kind == "i32":
                delta = _sign32(delta)
            address = (left.bits + sign * _sign64(delta)) & ((1 << 64) - 1)
            return Value(left.kind, address, left.width)
        lv, rv, width = _promote_pair(left, right)
        result = lv + sign * rv
        if width == 32:
            return Value("i32", _wrap32(result))
        return Value("i64", _wrap64(result))

    def _deref(self, value):
        if value.kind == "pinteger" or value.kind == "pcardinal":
            loaded = self.reader.read(value.bits, 4)
            if value.kind == "pcardinal":
                return Value("u32", loaded & 0xFFFFFFFF)
            return Value("i32", _wrap32(loaded))
        if value.kind in ("pnativeint", "ppointer", "pint64", "pnativeuint"):
            loaded = self.reader.read(value.bits, 8)
            if value.kind == "pnativeuint":
                return Value("u64", loaded & ((1 << 64) - 1))
            return Value("i64", _sign64(loaded))
        if value.kind == "pbyte":
            loaded = self.reader.read(value.bits, 1)
            return Value("i32", loaded & 0xFF)
        raise TypeError("cannot dereference %s" % value.kind)


def extract_function(source, name):
    matches = list(
        re.finditer(r"\bfunction\s+%s\b" % re.escape(name), source, re.IGNORECASE)
    )
    if not matches:
        raise AssertionError("function %s not found" % name)
    # The interface declaration has no body. Use the implementation, whose
    # begin appears before the next routine header.
    marker = None
    for candidate in matches:
        window = source[candidate.end() : candidate.end() + 2500]
        begin_at = re.search(r"\bbegin\b", window, re.IGNORECASE)
        next_routine = re.search(
            r"\b(function|procedure|constructor|destructor)\b",
            window,
            re.IGNORECASE,
        )
        if begin_at and (next_routine is None or begin_at.start() < next_routine.start()):
            marker = candidate
            break
    if marker is None:
        raise AssertionError("function %s has no implementation body" % name)
    cursor = marker.start()
    seen_begin = 0
    depth = 0
    body_start = None
    i = cursor
    text = source
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        if ch == "{":
            i = text.find("}", i + 1)
            if i < 0:
                break
            i += 1
            continue
        if ch == "(" and i + 1 < n and text[i + 1] == "*":
            i = text.find("*)", i + 2)
            if i < 0:
                break
            i += 2
            continue
        if text[i : i + 5].lower() == "begin" and not (
            i + 5 < n and (text[i + 5].isalnum() or text[i + 5] == "_")
        ):
            if body_start is None:
                body_start = i
                depth = 1
            else:
                depth += 1
            i += 5
            continue
        if text[i : i + 3].lower() == "end" and not (
            i + 3 < n and (text[i + 3].isalnum() or text[i + 3] == "_")
        ):
            if body_start is not None:
                depth -= 1
                if depth == 0:
                    return text[body_start : i + 3]
            i += 3
            continue
        i += 1
    raise AssertionError("could not slice body of %s" % name)


def rtti_threshold(utils_source):
    body = extract_function(utils_source, "CheckClassHasRTTI")
    match = re.search(
        r"virtualMethods\s*>\s*(-?\d+)",
        body,
        re.IGNORECASE,
    )
    if match is None:
        raise AssertionError("CheckClassHasRTTI no longer compares virtualMethods")
    call = re.search(
        r"GetVirtualMethodCount\s*\(\s*GetTypeData\s*\(\s*info\s*\)\s*\.\s*ClassType\s*\)",
        body,
        re.IGNORECASE,
    )
    if call is None:
        raise AssertionError(
            "CheckClassHasRTTI does not pass GetTypeData(info).ClassType to GetVirtualMethodCount"
        )
    return int(match.group(1))


def native_count(class_pointer, name_pointer, vmt_parent):
    parent_slot = class_pointer + vmt_parent
    return (name_pointer - parent_slot - POINTER_SIZE) // POINTER_SIZE


def name_pointer_for_count(class_pointer, count, vmt_parent):
    parent_slot = class_pointer + vmt_parent
    return parent_slot + POINTER_SIZE + count * POINTER_SIZE


def truncated_read_address(class_pointer, vmt_class_name):
    low = _sign32(class_pointer & 0xFFFFFFFF)
    return (low + vmt_class_name) & ((1 << 64) - 1)


class ClassImage:
    def __init__(self, layout, count, decoy_count=None):
        self.layout = layout
        self.class_pointer = CLASS_POINTER
        self.count = count
        self.name_pointer = name_pointer_for_count(
            self.class_pointer, count, layout["vmtParent"]
        )
        self.slot = (self.class_pointer + layout["vmtClassName"]) & ((1 << 64) - 1)
        self.truncated = truncated_read_address(self.class_pointer, layout["vmtClassName"])
        self.pages = PageMap()
        high = self.slot & ~(PAGE - 1)
        self.pages.map(high, PAGE * 2)
        self.pages.write(self.slot, self.name_pointer.to_bytes(8, "little"))
        self.decoy = decoy_count is not None
        if self.decoy:
            low_page = self.truncated & ~(PAGE - 1)
            self.pages.map(low_page, PAGE * 2)
            loaded = self._decoy_dword(decoy_count)
            self.pages.write(self.truncated, loaded.to_bytes(4, "little", signed=True))
        self.reader = SlotReader()

    def close(self):
        self.reader.close()
        self.pages.close()

    def _decoy_dword(self, decoy_count):
        # 32-bit evaluation of the published formula at the truncated class.
        low = _sign32(self.class_pointer & 0xFFFFFFFF)
        parent = _wrap32(low + self.layout["vmtParent"])
        return _wrap32(decoy_count * POINTER_SIZE + parent + POINTER_SIZE)

    def run_count(self, body):
        evaluator = Evaluator(self.reader, self.layout)
        result = evaluator.eval_body(
            body,
            {"AClass": Value("ptr", self.class_pointer)},
        )
        return _sign32(result.bits), list(self.reader.reads)


class RejectedClass(Exception):
    pass


def validate_class(body, image, threshold):
    """The class-mock / class-stub / auto-mock validation decision.

    CheckMockObject and TStub<T>.Create both reject the class when
    CheckClassHasRTTI is false. CheckClassHasRTTI's result is only the
    comparison of GetVirtualMethodCount(GetTypeData(info).ClassType)
    against the threshold in Delphi.Mocks.Utils.pas. GetTypeData for a
    tkClass type returns the TTypeData whose ClassType field is the
    class reference (System.TypInfo, Alexandria).
    """
    count, reads = image.run_count(body)
    if count > threshold:
        return count, reads
    raise RejectedClass(count)


class TruncatedClassPointerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.utils = UTILS.read_text(encoding="latin1")
        cls.validation = VALIDATION.read_text(encoding="latin1")
        cls.mocks = MOCKS.read_text(encoding="latin1")
        cls.automock = AUTOMOCK.read_text(encoding="latin1")
        cls.body = extract_function(cls.utils, "GetVirtualMethodCount")
        cls.threshold = rtti_threshold(cls.utils)
        cls._assert_callers(cls)

    @staticmethod
    def _assert_callers(cls):
        # Each public class entry reaches the counter with no extra guard.
        if "TMocksValidation.CheckMockType(pInfo)" not in cls.mocks:
            raise AssertionError("TMock<T>.Create no longer calls CheckMockType")
        if not re.search(
            r"tkClass\s*:\s*CheckMockObject\s*\(\s*ATypeInfo\s*\)",
            cls.validation,
        ):
            raise AssertionError("CheckMockType no longer validates classes")
        if "CheckClassHasRTTI(ATypeInfo)" not in cls.validation:
            raise AssertionError("CheckMockObject no longer calls CheckClassHasRTTI")
        if "CheckClassHasRTTI(pInfo)" not in cls.mocks:
            raise AssertionError("TStub<T>.Create no longer calls CheckClassHasRTTI")
        if "TMocksValidation.CheckMockType(ATypeInfo)" not in cls.automock:
            raise AssertionError("TAutoMock.Mock no longer calls CheckMockType")
        if "TMock<T>.Create(FAutoMocker, nil)" not in cls.mocks:
            raise AssertionError("TAutoMockContainer.Mock<T> no longer calls TMock<T>.Create")
        if "FAutoMocker.Mock(ATypeInfo)" not in cls.mocks:
            raise AssertionError("TAutoMockContainer.Mock no longer calls TAutoMock.Mock")
        if re.search(
            r"tkInterface\s*:\s*CheckClassHasRTTI",
            cls.validation,
        ):
            raise AssertionError("interface mocks unexpectedly share the class counter")

    def tearDown(self):
        image = getattr(self, "_image", None)
        if image is not None:
            image.close()
            self._image = None

    def _open(self, layout, count, decoy_count=None):
        previous = getattr(self, "_image", None)
        if previous is not None:
            previous.close()
        self._image = ClassImage(layout, count, decoy_count)
        return self._image

    def _assert_real_slot(self, layout, count):
        image = self._open(layout, count)
        got, reads = image.run_count(self.body)
        self.assertEqual(
            got,
            count,
            "GetVirtualMethodCount returned %s for layout %s; real count is %s; reads %s; truncated address 0x%x"
            % (got, layout["name"], count, reads, image.truncated),
        )
        self.assertTrue(reads, "counter did not read the method table")
        for address, size, rc in reads:
            self.assertEqual(rc, 0)
            self.assertGreaterEqual(address, image.slot)
            self.assertLess(address + size, image.slot + POINTER_SIZE)
        widths = [size for _address, size, _rc in reads]
        self.assertIn(8, widths, "class-name slot was not read as a native pointer: %s" % (reads,))
        self.assertNotIn(
            image.truncated,
            [address for address, _size, _rc in reads],
            "read used the truncated class pointer",
        )

    def test_mock_create_uses_full_class_pointer(self):
        for layout in LAYOUTS:
            with self.subTest(entry="TMock.Create", layout=layout["name"]):
                self._assert_real_slot(layout, self.threshold + 1)

    def test_stub_create_uses_full_class_pointer(self):
        for layout in LAYOUTS:
            with self.subTest(entry="TStub.Create", layout=layout["name"]):
                self._assert_real_slot(layout, self.threshold + 1)

    def test_automock_uses_full_class_pointer(self):
        for layout in LAYOUTS:
            with self.subTest(entry="TAutoMock.Mock", layout=layout["name"]):
                self._assert_real_slot(layout, self.threshold + 1)

    def test_truncated_bytes_cannot_accept_a_rejected_class(self):
        # Real count equals the threshold, so CheckClassHasRTTI is false and
        # every class constructor must raise. A readable dword at the
        # truncated address encodes a count above the threshold. That dword
        # is visible only to the truncating load.
        for entry in ("TMock.Create", "TStub.Create", "TAutoMock.Mock"):
            for layout in LAYOUTS:
                with self.subTest(entry=entry, layout=layout["name"]):
                    image = self._open(
                        layout,
                        self.threshold,
                        decoy_count=self.threshold + 50,
                    )
                    with self.assertRaises(RejectedClass):
                        validate_class(self.body, image, self.threshold)
                    for address, _size, _rc in image.reader.reads:
                        self.assertNotEqual(address, image.truncated)

    def test_unmapped_truncated_address_is_not_touched(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout["name"]):
                image = self._open(layout, self.threshold)
                got, reads = image.run_count(self.body)
                self.assertEqual(got, self.threshold)
                self.assertTrue(all(rc == 0 for _a, _s, rc in reads))


if __name__ == "__main__":
    unittest.main(verbosity=2)
