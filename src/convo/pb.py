"""Minimal schema-less protobuf reader, enough to pull known fields out of
Antigravity's step blobs by field-number path."""

from __future__ import annotations


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def fields(b: bytes) -> list[tuple[int, int, object]]:
    """One level of (field_number, wire_type, value). Stops quietly on bad data."""
    out = []
    i, n = 0, len(b)
    try:
        while i < n:
            key, i = _varint(b, i)
            f, w = key >> 3, key & 7
            if w == 0:
                v, i = _varint(b, i)
            elif w == 1:
                v = b[i:i + 8]
                i += 8
            elif w == 5:
                v = b[i:i + 4]
                i += 4
            elif w == 2:
                ln, i = _varint(b, i)
                v = b[i:i + ln]
                i += ln
            else:
                break
            out.append((f, w, v))
    except IndexError:
        pass
    return out


def chunks(b: bytes) -> list[bytes] | None:
    """The message cut into the exact bytes of each top-level field, so some can
    be dropped and the rest written back untouched. None if it does not parse cleanly."""
    out = []
    i, n = 0, len(b)
    try:
        while i < n:
            start = i
            key, i = _varint(b, i)
            w = key & 7
            if w == 0:
                _, i = _varint(b, i)
            elif w == 1:
                i += 8
            elif w == 5:
                i += 4
            elif w == 2:
                ln, i = _varint(b, i)
                i += ln
            else:
                return None
            if i > n:
                return None
            out.append(b[start:i])
    except IndexError:
        return None
    return out


def all_at(b: bytes, *path: int) -> list:
    """Every value found by descending through the given field numbers."""
    cur = [b]
    for p in path:
        nxt = []
        for c in cur:
            if isinstance(c, (bytes, bytearray)):
                nxt += [v for f, _, v in fields(c) if f == p]
        cur = nxt
    return cur


def first(b: bytes, *path: int):
    r = all_at(b, *path)
    return r[0] if r else None


def text(b: bytes, *path: int) -> str:
    v = first(b, *path)
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else ""
