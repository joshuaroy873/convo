"""Antigravity (agy) conversations: ~/.gemini/antigravity{,-cli}/conversations/<id>.db

Each conversation is a SQLite file whose `steps` rows hold protobuf blobs. There
is no published schema; the field numbers below were worked out from the data.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote

from .. import pb
from ..common import Conv, Msg, cached_scan, env_path, one_line, parse_iso, recently_written

ROOT = env_path("CONVO_AGY_HOME", "~/.gemini")
DIRS = [("antigravity", "ide"), ("antigravity-cli", "cli")]

USER, ASSISTANT, TOOL, SYSTEM_MSG, ERROR, CHECKPOINT = 14, 15, 132, 101, 17, 23
_TITLE = re.compile(r'title:"((?:[^"\\]|\\.)*)"')
_REQUEST = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)


def _step_ts(payload: bytes) -> float | None:
    v = pb.first(payload, 5, 1, 1)
    return float(v) if isinstance(v, int) else None


def _scan(db: Path) -> dict:
    d = dict(nmsg=0, first="", created=None, updated=None)
    try:
        con = _ro(db)
        for stype, payload in con.execute("select step_type, step_payload from steps order by idx"):
            payload = payload or b""
            ts = _step_ts(payload)
            if ts:
                d["created"] = d["created"] or ts
                d["updated"] = ts
            if stype == USER:
                d["nmsg"] += 1
                d["first"] = d["first"] or one_line(pb.text(payload, 19, 2), 300)
            elif stype == ASSISTANT and pb.first(payload, 20, 1):
                d["nmsg"] += 1
        con.close()
    except sqlite3.Error:
        pass
    if not d["nmsg"]:  # unreadable or empty database: use agy's own plain-text log if it kept one
        log = _log_file(db)
        said = [m for m in (_log_msgs(log) if log else []) if m.kind == "text" and m.role in ("user", "assistant")]
        if said:
            d["nmsg"] = len(said)
            d["first"] = one_line(next((m.text for m in said if m.role == "user"), ""), 300)
            stamps = [m.ts for m in said if m.ts]
            d["created"], d["updated"] = (min(stamps), max(stamps)) if stamps else (None, None)
    return d


def _log_file(db: Path) -> Path | None:
    logs = db.parent.parent / "brain" / db.stem / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        if (logs / name).is_file():
            return logs / name
    return None


def _log_calls(raw) -> list[tuple[str, str]]:
    """Tool calls are logged as a Python-style list whose argument values are themselves JSON strings."""
    try:
        calls = ast.literal_eval(raw) if isinstance(raw, str) else raw
    except (ValueError, SyntaxError):
        return [("tool", str(raw))]
    out = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        plain = {}
        for k, v in args.items():
            try:
                plain[k] = json.loads(v) if isinstance(v, str) else v
            except ValueError:
                plain[k] = v
        out.append((str(call.get("name") or "tool"), _pretty_args(json.dumps(plain, ensure_ascii=False))))
    return out


def _log_msgs(path: Path) -> list[Msg]:
    out = []
    with open(path, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            kind, ts, body = o.get("type"), parse_iso(o.get("created_at")), o.get("content") or ""
            if kind == "USER_INPUT":
                m = _REQUEST.search(body)
                out.append(Msg("user", (m.group(1) if m else body).strip(), ts))
            elif kind == "PLANNER_RESPONSE":
                if body.strip():
                    out.append(Msg("assistant", body, ts))
                out += [Msg("assistant", args, ts, "call", name) for name, args in _log_calls(o.get("tool_calls") or [])]
            elif kind == "GENERIC":
                out.append(Msg("tool", body, ts, "result"))
            elif kind == "SYSTEM_MESSAGE":
                out.append(Msg("system", body, ts, "note"))
    return [m for m in out if m.text]


def _summaries() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sub, _ in DIRS:
        db = ROOT / sub / "conversation_summaries.db"
        if not db.exists():
            continue
        try:
            con = _ro(db)
            for cid, title, uris, mod in con.execute(
                "select conversation_id, title, workspace_uris, last_modified_time from conversation_summaries"
            ):
                s = out.setdefault(cid, {"title": "", "cwd": "", "updated": None})
                s["title"] = s["title"] or title or ""
                try:
                    first_uri = (json.loads(uris) or [""])[0] if uris else ""
                except ValueError:
                    first_uri = ""
                if first_uri.startswith("file://"):
                    s["cwd"] = s["cwd"] or unquote(first_uri[7:])
                ts = parse_iso(str(mod).replace(" ", "T", 1)) if mod else None
                if ts and ts > 0:
                    s["updated"] = max(s["updated"] or 0, ts)
            con.close()
        except sqlite3.Error:
            pass
    return out


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _annot_title(cid: str) -> str:
    for sub, _ in DIRS:
        try:
            m = _TITLE.search((ROOT / sub / "annotations" / f"{cid}.pbtxt").read_text())
        except OSError:
            continue
        if m and m.group(1):
            return _unescape(m.group(1))
    return ""


def is_live(conv: Conv) -> bool:
    return recently_written(conv.paths)


def list_convs() -> list[Conv]:
    summ = _summaries()
    out = []
    for sub, origin in DIRS:
        for db in sorted((ROOT / sub / "conversations").glob("*.db")):
            cid = db.stem
            d = cached_scan(db, _scan, also=(f"{db}-wal",))  # new messages sit in the log until agy flushes
            st = db.stat()
            s = summ.get(cid, {})
            title = _annot_title(cid) or s.get("title") or one_line(d["first"], 80) or "(empty conversation)"
            out.append(Conv(
                tool="agy", id=cid, title=title, cwd=s.get("cwd") or "", origin=origin,
                created=d["created"] or st.st_mtime, updated=d["updated"] or st.st_mtime,
                size=st.st_size, nmsg=d["nmsg"], paths=[str(db)], first=d["first"], extra={"dir": sub},
                live=recently_written([db]),
            ))
    return out


def _pretty_args(raw: str) -> str:
    try:
        o = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(o, dict):
        for k in ("CommandLine", "AbsolutePath", "TargetFile", "Query", "Url", "SearchPath"):
            if isinstance(o.get(k), str):
                return o[k]
    return raw


def messages(conv: Conv) -> list[Msg]:
    db = Path(conv.paths[0])
    try:
        out = _db_msgs(db)
    except sqlite3.Error:
        out = []
    if not out:
        log = _log_file(db)
        out = _log_msgs(log) if log else []
    return out


def _db_msgs(db: Path) -> list[Msg]:
    out = []
    con = _ro(db)
    for stype, payload in con.execute("select step_type, step_payload from steps order by idx"):
        payload = payload or b""
        ts = _step_ts(payload)
        if stype == USER:
            out.append(Msg("user", pb.text(payload, 19, 2), ts))
        elif stype == ASSISTANT:
            think = pb.text(payload, 20, 3)
            if think.strip():
                out.append(Msg("assistant", think, ts, "thinking"))
            body = pb.text(payload, 20, 1)
            if body.strip():
                out.append(Msg("assistant", body, ts))
            for call in pb.all_at(payload, 20, 7):
                out.append(Msg("assistant", _pretty_args(pb.text(call, 3)), ts, "call", pb.text(call, 2) or "tool"))
        elif stype == TOOL:
            res = pb.text(payload, 140, 2, 1)
            if res:
                out.append(Msg("tool", res, ts, "result"))
        elif stype == SYSTEM_MSG:
            out.append(Msg("system", pb.text(payload, 114, 1), ts, "note"))
        elif stype == ERROR:
            out.append(Msg("system", pb.text(payload, 24, 3, 2) or "(interrupted)", ts, "note"))
        elif stype == CHECKPOINT:
            out.append(Msg("system", pb.text(payload, 30, 5), ts, "note"))
    con.close()
    return [m for m in out if m.text]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _check_layout(conv: Conv) -> None:
    """Refuse to change agy data when its storage does not look the way this
    module expects, rather than risk a half-applied edit."""
    def cols(db: Path, table: str) -> set[str]:
        con = _ro(db)
        try:
            return {r[1] for r in con.execute(f'pragma table_info("{table}")')}
        finally:
            con.close()

    if not {"idx", "step_type", "step_payload"} <= cols(Path(conv.paths[0]), "steps"):
        raise RuntimeError("this Antigravity conversation has an unfamiliar layout; this agy version is not supported for changes")
    for sub, _ in DIRS:
        db = ROOT / sub / "conversation_summaries.db"
        if db.exists() and not {"conversation_id", "title"} <= cols(db, "conversation_summaries"):
            raise RuntimeError(f"{sub}/conversation_summaries.db has an unfamiliar layout; this agy version is not supported for changes")


def rename(conv: Conv, title: str) -> None:
    """Best effort: agy keeps the title in an annotation file, a summaries table
    and a JSON cache, so all copies that exist are updated."""
    _check_layout(conv)
    field = f'title:"{_escape(title)}"'
    own = ROOT / conv.extra.get("dir", DIRS[0][0]) / "annotations" / f"{conv.id}.pbtxt"
    targets = {own} | {ROOT / sub / "annotations" / f"{conv.id}.pbtxt" for sub, _ in DIRS}
    for path in targets:
        if path.exists():
            old = path.read_text()
            # a lambda keeps backslashes in the title from being read as regex escapes
            new = _TITLE.sub(lambda _m: field, old, count=1) if _TITLE.search(old) else (field + (" " + old if old.strip() else ""))
            path.write_text(new)
        elif path == own:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(field)
    for sub, _ in DIRS:
        db = ROOT / sub / "conversation_summaries.db"
        if db.exists():
            con = sqlite3.connect(db, timeout=5)
            try:
                con.execute("update conversation_summaries set title=? where conversation_id=?", (title, conv.id))
                con.commit()
            finally:
                con.close()
        meta = ROOT / sub / "cache" / "conversation_metadata.json"
        try:
            data = json.loads(meta.read_text())
            entry = data["conversations"][conv.id]["summary"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        entry["Title"] = title
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(meta)


def _side_indexes(cid: str) -> list[dict]:
    """agy rebuilds its conversation list from these on start, so an entry left
    here brings a removed conversation back."""
    out = []
    for sub, _ in DIRS:
        base = ROOT / sub
        out += [{"kind": "pb", "file": str(p), "id": cid} for p in sorted(base.glob("*_summaries_proto.pb"))]
        meta = base / "cache" / "conversation_metadata.json"
        if meta.exists():
            out.append({"kind": "json", "file": str(meta), "path": ["conversations"], "id": cid})
    return out


def trash_plan(conv: Conv) -> dict:
    _check_layout(conv)
    files, rows = [], []
    for sub, _ in DIRS:
        base = ROOT / sub
        cands = [base / "conversations" / f"{conv.id}{ext}" for ext in (".db", ".db-wal", ".db-shm", ".pb")]
        cands += [base / "brain" / conv.id, base / "annotations" / f"{conv.id}.pbtxt"]
        files += [str(p) for p in cands if p.exists()]
        db = base / "conversation_summaries.db"
        if db.exists():
            rows.append({"db": str(db), "table": "conversation_summaries", "col": "conversation_id"})
            rows.append({"db": str(db), "table": "conversations_fts", "col": "conversation_id"})
    return {"files": files, "rows": rows, "blobs": _side_indexes(conv.id)}


def strays() -> list[dict]:
    """Things agy still holds for conversations whose transcript is gone: an
    index entry, a title note or a working folder."""
    have, seen, titles = set(), {}, {}
    for sub, _ in DIRS:
        base = ROOT / sub
        have |= {p.stem for p in (base / "conversations").glob("*") if p.suffix in (".db", ".pb")}
        for p in (base / "annotations").glob("*.pbtxt"):
            seen.setdefault(p.stem, []).append(p)
        for p in (base / "brain").glob("*"):
            if p.is_dir() and _UUID.match(p.name):
                seen.setdefault(p.name, []).append(p)
    for cid, s in _summaries().items():
        seen.setdefault(cid, [])
        titles[cid] = s["title"]
    listed_ids = set(titles)
    for sub, _ in DIRS:
        for p in (ROOT / sub).glob("*_summaries_proto.pb"):
            try:
                for chunk in pb.chunks(p.read_bytes()) or []:
                    cid = pb.text(chunk, chunk[0] >> 3, 1)
                    if chunk[0] >> 3 == 1 and _UUID.match(cid):
                        seen.setdefault(cid, [])
                        listed_ids.add(cid)
            except OSError:
                pass
    out = []
    for cid, files in sorted(seen.items()):
        if cid in have or recently_written(files, 3600):
            continue
        rows = []
        for sub, _ in DIRS:
            db = ROOT / sub / "conversation_summaries.db"
            if db.exists():
                rows += [{"db": str(db), "table": t, "col": "conversation_id", "values": [cid]}
                         for t in ("conversation_summaries", "conversations_fts")]
        listed = cid in listed_ids
        out.append({"tool": "agy", "id": cid, "label": titles.get(cid) or _annot_title(cid) or cid,
                    "reason": "listed by Antigravity, but its transcript is not on this machine" if listed
                    else "leftover files of a conversation that no longer exists",
                    "plan": {"files": [str(p) for p in files], "rows": rows if listed else [], "lines": [],
                             "blobs": _side_indexes(cid) if listed else []}})
    return out


def resume_cmd(conv: Conv) -> list[str]:
    return ["agy", "--conversation", conv.id]
