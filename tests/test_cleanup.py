"""Orphans, the trash preview and log, stats, and the agy fallback transcript."""

import os
import shutil
import sqlite3
import time

from conftest import AGY_ID, CLAUDE_ID, CLAUDE_LIVE_ID, CODEX_ID, fingerprint, get
from convo import cli, orphans, stats, store, trash
from convo.sources import SOURCES
from convo.tui import Confirm, Orphans
from test_app import drive, settle

STRAY = "99999999-aaaa-4bbb-8ccc-000000000009"
GONE = "88888888-aaaa-4bbb-8ccc-000000000008"


def texts(conv, role, kind="text"):
    return [m.text for m in store.messages(conv) if m.role == role and m.kind == kind]


def backdate(*paths):
    old = time.time() - 7200
    for p in paths:
        os.utime(p, (old, old))


def add_strays(stores):
    """State left behind for conversations that no longer exist."""
    leftover = stores["claude"] / "session-env" / STRAY
    leftover.mkdir()
    (leftover / "env").write_text("x" * 100)
    note = stores["agy"] / "antigravity-cli" / "annotations" / f"{GONE}.pbtxt"
    note.write_text('title:"Deleted long ago"')
    backdate(leftover, note)


# --- agy fallback transcript ---

def test_agy_falls_back_to_its_log_when_the_database_is_unreadable(stores):
    db = stores["agy"] / "antigravity-cli" / "conversations" / f"{AGY_ID}.db"
    db.write_bytes(b"this is not a database at all, " * 40)
    backdate(db)
    c = get("agy")
    assert (c.nmsg, c.first) == (3, "Why is the tunnel down?")
    assert texts(c, "user") == ["Why is the tunnel down?"]  # request text only, not the metadata around it
    assert texts(c, "assistant") == ["Let me check the daemon.", "The daemon had stopped; restart it."]
    assert texts(c, "assistant", "call") == ["tailscale status"]
    assert texts(c, "tool", "result") == ["daemon stopped: narwhal"]


def test_agy_falls_back_when_the_database_has_no_steps(stores):
    db = stores["agy"] / "antigravity-cli" / "conversations" / f"{AGY_ID}.db"
    con = sqlite3.connect(db)
    con.execute("delete from steps")
    con.commit()
    con.close()
    backdate(db)
    assert len(texts(get("agy"), "assistant")) == 2


def test_agy_prefers_the_database(stores):
    assert texts(get("agy"), "assistant", "thinking") == ["check the daemon"]  # only the database has thinking


# --- orphans ---

def test_nothing_is_an_orphan_in_a_tidy_store(stores):
    found = orphans.find(store.load_all()[0])
    # two index entries whose transcripts are not here: one in the summaries table, one in the proto list
    assert [(o.tool, o.id) for o in found] == [("agy", "aaaaaaaa-aaaa-4bbb-8ccc-00000000000a"), ("agy", "other")]
    assert all(o.reason.startswith("listed by Antigravity") for o in found)


def test_finds_leftovers_and_conversations_without_a_project(stores):
    add_strays(stores)
    shutil.rmtree(stores["cwd"])
    found = {(o.tool, o.id): o for o in orphans.find(store.load_all()[0])}
    assert found[("claude", STRAY)].reason.startswith("leftover files") and found[("claude", STRAY)].size == 100
    assert found[("agy", GONE)].label == "Deleted long ago"
    for key in (("claude", CLAUDE_ID), ("codex", CODEX_ID), ("agy", AGY_ID)):
        assert found[key].reason == "project folder is gone"
    assert ("claude", CLAUDE_LIVE_ID) not in found  # a running session is never offered


def test_empty_conversation_is_an_orphan(stores):
    db = stores["agy"] / "antigravity-cli" / "conversations" / f"{AGY_ID}.db"
    con = sqlite3.connect(db)
    con.execute("delete from steps")
    con.commit()
    con.close()
    shutil.rmtree(stores["agy"] / "antigravity-cli" / "brain" / AGY_ID / ".system_generated")
    backdate(db)
    found = {o.id: o for o in orphans.find(store.load_all()[0])}
    assert found[AGY_ID].reason == "no messages"


def test_fresh_leftovers_are_not_offered(stores):
    (stores["claude"] / "session-env" / STRAY).mkdir()  # just created: a session may be starting up
    assert STRAY not in [o.id for o in orphans.find(store.load_all()[0])]


def test_removing_orphans_is_reversible(stores):
    add_strays(stores)
    before = {t: fingerprint(stores[t]) for t in ("claude", "agy")}
    for o in orphans.find(store.load_all()[0]):
        orphans.remove(o)
    assert not (stores["claude"] / "session-env" / STRAY).exists()
    con = sqlite3.connect(stores["agy"] / "antigravity-cli" / "conversation_summaries.db")
    assert con.execute("select count(*) from conversation_summaries where conversation_id='other'").fetchone() == (0,)
    con.close()
    assert len(store.load_all()[0]) == 4  # no real conversation was touched
    assert orphans.find(store.load_all()[0]) == []
    for entry, _ in reversed(trash.entries()):
        trash.restore(entry)
    assert {t: fingerprint(stores[t]) for t in ("claude", "agy")} == before


# --- preview and log ---

def test_preview_names_what_a_trash_moves(stores):
    before = fingerprint(stores["claude"])
    c = get("claude", CLAUDE_ID)
    lines = trash.preview(SOURCES["claude"].trash_plan(c), c.id)
    assert any(line.endswith(f"{CLAUDE_ID}.jsonl") for line in lines)
    assert any(line.endswith(f"file-history/{CLAUDE_ID}/") for line in lines)
    assert any(line.startswith("2 rows in ") and line.endswith("history.jsonl") for line in lines)
    c = get("codex")
    lines = trash.preview(SOURCES["codex"].trash_plan(c), c.id)
    assert "1 record in state_5.sqlite (threads)" in lines and "2 records in thread_history_1.sqlite (thread_items)" in lines
    assert fingerprint(stores["claude"]) == before  # and it changed nothing
    assert trash.entries() == []


def test_log_records_trash_restore_and_delete(stores):
    assert trash.read_log() == []
    trash.put(get("agy"))
    trash.restore(trash.entries()[0][0])
    trash.put(get("agy"))
    trash.purge(trash.entries()[0][0])
    log = trash.read_log()
    assert [(r["action"], r["id"]) for r in log] == [("trash", AGY_ID), ("restore", AGY_ID), ("trash", AGY_ID), ("delete", AGY_ID)]
    assert log[0]["title"] == "Tunnel down" and any(p.endswith(f"{AGY_ID}.db") for p in log[0]["paths"])


# --- stats ---

def test_stats_report(stores):
    text = stats.report(store.load_all()[0], trashed=2)
    assert "conversations   4" in text and "in the trash    2" in text
    assert "claude  cli          1         2" in text and "codex   vscode       1         4" in text
    assert str(stores["cwd"]) in text or "work/demo" in text
    assert "Mon" in text and "21–24h" in text
    assert stats.report([]) == "No conversations."


# --- commands ---

def test_commands(stores, capsys, monkeypatch):
    add_strays(stores)
    cli.main(["stats", "tool:codex"])
    assert "conversations   1" in capsys.readouterr().out
    cli.main(["rm", "3333", "--dry-run"])
    out = capsys.readouterr().out
    assert "would move to trash" in out and f"{AGY_ID}.db" in out and trash.entries() == []
    cli.main(["orphans"])
    out = capsys.readouterr().out
    assert STRAY[:8] in out and "Deleted long ago" in out and trash.entries() == []
    cli.main(["orphans", "leftover", "--trash", "--yes"])  # only the items matching a word
    assert len(trash.entries()) == 2
    cli.main(["orphans", "--trash", "--yes"])
    assert len(trash.entries()) == 4
    capsys.readouterr()
    cli.main(["orphans"])
    assert "nothing to clean up" in capsys.readouterr().out
    cli.main(["trash", "log"])
    out = capsys.readouterr().out
    assert out.count("trash ") == 4 and "leftover: " in out


# --- interface ---

def test_trash_confirmation_lists_paths(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"tool:agy", "enter", "d")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        assert "This moves:" in app.screen.prompt and f"{AGY_ID}.db" in app.screen.prompt
        assert "conversation_summaries" in app.screen.prompt
        await pilot.press("n")
        await pilot.pause()
        assert trash.entries() == []
    drive(script)


def test_cleanup_screen(stores):
    add_strays(stores)

    async def script(app, pilot):
        await pilot.press("c")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, Orphans) and len(screen.items) == 4
        await pilot.press("space")
        assert len(screen.marks) == 1
        await pilot.press("a")
        assert len(screen.marks) == 4
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert screen.items == [] and len(trash.entries()) == 4
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and len(app.all) == 4  # real conversations untouched
        await pilot.press("u")
        await pilot.pause()
        assert len(trash.entries()) == 3
    drive(script)


def test_restore_survives_the_tool_relisting_a_trashed_conversation(stores):
    db = stores["agy"] / "antigravity-cli" / "conversation_summaries.db"
    before = fingerprint(stores["agy"])
    trash.put(get("agy"))
    con = sqlite3.connect(db)  # a running agy writes its index entry back
    con.execute("insert into conversation_summaries values (?, 'Tunnel down', '[]', '2026-01-02 10:00:15+00:00', null)", (AGY_ID,))
    con.commit()
    con.close()
    again = [o for o in orphans.find(store.load_all()[0]) if o.id == AGY_ID]
    assert again and again[0].reason.startswith("already in the trash") and again[0].label == "Tunnel down"
    trash.restore(trash.entries()[0][0])
    assert fingerprint(stores["agy"]) == before and trash.entries() == []


def test_agy_trash_clears_the_indexes_agy_rebuilds_from(stores):
    from convo import pb
    base = stores["agy"] / "antigravity-cli"
    proto, meta = base / "jetbox_summaries_proto.pb", base / "cache" / "conversation_metadata.json"
    original = proto.read_bytes()
    c = get("agy")
    lines = trash.preview(SOURCES["agy"].trash_plan(c), c.id)
    assert sum(line.startswith("its entry in ") for line in lines) == 2
    trash.put(c)
    left = [pb.text(chunk, chunk[0] >> 3, 1) for chunk in pb.chunks(proto.read_bytes())]
    assert left == ["aaaaaaaa-aaaa-4bbb-8ccc-00000000000a"] * 2  # both kinds of entry for the id are gone
    assert AGY_ID not in meta.read_text() and "other-session" in meta.read_text()
    assert AGY_ID not in [o.id for o in orphans.find(store.load_all()[0])]  # nothing left to re-list it
    trash.restore(trash.entries()[0][0])
    assert proto.read_bytes() == original and AGY_ID in meta.read_text()


def test_pb_chunks_round_trip():
    from conftest import fld
    from convo import pb
    blob = fld(1, "a") + fld(2, 300) + fld(3, fld(1, "nested"))
    assert b"".join(pb.chunks(blob)) == blob and len(pb.chunks(blob)) == 3
    assert pb.chunks(blob[:-2]) is None  # damaged data is refused, never rewritten
