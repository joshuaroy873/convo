"""A plain-text summary of what is on disk."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .common import Conv, human_size, short_path, when

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SPARK = "▁▂▃▄▅▆▇█"
_DAY = timedelta(days=1)
BAR = 10


def _bar(n: int, top: int, width: int = 28) -> str:
    return "█" * max(1 if n else 0, round(width * n / top)) if top else ""


def report(convs: list[Conv], trashed: int = 0) -> str:
    if not convs:
        return "No conversations."
    out = ["Totals", f"  conversations   {len(convs)}", f"  messages        {sum(c.nmsg for c in convs)}",
           f"  on disk         {human_size(sum(c.size for c in convs))}",
           f"  first to last   {when(min(c.created for c in convs), True)}  →  {when(max(c.updated for c in convs), True)}"]
    if trashed:
        out.append(f"  in the trash    {trashed}")
    out += ["", "By tool", f"  {'tool':8}{'via':8}{'chats':>6}{'messages':>10}{'disk':>9}"]
    groups: dict[tuple[str, str], list[Conv]] = {}
    for c in convs:
        groups.setdefault((c.tool, c.origin), []).append(c)
    for (tool, via), cs in sorted(groups.items()):
        out.append(f"  {tool:8}{via:8}{len(cs):>6}{sum(c.nmsg for c in cs):>10}{human_size(sum(c.size for c in cs)):>9}")
    out += ["", "Busiest projects"]
    by_dir: dict[str, list[Conv]] = {}
    for c in convs:
        by_dir.setdefault(c.cwd or "(none)", []).append(c)
    for cwd, cs in sorted(by_dir.items(), key=lambda kv: -sum(c.nmsg for c in kv[1]))[:8]:
        out.append(f"  {len(cs):>3} chats {sum(c.nmsg for c in cs):>6} messages  {short_path(cwd)}")
    # each conversation counts once, at the time it was last active
    last = [datetime.fromtimestamp(c.updated) for c in convs]
    days, hours = Counter(d.weekday() for d in last), Counter(d.hour for d in last)
    out += ["", "Last active, by weekday"]
    out += [f"  {DAYS[i]} {days[i]:>4} {_bar(days[i], max(days.values()))}" for i in range(7)]
    out += ["", "Last active, by time of day"]
    blocks = [(f"{h:02d}–{h + 3:02d}h", sum(hours[x] for x in range(h, h + 3))) for h in range(0, 24, 3)]
    top = max(n for _, n in blocks)
    out += [f"  {label} {n:>4} {_bar(n, top)}" for label, n in blocks]
    return "\n".join(out)


def _bars(count: Counter, styles: dict[str, str], total: int) -> list[Text]:
    names, digits = max(map(len, styles)), len(str(max(count.values(), default=0)))
    rows = []
    for name, style in styles.items():
        n = count[name]
        full = max(1 if n else 0, round(BAR * n / total)) if total else 0
        rows.append(Text.assemble((f"{name:{names}} ", style), ("█" * full, style), ("░" * (BAR - full), "dim"),
                                  (f" {n:>{digits}}", "bold")))
    return rows


def panel(convs: list[Conv], tools: dict[str, str], vias: dict[str, str], trashed: int = 0, width: int = 100,
          now: float | None = None) -> Text:
    """Three rows for the top of the list: share per tool, share per origin, totals, recent activity.

    `tools` and `vias` map each name to its colour. Columns that do not fit are left out from the
    right, and a very narrow screen gets one line of counts.
    """
    now = datetime.now().timestamp() if now is None else now
    total = len(convs)
    by_tool = Counter(c.tool for c in convs)
    # each conversation counts once, on the day it was last active
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    ago = Counter(int((midnight - c.updated) // 86400) + 1 if c.updated < midnight else 0 for c in convs)
    days = [ago[d] for d in range(13, -1, -1)]
    top = max(days) or 1
    spark = "".join(SPARK[0] if not n else SPARK[max(1, round(n / top * 7))] for n in days)
    columns = [
        _bars(by_tool, tools, total),
        _bars(Counter(c.origin for c in convs), vias, total),
        [Text(f"{total:,} chats"), Text(f"{sum(c.nmsg for c in convs):,} messages"),
         Text(f"{human_size(sum(c.size for c in convs))} on disk")],
        [Text.assemble(("last 14 days  ", "dim"), (spark, "bold")), Text(f"today {ago[0]} · this week {sum(days[-7:])}"),
         Text(f"trash {trashed}")],
    ]
    widths = [max(cell.cell_len for cell in col) for col in columns]
    fit = len(columns)
    while fit and sum(widths[:fit]) + 3 * (fit - 1) > width:
        fit -= 1
    if fit < 3:
        line = Text(f"{total} chats")
        for tool, style in tools.items():
            line.append("  ").append(f"{tool} {by_tool[tool]}", style)
        return line.append(f"  trash {trashed}" if trashed else "")
    out = Text()
    for row in range(3):
        out.append("\n" if row else "")
        for i in range(fit):
            cell = columns[i][row] if row < len(columns[i]) else Text()
            out.append(" │ " if i else "", "dim").append_text(cell).append(" " * (widths[i] - cell.cell_len) if i < fit - 1 else "")
    return out


# --- the stats screen ---

ACCENT = "steel_blue1"
EIGHTHS = " ▁▂▃▄▅▆▇█"


def _box(title: str, body: RenderableType, height: int | None = None) -> Panel:
    return Panel(body, title=f"[bold]{title}[/]", title_align="left", border_style="grey50", padding=(0, 1), height=height)


def _side_by_side(boxes: list[RenderableType], width: int) -> RenderableType:
    if width < 100:
        return Group(*boxes)
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in boxes:
        grid.add_column(ratio=1)
    grid.add_row(*boxes)
    return grid


def _shares(groups: dict[str, list[Conv]], styles: dict[str, str], total: int, bar: int = 14) -> Table:
    """One row per name: a bar for its share of the conversations, then chats, share, messages and disk."""
    t = Table.grid(padding=(0, 2))
    t.add_column()
    t.add_column()
    for _ in range(4):
        t.add_column(justify="right")
    t.add_row("", "", *(Text(h, style="dim") for h in ("chats", "share", "messages", "disk")))
    for name, style in styles.items():
        cs = groups.get(name, [])
        full = max(1 if cs else 0, round(bar * len(cs) / total)) if total else 0
        t.add_row(Text(name, style=style), Text.assemble(("█" * full, style), ("░" * (bar - full), "grey30")),
                  Text(str(len(cs)), style="bold"), f"{100 * len(cs) / total:.0f}%" if total else "-",
                  f"{sum(c.nmsg for c in cs):,}", human_size(sum(c.size for c in cs)))
    return t


def _hbars(rows: list[tuple[str, int]], bar: int = 26, style: str = ACCENT) -> Table:
    t = Table.grid(padding=(0, 1))
    top = max((n for _, n in rows), default=0) or 1
    for label, n in rows:
        t.add_row(Text(label, style="dim"), Text.assemble(("█" * max(1 if n else 0, round(bar * n / top)), style),
                                                         (f" {n}", "bold") if n else ("·", "dim")))
    return t


def _columns(days: list[int], first_day: datetime, height: int = 5) -> Text:
    """A small column chart, one column per day, oldest on the left."""
    top = max(days) or 1
    out = Text()
    for level in range(height, 0, -1):
        out.append(f"{top if level == height else '':>4} ", "dim")
        for n in days:
            eighths = round(n / top * height * 8) - (level - 1) * 8
            out.append(EIGHTHS[max(0, min(8, eighths)) if n else 0] * 2 + " ", ACCENT)
        out.append("\n")
    axis = [" "] * (len(days) * 3)
    for i in range(0, len(days), 7):
        label = (first_day.replace(hour=12) + _DAY * i).strftime("%d %b")
        axis[i * 3:i * 3 + len(label)] = label
    return out.append("     " + "".join(axis).rstrip(), "dim")


def view(convs: list[Conv], tools: dict[str, str], vias: dict[str, str], trashed: int = 0, width: int = 110,
         now: float | None = None) -> RenderableType:
    """What the stats screen shows: the same facts as report(), drawn as boxes and bars."""
    if not convs:
        return Text("No conversations.", style="dim")
    now = datetime.now().timestamp() if now is None else now
    total = len(convs)
    tiles = [(f"{total:,}", "chats"), (f"{sum(c.nmsg for c in convs):,}", "messages"),
             (human_size(sum(c.size for c in convs)), "on disk"), (f"{trashed:,}", "in the trash"),
             (when(min(c.created for c in convs)).replace(" today", ""), "first started"),
             (when(max(c.updated for c in convs)), "last active")]
    head = Table.grid(expand=True, padding=(0, 1))
    per_row = 6 if width >= 100 else 3
    for _ in range(per_row):
        head.add_column(ratio=1)
    boxes = [Panel(Align.center(Text.assemble((big, "bold"), "\n", (small, "dim"), justify="center")), border_style="grey50")
             for big, small in tiles]
    for i in range(0, len(boxes), per_row):
        head.add_row(*boxes[i:i + per_row])

    by_tool: dict[str, list[Conv]] = {}
    by_via: dict[str, list[Conv]] = {}
    by_dir: dict[str, list[Conv]] = {}
    for c in convs:
        by_tool.setdefault(c.tool, []).append(c)
        by_via.setdefault(c.origin, []).append(c)
        by_dir.setdefault(c.cwd or "(no folder)", []).append(c)

    # each conversation counts once, at the time it was last active
    last = [datetime.fromtimestamp(c.updated) for c in convs]
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    span = max(7, min(30, (width - 10) // 3))  # as many days as fit, a month at most
    per_day = Counter((today.date() - d.date()).days for d in last)
    days = [per_day[i] for i in range(span - 1, -1, -1)]
    weekdays, hours = Counter(d.weekday() for d in last), Counter(d.hour for d in last)

    projects = Table.grid(padding=(0, 2))
    busiest = sorted(by_dir.items(), key=lambda kv: -sum(c.nmsg for c in kv[1]))[:8]
    most = max((sum(c.nmsg for c in cs) for _, cs in busiest), default=0) or 1
    for cwd, cs in busiest:
        n = sum(c.nmsg for c in cs)
        mix = Text()
        for tool, style in tools.items():  # the bar is split by which tool the messages were with
            mix.append("█" * round(30 * sum(c.nmsg for c in cs if c.tool == tool) / most), style)
        path = short_path(cwd)
        projects.add_row(Text(path if len(path) <= 44 else "…" + path[-43:]), mix or Text("▏", style="dim"),
                         Text(f"{n:,} messages", style="bold"), Text(f"{len(cs)} chats", style="dim"))

    return Group(
        head,
        _side_by_side([_box("By tool", _shares(by_tool, tools, total)),
                       _box("By where it was started", _shares(by_via, vias, total))], width),
        _box(f"Conversations by the day they were last active, past {span} days", _columns(days, today - _DAY * (span - 1))),
        _side_by_side([_box("By weekday", _hbars([(DAYS[i], weekdays[i]) for i in range(7)]), height=10),
                       _box("By time of day", _hbars([(f"{h:02d}–{h + 3:02d}h", sum(hours[x] for x in range(h, h + 3)))
                                                      for h in range(0, 24, 3)]), height=10)], width),
        _box("Busiest projects, by messages (bar coloured by tool)", projects),
    )
