"""Trash must remove a conversation completely, restore it exactly, and only
erase on purge."""

import sqlite3

import pytest

from conftest import AGY_ID, CLAUDE_ID, CLAUDE_LIVE_ID, CODEX_ID, CODEX_SEG, fingerprint, get
from convo import store, trash


def listed(tool):
    return [c.id for c in store.load_all()[0] if c.tool == tool]


def leftovers(root, *ids):
    return [str(p) for p in root.rglob("*") if any(i in p.name for i in ids)]


@pytest.mark.parametrize("tool,cid", [("claude", CLAUDE_ID), ("codex", CODEX_ID), ("agy", AGY_ID)])
def test_trash_then_restore_is_exact(stores, tool, cid):
    before = fingerprint(stores[tool])
    trash.put(get(tool, cid))
    assert cid not in listed(tool)
    assert leftovers(stores[tool], cid, CODEX_SEG) == []
    assert len(trash.entries()) == 1
    entry, manifest = trash.entries()[0]
    assert (manifest["tool"], manifest["id"]) == (tool, cid)
    trash.restore(entry)
    assert trash.entries() == []
    assert fingerprint(stores[tool]) == before
    assert cid in listed(tool)


def test_claude_trash_takes_side_files(stores):
    trash.put(get("claude", CLAUDE_ID))
    root = stores["claude"]
    assert not (root / "file-history" / CLAUDE_ID).exists() and not (root / "session-env" / CLAUDE_ID).exists()
    assert listed("claude") == [CLAUDE_LIVE_ID]  # the other session is untouched


def test_running_session_cannot_be_trashed(stores):
    with pytest.raises(RuntimeError, match="running session"):
        trash.put(get("claude", CLAUDE_LIVE_ID))
    assert trash.entries() == []


def test_codex_trash_removes_exactly_its_records(stores):
    trash.put(get("codex"))
    root = stores["codex"]
    con = sqlite3.connect(root / "state_5.sqlite")
    assert con.execute("select id from threads").fetchall() == [("someone-else",)]
    assert con.execute("select count(*) from thread_read_receipts").fetchone() == (0,)
    con.close()
    con = sqlite3.connect(root / "thread_history_1.sqlite")
    assert con.execute("select thread_id from thread_items").fetchall() == [("someone-else",)]  # segment rows go too
    con.close()
    index = (root / "session_index.jsonl").read_text()
    assert CODEX_ID not in index and "someone-else" in index


def test_agy_trash_removes_summary_and_search_rows(stores):
    trash.put(get("agy"))
    con = sqlite3.connect(stores["agy"] / "antigravity-cli" / "conversation_summaries.db")
    assert con.execute("select conversation_id from conversation_summaries").fetchall() == [("other",)]
    assert con.execute("select conversation_id from conversations_fts").fetchall() == [("other",)]
    con.close()


def test_agy_restore_brings_back_search_rows_and_blobs(stores):
    trash.put(get("agy"))
    trash.restore(trash.entries()[0][0])
    con = sqlite3.connect(stores["agy"] / "antigravity-cli" / "conversation_summaries.db")
    assert con.execute("select body from conversations_fts where conversations_fts match 'narwhal'").fetchall() == [("tunnel narwhal",)]
    assert con.execute("select raw_summary from conversation_summaries where conversation_id=?", (AGY_ID,)).fetchone() == (b"\x00\x01",)
    con.close()


def test_purge_erases_for_good(stores):
    for tool, cid in (("claude", CLAUDE_ID), ("codex", CODEX_ID), ("agy", AGY_ID)):
        trash.put(get(tool, cid))
    for entry, _ in trash.entries():
        trash.purge(entry)
    assert trash.entries() == [] and not any(trash.TRASH.iterdir())
    assert [c.id for c in store.load_all()[0]] == [CLAUDE_LIVE_ID]
    for tool in ("claude", "codex", "agy"):
        assert leftovers(stores[tool], CLAUDE_ID, CODEX_ID, CODEX_SEG, AGY_ID) == []


def test_restore_refuses_to_overwrite(stores):
    c = get("claude", CLAUDE_ID)
    trash.put(c)
    open(c.paths[0], "w").write("something new\n")
    with pytest.raises(RuntimeError, match="already exists"):
        trash.restore(trash.entries()[0][0])
    assert len(trash.entries()) == 1  # still safely in the trash


def test_failed_trash_rolls_back(stores, monkeypatch):
    before = fingerprint(stores["agy"])
    monkeypatch.setattr(trash, "_take_rows", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        trash.put(get("agy"))
    assert fingerprint(stores["agy"]) == before and trash.entries() == []


def test_entries_are_in_the_order_they_were_trashed(stores):
    # all within one second, and named so that alphabetical order would be wrong
    trash.stash("claude", "zzz", "first", "", {"files": [], "rows": []})
    trash.stash("claude", "aaa", "second", "", {"files": [], "rows": []})
    trash.stash("agy", "mmm", "third", "", {"files": [], "rows": []})
    assert [m["title"] for _, m in trash.entries()] == ["first", "second", "third"]
