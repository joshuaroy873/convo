"""Interactive view: conversation list, transcript viewer, rename/trash dialogs."""

from __future__ import annotations

import os
import time
from datetime import datetime

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, RichLog, Static

from . import config, orphans, stats, store, trash
from .cli import command_line, resume
from .common import Conv, Msg, human_size, one_line, short_path, when
from .query import parse
from .sources import claude

TOOL_STYLE = {"claude": "dark_orange", "codex": "green3", "agy": "dodger_blue1"}
SORTS = [
    ("updated", lambda c: -c.updated),
    ("created", lambda c: -c.created),
    ("size", lambda c: -c.size),
    ("messages", lambda c: -c.nmsg),
    ("title", lambda c: c.title.lower()),
    ("project", lambda c: (c.cwd.lower(), -c.updated)),
]
GROUPS = ("none", "project", "date")
MAX_BODY = 20000  # characters of one message shown in the viewer; exports are complete


class Ask(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, value: str = ""):
        super().__init__()
        self.prompt, self.value = prompt, value

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.prompt)
            yield Input(value=self.value, id="ask")
            yield Label("enter = save · esc = cancel", classes="hint")

    @on(Input.Submitted, "#ask")
    def done(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.prompt)
            yield Label("y = yes · n / esc = no", classes="hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


HELP = """\
[b]List[/b]
  enter      open the conversation          r      resume it in its tool
  s          stats for what is listed       g      quit, leaving the shell in its folder
  /          search titles, projects and what was said inside conversations
  t          cycle the tool filter          o      cycle the sort order
  b          group by project / date        m      Claude's memory notes
  n          rename                         y      copy the command that resumes it
  space      mark for a bulk trash          d      trash the marked ones, or this one
  u          undo the last trash            e / E  export Markdown / JSON
  c          clean up orphans               f5     reload
  esc        clear marks, then the search   q      quit

[b]Inside a conversation[/b]
  t          show or hide tool output       /      find text
  n / N      next / previous match          [ / ]  previous / next prompt
  g / G      top / end                      e      export Markdown
  esc        back

[b]Search terms[/b]  (mix with plain words)
  tool:claude        only Claude            -tool:claude     everything except Claude
  tool:claude,codex  Claude or Codex        via:cli          started in a terminal
  via:vscode         started in VS Code     via:ide          started in the Antigravity IDE
  dir:backend        project folder contains "backend"       dir:backend,!test   …but not "test"
  date:today         also yesterday, week, month
  date:<2d           active in the last 2 days               date:>1w            not active for over a week
"""


class Help(ModalScreen):
    BINDINGS = [Binding("escape,q,question_mark,f1", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="helpbox"):
            yield Static(HELP)
            yield Label("esc = close", classes="hint")

    def action_close(self) -> None:
        self.dismiss()


def moves(plan: dict, cid: str, limit: int = 9) -> str:
    """The paths and records a trash would take, for a confirmation dialog."""
    lines = trash.preview(plan, cid)
    extra = [f"… and {len(lines) - limit} more"] if len(lines) > limit else []
    # a long path keeps its end, where the name that identifies it is
    return "\n".join("  " + (ln if len(ln) <= 66 else "…" + ln[-65:]) for ln in lines[:limit] + extra)


class Orphans(Screen):
    """Empty conversations, ones whose project is gone, and leftover state."""

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("space", "mark", "Mark"),
        Binding("a", "all", "Mark all"),
        Binding("d", "trash", "Trash"),
        Binding("question_mark,f1", "app.help", "Help"),
    ]

    def __init__(self):
        super().__init__()
        self.items: list[orphans.Orphan] = []
        self.marks: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Static(id="vhead")
        yield DataTable(id="otable", cursor_type="row", zebra_stripes=True)
        yield Static(id="odetail")
        yield Footer()

    def on_mount(self) -> None:
        self.fill()

    def current(self) -> orphans.Orphan | None:
        row = self.query_one("#otable", DataTable).cursor_row
        return self.items[row] if 0 <= row < len(self.items) else None

    def fill(self, row: int = 0) -> None:
        self.items = orphans.find(store.load_all()[0])
        self.marks &= {o.key for o in self.items}
        self.query_one("#vhead", Static).update(
            f" Clean up: {len(self.items)} item(s), {human_size(sum(o.size for o in self.items))}"
            + (f"  |  marked: {len(self.marks)}" if self.marks else "") + "   (trash is reversible: u in the list undoes)")
        t = self.query_one("#otable", DataTable)
        t.clear(columns=True)
        t.add_column("Tool", width=6)
        t.add_column("Why", width=44)
        t.add_column("What", width=max(20, self.size.width - 6 - 44 - 8 - 10))
        t.add_column("Size", width=6)
        for o in self.items:
            t.add_row(Text(o.tool, style=TOOL_STYLE[o.tool]), one_line(o.reason, 44),
                      ("✓ " if o.key in self.marks else "") + one_line(o.label, 80), Text(human_size(o.size), justify="right"), key=o.key)
        if self.items:
            t.move_cursor(row=min(row, len(self.items) - 1))
        self.paint()

    def paint(self) -> None:
        o = self.current()
        d = self.query_one("#odetail", Static)
        if not o:
            d.update(Text("Nothing to clean up.", style="dim"))
            return
        try:
            body = moves(orphans.plan_of(o), o.id, 5)
        except Exception as e:
            body = f"  cannot be removed: {e}"
        d.update(Text.assemble((f"{o.reason}\n", "yellow"), ("Trashing it moves:\n", "dim"), body))

    @on(DataTable.RowHighlighted, "#otable")
    def moved(self) -> None:
        self.paint()

    def action_mark(self) -> None:
        o = self.current()
        if o:
            self.marks ^= {o.key}
            self.fill(self.query_one("#otable", DataTable).cursor_row + 1)

    def action_all(self) -> None:
        keys = {o.key for o in self.items}
        self.marks = set() if self.marks == keys else keys
        self.fill(self.query_one("#otable", DataTable).cursor_row)

    def action_trash(self) -> None:
        cur = self.current()
        targets = [o for o in self.items if o.key in self.marks] or ([cur] if cur else [])
        if not targets:
            return

        def done(ok: bool | None) -> None:
            if not ok:
                return
            failed = 0
            for o in targets:
                try:
                    orphans.remove(o)
                except Exception as e:
                    failed += 1
                    self.notify(f"Could not trash {one_line(o.label, 40)}: {e}", severity="error")
            self.marks.clear()
            self.fill(self.query_one("#otable", DataTable).cursor_row)
            self.notify(f"Moved {len(targets) - failed} to trash", timeout=4)

        what = f"“{one_line(targets[0].label, 60)}”" if len(targets) == 1 else f"{len(targets)} items"
        self.app.push_screen(Confirm(f"Move {what} to the trash?\n\nNothing is erased until the trash is emptied."), done)

    def action_back(self) -> None:
        self.dismiss()


class Stats(Screen):
    """Totals for the conversations currently listed."""

    BINDINGS = [Binding("escape,q,s", "back", "Back")]

    def __init__(self, convs: list[Conv], scope: str):
        super().__init__()
        self.convs, self.scope = convs, scope

    def compose(self) -> ComposeResult:
        yield Static(f" Stats  {self.scope}", id="vhead")
        yield RichLog(id="log", wrap=False, markup=False, highlight=False, auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log", RichLog).write(Text(stats.report(self.convs, len(trash.entries()))))

    def action_back(self) -> None:
        self.dismiss()


class Note(Screen):
    """One memory note, read-only."""

    BINDINGS = [Binding("escape,q", "back", "Back")]

    def __init__(self, memo: dict):
        super().__init__()
        self.memo = memo

    def compose(self) -> ComposeResult:
        yield Static(f" memory  {self.memo['name']}   {short_path(self.memo['project'])}", id="vhead")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        try:
            body = open(self.memo["path"], encoding="utf-8").read()
        except OSError as e:
            body = f"Could not read the note: {e}"
        self.call_after_refresh(lambda: self.query_one("#log", RichLog).write(Markdown(body), expand=True))

    def action_back(self) -> None:
        self.dismiss()


class Memory(Screen):
    """The notes Claude keeps between sessions, across every project."""

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("d", "trash", "Trash"),
        Binding("question_mark,f1", "app.help", "Help"),
    ]

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Static(id="vhead")
        yield DataTable(id="otable", cursor_type="row", zebra_stripes=True)
        yield Static(id="odetail")
        yield Footer()

    def on_mount(self) -> None:
        self.fill()

    def current(self) -> dict | None:
        row = self.query_one("#otable", DataTable).cursor_row
        return self.items[row] if 0 <= row < len(self.items) else None

    def fill(self, row: int = 0) -> None:
        self.items = sorted(claude.memories(), key=lambda m: (m["project"], m["name"]))
        self.query_one("#vhead", Static).update(f" Claude memory: {len(self.items)} note(s) in "
                                                f"{len({m['project'] for m in self.items})} project(s)   (enter = read)")
        t = self.query_one("#otable", DataTable)
        t.clear(columns=True)
        t.add_column("Project", width=34)
        t.add_column("Type", width=10)
        t.add_column("Note", width=max(20, self.size.width - 34 - 10 - 6 - 11 - 12))
        t.add_column("Size", width=6)
        t.add_column("Updated", width=11)
        for m in self.items:
            proj = short_path(m["project"])
            t.add_row(proj if len(proj) <= 34 else "…" + proj[-33:], m["type"] or "-", one_line(m["name"], 70),
                      Text(human_size(m["size"]), justify="right"), when(m["updated"]), key=m["path"])
        if self.items:
            t.move_cursor(row=min(row, len(self.items) - 1))
        self.paint()

    def paint(self) -> None:
        m = self.current()
        d = self.query_one("#odetail", Static)
        if not m:
            d.update(Text("Claude has no memory notes.", style="dim"))
            return
        wrote = ("written by a conversation that no longer exists" if m["origin_gone"]
                 else f"written by conversation {m['origin'][:8]}" if m["origin"] else "origin not recorded")
        d.update(Text.assemble((m["name"] + "\n", "bold"), (one_line(m["description"], 300) + "\n", ""),
                               (f"{wrote}\n{short_path(m['path'])}", "dim")))

    @on(DataTable.RowHighlighted, "#otable")
    def moved(self) -> None:
        self.paint()

    @on(DataTable.RowSelected, "#otable")
    def opened(self) -> None:
        m = self.current()
        if m:
            self.app.push_screen(Note(m))

    def action_trash(self) -> None:
        m = self.current()
        if not m:
            return

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                trash.stash("claude", m["name"], f"memory: {m['name']}", m["project"],
                            {"files": [m["path"]], "rows": [], "lines": []})
            except Exception as e:
                self.notify(f"Could not trash: {e}", severity="error")
                return
            self.fill(self.query_one("#otable", DataTable).cursor_row)
            self.notify("Moved the note to trash  (u in the list undoes)", timeout=4)

        self.app.push_screen(Confirm(f"Move this memory note to the trash?\n\n“{one_line(m['name'], 60)}”\n\n"
                                     "Claude will stop recalling it. Its line in that project's MEMORY.md index stays."), done)

    def action_back(self) -> None:
        self.dismiss()


class Viewer(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("t", "tools", "Tool output"),
        Binding("slash", "find", "Find"),
        Binding("n", "next(1)", "Next match"),
        Binding("N", "next(-1)", "Prev match", show=False),
        Binding("right_square_bracket", "prompt(1)", "Next prompt"),
        Binding("left_square_bracket", "prompt(-1)", "Prev prompt"),
        Binding("g", "top", "Top", show=False),
        Binding("G", "end", "End", show=False),
        Binding("e", "export", "Export"),
        Binding("question_mark,f1", "app.help", "Help"),
    ]

    def __init__(self, conv: Conv, find: str = ""):
        super().__init__()
        self.conv = conv
        self.msgs: list[Msg] = []
        self.show_tools = False  # the app's setting is applied on mount
        self.needle = find
        self.marks: list[tuple[int, Msg]] = []  # first rendered line of each shown message
        self.hits: list[int] = []
        self.hit = -1

    def compose(self) -> ComposeResult:
        yield Static(id="vhead")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=False)
        yield Input(placeholder="find in conversation…", id="find")
        yield Footer()

    def on_mount(self) -> None:
        c = self.conv
        self.show_tools = bool(getattr(self.app, "cfg", None) and self.app.cfg.show_tools)
        self.query_one("#vhead", Static).update(Text.assemble(
            (f" {c.tool} ", f"bold black on {TOOL_STYLE[c.tool]}"), " ", (c.title, "bold"),
            ("   " + short_path(c.cwd), "dim")))
        self.query_one("#log", RichLog).write(Text("Loading…", style="dim"))
        self.load()

    @work(thread=True)
    def load(self) -> None:
        try:
            msgs = store.messages(self.conv)
        except Exception as e:
            self.app.call_from_thread(self.notify, f"Could not read conversation: {e}", severity="error")
            msgs = []
        self.app.call_from_thread(self.loaded, msgs)

    def loaded(self, msgs: list[Msg]) -> None:
        self.msgs = msgs
        self.draw()
        if self.needle:
            self.search(self.needle)

    def draw(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        put = lambda r: log.write(r, expand=True)  # wrap at the viewer width, not RichLog's default
        self.marks = []
        label = self.conv.tool.upper()
        for m in self.msgs:
            if m.kind in ("result", "thinking") and not self.show_tools:
                continue
            self.marks.append((len(log.lines), m))
            stamp = ("  " + when(m.ts, True), "dim") if m.ts else ""
            body = m.text if len(m.text) <= MAX_BODY else m.text[:MAX_BODY] + f"\n… [{len(m.text) - MAX_BODY} more characters]"
            if m.kind == "call":
                shown = body if self.show_tools else one_line(body, 150)
                put(Text.assemble(("  ⚙ " + m.name + "  ", "bold yellow"), (shown, "dim")))
            elif m.kind == "result":
                put(Text(body[:4000] + ("\n… [truncated]" if len(body) > 4000 else ""), style="dim"))
            elif m.kind == "thinking":
                put(Text(body, style="dim italic"))
            elif m.kind == "note":
                put(Text("  · " + one_line(body, 300), style="dim italic"))
            elif m.role == "user":
                put(Text(""))
                put(Text.assemble((" YOU ", "bold black on cyan"), stamp))
                put(Text(body))
            else:
                put(Text(""))
                put(Text.assemble((f" {label} ", f"bold black on {TOOL_STYLE[self.conv.tool]}"), stamp))
                try:
                    put(Markdown(body))
                except Exception:
                    put(Text(body))
        if not self.marks:
            put(Text("(no messages)", style="dim"))

    def _goto(self, line: int) -> None:
        self.query_one("#log", RichLog).scroll_to(y=max(0, line - 1), animate=False)

    def search(self, needle: str) -> None:
        self.needle = needle
        low = needle.lower()
        find = lambda: [line for line, m in self.marks if low in m.text.lower()]
        self.hits = find()
        if not self.hits and not self.show_tools and any(low in m.text.lower() for m in self.msgs):
            self.show_tools = True  # the match is inside hidden tool output
            self.draw()
            self.hits = find()
        self.hit = -1
        if self.hits:
            self.action_next(1)
        else:
            self.notify(f"No match for “{needle}”")

    def action_next(self, step: int) -> None:
        if not self.hits:
            if self.needle:
                self.notify("No matches")
            return
        self.hit = (self.hit + step) % len(self.hits)
        self._goto(self.hits[self.hit])
        self.notify(f"“{self.needle}” — message {self.hit + 1} of {len(self.hits)}", timeout=2)

    def action_prompt(self, step: int) -> None:
        log = self.query_one("#log", RichLog)
        y = int(log.scroll_offset.y) + 1
        lines = [line for line, m in self.marks if m.role == "user" and m.kind == "text"]
        nxt = [ln for ln in lines if ln > y + 1] if step > 0 else [ln for ln in reversed(lines) if ln < y - 1]
        if nxt:
            self._goto(nxt[0])

    def action_tools(self) -> None:
        self.show_tools = not self.show_tools
        self.draw()
        self.notify("Tool output and thinking " + ("shown" if self.show_tools else "hidden"), timeout=2)

    def action_find(self) -> None:
        box = self.query_one("#find", Input)
        box.add_class("on")
        box.value = self.needle
        box.focus()

    @on(Input.Submitted, "#find")
    def find_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        event.input.remove_class("on")
        self.query_one("#log", RichLog).focus()
        if event.value.strip():
            self.search(event.value.strip())

    def action_top(self) -> None:
        self.query_one("#log", RichLog).scroll_home(animate=False)

    def action_end(self) -> None:
        self.query_one("#log", RichLog).scroll_end(animate=False)

    def action_export(self) -> None:
        self.app.export(self.conv)

    def action_back(self) -> None:
        box = self.query_one("#find", Input)
        if box.has_class("on"):
            box.remove_class("on")
            self.query_one("#log", RichLog).focus()
        else:
            self.dismiss()


class ConvoApp(App):
    TITLE = "convo"
    CSS = """
    #stats { height: auto; background: $boost; padding: 0 1; }
    #status { height: 1; background: $primary-darken-2; color: $text; padding: 0 1; }
    #query, #find { display: none; height: 3; }
    #query.on, #find.on { display: block; }
    #table, #otable { height: 1fr; }
    #odetail { height: 8; border-top: solid $primary; padding: 0 1; }
    #detail { height: 7; border-top: solid $primary; padding: 0 1; }
    #vhead { height: 1; background: $primary-darken-2; }
    #log { height: 1fr; padding: 0 1; }
    Ask, Confirm { align: center middle; }
    #box { width: 76; max-width: 95%; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    #helpbox { width: 104; max-width: 98%; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    Help { align: center middle; }
    .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "filter", "Search"),
        Binding("t", "tool", "Tool", show=False),
        Binding("o", "sort", "Sort", show=False),
        Binding("r", "resume", "Resume", show=False),
        Binding("n", "rename", "Rename", show=False),
        Binding("s", "stats", "Stats"),
        Binding("space", "mark", "Mark"),
        Binding("d", "trash", "Trash"),
        Binding("u", "undo", "Undo trash"),
        Binding("e", "export('md')", "Export", show=False),
        Binding("y", "copy", "Copy command", show=False),
        Binding("b", "group", "Group"),
        Binding("g", "goto", "Go to folder", show=False),
        Binding("m", "memory", "Memory"),
        Binding("c", "cleanup", "Clean up"),
        Binding("question_mark,f1", "help", "Help"),
        Binding("E", "export('json')", "Export JSON", show=False),
        Binding("f5", "reload", "Reload", show=False),
        Binding("escape", "clear", "Clear", show=False),
    ]

    def __init__(self, query: str = "", cfg: config.Config | None = None):
        super().__init__()
        self.cfg = cfg or config.load()
        self.all: list[Conv] = []
        self.shown: list[Conv] = []
        self.rows: list[Conv | str] = []  # what each table row is: a conversation, or a group heading
        self.group = self.cfg.group
        self.folded: set[str] = set()
        self.counts: dict[str, int] = {}
        self.flt = query
        self.marks: set[str] = set()
        self.tool: str | None = None
        self.sort = next(i for i, (name, _) in enumerate(SORTS) if name == self.cfg.sort)
        # what was said in each conversation, read in the background the first time a search needs it
        self.texts: dict[str, tuple[float, str, str]] = {}
        self.indexing = False
        self.unreadable: set[str] = set()
        self.sel: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="stats")
        yield Static(id="status")
        yield Input(id="query")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()
        self.query_one("#table", DataTable).focus()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # List actions must not fire from the viewer or a dialog, where the same keys mean other things.
        if action in ("quit", "command_palette", "help"):
            return True
        return len(self.screen_stack) == 1

    # --- data ---

    def reload(self, keep: str | None = None) -> None:
        convs, errors = store.load_all()
        for e in errors:
            # a store that failed to load keeps the rows it had, rather than vanishing from the list
            convs += [c for c in self.all if c.tool == e.split(":", 1)[0]]
            self.notify(f"{e} (showing what was listed before)", severity="warning")
        self.all = convs
        self.marks &= {c.key for c in convs}
        self.unreadable.clear()
        self.sel = keep or self.sel
        self.fill(keep)
        self.read_texts()

    def current(self) -> Conv | None:
        item = self.row_item()
        return item if isinstance(item, Conv) else None

    def row_item(self) -> Conv | str | None:
        row = self.query_one("#table", DataTable).cursor_row
        return self.rows[row] if 0 <= row < len(self.rows) else None

    @staticmethod
    def row_key(item: Conv | str) -> str:
        return item.key if isinstance(item, Conv) else f"group:{item}"

    def grouped(self, rows: list[Conv]) -> list[Conv | str]:
        """The rows with a heading before each group; a folded group shows its heading only."""
        if self.group == "none":
            return list(rows)
        groups: dict[str, list[Conv]] = {}
        if self.group == "project":
            for c in rows:
                groups.setdefault(short_path(c.cwd) or "(no project)", []).append(c)
            order = sorted(groups, key=lambda g: -max(c.updated for c in groups[g]))
        else:
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            edges = [("Today", midnight), ("Yesterday", midnight - 86400), ("Last 7 days", time.time() - 7 * 86400),
                     ("Last 30 days", time.time() - 30 * 86400), ("Older", float("-inf"))]
            for c in rows:
                groups.setdefault(next(name for name, edge in edges if c.updated >= edge), []).append(c)
            order = [name for name, _ in edges if name in groups]
        out: list[Conv | str] = []
        for name in order:
            out.append(name)
            if name not in self.folded:
                out += groups[name]
        self.counts = {name: len(groups[name]) for name in order}
        return out

    def fill(self, keep: str | None = None) -> None:
        self.query_one("#stats", Static).update(stats.panel(self.all, TOOL_STYLE, len(trash.entries()), self.size.width - 2))
        t = self.query_one("#table", DataTable)
        cur = self.row_item()
        # self.sel remembers the selection while a filter hides it, e.g. right after a rename
        keep = keep or (self.row_key(cur) if cur is not None else self.sel)
        query = parse(self.flt)
        rows = []
        for c in self.all:
            if self.tool and c.tool != self.tool:
                continue
            if query.match(c, self.said(c)[1]):
                rows.append(c)
        rows.sort(key=SORTS[self.sort][1])
        self.shown = rows
        self.rows = self.grouped(rows)
        wide = max(20, self.size.width - 6 - 7 - 30 - 6 - 7 - 12 - 16)
        t.clear(columns=True)
        t.add_column("Tool", width=6)
        t.add_column("Via", width=6)
        t.add_column("Title", width=wide)
        t.add_column("Project", width=30)
        t.add_column("Msgs", width=5)
        t.add_column("Size", width=6)
        t.add_column("Updated", width=11)
        for c in self.rows:
            if isinstance(c, str):
                fold = "▸" if c in self.folded else "▾"
                name = c if len(c) <= wide - 10 else "…" + c[-(wide - 11):]  # a path keeps its end, which tells projects apart
                t.add_row("", "", Text(f"{fold} {name}  ({self.counts[c]})", style="bold"), "", "", "", "",
                          key=self.row_key(c))
                continue
            flag = ("✓ " if c.key in self.marks else "") + ("● " if c.live else "") + ("▣ " if c.archived else "")
            proj = short_path(c.cwd) or "-"
            proj = proj if len(proj) <= 30 else "…" + proj[-29:]
            t.add_row(Text(c.tool, style=TOOL_STYLE[c.tool]), c.origin, one_line(flag + c.title, wide), proj,
                      Text(str(c.nmsg), justify="right"), Text(human_size(c.size), justify="right"), when(c.updated),
                      key=c.key)
        first = next((i for i, c in enumerate(self.rows) if isinstance(c, Conv)), 0)
        idx = next((i for i, c in enumerate(self.rows) if self.row_key(c) == keep), first)
        if self.rows:
            t.move_cursor(row=idx)
        self.paint()

    def paint(self) -> None:
        parts = [f"{len(self.shown)} of {len(self.all)} shown", f"sort: {SORTS[self.sort][0]}"]
        if self.group != "none":
            parts.append(f"group: {self.group}")
        if self.tool:
            parts.append(f"tool: {self.tool}")
        if self.flt:
            parts.append(f"search: {self.flt}")
        if self.indexing:
            parts.append("reading conversations…")
        if self.marks:
            parts.append(f"marked: {len(self.marks)}")
        self.query_one("#status", Static).update("  ·  ".join(parts))
        c = self.current()
        d = self.query_one("#detail", Static)
        if not c:
            heading = self.row_item()
            d.update(Text(f"{heading}\nenter folds or unfolds this group" if isinstance(heading, str)
                          else "No conversations match.", style="dim"))
            return
        flags = ("  ● in use" if c.live else "") + ("  ▣ archived" if c.archived else "")
        missing = "" if not c.cwd or os.path.isdir(c.cwd) else "  (folder no longer exists)"
        body = Text()
        body.append(c.title, style="bold")
        body.append(flags + "\n", style="yellow")
        body.append(f"{c.tool} / {c.origin}  ·  {c.nmsg} messages  ·  {human_size(c.size)}  ·  "
                    f"{when(c.created, True)} → {when(c.updated, True)}\n", style="dim")
        branch = f"  ⎇ {c.branch}" if c.branch else ""
        body.append(f"{c.id}   {short_path(c.cwd) or '-'}{missing}{branch}\n", style="dim")
        text, low = self.said(c)
        word = parse(self.flt).inside(c, low)
        if word:
            i, count = low.find(word), low.count(word)
            body.append(f"“{word}” {count} time{'s' if count != 1 else ''} inside: ", style="yellow")
            body.append(one_line(text[max(0, i - 80):i + 160], 240))
        else:
            body.append("» " + (one_line(c.first, 400) or "(no prompt)"))
        d.update(body)

    @on(DataTable.RowHighlighted, "#table")
    def moved(self) -> None:
        cur = self.row_item()
        if cur is not None:
            self.sel = self.row_key(cur)
        self.paint()

    @on(DataTable.RowSelected, "#table")
    def opened(self) -> None:
        item = self.row_item()
        if isinstance(item, str):
            self.folded ^= {item}
            self.fill(self.row_key(item))
        elif item:
            self.action_open()

    def action_stats(self) -> None:
        scope = f"for the {len(self.shown)} conversations matching “{self.flt}”" if self.flt or self.tool \
            else f"for all {len(self.shown)} conversations"
        self.push_screen(Stats(list(self.shown), scope))

    def action_open(self) -> None:
        c = self.current()
        if c:
            self.push_screen(Viewer(c, parse(self.flt).inside(c, self.said(c)[1]) or ""))

    def action_group(self) -> None:
        self.group = GROUPS[(GROUPS.index(self.group) + 1) % len(GROUPS)]
        self.folded.clear()
        self.fill()

    def action_memory(self) -> None:
        self.push_screen(Memory(), lambda _: self.paint())

    def action_goto(self) -> None:
        c = self.current()
        if not c:
            return
        if not c.cwd or not os.path.isdir(c.cwd):
            self.notify("That conversation's folder no longer exists.", severity="warning")
            return
        self.exit(c.cwd)  # the command line passes it to the shell wrapper

    def on_resize(self) -> None:
        if self.all:
            self.fill()

    # --- filter and full-text search ---

    def action_filter(self) -> None:
        box = self.query_one("#query", Input)
        box.placeholder = "words to find in titles, projects and conversations · tool:claude via:vscode dir:name date:<2d"
        box.add_class("on")
        box.value = self.flt
        box.focus()

    @on(Input.Changed, "#query")
    def typed(self, event: Input.Changed) -> None:
        if event.value != self.flt:
            self.flt = event.value
            self.fill()  # titles and projects answer at once; conversation text joins when it has been read
            self.read_texts()

    @on(Input.Submitted, "#query")
    def entered(self, event: Input.Submitted) -> None:
        event.input.remove_class("on")
        self.query_one("#table", DataTable).focus()

    def said(self, c: Conv) -> tuple[str, str]:
        """A conversation's text and its lowercase form, or empty strings until it has been read."""
        hit = self.texts.get(c.key)
        return (hit[1], hit[2]) if hit and hit[0] == c.updated else ("", "")

    def read_texts(self) -> None:
        """Start reading conversation text if a search needs it and some is missing."""
        if self.indexing or not parse(self.flt).words:
            return
        if any(not self.said(c)[1] and c.key not in self.unreadable for c in self.all):
            self.indexing = True
            self.paint()
            self.index()

    @work(thread=True, exclusive=True, group="index")
    def index(self) -> None:
        for c in list(self.all):
            if self.said(c)[1] or c.key in self.unreadable:
                continue
            try:
                text = store.text_of(c)
            except Exception:
                text = ""
            if text:
                self.texts[c.key] = (c.updated, text, text.lower())
            else:
                self.unreadable.add(c.key)  # nothing to search; do not try again on every keystroke
        self.call_from_thread(self.indexed)

    def indexed(self) -> None:
        self.indexing = False
        self.fill()
        self.read_texts()  # the list may have been reloaded meanwhile

    def action_clear(self) -> None:
        box = self.query_one("#query", Input)
        if box.has_class("on"):
            box.remove_class("on")
            self.query_one("#table", DataTable).focus()
            return
        if self.marks:
            self.marks.clear()
            self.fill()
            return
        if self.flt or self.tool:
            self.flt, self.tool = "", None
            self.fill()

    def action_tool(self) -> None:
        order = [None, *store.SOURCES]
        self.tool = order[(order.index(self.tool) + 1) % len(order)]
        self.fill()

    def action_sort(self) -> None:
        self.sort = (self.sort + 1) % len(SORTS)
        self.fill()

    def action_reload(self) -> None:
        self.reload()
        self.notify("Reloaded", timeout=2)

    # --- actions on the selected conversation ---

    def action_resume(self) -> None:
        c = self.current()
        if not c:
            return
        try:
            with self.suspend():
                resume(c)
        except Exception as e:
            self.notify(str(e), severity="error")
        self.reload(c.key)

    def action_rename(self) -> None:
        c = self.current()
        if not c:
            return

        def done(title: str | None) -> None:
            if not title or title == c.title:
                return
            try:
                store.rename(c, title)
            except Exception as e:
                self.notify(f"Rename failed: {e}", severity="error")
                return
            self.reload(c.key)
            self.notify(f"Renamed to “{title}”", timeout=3)

        self.push_screen(Ask(f"Rename {c.tool} conversation", c.title), done)

    def action_cleanup(self) -> None:
        self.push_screen(Orphans(), lambda _: self.reload())

    def action_help(self) -> None:
        if not isinstance(self.screen, Help):
            self.push_screen(Help())

    def action_mark(self) -> None:
        c = self.current()
        if not c:
            return
        self.marks ^= {c.key}
        row = self.query_one("#table", DataTable).cursor_row
        self.fill(self.row_key(self.rows[min(row + 1, len(self.rows) - 1)]))

    def action_copy(self) -> None:
        c = self.current()
        if c:
            line = command_line(c)
            self.copy_to_clipboard(line)
            self.notify(f"Copied: {line}", timeout=5)

    def action_trash(self) -> None:
        cur = self.current()
        targets = [c for c in self.all if c.key in self.marks] or ([cur] if cur else [])
        if not targets:
            return
        if len(targets) == 1 and targets[0].live:
            self.notify("That conversation is in use; close its session, or wait a minute after its last message.",
                        severity="warning")
            return
        what = (f"this {targets[0].tool} conversation" if len(targets) == 1 else f"these {len(targets)} conversations")
        names = "\n".join("“" + one_line(c.title, 60) + "”" for c in targets[:8])
        names += f"\n… and {len(targets) - 8} more" if len(targets) > 8 else ""
        try:
            plans = [(c, store.SOURCES[c.tool].trash_plan(c)) for c in targets]
        except Exception as e:  # a store convo will not write to
            self.notify(f"Cannot trash: {e}", severity="error")
            return
        if len(targets) == 1:
            names += "\n\nThis moves:\n" + moves(plans[0][1], targets[0].id)
        else:
            names += f"\n\nThis moves {sum(len(trash.preview(p, c.id)) for c, p in plans)} files, folders and index records."
        note = ("\n\nIf a tool is running (for example its VS Code panel), it may keep listing a conversation until it restarts."
                if any(c.tool in ("agy", "codex") for c in targets) else "")

        def done(ok: bool | None) -> None:
            if not ok:
                return
            failed = []
            for c in targets:
                try:
                    trash.put(c)
                except Exception as e:
                    failed.append(f"{one_line(c.title, 40)}: {e}")
            self.marks.clear()
            self.reload()
            moved = len(targets) - len(failed)
            if moved:
                self.notify(f"Moved {moved} to trash  (u = undo, one at a time)" if len(targets) > 1
                            else f"Moved to trash: {one_line(targets[0].title, 50)}  (u = undo)", timeout=5)
            for line in failed:
                self.notify(f"Could not trash {line}", severity="error")
            self.offer_project_cleanup(sorted({c.cwd for c in targets if c.cwd}))

        self.push_screen(Confirm(f"Move {what} to the trash?\n\n{names}\n\nu undoes it.{note}"), done)

    def offer_project_cleanup(self, folders: list[str]) -> None:
        """After the last conversation of a project is trashed, offer to trash
        what Claude still keeps for that project. One question per project."""
        left = {c.cwd for c in self.all}
        for i, cwd in enumerate(folders):
            plan = None if cwd in left else claude.project_state(cwd)
            if not plan:
                continue

            def done(ok: bool | None, cwd=cwd, plan=plan, rest=folders[i + 1:]) -> None:
                if ok:
                    try:
                        trash.stash("claude", "project", f"project state: {short_path(cwd)}", cwd, plan)
                        self.notify(f"Moved project state to trash: {short_path(cwd)}  (u = undo)", timeout=5)
                    except Exception as e:
                        self.notify(f"Could not trash the project state: {e}", severity="error")
                    self.paint()
                self.offer_project_cleanup(rest)

            self.push_screen(Confirm(
                f"That was the last conversation for\n{short_path(cwd)}\n\nAlso move what Claude still keeps for this project "
                f"to the trash?\nThat is its memory notes and its trust and tool-permission settings.\n\n"
                f"{moves(plan, cwd)}\n\nMost people answer no."), done)
            return

    def action_undo(self) -> None:
        ents = trash.entries()
        if not ents:
            self.notify("Trash is empty", timeout=2)
            return
        d, m = ents[-1]
        try:
            trash.restore(d)
        except Exception as e:
            self.notify(f"Could not restore: {e}", severity="error")
            return
        self.reload(f"{m['tool']}:{m['id']}")
        self.notify(f"Restored: {one_line(m['title'], 50)}", timeout=4)

    def export(self, c: Conv, fmt: str = "md") -> None:
        try:
            path = store.export(c, fmt)
        except Exception as e:
            self.notify(f"Export failed: {e}", severity="error")
            return
        self.notify(f"Exported to {short_path(str(path))}", timeout=6)

    def action_export(self, fmt: str) -> None:
        c = self.current()
        if c:
            self.export(c, fmt)
