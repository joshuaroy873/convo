"""Loads conversations from every tool, and exports them."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .common import EXPORTS, Conv, Msg, save_cache, short_path, slug, when
from .sources import SOURCES


def load_all() -> tuple[list[Conv], list[str]]:
    convs, errors = [], []
    for name, mod in SOURCES.items():
        try:
            convs += mod.list_convs()
        except Exception as e:  # one broken store should not hide the others
            errors.append(f"{name}: {e}")
    save_cache()
    return convs, errors


def messages(conv: Conv) -> list[Msg]:
    return SOURCES[conv.tool].messages(conv)


def rename(conv: Conv, title: str) -> None:
    SOURCES[conv.tool].rename(conv, title)


def resolve(convs: list[Conv], ref: str) -> Conv:
    ref = ref.split(":", 1)[-1] if ref.split(":", 1)[0] in SOURCES else ref
    hits = [c for c in convs if c.id.startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"no conversation matches {ref!r}" if not hits else f"{ref!r} is ambiguous ({len(hits)} matches)")


def text_of(conv: Conv) -> str:
    """Everything said in a conversation, tool output included, as one string."""
    return "\n".join(m.text for m in messages(conv))


def search(convs: list[Conv], query: str) -> list[Conv]:
    """Conversations matching a query. A transcript is only read when the
    title and project did not already settle the question."""
    from .query import parse
    q = parse(query)
    out = []
    for c in convs:
        if not q.terms(c):
            continue
        if q.match(c):
            out.append(c)
        elif q.words:
            try:
                if q.match(c, text_of(c).lower()):
                    out.append(c)
            except Exception:  # an unreadable transcript simply does not match
                pass
    return out


def find(convs: list[Conv], text: str) -> Conv:
    """A conversation by id prefix, or else the most recent one matching a query."""
    ref = text.split(":", 1)[-1] if text.split(":", 1)[0] in SOURCES else text
    hits = [c for c in convs if c.id.startswith(ref)]
    if len(hits) != 1:
        hits = sorted(search(convs, text), key=lambda c: -c.updated)
    if not hits:
        raise SystemExit(f"no conversation matches {text!r}")
    return hits[0]


# --- export --------------------------------------------------------------

_LABEL = {"claude": "Claude", "codex": "Codex", "agy": "Antigravity"}


def to_markdown(conv: Conv, msgs: list[Msg], tools: bool = False) -> str:
    out = [f"# {conv.title}", "",
           f"- Tool: {_LABEL[conv.tool]} ({conv.origin})",
           f"- Project: `{short_path(conv.cwd) or '-'}`",
           f"- Session: `{conv.id}`",
           f"- Created: {when(conv.created, True)} · Updated: {when(conv.updated, True)}", ""]
    for m in msgs:
        if m.kind in ("result", "thinking") and not tools:
            continue
        stamp = f" · {when(m.ts, True)}" if m.ts else ""
        if m.kind == "call":
            fence = "````" if "```" in m.text else "```"
            brief = m.text[:160].replace("\n", " ")
            out += [f"**{m.name}**", "", fence, m.text, fence, ""] if tools else [f"> ⚙ {m.name}: `{brief}`", ""]
        elif m.kind == "result":
            fence = "````" if "```" in m.text else "```"
            out += ["<details><summary>output</summary>", "", fence, m.text, fence, "", "</details>", ""]
        elif m.kind == "thinking":
            out += ["<details><summary>thinking</summary>", "", m.text, "", "</details>", ""]
        elif m.kind == "note":
            out += [f"*{m.text.strip()}*", ""]
        else:
            who = "You" if m.role == "user" else _LABEL[conv.tool]
            out += [f"## {who}{stamp}", "", m.text.strip(), ""]
    return "\n".join(out)


def to_json(conv: Conv, msgs: list[Msg]) -> str:
    return json.dumps({
        "tool": conv.tool, "origin": conv.origin, "id": conv.id, "title": conv.title, "cwd": conv.cwd,
        "created": conv.created, "updated": conv.updated,
        "messages": [{"role": m.role, "kind": m.kind, "name": m.name, "ts": m.ts, "text": m.text} for m in msgs],
    }, ensure_ascii=False, indent=1)


def export(conv: Conv, fmt: str = "md", dest: Path | None = None, tools: bool = False) -> Path:
    msgs = messages(conv)
    body = to_json(conv, msgs) if fmt == "json" else to_markdown(conv, msgs, tools)
    if dest is None:
        day = time.strftime("%Y-%m-%d", time.localtime(conv.updated))
        base = f"{day}-{conv.tool}-{slug(conv.title)}-{conv.id[:8]}"
        dest = EXPORTS / f"{base}.{fmt}"
        n = 1
        while dest.exists():  # an earlier export is never overwritten
            n += 1
            dest = EXPORTS / f"{base}-{n}.{fmt}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body)
    return dest
