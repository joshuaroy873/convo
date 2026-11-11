"""The filter language of the list: plain words plus tool:, via:, dir: and date: terms.

    tool:claude,codex   -tool:agy   tool:claude,!codex
    via:vscode          dir:backend,!test
    date:today  date:yesterday  date:week  date:month  date:<2d  date:>1w
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .common import Conv

TOOLS = ("claude", "codex", "agy")
VIAS = ("cli", "vscode", "ide")
_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}
_REL = re.compile(r"([<>])(\d+)(mo|m|h|d|w|y)$")


@dataclass
class Query:
    words: list[str] = field(default_factory=list)
    tools: set[str] = field(default_factory=set)
    not_tools: set[str] = field(default_factory=set)
    vias: set[str] = field(default_factory=set)
    not_vias: set[str] = field(default_factory=set)
    dirs: list[str] = field(default_factory=list)
    not_dirs: list[str] = field(default_factory=list)
    since: float | None = None
    until: float | None = None

    def match(self, c: Conv) -> bool:
        if (self.tools and c.tool not in self.tools) or c.tool in self.not_tools:
            return False
        if (self.vias and c.origin not in self.vias) or c.origin in self.not_vias:
            return False
        cwd = c.cwd.lower()
        if (self.dirs and not any(d in cwd for d in self.dirs)) or any(d in cwd for d in self.not_dirs):
            return False
        if (self.since is not None and c.updated < self.since) or (self.until is not None and c.updated >= self.until):
            return False
        hay = f"{c.tool} {c.origin} {c.title} {c.cwd} {c.id} {c.branch}".lower()
        return all(w in hay for w in self.words)


def _pick(value: str, known: tuple[str, ...]) -> str | None:
    """A known name, or the one known name the value is a prefix of."""
    hits = [k for k in known if k == value] or [k for k in known if k.startswith(value)]
    return hits[0] if len(hits) == 1 else None


def _date(value: str, now: float) -> tuple[float | None, float | None] | None:
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "today":
        return midnight.timestamp(), None
    if value == "yesterday":
        return (midnight - timedelta(days=1)).timestamp(), midnight.timestamp()
    if value in ("week", "month"):
        return now - _UNIT["w" if value == "week" else "mo"], None
    m = _REL.match(value)
    if not m:
        return None
    edge = now - int(m.group(2)) * _UNIT[m.group(3)]
    return (edge, None) if m.group(1) == "<" else (None, edge)


def parse(text: str, now: float | None = None) -> Query:
    """Anything that is not a well-formed term is treated as a plain word."""
    now = time.time() if now is None else now
    q = Query()
    for token in text.lower().split():
        negate = token.startswith("-") and ":" in token
        key, sep, value = token[1:].partition(":") if negate else token.partition(":")
        if key == "agent":
            key = "tool"
        if not sep or not value or key not in ("tool", "via", "dir", "date"):
            q.words.append(token)
            continue
        if key == "date":
            span = _date(value, now)
            if span is None or negate:
                q.words.append(token)
            else:
                q.since, q.until = span
            continue
        yes, no = [], []
        for part in filter(None, value.split(",")):
            exclude = negate or part.startswith("!")
            part = part.lstrip("!")
            if key != "dir":
                part = _pick(part, TOOLS if key == "tool" else VIAS)
            if not part:
                yes = no = None
                break
            (no if exclude else yes).append(part)
        if yes is None:
            q.words.append(token)
        elif key == "tool":
            q.tools.update(yes)
            q.not_tools.update(no)
        elif key == "via":
            q.vias.update(yes)
            q.not_vias.update(no)
        else:
            q.dirs += yes
            q.not_dirs += no
    return q
