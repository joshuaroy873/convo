"""Codex threads: ~/.codex/sessions/Y/M/D/rollout-*.jsonl (+ archived_sessions/).

A thread can span several rollout files that share one session id; they are
read in order as a single conversation. Names live in session_index.jsonl and
the `threads` table of state_*.sqlite.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..common import Conv, Msg, cached_scan, env_path, now_iso, one_line, parse_iso, recently_written

ROOT = env_path("CODEX_HOME", "~/.codex")


def _ctext(item: dict) -> str:
    return "".join(c.get("text") or "" for c in item.get("content") or [] if isinstance(c, dict))


def _ri_text(p: dict) -> str:
    return "".join(c.get("text") or "" for c in p.get("content") or [] if isinstance(c, dict))


def _scan(path: Path) -> dict:
    d = dict(id="", cwd="", originator="", created=None, updated=None, nmsg=0, first="", last_user="", first_user="",
             branch="", helper=False)
    ri_n, ri_first, ri_last = 0, "", None
    with open(path, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t, p = o.get("type"), o.get("payload") or {}
            ts = parse_iso(o.get("timestamp"))
            if t == "session_meta":
                d["id"] = p.get("id") or p.get("session_id") or ""
                d["cwd"] = p.get("cwd") or ""
                d["originator"] = p.get("originator") or ""
                git = p.get("git")
                d["branch"] = (git.get("branch") or "") if isinstance(git, dict) else ""
                # threads a person did not start: spawned subagents and non-interactive `codex exec` runs
                source = p.get("source")
                d["helper"] = (p.get("thread_source") or "user") != "user" or source == "exec" \
                    or (isinstance(source, dict) and "subagent" in source) or "exec" in d["originator"]
                d["created"] = parse_iso(p.get("timestamp")) or ts
            elif t == "event_msg" and p.get("type") == "item_completed":
                it = p.get("item") or {}
                d["updated"] = ts or d["updated"]  # real activity, not later bookkeeping records
                if it.get("type") == "UserMessage":
                    txt = _ctext(it)
                    d["nmsg"] += 1
                    d["first_user"] = d["first_user"] or txt
                    d["last_user"] = txt
                    d["first"] = d["first"] or one_line(txt, 300)
                elif it.get("type") == "AgentMessage":
                    d["nmsg"] += 1
            elif t == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                txt = _ri_text(p)
                if p["role"] == "assistant" or not txt.lstrip().startswith("<"):
                    ri_n += 1
                    ri_last = ts or ri_last
                    if p["role"] == "user" and not ri_first:
                        ri_first = one_line(txt, 300)
    if not d["nmsg"]:  # older rollouts without item_completed events
        d["nmsg"], d["first"] = ri_n, ri_first
    d["updated"] = d["updated"] or ri_last
    return d


def _state_db() -> Path | None:
    dbs = sorted(ROOT.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime)
    return dbs[-1] if dbs else None


def _threads() -> dict[str, dict]:
    db = _state_db()
    if not db:
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        rows = {r["id"]: dict(r) for r in con.execute("select * from threads")}
        con.close()
        return rows
    except (sqlite3.Error, IndexError, KeyError):  # missing or unfamiliar table: fall back to the files alone
        return {}


def _index_names() -> dict[str, str]:
    names = {}
    try:
        for line in (ROOT / "session_index.jsonl").read_text().splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("id") and o.get("thread_name"):
                names[o["id"]] = o["thread_name"]
    except OSError:
        pass
    return names


hidden = 0  # helper threads left out of the last listing, for `convo doctor`


def is_live(conv: Conv) -> bool:
    return recently_written(conv.paths)


def list_convs() -> list[Conv]:
    global hidden
    hidden = 0
    files = sorted(ROOT.glob("sessions/**/rollout-*.jsonl")) + sorted(ROOT.glob("archived_sessions/**/rollout-*.jsonl"))
    groups: dict[str, list[tuple[Path, dict]]] = {}
    for path in files:
        d = cached_scan(path, _scan)
        groups.setdefault(d["id"] or path.stem, []).append((path, d))
    threads, names = _threads(), _index_names()
    out = []
    for tid, parts in groups.items():
        parts.sort(key=lambda x: x[0].name)
        d0 = parts[0][1]
        if d0.get("helper"):
            hidden += 1
            continue
        row = threads.get(tid) or {}
        first = next((d["first"] for _, d in parts if d["first"]), "")
        nmsg = sum(d["nmsg"] for _, d in parts)
        # a follow-on segment opens by repeating the previous segment's last prompt
        nmsg -= sum(1 for (_, a), (_, b) in zip(parts, parts[1:], strict=False) if a["last_user"] and a["last_user"] == b["first_user"])
        title = names.get(tid) or row.get("name") or row.get("title") or one_line(first, 80) or "(no prompt)"
        out.append(Conv(
            tool="codex", id=tid, title=one_line(title, 120), cwd=d0["cwd"],
            origin="vscode" if "vscode" in d0["originator"] else "cli",
            created=min(d["created"] or p.stat().st_mtime for p, d in parts),
            updated=max(d["updated"] or p.stat().st_mtime for p, d in parts),
            size=sum(p.stat().st_size for p, _ in parts), nmsg=nmsg,
            paths=[str(p) for p, _ in parts], first=first, live=recently_written([p for p, _ in parts]),
            branch=d0.get("branch") or row.get("git_branch") or "",
            archived=bool(row.get("archived")) or any("archived_sessions" in p.parts for p, _ in parts),
            extra={"indexed": bool(row)},
        ))
    return out


def _item_msgs(it: dict, ts) -> list[Msg]:
    k = it.get("type")
    if k == "UserMessage":
        return [Msg("user", _ctext(it), ts)]
    if k == "AgentMessage":
        return [Msg("assistant", _ctext(it), ts)]
    if k == "Reasoning":
        txt = "\n".join(s for s in it.get("summary_text") or [] if isinstance(s, str))
        return [Msg("assistant", txt, ts, "thinking")] if txt.strip() else []
    if k == "CommandExecution":
        cmd = it.get("command")
        cmd = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd or "")
        out = [Msg("assistant", cmd, ts, "call", "exec")]
        res = it.get("aggregated_output") or it.get("output") or ""
        if res:
            out.append(Msg("tool", res if isinstance(res, str) else json.dumps(res), ts, "result"))
        return out
    if k == "FileChange":
        return [Msg("assistant", ", ".join((it.get("changes") or {}).keys()), ts, "call", "edit")]
    if k in ("WebSearch", "Extension"):
        return [Msg("assistant", str(it.get("query") or ""), ts, "call", it.get("kind") or "web.search")]
    if k == "McpToolCall":
        out = [Msg("assistant", json.dumps(it.get("arguments"), ensure_ascii=False), ts, "call", f"{it.get('server')}.{it.get('tool')}")]
        if it.get("result"):
            out.append(Msg("tool", json.dumps(it["result"], ensure_ascii=False), ts, "result"))
        return out
    if k == "ImageView":
        return [Msg("assistant", str(it.get("path") or ""), ts, "call", "view-image")]
    if k == "ContextCompaction":
        return [Msg("system", "context compacted", ts, "note")]
    return []


def messages(conv: Conv) -> list[Msg]:
    out: list[Msg] = []
    last_user = ""
    for path in conv.paths:
        seg: list[Msg] = []
        fallback: list[Msg] = []
        with open(path, "rb") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t, p = o.get("type"), o.get("payload") or {}
                ts = parse_iso(o.get("timestamp"))
                if t == "event_msg" and p.get("type") == "item_completed":
                    seg += _item_msgs(p.get("item") or {}, ts)
                elif t == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    txt = _ri_text(p)
                    if txt and (p["role"] == "assistant" or not txt.lstrip().startswith("<")):
                        fallback.append(Msg(p["role"], txt, ts))
        if not any(m.role == "user" for m in seg):
            seg = fallback
        users = [m for m in seg if m.role == "user" and m.kind == "text"]
        if users and last_user and users[0].text == last_user:
            seg.remove(users[0])
        if users:
            last_user = users[-1].text
        out += seg
    return out


def _check_layout() -> None:
    """Refuse to change Codex data when its storage does not look the way this
    module expects, rather than risk a half-applied edit."""
    db = _state_db()
    if not db:
        raise RuntimeError("Codex's state database was not found, so its records cannot be updated safely")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    try:
        cols = {r[1] for r in con.execute("pragma table_info(threads)")}
    finally:
        con.close()
    if "id" not in cols or not cols & {"name", "title"}:
        raise RuntimeError(f"Codex's thread table in {db.name} has an unfamiliar layout; this Codex version is not supported for changes")


def rename(conv: Conv, title: str) -> None:
    """Record the new name in both places Codex keeps it."""
    _check_layout()
    rec = json.dumps({"id": conv.id, "thread_name": title, "updated_at": now_iso()}, ensure_ascii=False, separators=(",", ":"))
    idx = ROOT / "session_index.jsonl"
    lead = ""
    if idx.exists() and idx.stat().st_size:
        with open(idx, "rb") as f:
            f.seek(-1, 2)
            lead = "" if f.read(1) == b"\n" else "\n"
    with open(idx, "a") as f:
        f.write(lead + rec + "\n")
    db = _state_db()
    if db:
        con = sqlite3.connect(db, timeout=5)
        try:
            cols = {r[1] for r in con.execute("pragma table_info(threads)")}
            row = con.execute("select * from threads where id=?", (conv.id,)).fetchone()
            if row is not None:
                if "name" in cols:
                    con.execute("update threads set title = case when title is name then ? else title end, name = ? where id = ?",
                                (title, title, conv.id))
                elif "title" in cols:
                    con.execute("update threads set title = ? where id = ?", (title, conv.id))
                con.commit()
        finally:
            con.close()


def _thread_tables(db: Path) -> list[str]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        names = [n for (n,) in con.execute("select name from sqlite_master where type='table'")]
        out = [n for n in names if any(r[1] == "thread_id" for r in con.execute(f'pragma table_info("{n}")'))]
        con.close()
        return out
    except sqlite3.Error:
        return []


def trash_plan(conv: Conv) -> dict:
    """Everything `codex delete` would remove: the rollout files, the thread's
    row, its history projection (follow-on segments have ids of their own) and
    its name in the index."""
    _check_layout()
    ids = [conv.id] + [m.group(1) for p in conv.paths if (m := re.search(r"_([0-9a-f-]{36})\.jsonl$", p))]
    rows = []
    state = _state_db()
    if state:
        rows.append({"db": str(state), "table": "threads", "col": "id", "values": [conv.id]})
    for db in ([state] if state else []) + sorted(ROOT.glob("thread_history_*.sqlite")):
        rows += [{"db": str(db), "table": t, "col": "thread_id", "values": ids} for t in _thread_tables(db)]
    return {"files": list(conv.paths), "rows": rows,
            "lines": [{"file": str(ROOT / "session_index.jsonl"), "key": "id", "value": conv.id},
                      {"file": str(ROOT / "history.jsonl"), "key": "session_id", "value": conv.id}]}


def resume_cmd(conv: Conv) -> list[str]:
    return ["codex", "resume", conv.id]
