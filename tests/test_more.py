"""Settings, grouping, the action menu, memory notes, project cleanup and the shell wrapper."""

import json
import os
import stat
import subprocess

import pytest

from conftest import AGY_ID, CLAUDE_ID, CLAUDE_LIVE_ID, fingerprint, get
from convo import cli, config, store, trash
from convo.sources import claude
from convo.tui import Ask, Confirm, Memory, Menu, Note, Viewer
from test_app import drive, settle


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def end_live_session(stores):
    """Leave one Claude conversation in the project, with nothing running."""
    proj = stores["claude"] / "projects" / claude._encode(str(stores["cwd"]))
    (proj / f"{CLAUDE_LIVE_ID}.jsonl").unlink()
    (stores["claude"] / "sessions" / "1.json").unlink()


# --- settings ---

def test_defaults_without_a_file(stores):
    cfg = config.load()
    assert (cfg.sort, cfg.group, cfg.enter, cfg.show_tools, cfg.export_dir, cfg.problems) == ("updated", "none", "menu", False, "", [])


def test_settings_file_is_read_and_checked(stores):
    config.PATH.parent.mkdir(parents=True)
    config.PATH.write_text('sort = "size"\ngroup = "sideways"\nenter = "open"\nshow_tools = true\ncolour = "blue"\n')
    cfg = config.load()
    assert (cfg.sort, cfg.group, cfg.enter, cfg.show_tools) == ("size", "none", "open", True)
    assert len(cfg.problems) == 2 and "sideways" in cfg.problems[0] and "colour" in cfg.problems[1]
    config.PATH.write_text("this is = not [toml")
    assert config.load().sort == "updated" and len(config.load().problems) == 1


def test_template_matches_the_defaults(stores, capsys):
    cli.main(["config", "--init"])
    assert config.PATH.read_text() == config.TEMPLATE
    assert config.load() == config.Config()
    with pytest.raises(SystemExit, match="already exists"):
        cli.main(["config", "--init"])
    capsys.readouterr()
    cli.main(["config"])
    assert "enter       'menu'" in capsys.readouterr().out


def test_export_folder_setting(stores, capsys):
    config.PATH.parent.mkdir(parents=True)
    config.PATH.write_text(f'export_dir = "{stores["tmp"] / "elsewhere"}"\n')
    cli.main(["export", "3333"])
    assert capsys.readouterr().out.startswith(str(stores["tmp"] / "elsewhere"))


def test_settings_shape_the_list(stores):
    async def script(app, pilot):
        assert app.group == "project"
        assert [c.size for c in app.shown] == sorted((c.size for c in app.shown), reverse=True)  # largest first
        await pilot.press("down", "v")
        await settle(app, pilot)
        assert isinstance(app.screen, Viewer) and app.screen.show_tools
    drive(script, sort="size", group="project", show_tools=True)


# --- grouping ---

def test_group_by_project_and_fold(stores):
    async def script(app, pilot):
        table = app.query_one("#table")
        assert table.row_count == 4
        await pilot.press("b")
        assert app.group == "project" and table.row_count == 5 and isinstance(app.rows[0], str)
        assert app.current() is not None  # the cursor skips the heading to start with
        await pilot.press("up")
        await pilot.pause()
        assert app.current() is None
        await pilot.press("d", "space", "v")  # nothing acts on a heading
        await pilot.pause()
        assert len(app.screen_stack) == 1 and not app.marks and trash.entries() == []
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 1 and len(app.shown) == 4  # folded: still counted, not listed
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 5
        await pilot.press("b")
        assert app.group == "date" and app.rows[0] == "Older"
        await pilot.press("b")
        assert app.group == "none" and table.row_count == 4
    drive(script)


# --- action menu ---

def test_enter_opens_the_action_menu(stores):
    async def script(app, pilot):
        await pilot.press("slash", *"tool:agy", "enter", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Menu)
        await pilot.press("escape", "enter", "1")
        await settle(app, pilot)
        assert isinstance(app.screen, Viewer) and app.screen.conv.id == AGY_ID
        await pilot.press("escape", "enter", "3")
        await pilot.pause()
        assert isinstance(app.screen, Ask)
        await pilot.press("escape", "enter", "down", "down", "down", "down", "down", "down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) and "Move this agy conversation" in app.screen.prompt
    drive(script, enter="menu")


def test_go_to_folder_ends_with_the_folder(stores):
    async def script(app, pilot):
        await pilot.press("g")
        await pilot.pause()
        assert app.return_value == str(stores["cwd"])
    drive(script)


# --- memory notes ---

def test_memory_listing(stores, capsys):
    notes = claude.memories()
    assert len(notes) == 1  # the MEMORY.md index is not a note
    note = notes[0]
    assert (note["name"], note["type"], note["project"]) == ("list-style", "feedback", str(stores["cwd"]))
    assert note["description"] == "Prefers slicing over reversed()" and note["origin_gone"]
    cli.main(["memory"])
    out = capsys.readouterr().out
    assert "list-style" in out and "its conversation is gone" in out
    cli.main(["memory", "--project", "/nowhere"])
    assert capsys.readouterr().out == ""


def test_memory_screen(stores):
    async def script(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, Memory) and len(screen.items) == 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Note)
        await pilot.press("escape", "d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert screen.items == [] and trash.entries()[0][1]["title"] == "memory: list-style"
        await pilot.press("escape", "u")
        await pilot.pause()
        assert len(claude.memories()) == 1 and trash.entries() == []
    drive(script)


# --- project state ---

def test_project_state_only_when_no_conversation_is_left(stores):
    assert claude.project_state(str(stores["cwd"]))["files"] == []  # transcripts remain: the folder is not offered
    assert claude.project_state("/never/used") is None


def test_project_state_trash_and_restore(stores):
    end_live_session(stores)
    trash.put(get("claude", CLAUDE_ID))
    before = fingerprint(stores["claude"])
    cwd, settings = str(stores["cwd"]), stores["claude"] / ".claude.json"
    plan = claude.project_state(cwd)
    assert len(plan["files"]) == 1 and plan["blobs"][0]["id"] == cwd
    trash.stash("claude", "project", "project state", cwd, plan)
    data = json.loads(settings.read_text())
    assert list(data["projects"]) == ["/somewhere/else"] and data["numStartups"] == 3
    assert mode(settings) == 0o600  # a private file stays private
    assert claude.memories() == [] and claude.project_state(cwd) is None
    trash.restore(trash.entries()[-1][0])
    assert fingerprint(stores["claude"]) == before and mode(settings) == 0o600


def test_rewriting_history_keeps_it_private(stores):
    trash.put(get("claude", CLAUDE_ID))
    assert mode(stores["claude"] / "history.jsonl") == 0o600


def test_last_conversation_offers_project_cleanup(stores):
    end_live_session(stores)

    async def script(app, pilot):
        await pilot.press("space", "space", "d")  # two of the three: the project still has a conversation
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert len(app.screen_stack) == 1 and len(trash.entries()) == 2
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) and "last conversation" in app.screen.prompt
        assert ".claude.json" in app.screen.prompt
        await pilot.press("n")
        await pilot.pause()
        assert len(trash.entries()) == 3 and len(claude.memories()) == 1  # declined: nothing more moved
    drive(script)


def test_accepting_project_cleanup(stores):
    end_live_session(stores)
    for tool in ("codex", "agy"):
        trash.put(get(tool))

    async def script(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert trash.entries()[-1][1]["title"].startswith("project state: ")
        assert str(stores["cwd"]) not in json.loads((stores["claude"] / ".claude.json").read_text())["projects"]
        await pilot.press("u", "u")
        await pilot.pause()
        assert [c.id for c in store.load_all()[0] if c.tool == "claude"] == [CLAUDE_ID] and len(claude.memories()) == 1
    drive(script)


# --- shell wrapper ---

def test_setup_is_added_once(stores, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(stores["tmp"]))
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    cli.main(["setup"])
    cli.main(["setup"])
    rc = (stores["tmp"] / ".zshrc").read_text()
    assert rc.count('eval "$(convo init zsh)"') == 1 and "already set up" in capsys.readouterr().out
    cli.main(["setup", "--shell", "fish"])
    assert "convo init fish | source" in (stores["tmp"] / ".config/fish/config.fish").read_text()
    monkeypatch.setenv("SHELL", "/bin/tcsh")
    with pytest.raises(SystemExit, match="not supported"):
        cli.main(["init"])


def test_folder_is_handed_to_the_wrapper(stores, monkeypatch, capsys):
    monkeypatch.setattr("convo.tui.ConvoApp.run", lambda self: "/some where")
    target = stores["tmp"] / "cd-file"
    monkeypatch.setenv("CONVO_CD_FILE", str(target))
    cli.main([])
    assert target.read_text() == "/some where"
    monkeypatch.delenv("CONVO_CD_FILE")
    cli.main([])
    assert "cd '/some where'" in capsys.readouterr().out  # without the wrapper, the command is shown instead


def test_wrapper_changes_the_shell_folder(stores, capsys):
    cli.main(["init", "bash"])
    wrapper = capsys.readouterr().out
    bin_dir, dest = stores["tmp"] / "bin", stores["tmp"] / "a project"
    bin_dir.mkdir()
    dest.mkdir()
    fake = bin_dir / "convo"  # stands in for convo: asked to go somewhere, or not
    fake.write_text('#!/bin/sh\n[ "$1" = go ] && printf %s "$DEST" > "$CONVO_CD_FILE"\nexit 7\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DEST": str(dest)}
    run = lambda arg: subprocess.run(["bash", "-c", f'{wrapper}\ncd /\nconvo {arg}; echo "$? $PWD"'],
                                     env=env, capture_output=True, text=True).stdout.strip()
    assert run("go") == f"7 {dest}"  # folder changed, exit status passed through
    assert run("stay") == "7 /"
