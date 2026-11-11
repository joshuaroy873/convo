"""Builds small, entirely made-up Claude, Codex and agy stores in a temp folder
and points convo at them. Nothing here touches a real home directory."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from convo import common, config, store, trash
from convo.sources import agy, claude, codex

CLAUDE_ID = "11111111-aaaa-4bbb-8ccc-000000000001"
CLAUDE_LIVE_ID = "11111111-aaaa-4bbb-8ccc-000000000002"
CODEX_ID = "22222222-aaaa-7bbb-8ccc-000000000001"
CODEX_SEG = "22222222-aaaa-7bbb-8ccc-0000000000ff"
CODEX_HELPER = "22222222-aaaa-7bbb-8ccc-00000000aaaa"
AGY_ID = "33333333-aaaa-4bbb-8ccc-000000000001"


def jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))


# --- protobuf writer, the mirror of convo.pb ---

def varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fld(no: int, value) -> bytes:
    if isinstance(value, int):
        return varint(no << 3) + varint(value)
    data = value.encode() if isinstance(value, str) else value
    return varint(no << 3 | 2) + varint(len(data)) + data


def step(stype: int, ts: int, body: bytes, meta_extra: bytes = b"") -> bytes:
    return fld(1, stype) + fld(4, 3) + fld(5, fld(1, fld(1, ts)) + meta_extra) + body


# --- store builders ---

def make_claude(root: Path, cwd: Path) -> None:
    proj = root / "projects" / claude._encode(str(cwd))
    base = {"cwd": str(cwd), "entrypoint": "cli", "sessionId": CLAUDE_ID, "gitBranch": "feature/lists"}
    jsonl(proj / f"{CLAUDE_ID}.jsonl", [
        {"type": "user", "timestamp": "2026-01-02T10:00:00Z", **base,
         "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        {"type": "user", "timestamp": "2026-01-02T10:00:05Z", **base,
         "message": {"role": "user", "content": "<ide_opened_file>x.py</ide_opened_file>How do I reverse a list?"}},
        {"type": "assistant", "timestamp": "2026-01-02T10:00:09Z", **base,
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "simple question"},
             {"type": "text", "text": "Use `reversed()` or slicing."},
             {"type": "tool_use", "name": "Bash", "input": {"command": "python -c 'print([1,2][::-1])'"}}]}},
        {"type": "user", "timestamp": "2026-01-02T10:00:12Z", **base,
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "[2, 1] zebrafish"}]}},
        {"type": "assistant", "timestamp": "2026-01-02T10:00:15Z", **base, "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "ai-title", "aiTitle": "Reversing a list"},
    ])
    with open(proj / f"{CLAUDE_ID}.jsonl", "a") as f:
        f.write('{"type":"user","timestamp":"2026-01-02T10:01:00Z","mess')  # a line Claude is still writing
    jsonl(proj / CLAUDE_ID / "subagents" / "agent-1.jsonl", [{"type": "user"}])
    for sub in ("file-history", "session-env", "tasks"):
        (root / sub / CLAUDE_ID).mkdir(parents=True)
        (root / sub / CLAUDE_ID / "state").write_text(sub)
    (root / "security").mkdir()
    (root / "security" / f"security_warnings_state_{CLAUDE_ID}.json").write_text("{}")
    (root / "security" / "security_warnings_state_someone-else.json").write_text("{}")
    scratch = root / "_scratch" / proj.name
    (scratch / CLAUDE_ID).mkdir(parents=True)
    (scratch / CLAUDE_ID / "notes.txt").write_text("scratch work")
    (scratch / "someone-else").mkdir()
    jsonl(root / "history.jsonl", [
        {"display": "first prompt", "sessionId": "someone-else"},
        {"display": "How do I reverse a list?", "sessionId": CLAUDE_ID},
        {"display": "middle prompt", "sessionId": "someone-else"},
        {"display": "/model", "sessionId": CLAUDE_ID},
        {"display": "last prompt", "sessionId": "someone-else"}])
    # a second session that a running process (this test process) has open
    jsonl(proj / f"{CLAUDE_LIVE_ID}.jsonl", [
        {"type": "user", "timestamp": "2026-01-03T10:00:00Z", **base, "entrypoint": "claude-vscode",
         "message": {"role": "user", "content": "still working on this"}}])
    jsonl(proj / "memory" / "MEMORY.md", [])
    (proj / "memory" / "MEMORY.md").write_text("- [List style](list-style.md) — prefers slicing\n")
    (proj / "memory" / "list-style.md").write_text(
        "---\nname: list-style\ndescription: \"Prefers slicing over reversed()\"\nmetadata:\n  type: feedback\n"
        "  originSessionId: 00000000-dead-4bbb-8ccc-000000000000\n---\n\nUse `xs[::-1]`.\n")
    settings = root / ".claude.json"
    settings.write_text(json.dumps({"numStartups": 3, "projects": {
        str(cwd): {"hasTrustDialogAccepted": True, "allowedTools": ["Bash(ls:*)"]}, "/somewhere/else": {"allowedTools": []}}}, indent=2))
    settings.chmod(0o600)
    (root / "history.jsonl").chmod(0o600)
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "1.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": CLAUDE_LIVE_ID}))


def make_codex(root: Path, cwd: Path) -> None:
    def item(ts, kind, **kw):
        return {"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed", "item": {"type": kind, **kw}}}

    def text(s):
        return [{"type": "text", "text": s}]

    meta = {"type": "session_meta", "timestamp": "2026-02-01T09:00:00Z",
            "payload": {"id": CODEX_ID, "cwd": str(cwd), "originator": "codex_vscode", "timestamp": "2026-02-01T09:00:00Z"}}
    first = root / "sessions/2026/02/01" / f"rollout-2026-02-01T09-00-00-{CODEX_ID}.jsonl"
    second = root / "sessions/2026/02/02" / f"rollout-2026-02-02T09-00-00-{CODEX_ID}_{CODEX_SEG}.jsonl"
    jsonl(first, [
        meta,
        item("2026-02-01T09:00:01Z", "UserMessage", content=text("Port the script")),
        item("2026-02-01T09:00:05Z", "CommandExecution", command=["/bin/sh", "-c", "ls scripts"], aggregated_output="main.m"),
        item("2026-02-01T09:00:09Z", "AgentMessage", content=text("Ported it.")),
        item("2026-02-01T09:05:00Z", "UserMessage", content=text("Now add tests")),
    ])
    jsonl(second, [
        meta,
        item("2026-02-02T09:00:01Z", "UserMessage", content=text("Now add tests")),  # replayed at the start of a segment
        item("2026-02-02T09:00:09Z", "AgentMessage", content=text("Added three tests.")),
        {"timestamp": "2026-03-01T00:00:00Z", "type": "event_msg", "payload": {"type": "thread_settings_applied"}},
    ])
    helper = {"type": "session_meta", "timestamp": "2026-02-01T09:00:02Z",
              "payload": {"id": CODEX_HELPER, "cwd": str(cwd), "originator": "codex_vscode", "thread_source": "subagent",
                          "source": {"subagent": "review"}, "timestamp": "2026-02-01T09:00:02Z"}}
    jsonl(root / "sessions/2026/02/01" / f"rollout-2026-02-01T09-00-02-{CODEX_HELPER}.jsonl",
          [helper, item("2026-02-01T09:00:03Z", "UserMessage", content=text("review this diff"))])
    jsonl(root / "history.jsonl", [{"session_id": CODEX_ID, "ts": "1", "text": "Port the script"},
                                   {"session_id": "someone-else", "ts": "2", "text": "other"}])
    jsonl(root / "session_index.jsonl", [
        {"id": CODEX_ID, "thread_name": "Port MATLAB script", "updated_at": "2026-02-02T09:00:09Z"},
        {"id": "someone-else", "thread_name": "Unrelated", "updated_at": "2026-02-02T09:00:09Z"}])
    con = sqlite3.connect(root / "state_5.sqlite")
    con.execute("create table threads (id text primary key, rollout_path text, title text, name text, archived integer, git_branch text)")
    con.execute("insert into threads values (?,?,?,?,0,'pyPort')", (CODEX_ID, str(second), "Port MATLAB script", "Port MATLAB script"))
    con.execute("insert into threads values ('someone-else','x','Unrelated','Unrelated',0,null)")
    con.execute("create table thread_read_receipts (thread_id text, at integer)")
    con.execute("insert into thread_read_receipts values (?, 1)", (CODEX_ID,))
    con.commit()
    con.close()
    con = sqlite3.connect(root / "thread_history_1.sqlite")
    con.execute("create table thread_items (thread_id text, item_id text, item_json text)")
    con.executemany("insert into thread_items values (?,?,?)",
                    [(CODEX_ID, "a", "{}"), (CODEX_SEG, "b", "{}"), ("someone-else", "c", "{}")])
    con.commit()
    con.close()


def make_agy(root: Path, cwd: Path) -> None:
    base = root / "antigravity-cli"
    (base / "conversations").mkdir(parents=True)
    con = sqlite3.connect(base / "conversations" / f"{AGY_ID}.db")
    con.execute("create table steps (idx integer primary key, step_type integer, step_payload blob)")
    call = fld(1, "call_1") + fld(2, "run_command") + fld(3, json.dumps({"CommandLine": "tailscale status"}))
    steps = [
        step(agy.USER, 1767348000, fld(19, fld(2, "Why is the tunnel down?"))),
        step(agy.ASSISTANT, 1767348005, fld(20, fld(3, "check the daemon") + fld(1, "Let me check the daemon.") + fld(7, call))),
        step(agy.TOOL, 1767348009, fld(140, fld(2, fld(1, "daemon stopped: narwhal")))),
        step(agy.ASSISTANT, 1767348015, fld(20, fld(1, "The daemon had stopped; restart it."))),
    ]
    con.executemany("insert into steps values (?,?,?)", [(i, s[1], s) for i, s in enumerate(steps)])
    con.commit()
    con.close()
    (base / "annotations").mkdir()
    (base / "annotations" / f"{AGY_ID}.pbtxt").write_text('title:"Tunnel down" last_user_view_time:{seconds:1 nanos:2}')
    (base / "brain" / AGY_ID).mkdir(parents=True)
    (base / "brain" / AGY_ID / "note.md").write_text("scratch")
    jsonl(base / "brain" / AGY_ID / ".system_generated" / "logs" / "transcript.jsonl", [
        {"step_index": "0", "type": "USER_INPUT", "created_at": "2026-01-02T10:00:00Z",
         "content": "<USER_REQUEST>\nWhy is the tunnel down?\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
        {"step_index": "1", "type": "PLANNER_RESPONSE", "created_at": "2026-01-02T10:00:05Z", "content": "Let me check the daemon.",
         "tool_calls": "[{'name': 'run_command', 'args': {'CommandLine': '\"tailscale status\"', 'WaitMsBeforeAsync': '500'}}]"},
        {"step_index": "2", "type": "GENERIC", "created_at": "2026-01-02T10:00:09Z", "content": "daemon stopped: narwhal"},
        {"step_index": "3", "type": "PLANNER_RESPONSE", "created_at": "2026-01-02T10:00:15Z",
         "content": "The daemon had stopped; restart it."}])
    con = sqlite3.connect(base / "conversation_summaries.db")
    con.execute("create table conversation_summaries (conversation_id text primary key, title text, "
                "workspace_uris text, last_modified_time text, raw_summary blob)")
    con.execute("insert into conversation_summaries values (?,?,?,?,?)",
                (AGY_ID, "Tunnel down", json.dumps([cwd.as_uri()]), "2026-01-02 10:00:15+00:00", b"\x00\x01"))
    con.execute("insert into conversation_summaries values ('other','Other','[]','2026-01-01 00:00:00+00:00',null)")
    con.execute("create virtual table conversations_fts using fts5(conversation_id unindexed, body)")
    con.execute("insert into conversations_fts values (?, 'tunnel narwhal')", (AGY_ID,))
    con.execute("insert into conversations_fts values ('other', 'something else')")
    con.commit()
    con.close()
    (base / "cache").mkdir()
    (base / "cache" / "conversation_metadata.json").write_text(json.dumps(
        {"conversations": {AGY_ID: {"summary": {"ID": AGY_ID, "Title": "Tunnel down"}}, "other-session": {}}}, indent=2))
    # the list agy rebuilds its index from on start: summaries (field 1) and per-id marks (field 2)
    (base / "jetbox_summaries_proto.pb").write_bytes(
        fld(1, fld(1, "aaaaaaaa-aaaa-4bbb-8ccc-00000000000a") + fld(2, fld(1, "Kept")))
        + fld(1, fld(1, AGY_ID) + fld(2, fld(1, "Tunnel down") + fld(2, 4)))
        + fld(2, fld(1, AGY_ID) + fld(2, 1767348000))
        + fld(2, fld(1, "aaaaaaaa-aaaa-4bbb-8ccc-00000000000a") + fld(2, 1767348001)))


def fingerprint(root: Path) -> dict:
    """Every file's path and content hash, plus every SQLite table's rows, so a
    restore can be checked against the state before trashing."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            out[rel] = "dir"
        elif p.suffix in (".sqlite", ".db"):
            con = sqlite3.connect(p)
            for (t,) in con.execute("select name from sqlite_master where type='table' and name not like '%\\_fts\\_%' escape '\\'"):
                out[f"{rel}:{t}"] = sorted(map(repr, con.execute(f'select * from "{t}"').fetchall()))
            con.close()
        elif not p.name.endswith(("-wal", "-shm")):
            data = p.read_bytes()
            # restored index lines are appended, so compare JSONL as a set of lines
            if p.suffix == ".jsonl":
                out[rel] = sorted(data.splitlines())
            elif p.suffix == ".json":  # key order is not significant
                try:
                    out[rel] = json.dumps(json.loads(data), sort_keys=True)
                except ValueError:
                    out[rel] = hashlib.md5(data).hexdigest()
            else:
                out[rel] = hashlib.md5(data).hexdigest()
    return out


@pytest.fixture
def stores(tmp_path, monkeypatch):
    cwd = tmp_path / "work" / "demo"
    cwd.mkdir(parents=True)
    roots = {"claude": tmp_path / "claude", "codex": tmp_path / "codex", "agy": tmp_path / "gemini"}
    make_claude(roots["claude"], cwd)
    make_codex(roots["codex"], cwd)
    make_agy(roots["agy"], cwd)
    old = time.time() - 3600  # anything written in the last 90 seconds counts as in use
    for p in tmp_path.rglob("*"):
        os.utime(p, (old, old))
    monkeypatch.setattr(claude, "SCRATCH", roots["claude"] / "_scratch")
    monkeypatch.setattr(claude, "ROOT", roots["claude"])
    monkeypatch.setattr(codex, "ROOT", roots["codex"])
    monkeypatch.setattr(agy, "ROOT", roots["agy"])
    monkeypatch.setattr(trash, "TRASH", tmp_path / "data" / "trash")
    monkeypatch.setattr(store, "EXPORTS", tmp_path / "exports")
    monkeypatch.setattr(common, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(config, "PATH", tmp_path / "config" / "config.toml")
    monkeypatch.delenv("CONVO_CD_FILE", raising=False)
    monkeypatch.delenv("CONVO_EXPORT_DIR", raising=False)
    monkeypatch.setattr(common, "_cache", None)
    return {"cwd": cwd, **roots, "tmp": tmp_path}


def get(tool: str, cid: str | None = None):
    convs, errors = store.load_all()
    assert not errors
    return next(c for c in convs if c.tool == tool and (cid is None or c.id == cid))
