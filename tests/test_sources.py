"""Listing, reading and renaming for each tool."""

import json
import sqlite3

import pytest

from conftest import AGY_ID, CLAUDE_ID, CLAUDE_LIVE_ID, CODEX_ID, fld, get
from convo import pb, store
from convo.sources import agy, codex


def texts(conv, role, kind="text"):
    return [m.text for m in store.messages(conv) if m.role == role and m.kind == kind]


def test_lists_every_tool(stores):
    convs, errors = store.load_all()
    assert not errors
    assert sorted((c.tool, c.id) for c in convs) == [
        ("agy", AGY_ID), ("claude", CLAUDE_ID), ("claude", CLAUDE_LIVE_ID), ("codex", CODEX_ID)]
    assert all(c.cwd == str(stores["cwd"]) for c in convs)


def test_one_broken_store_does_not_hide_the_others(stores, monkeypatch):
    monkeypatch.setattr(codex, "list_convs", lambda: 1 / 0)
    convs, errors = store.load_all()
    assert {c.tool for c in convs} == {"claude", "agy"}
    assert len(errors) == 1 and errors[0].startswith("codex:")


def test_claude_summary_and_messages(stores):
    c = get("claude", CLAUDE_ID)
    assert (c.title, c.origin, c.nmsg, c.live) == ("Reversing a list", "cli", 2, False)
    assert c.first == "How do I reverse a list?"  # slash command and IDE tags are not the prompt
    assert texts(c, "user") == ["How do I reverse a list?"]
    assert texts(c, "assistant") == ["Use `reversed()` or slicing."]  # sidechain chatter excluded
    assert texts(c, "assistant", "call") == ["python -c 'print([1,2][::-1])'"]
    assert texts(c, "tool", "result") == ["[2, 1] zebrafish"]
    assert texts(c, "user", "note") == ["/model"]


def test_claude_marks_running_session(stores):
    c = get("claude", CLAUDE_LIVE_ID)
    assert c.live and c.origin == "vscode"


def test_claude_rename_writes_the_record_claude_looks_for(stores):
    c = get("claude", CLAUDE_ID)
    store.rename(c, 'New "name" \\ é')
    last = open(c.paths[0], encoding="utf-8").read().splitlines()[-1]
    # Claude finds titles by substring, so the compact form matters
    assert '"type":"custom-title"' in last and '"customTitle":"' in last
    assert json.loads(last) == {"type": "custom-title", "customTitle": 'New "name" \\ é', "sessionId": CLAUDE_ID}
    assert get("claude", CLAUDE_ID).title == 'New "name" \\ é'


def test_codex_joins_segments_into_one_conversation(stores):
    c = get("codex")
    assert (c.title, c.origin, len(c.paths), c.archived) == ("Port MATLAB script", "vscode", 2, False)
    assert texts(c, "user") == ["Port the script", "Now add tests"]  # the replayed prompt appears once
    assert texts(c, "assistant") == ["Ported it.", "Added three tests."]
    assert c.nmsg == 4
    assert texts(c, "assistant", "call") == ["ls scripts"] and texts(c, "tool", "result") == ["main.m"]


def test_codex_updated_ignores_bookkeeping_records(stores):
    assert get("codex").updated == pytest.approx(1769677209 + 345600, abs=86400 * 2)  # early Feb, not the March record


def test_codex_rename_updates_index_and_database(stores):
    c = get("codex")
    store.rename(c, "PRB port")
    assert get("codex").title == "PRB port"
    last = json.loads((stores["codex"] / "session_index.jsonl").read_text().splitlines()[-1])
    assert (last["id"], last["thread_name"]) == (CODEX_ID, "PRB port")
    con = sqlite3.connect(stores["codex"] / "state_5.sqlite")
    assert con.execute("select name, title from threads where id=?", (CODEX_ID,)).fetchone() == ("PRB port", "PRB port")
    assert con.execute("select name from threads where id='someone-else'").fetchone() == ("Unrelated",)
    con.close()


def test_codex_refuses_changes_on_unfamiliar_layout(stores):
    con = sqlite3.connect(stores["codex"] / "state_5.sqlite")
    con.execute("alter table threads rename column id to thread_key")
    con.commit()
    con.close()
    c = get("codex")  # still readable
    with pytest.raises(RuntimeError, match="unfamiliar layout"):
        store.rename(c, "x")
    with pytest.raises(RuntimeError, match="unfamiliar layout"):
        codex.trash_plan(c)


def test_agy_summary_and_messages(stores):
    c = get("agy")
    assert (c.title, c.origin, c.nmsg, c.first) == ("Tunnel down", "cli", 3, "Why is the tunnel down?")
    assert texts(c, "user") == ["Why is the tunnel down?"]
    assert texts(c, "assistant") == ["Let me check the daemon.", "The daemon had stopped; restart it."]
    assert texts(c, "assistant", "call") == ["tailscale status"]
    assert [m.name for m in store.messages(c) if m.kind == "call"] == ["run_command"]
    assert texts(c, "tool", "result") == ["daemon stopped: narwhal"]
    assert texts(c, "assistant", "thinking") == ["check the daemon"]


def test_agy_rename_updates_every_copy_of_the_title(stores):
    c = get("agy")
    title = 'Fix "tunnel" \\d'
    store.rename(c, title)
    base = stores["agy"] / "antigravity-cli"
    note = (base / "annotations" / f"{AGY_ID}.pbtxt").read_text()
    assert note == 'title:"Fix \\"tunnel\\" \\\\d" last_user_view_time:{seconds:1 nanos:2}'
    con = sqlite3.connect(base / "conversation_summaries.db")
    assert con.execute("select title from conversation_summaries where conversation_id=?", (AGY_ID,)).fetchone() == (title,)
    con.close()
    meta = json.loads((base / "cache" / "conversation_metadata.json").read_text())
    assert meta["conversations"][AGY_ID]["summary"]["Title"] == title
    assert get("agy").title == title


def test_agy_refuses_changes_on_unfamiliar_layout(stores):
    con = sqlite3.connect(stores["agy"] / "antigravity-cli" / "conversation_summaries.db")
    con.execute("alter table conversation_summaries rename column title to heading")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="unfamiliar layout"):
        agy.trash_plan(get("agy"))


def test_pb_reads_nested_and_repeated_fields():
    blob = fld(1, 300) + fld(5, fld(2, "a") + fld(2, "b") + fld(3, fld(1, "deep"))) + fld(9, "top")
    assert pb.first(blob, 1) == 300
    assert pb.all_at(blob, 5, 2) == [b"a", b"b"]
    assert pb.text(blob, 5, 3, 1) == "deep"
    assert pb.text(blob, 9) == "top" and pb.text(blob, 7) == ""
    assert pb.fields(b"\x0a\xff") == []  # truncated data stops quietly


def test_export_markdown_and_json(stores):
    c = get("claude", CLAUDE_ID)
    md = store.export(c).read_text()
    assert md.startswith("# Reversing a list") and "How do I reverse a list?" in md and "zebrafish" not in md
    assert "zebrafish" in store.export(c, tools=True).read_text()
    data = json.loads(store.export(c, "json").read_text())
    assert data["id"] == CLAUDE_ID and [m["kind"] for m in data["messages"]].count("result") == 1


def test_resolve_by_prefix(stores):
    convs, _ = store.load_all()
    assert store.resolve(convs, "2222").tool == "codex"
    assert store.resolve(convs, f"agy:{AGY_ID[:8]}").tool == "agy"
    with pytest.raises(SystemExit, match="ambiguous"):
        store.resolve(convs, "1111")
    with pytest.raises(SystemExit, match="no conversation"):
        store.resolve(convs, "zzzz")
