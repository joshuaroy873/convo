"""Trash: conversations are moved here, with everything needed to put them
back, and only erased when the trash is emptied."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

from . import pb
from .common import DATA, Conv, short_path
from .sources import SOURCES

TRASH = DATA / "trash"

def _enc(v):
    return {"__b64__": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray)) else v


def _dec(v):
    return base64.b64decode(v["__b64__"]) if isinstance(v, dict) and "__b64__" in v else v


def _take_rows(spec: dict, value: str) -> dict | None:
    """Copy matching rows out of a table, then delete them."""
    values = spec.get("values") or [value]
    where = f'"{spec["col"]}" in ({", ".join("?" for _ in values)})'
    con = sqlite3.connect(spec["db"], timeout=5)
    try:
        cur = con.execute(f'select * from "{spec["table"]}" where {where}', values)
        cols = [c[0] for c in cur.description]
        rows = [[_enc(v) for v in r] for r in cur.fetchall()]
        if not rows:
            return None
        con.execute(f'delete from "{spec["table"]}" where {where}', values)
        con.commit()
        return {**spec, "cols": cols, "rows": rows}
    except sqlite3.Error:
        con.rollback()
        return None
    finally:
        con.close()


def _put_rows(saved: dict) -> None:
    con = sqlite3.connect(saved["db"], timeout=5)
    try:
        cols = ", ".join(f'"{c}"' for c in saved["cols"])
        marks = ", ".join("?" for _ in saved["cols"])
        # A running tool may have written these records again in the meantime; the saved ones replace them.
        at = saved["cols"].index(saved["col"])
        keys = sorted({r[at] for r in saved["rows"]})
        con.execute(f'delete from "{saved["table"]}" where "{saved["col"]}" in ({", ".join("?" for _ in keys)})', keys)
        con.executemany(f'insert into "{saved["table"]}" ({cols}) values ({marks})',
                        [[_dec(v) for v in r] for r in saved["rows"]])
        con.commit()
    finally:
        con.close()


def _take_lines(spec: dict) -> dict | None:
    """Pull the JSONL records whose key equals a value out of a file, noting
    where each one sat so it can go back in the same place."""
    path = Path(spec["file"])
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    keep, gone, at = [], [], []
    for i, line in enumerate(lines):
        try:
            hit = json.loads(line).get(spec["key"]) == spec["value"]
        except (ValueError, AttributeError):
            hit = False
        if hit:
            gone.append(line)
            at.append(i)
        else:
            keep.append(line)
    if not gone:
        return None
    _write_lines(path, keep)
    return {**spec, "removed": gone, "at": at}


def _put_lines(saved: dict) -> None:
    path = Path(saved["file"])
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    at = saved.get("at") or [len(lines) + i for i in range(len(saved["removed"]))]
    for i, line in zip(at, saved["removed"], strict=False):
        lines.insert(min(i, len(lines)), line)
    _write_lines(path, lines)


def _write_lines(path: Path, lines: list[str]) -> None:
    _replace(path, "".join(ln + "\n" for ln in lines).encode())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _take_blob(spec: dict) -> dict | None:
    """Take one conversation's entry out of a tool's side index: a key in a JSON
    file, or the entries for an id in a protobuf list."""
    path = Path(spec["file"])
    try:
        if spec["kind"] == "json":
            data = json.loads(path.read_text())
            node = data
            for key in spec["path"]:
                node = node[key]
            if spec["id"] not in node:
                return None
            saved = {**spec, "value": node.pop(spec["id"])}
            payload = json.dumps(data, indent=2, ensure_ascii=False).encode()
        else:
            parts = pb.chunks(path.read_bytes())
            if parts is None:
                return None
            hit = [i for i, c in enumerate(parts) if pb.text(c, c[0] >> 3, 1) == spec["id"]]
            if not hit:
                return None
            saved = {**spec, "at": hit, "chunks": [_b64(parts[i]) for i in hit]}
            payload = b"".join(c for i, c in enumerate(parts) if i not in hit)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
    _replace(path, payload)
    return saved


def _put_blob(saved: dict) -> None:
    path = Path(saved["file"])
    try:
        if saved["kind"] == "json":
            data = json.loads(path.read_text())
            node = data
            for key in saved["path"]:
                node = node.setdefault(key, {})
            node[saved["id"]] = saved["value"]
            payload = json.dumps(data, indent=2, ensure_ascii=False).encode()
        else:
            parts = pb.chunks(path.read_bytes()) or []
            parts = [c for c in parts if pb.text(c, c[0] >> 3, 1) != saved["id"]]  # the tool may have written it again
            for i, chunk in zip(saved["at"], saved["chunks"], strict=False):
                parts.insert(min(i, len(parts)), base64.b64decode(chunk))
            payload = b"".join(parts)
    except (OSError, ValueError, TypeError):
        return  # a side index the tool rebuilds; not worth failing a restore over
    _replace(path, payload)


def _replace(path: Path, payload: bytes) -> None:
    """Swap in new contents in one step, keeping the file's permissions: some
    of these files are private to the user."""
    tmp = path.with_name(path.name + ".convo-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    try:
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
    except OSError:
        pass
    tmp.replace(path)


def _write_manifest(entry: Path, m: dict) -> None:
    (entry / "manifest.json").write_text(json.dumps(m, indent=1))


def _log(action: str, m: dict) -> None:
    """Append-only record of what was trashed, restored and deleted."""
    rec = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "tool": m["tool"], "id": m["id"],
           "title": m["title"], "paths": [f["orig"] for f in m["files"]]}
    try:
        TRASH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRASH.parent / "log.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_log() -> list[dict]:
    try:
        return [json.loads(line) for line in (TRASH.parent / "log.jsonl").read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def preview(plan: dict, cid: str) -> list[str]:
    """What a plan would move, in words, without changing anything."""
    out = [short_path(p) + ("/" if Path(p).is_dir() else "") for p in plan["files"]]
    for spec in plan["rows"]:
        values = spec.get("values") or [cid]
        try:
            con = sqlite3.connect(f"file:{spec['db']}?mode=ro", uri=True, timeout=3)
            marks = ", ".join("?" for _ in values)
            n = con.execute(f'select count(*) from "{spec["table"]}" where "{spec["col"]}" in ({marks})', values).fetchone()[0]
            con.close()
        except sqlite3.Error:
            n = 0
        if n:
            out.append(f"{n} record{'s' if n != 1 else ''} in {Path(spec['db']).name} ({spec['table']})")
    for spec in plan.get("lines", []):
        try:
            n = sum(1 for line in Path(spec["file"]).read_text().splitlines()
                    if f'"{spec["key"]}"' in line and spec["value"] in line)
        except OSError:
            n = 0
        if n:
            out.append(f"{n} row{'s' if n != 1 else ''} in {short_path(spec['file'])}")
    for spec in plan.get("blobs", []):
        try:
            if spec["id"].encode() in Path(spec["file"]).read_bytes():
                out.append(f"its entry in {short_path(spec['file'])}")
        except OSError:
            pass
    return out


def put(conv: Conv) -> dict:
    """Move a conversation to the trash."""
    mod = SOURCES[conv.tool]
    if mod.is_live(conv):  # asked afresh: the listing may be minutes old
        raise RuntimeError("this conversation is open in a running session, or was written to in the last 90 seconds")
    return stash(conv.tool, conv.id, conv.title, conv.cwd, mod.trash_plan(conv))


def stash(tool: str, cid: str, title: str, cwd: str, plan: dict) -> dict:
    """Carry out a plan: move its files into a new trash entry and take its
    records out of the tool's indexes. Everything needed to put it back is
    recorded in the entry's manifest."""
    entry = TRASH / f"{time.strftime('%Y%m%d-%H%M%S')}-{tool}-{cid[:8]}"
    n = 1
    while entry.exists():
        n += 1
        entry = entry.with_name(f"{entry.name}-{n}")
    (entry / "files").mkdir(parents=True)
    m = {"tool": tool, "id": cid, "title": title, "cwd": cwd, "when": time.time(),
         "files": [], "rows": [], "lines": [], "blobs": []}
    try:
        for i, src in enumerate(plan["files"]):
            dst = entry / "files" / str(i)
            shutil.move(src, dst)
            m["files"].append({"orig": src, "stored": str(dst)})
            _write_manifest(entry, m)
        for spec in plan["rows"]:
            saved = _take_rows(spec, cid)
            if saved:
                m["rows"].append(saved)
                _write_manifest(entry, m)
        for spec in plan.get("lines", []):
            saved = _take_lines(spec)
            if saved:
                m["lines"].append(saved)
                _write_manifest(entry, m)
        for spec in plan.get("blobs", []):
            saved = _take_blob(spec)
            if saved:
                m["blobs"].append(saved)
                _write_manifest(entry, m)
    except Exception:
        _restore(entry, m)
        raise
    _write_manifest(entry, m)
    _log("trash", m)
    return m


def entries() -> list[tuple[Path, dict]]:
    out = []
    for d in sorted(TRASH.glob("*")):
        try:
            out.append((d, json.loads((d / "manifest.json").read_text())))
        except (OSError, ValueError):
            continue
    # oldest first by the moment of trashing: folder names only resolve to the second
    return sorted(out, key=lambda e: (e[1].get("when", 0), e[0].name))


def _restore(entry: Path, m: dict) -> None:
    clash = [f["orig"] for f in m["files"] if Path(f["orig"]).exists()]
    if clash:
        raise RuntimeError(f"cannot restore, already exists: {clash[0]}")
    for f in m["files"]:
        Path(f["orig"]).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(f["stored"], f["orig"])
    for saved in m["rows"]:
        _put_rows(saved)
    for saved in m.get("lines", []):
        _put_lines(saved)
    for saved in m.get("blobs", []):
        _put_blob(saved)
    shutil.rmtree(entry, ignore_errors=True)


def restore(entry: Path) -> dict:
    m = json.loads((entry / "manifest.json").read_text())
    _restore(entry, m)
    _log("restore", m)
    return m


def purge(entry: Path) -> dict:
    """Permanently delete one trashed conversation."""
    m = json.loads((entry / "manifest.json").read_text())
    shutil.rmtree(entry)
    _log("delete", m)
    return m
