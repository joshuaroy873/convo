"""A plain-text summary of what is on disk."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .common import Conv, human_size, short_path, when

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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
