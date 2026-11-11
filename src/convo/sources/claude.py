"""Claude Code sessions: ~/.claude/projects/<encoded cwd>/<session id>.jsonl"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from ..common import Conv, Msg, cached_scan, env_path, one_line, parse_iso, pid_alive, recently_written

ROOT = env_path("CLAUDE_CONFIG_DIR", "~/.claude")
# Claude gives each session a scratch folder here, keyed by user id rather than by config home.
SCRATCH = Path(tempfile.gettempdir()) / f"claude-{os.getuid()}"

_BLOCKS = re.compile(
    r"<(ide_opened_file|ide_selection|system-reminder|local-command-stdout|local-command-stderr|"
    r"local-command-caveat|command-message|command-args|command-name|task-notification)>.*?</\1>",
    re.S,
)
_CMD = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def _prompt(raw: str) -> tuple[str, bool]:
    """Readable form of a user turn, and whether it was only a bare slash command."""
    rest = re.sub(r"</?pasted_content[^>]*>", "", _BLOCKS.sub("", raw)).strip()
    m = _CMD.search(raw)
    if not m:
        return rest, False
    a = _ARGS.search(raw)
    args = a.group(1).strip() if a else ""
    cmd = (m.group(1).strip() + " " + args).strip()
    return (cmd + ("\n" + rest if rest else "")), not (args or rest)


def _user_text(content) -> str | None:
    """Text the user typed, or None when the entry only carries tool results."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text") or "")
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts) if parts else None


def _encode(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _scan(path: Path) -> dict:
    d = dict(custom="", ai="", summary="", first="", nmsg=0, cwd="", entry="", created=None, updated=None, branch="")
    proj = path.parent.name
    with open(path, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "custom-title":
                d["custom"] = o.get("customTitle") or ""
            elif t == "ai-title":
                d["ai"] = o.get("aiTitle") or d["ai"]
            elif t == "summary":
                d["summary"] = o.get("summary") or d["summary"]
            elif t in ("user", "assistant"):
                if o.get("isSidechain"):
                    continue
                ts = parse_iso(o.get("timestamp"))
                if ts:
                    d["created"] = d["created"] or ts
                    d["updated"] = ts
                cwd = o.get("cwd")
                # The session belongs to the directory it was launched from, which is
                # what the project folder name encodes; later `cd`s do not move it.
                if cwd and (not d["cwd"] or (_encode(d["cwd"]) != proj and _encode(cwd) == proj)):
                    d["cwd"] = cwd
                d["entry"] = d["entry"] or o.get("entrypoint") or ""
                d["branch"] = o.get("gitBranch") or d["branch"]
                msg = o.get("message") or {}
                if t == "user":
                    if o.get("isMeta"):
                        continue
                    raw = _user_text(msg.get("content"))
                    if raw is None:
                        continue
                    text, bare = _prompt(raw)
                    if text and not bare and not o.get("isCompactSummary"):
                        d["nmsg"] += 1
                        d["first"] = d["first"] or one_line(text, 300)
                else:
                    c = msg.get("content")
                    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" and b.get("text") for b in c):
                        d["nmsg"] += 1
    return d


def live_ids() -> set[str]:
    out = set()
    for f in (ROOT / "sessions").glob("*.json"):
        try:
            o = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if pid_alive(o.get("pid")) and o.get("sessionId"):
            out.add(o["sessionId"])
    return out


def _sidecar(path: Path) -> Path:
    """Claude also keeps a renamed session's title in a small file beside the transcript."""
    return path.with_suffix("") / "custom-title.json"


def _sidecar_title(path: Path) -> str:
    try:
        title = json.loads(_sidecar(path).read_text()).get("customTitle")
    except (OSError, ValueError, AttributeError):
        return ""
    return title if isinstance(title, str) else ""


def is_live(conv: Conv) -> bool:
    return conv.id in live_ids()


def list_convs() -> list[Conv]:
    live = live_ids()
    out = []
    for path in sorted((ROOT / "projects").glob("*/*.jsonl")):
        d = cached_scan(path, _scan)
        st = path.stat()
        sid = path.stem
        title = _sidecar_title(path) or d["custom"] or d["ai"] or d["summary"] or one_line(d["first"], 80) or "(no prompt)"
        out.append(Conv(
            tool="claude", id=sid, title=title, cwd=d["cwd"],
            origin="vscode" if "vscode" in d["entry"] else "cli",
            created=d["created"] or st.st_mtime, updated=d["updated"] or st.st_mtime,
            size=st.st_size, nmsg=d["nmsg"], paths=[str(path)], first=d["first"], live=sid in live,
            branch="" if d["branch"] == "HEAD" else d["branch"],
        ))
    return out


def _call_summary(inp) -> str:
    if isinstance(inp, dict):
        for k in ("command", "file_path", "pattern", "query", "url", "prompt", "description", "skill"):
            if isinstance(inp.get(k), str):
                return inp[k]
    return json.dumps(inp, ensure_ascii=False)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            (b.get("text") or "") if b.get("type") == "text" else f"[{b.get('type')}]"
            for b in content if isinstance(b, dict)
        )
    return ""


def messages(conv: Conv) -> list[Msg]:
    out = []
    with open(conv.paths[0], "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
                continue
            ts = parse_iso(o.get("timestamp"))
            content = (o.get("message") or {}).get("content")
            if t == "user":
                raw = _user_text(content)
                if raw is not None:
                    text, bare = _prompt(raw)
                    if text:
                        if o.get("isCompactSummary"):
                            out.append(Msg("system", text, ts, "note"))
                        else:
                            out.append(Msg("user", text, ts, "note" if bare else "text"))
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            out.append(Msg("tool", _result_text(b.get("content")), ts, "result"))
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and b.get("text"):
                        out.append(Msg("assistant", b["text"], ts))
                    elif bt == "thinking" and b.get("thinking"):
                        out.append(Msg("assistant", b["thinking"], ts, "thinking"))
                    elif bt == "tool_use":
                        out.append(Msg("assistant", _call_summary(b.get("input")), ts, "call", b.get("name") or "tool"))
    return out


def rename(conv: Conv, title: str) -> None:
    """Append a custom-title record, the same thing /rename writes."""
    path = Path(conv.paths[0])
    rec = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": conv.id},
                     ensure_ascii=False, separators=(",", ":"))  # Claude matches the compact form
    with open(path, "rb+") as f:
        f.seek(0, 2)
        lead = b""
        if f.tell():
            f.seek(-1, 2)
            lead = b"" if f.read(1) == b"\n" else b"\n"
        f.write(lead + rec.encode() + b"\n")
    side = _sidecar(path)
    if side.exists():  # keep the copy Claude reads first in step
        side.write_text(json.dumps({"customTitle": title}, ensure_ascii=False))


def trash_plan(conv: Conv) -> dict:
    path = Path(conv.paths[0])
    # Every path is built from the session id, never searched for, so nothing can widen onto a neighbour.
    files = [path, path.with_suffix(""), SCRATCH / path.parent.name / conv.id]
    files += [ROOT / sub / conv.id for sub in ("file-history", "session-env", "tasks")]
    files += sorted((ROOT / "security").glob(f"security_warnings_state_{conv.id}.*"))
    return {"files": [str(p) for p in files if p.exists()], "rows": [],
            "lines": [{"file": str(ROOT / "history.jsonl"), "key": "sessionId", "value": conv.id}]}


def settings_file() -> Path | None:
    """Claude's main settings file: inside a relocated config home, beside the default one."""
    for cand in (ROOT / ".claude.json", ROOT.parent / ".claude.json"):
        if cand.is_file():
            return cand
    return None


def project_state(cwd: str) -> dict | None:
    """What Claude still keeps for a project that has no conversations left:
    its folder (memory notes included) and its entry in the settings file."""
    folder = ROOT / "projects" / _encode(cwd)
    # the folder name is a lossy encoding, so one that still holds a transcript may belong to another path
    files = [str(folder)] if folder.is_dir() and not any(folder.glob("*.jsonl")) else []
    blobs = []
    settings = settings_file()
    try:
        if settings and cwd in (json.loads(settings.read_text()).get("projects") or {}):
            blobs = [{"kind": "json", "file": str(settings), "path": ["projects"], "id": cwd}]
    except (OSError, ValueError, AttributeError):
        pass
    return {"files": files, "rows": [], "lines": [], "blobs": blobs} if files or blobs else None


def _front(text: str) -> dict:
    """The few fields convo shows from a memory note's header."""
    out = {}
    if text.startswith("---"):
        for line in text.split("\n---", 1)[0].splitlines()[1:]:
            key, sep, value = line.strip().partition(":")
            if sep and key in ("name", "description", "type", "originSessionId"):
                out[key] = value.strip().strip('"')
    return out


def memories() -> list[dict]:
    """The notes Claude keeps between sessions, one folder per project."""
    known = {p.stem for p in (ROOT / "projects").glob("*/*.jsonl")}
    # a project folder's name does not decode back to its path, so look the path up among the projects Claude knows
    paths = {}
    settings = settings_file()
    try:
        paths = {_encode(p): p for p in (json.loads(settings.read_text()).get("projects") or {})} if settings else {}
    except (OSError, ValueError, AttributeError):
        pass
    out = []
    for folder in sorted((ROOT / "projects").glob("*/memory")):
        cwd = next((cached_scan(t, _scan)["cwd"] for t in sorted(folder.parent.glob("*.jsonl"))), "") \
            or paths.get(folder.parent.name, "")
        for path in sorted(folder.glob("*.md")):
            if path.name == "MEMORY.md":  # the index of the notes, not a note
                continue
            try:
                head = _front(path.read_text())
                st = path.stat()
            except OSError:
                continue
            origin = head.get("originSessionId", "")
            out.append({"path": str(path), "project": cwd or folder.parent.name, "name": head.get("name") or path.stem,
                        "type": head.get("type", ""), "description": head.get("description", ""), "size": st.st_size,
                        "updated": st.st_mtime, "origin": origin, "origin_gone": bool(origin) and origin not in known})
    return out


_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def strays() -> list[dict]:
    """Per-session state folders whose conversation no longer exists. Scratch
    folders and prompt-history rows are left out: other Claude configurations
    share the first, and the second is your own recall history."""
    known = {p.stem for p in (ROOT / "projects").glob("*/*.jsonl")} | live_ids()
    found: dict[str, list[Path]] = {}
    for sub in ("file-history", "session-env", "tasks"):
        for p in (ROOT / sub).glob("*"):
            if p.is_dir() and _UUID.match(p.name) and p.name not in known:
                found.setdefault(p.name, []).append(p)
    return [{"tool": "claude", "id": sid, "label": sid, "reason": "leftover files of a conversation that no longer exists",
             "plan": {"files": [str(p) for p in files], "rows": [], "lines": []}}
            for sid, files in sorted(found.items()) if not recently_written(files, 3600)]


def resume_cmd(conv: Conv) -> list[str]:
    return ["claude", "--resume", conv.id]
