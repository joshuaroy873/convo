"""Filter terms, safety checks and the smaller conveniences."""

import json
import os
import time

import pytest

from conftest import AGY_ID, CLAUDE_ID, CLAUDE_LIVE_ID, CODEX_ID, fingerprint, get
from convo import cli, common, store, trash
from convo.query import parse
from convo.sources import claude, codex

NOW = 1_800_000_000.0


def conv(**kw):
    base = dict(tool="claude", id="abc", title="Fix auth bug", cwd="/work/backend", origin="cli",
                created=NOW - 10 * 86400, updated=NOW - 3600, size=1, nmsg=1, paths=[], branch="main")
    return common.Conv(**{**base, **kw})


@pytest.mark.parametrize("text,expected", [
    ("", True),
    ("auth", True), ("auth nothere", False), ("main", True),
    ("tool:claude", True), ("tool:cl", True), ("agent:claude,codex", True), ("tool:codex", False),
    ("-tool:claude", False), ("tool:claude,!codex", True), ("tool:!claude", False),
    ("via:cli", True), ("via:vscode", False), ("-via:vscode", True),
    ("dir:backend", True), ("dir:BACK", True), ("dir:frontend", False), ("dir:work,!backend", False),
    ("date:today", False), ("date:<2d", True), ("date:<30m", False), ("date:>1w", False), ("date:>30m", True),
    ("date:week", True), ("date:month auth tool:claude", True),
    ("tool:nonsense", False),  # not a known tool, so it is just a word that does not appear
    ("date:whenever", False),
])
def test_filter_terms(text, expected):
    midday = NOW - NOW % 86400 + 12 * 3600  # keep "today" away from midnight whatever the timezone
    c = conv(updated=midday - 4000 if "today" not in text else midday - 86400 * 2)
    assert parse(text, now=midday).match(c) is expected


def test_date_today_and_yesterday():
    noon = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))
    assert parse("date:today", now=noon).match(conv(updated=noon - 3600))
    assert not parse("date:today", now=noon).match(conv(updated=noon - 86400))
    assert parse("date:yesterday", now=noon).match(conv(updated=noon - 86400))
    assert not parse("date:yesterday", now=noon).match(conv(updated=noon - 3600))


def test_unfinished_last_line_is_ignored(stores):
    c = get("claude", CLAUDE_ID)
    assert open(c.paths[0]).read().endswith('"mess')  # the fixture really ends mid-record
    assert c.nmsg == 2 and len([m for m in store.messages(c) if m.kind == "text"]) == 2


def test_branch_is_read(stores):
    assert get("claude", CLAUDE_ID).branch == "feature/lists"
    assert get("codex").branch == "pyPort"
    assert parse("lists").match(get("claude", CLAUDE_ID))


def test_claude_title_sidecar_is_read_and_kept_in_step(stores):
    c = get("claude", CLAUDE_ID)
    side = claude._sidecar(stores["claude"] / "projects" / claude._encode(str(stores["cwd"])) / f"{CLAUDE_ID}.jsonl")
    side.write_text(json.dumps({"customTitle": "Named in VS Code"}))
    assert get("claude", CLAUDE_ID).title == "Named in VS Code"
    store.rename(c, "Named in convo")
    assert json.loads(side.read_text()) == {"customTitle": "Named in convo"}
    assert get("claude", CLAUDE_ID).title == "Named in convo"
    side.write_text("not json")
    assert get("claude", CLAUDE_ID).title == "Named in convo"  # falls back to the transcript


def test_claude_trash_takes_every_session_artifact(stores):
    root = stores["claude"]
    history = (root / "history.jsonl").read_text()
    before = fingerprint(root)
    trash.put(get("claude", CLAUDE_ID))
    assert [p for p in root.rglob("*") if CLAUDE_ID in p.name] == []
    left = [json.loads(line) for line in (root / "history.jsonl").read_text().splitlines()]
    assert [r["display"] for r in left] == ["first prompt", "middle prompt", "last prompt"]
    assert (root / "security" / "security_warnings_state_someone-else.json").exists()
    assert (root / "_scratch" / claude._encode(str(stores["cwd"])) / "someone-else").exists()
    trash.restore(trash.entries()[0][0])
    assert (root / "history.jsonl").read_text() == history  # rows back in their original places
    assert fingerprint(root) == before


def test_codex_trash_takes_prompt_history(stores):
    trash.put(get("codex"))
    rows = [json.loads(line) for line in (stores["codex"] / "history.jsonl").read_text().splitlines()]
    assert [r["session_id"] for r in rows] == ["someone-else"]


def test_codex_helper_threads_are_hidden(stores):
    convs, _ = store.load_all()
    assert [c.id for c in convs if c.tool == "codex"] == [CODEX_ID]
    assert codex.hidden == 1


@pytest.mark.parametrize("tool,suffix", [("codex", ""), ("agy", ""), ("agy", "-wal")])
def test_recently_written_conversation_is_in_use(stores, tool, suffix):
    c = get(tool)
    assert not c.live
    target = c.paths[-1] + suffix
    open(target, "a").close()
    os.utime(target)
    assert get(tool).live
    with pytest.raises(RuntimeError, match="last 90 seconds"):
        trash.put(c)  # checked afresh, even though this listing predates the write
    assert trash.entries() == []


def test_scan_cache_notices_a_companion_file(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(common, "_cache", None)
    db, wal, calls = tmp_path / "c.db", tmp_path / "c.db-wal", []
    db.write_text("x")
    scan = lambda p: calls.append(p) or len(calls)
    assert common.cached_scan(db, scan, also=(wal,)) == 1
    assert common.cached_scan(db, scan, also=(wal,)) == 1
    wal.write_text("new message")
    assert common.cached_scan(db, scan, also=(wal,)) == 2


def test_export_never_overwrites(stores):
    c = get("agy")
    first, second = store.export(c), store.export(c)
    assert first != second and first.exists() and second.name.endswith("-2.md")


def test_find_by_id_or_words(stores):
    convs, _ = store.load_all()
    assert store.find(convs, "3333").id == AGY_ID
    assert store.find(convs, "tunnel").id == AGY_ID
    assert store.find(convs, "tool:claude").id == CLAUDE_LIVE_ID  # the most recent match
    assert store.find(convs, "dir:demo port").id == CODEX_ID
    with pytest.raises(SystemExit, match="no conversation"):
        store.find(convs, "tool:codex tunnel")


def test_command_line_for_pasting(stores):
    c = get("codex")
    assert cli.command_line(c) == f"cd {stores['cwd']} && codex resume {CODEX_ID}"
    assert cli.command_line(conv(cwd="/a dir/x", id="i1")) == "cd '/a dir/x' && claude --resume i1"


def test_list_with_filter_and_doctor(stores, capsys):
    cli.main(["list", "tool:agy", "tunnel"])
    out = capsys.readouterr().out
    assert "Tunnel down" in out and "Reversing" not in out
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert out.count("rename and trash available") == 3
    assert "hidden    1 subagent" in out and "listed    2 conversation(s)" in out


def test_commands_search_inside_conversations(stores, capsys):
    convs, _ = store.load_all()
    assert [c.id for c in store.search(convs, "narwhal")] == [AGY_ID]
    assert [c.id for c in store.search(convs, "tool:claude narwhal")] == []
    assert store.find(convs, "three tests").id == CODEX_ID
    cli.main(["list", "zebrafish"])
    out = capsys.readouterr().out
    assert "Reversing a list" in out and "Tunnel" not in out


def test_words_can_match_text_when_it_is_given():
    c = conv()
    q = parse("auth pineapple")
    assert not q.match(c) and q.match(c, "we talked about pineapple here")
    assert q.inside(c, "we talked about pineapple here") == "pineapple"
    assert parse("auth").inside(c, "auth appears inside too") is None  # already explained by the title
