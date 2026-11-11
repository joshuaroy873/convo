"""A plain-text summary of what is on disk."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from rich.text import Text

from .common import Conv, human_size, short_path, when

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SPARK = "▁▂▃▄▅▆▇█"
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
