"""Things worth cleaning up: conversations with nothing in them or whose
project is gone, and state the tools left behind for conversations that no
longer exist. Finding is read-only; removal goes through the trash."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import trash
from .common import Conv
from .sources import SOURCES, agy, claude


@dataclass
class Orphan:
    tool: str
    id: str
    label: str
    reason: str
    size: int
    conv: Conv | None = None
    plan: dict | None = None

    @property
    def key(self) -> str:
        return f"{self.tool}:{self.id}"


def _size(paths: list[str]) -> int:
    total = 0
    for p in map(Path, paths):
        try:
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        except OSError:
            pass
    return total


def find(convs: list[Conv]) -> list[Orphan]:
    out = []
    for c in convs:
        if c.live:
            continue
        if c.nmsg == 0:
            reason = "no messages"
        elif c.cwd and not os.path.isdir(c.cwd):
            reason = "project folder is gone"
        else:
            continue
        out.append(Orphan(c.tool, c.id, c.title, reason, c.size, conv=c))
    trashed = {(m["tool"], m["id"]): m["title"] for _, m in trash.entries()}
    for mod in (claude, agy):
        try:
            found = mod.strays()
        except Exception:  # an unreadable store has nothing safe to offer
            continue
        for s in found:
            again = trashed.get((s["tool"], s["id"]))
            out.append(Orphan(s["tool"], s["id"], again or s["label"],
                              "already in the trash, but the tool has listed it again" if again else s["reason"],
                              _size(s["plan"]["files"]), plan=s["plan"]))
    return out


def plan_of(o: Orphan) -> dict:
    return SOURCES[o.tool].trash_plan(o.conv) if o.conv else o.plan


def remove(o: Orphan) -> dict:
    """Move one orphan to the trash, where it can still be restored."""
    if o.conv:
        return trash.put(o.conv)
    return trash.stash(o.tool, o.id, f"leftover: {o.label}", "", o.plan)
