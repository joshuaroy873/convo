"""The interactive view and the command line, driven without a real terminal."""

import asyncio
import json

from textual.widgets import DataTable, Input, RichLog

from conftest import AGY_ID, CLAUDE_ID, CODEX_ID, get
from convo import cli, config, trash
from convo.tui import ConvoApp, Viewer


def drive(script, query="", **settings):
    async def main():
        app = ConvoApp(query, config.Config(**settings))
        async with app.run_test(size=(140, 36)) as pilot:
            await pilot.pause()
            await script(app, pilot)
    asyncio.run(main())


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_list_filter_and_view(stores):
    async def script(app, pilot):
        table = app.query_one("#table", DataTable)
        assert table.row_count == 4
        await pilot.press("slash", *"codex", "enter")
        assert [c.id for c in app.shown] == [CODEX_ID]
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Viewer)
        log = app.screen.query_one("#log", RichLog)
        shown = "\n".join(strip.text for strip in log.lines)
        assert "Port the script" in shown and "Added three tests." in shown and "main.m" not in shown
        await pilot.press("t")  # tool output on
        await pilot.pause()
        assert "main.m" in "\n".join(strip.text for strip in log.lines)
        await pilot.press("d")  # list keys must do nothing inside the viewer
        await pilot.pause()
        assert isinstance(app.screen, Viewer) and trash.entries() == []
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, Viewer)
    drive(script)


def test_search_looks_inside_conversations(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"narwhal")  # a word that only appears in agy tool output
        await settle(app, pilot)
        assert [c.id for c in app.shown] == [AGY_ID]  # no Enter needed: the match joins as soon as the text is read
        assert not app.indexing and "narwhal" in str(app.query_one("#detail").render())
        await pilot.press("enter", "enter")
        await settle(app, pilot)
        assert app.screen.show_tools and len(app.screen.hits) == 1  # the viewer opens on the match, tool output shown
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert len(app.shown) == 4
    drive(script)


def test_search_mixes_titles_text_and_terms(stores):
    async def script(app, pilot):
        box = app.query_one("#query", Input)
        await pilot.press("slash")
        for query, expected in [
            ("tunnel", {AGY_ID}),                    # in a title: answered at once
            ("tunnel narwhal", {AGY_ID}),            # one word in the title, the other only inside
            ("tool:codex narwhal", set()),           # the term still narrows
            ("tests", {CODEX_ID}),                   # only said inside a Codex conversation
            ("reversed", {CLAUDE_ID}),
            ("zebrafish tool:claude", {CLAUDE_ID}),  # inside Claude tool output
            ("nowhere-at-all", set()),
        ]:
            box.value = query
            await settle(app, pilot)
            assert {c.id for c in app.shown} == expected, query
    drive(script)


def test_search_text_is_reread_after_a_conversation_changes(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"walrus")
        await settle(app, pilot)
        assert app.shown == []
        c = get("claude", CLAUDE_ID)
        with open(c.paths[0], "a") as f:
            f.write("\n" + json.dumps({"type": "user", "timestamp": "2026-01-05T10:00:00Z", "cwd": str(stores["cwd"]),
                                       "message": {"role": "user", "content": "walrus"}}) + "\n")
        await pilot.press("enter", "f5")
        await settle(app, pilot)
        assert [c.id for c in app.shown] == [CLAUDE_ID]
    drive(script)


def test_rename_trash_and_undo(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"reversing", "enter")
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#ask", Input).value = "List tricks"
        await pilot.press("enter")
        await pilot.pause()
        assert get("claude", CLAUDE_ID).title == "List tricks"
        await pilot.press("escape")
        await pilot.pause()
        assert app.current().id == CLAUDE_ID  # selection follows the renamed row
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert CLAUDE_ID not in [c.id for c in app.all] and len(trash.entries()) == 1
        await pilot.press("u")
        await pilot.pause()
        assert CLAUDE_ID in [c.id for c in app.all] and trash.entries() == []
    drive(script)


def test_cli_round_trip(stores, capsys):
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "Reversing a list" in out and "Port MATLAB script" in out and "Tunnel down" in out
    cli.main(["rename", "3333", "Tunnel fixed"])
    cli.main(["rm", "3333"])
    cli.main(["trash", "list"])
    assert "Tunnel fixed" in capsys.readouterr().out
    cli.main(["trash", "restore"])
    cli.main(["show", "3333"])
    assert "# Tunnel fixed" in capsys.readouterr().out
    cli.main(["rm", "2222"])
    cli.main(["trash", "empty", "--yes"])
    capsys.readouterr()
    cli.main(["list", "--tool", "codex"])
    assert "Port MATLAB script" not in capsys.readouterr().out


def test_starts_filtered_and_understands_terms(stores):
    async def script(app, pilot):
        assert [c.id for c in app.shown] == [AGY_ID]
        await pilot.press("slash")
        app.query_one("#query", Input).value = "tool:codex via:vscode dir:demo"
        await pilot.press("enter")
        assert [c.id for c in app.shown] == [CODEX_ID]
        detail = str(app.query_one("#detail").render())
        assert "pyPort" in detail
    drive(script, "tunnel")


def test_mark_and_bulk_trash(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"-via:vscode", "enter")  # leaves the claude cli, codex... rows that are not live
        ids = [c.id for c in app.shown]
        assert set(ids) == {CLAUDE_ID, AGY_ID}
        await pilot.press("space", "space")
        assert len(app.marks) == 2
        await pilot.press("escape")  # first escape only clears the marks
        await pilot.pause()
        assert not app.marks and len(app.shown) == 2
        await pilot.press("up", "space", "space", "d")  # back to the first row, then mark both
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert len(trash.entries()) == 2 and not app.marks
        assert {c.id for c in app.all} == {CODEX_ID, "11111111-aaaa-4bbb-8ccc-000000000002"}
    drive(script)


def test_copy_command_and_help(stores, monkeypatch):
    copied = []

    async def script(app, pilot):
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        await pilot.press("slash", *"tool:agy", "enter", "y")
        assert copied == [f"cd {stores['cwd']} && agy --conversation {AGY_ID}"]
        await pilot.press("question_mark")
        await pilot.pause()
        assert type(app.screen).__name__ == "Help"
        await pilot.press("d")  # keys behind the help screen do nothing
        await pilot.pause()
        assert type(app.screen).__name__ == "Help" and trash.entries() == []
        await pilot.press("escape", "enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Viewer)
        await pilot.press("f1")
        await pilot.pause()
        assert type(app.screen).__name__ == "Help"
    drive(script)


def test_reload_failure_keeps_the_previous_rows(stores, monkeypatch):
    async def script(app, pilot):
        from convo.sources import codex
        assert len(app.all) == 4
        monkeypatch.setattr(codex, "list_convs", lambda: 1 / 0)
        await pilot.press("f5")
        await pilot.pause()
        assert len(app.all) == 4 and CODEX_ID in [c.id for c in app.all]
    drive(script)
