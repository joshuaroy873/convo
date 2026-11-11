"""Shared data model and small helpers."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HOME = Path.home()


def env_path(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or default).expanduser()


DATA = env_path("CONVO_DATA", "~/.local/share/convo")
CACHE = env_path("CONVO_CACHE", "~/.cache/convo")
EXPORTS = env_path("CONVO_EXPORT_DIR", "~/Documents/chat-exports")


@dataclass
class Conv:
    tool: str  # claude | codex | agy
    id: str
    title: str
    cwd: str
    origin: str  # cli | vscode | ide
    created: float
    updated: float
    size: int
    nmsg: int
    paths: list[str]
    first: str = ""
    branch: str = ""
    archived: bool = False
    live: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.tool}:{self.id}"


@dataclass
class Msg:
    role: str  # user | assistant | tool | system
    text: str
    ts: float | None = None
    kind: str = "text"  # text | call | result | thinking | note
    name: str = ""


def parse_iso(s) -> float | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def short_path(p: str) -> str:
    home = str(HOME)
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def one_line(s: str, n: int = 200) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def human_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit in "BK" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


def when(ts: float | None, full: bool = False) -> str:
    if not ts:
        return "-"
    d = datetime.fromtimestamp(ts)
    if full:
        return d.strftime("%Y-%m-%d %H:%M")
    return d.strftime("%H:%M today") if d.date() == datetime.now().date() else d.strftime("%Y-%m-%d")


def slug(s: str, n: int = 50) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:n].strip("-") or "untitled"


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


# --- per-file scan cache -------------------------------------------------
# Scanning a transcript for its summary means parsing the whole file, so the
# result is cached against the file's mtime and size.

_CACHE_VER = 6
_cache: dict | None = None
_dirty = False


def _cache_file() -> Path:
    return CACHE / "scan.json"


def recently_written(paths, secs: float = 90) -> bool:
    """Whether any of the files (or a SQLite write-ahead log beside one) changed
    in the last few seconds, which is the sign of a tool still working on it."""
    now = time.time()
    for p in paths:
        for cand in (p, f"{p}-wal"):
            try:
                if now - os.stat(cand).st_mtime < secs:
                    return True
            except OSError:
                pass
    return False


def cached_scan(path: Path, fn, also: tuple = ()):
    """Return fn(path), reusing the cached result while the file is unchanged.
    `also` names companion files (a SQLite write-ahead log) that count as part of it."""
    global _cache, _dirty
    if _cache is None:
        try:
            raw = json.loads(_cache_file().read_text())
            _cache = raw["files"] if raw.get("ver") == _CACHE_VER else {}
        except (OSError, ValueError, KeyError):
            _cache = {}
    st = path.stat()
    sig = [st.st_mtime_ns, st.st_size]
    for extra in also:
        try:
            es = os.stat(extra)
            sig += [es.st_mtime_ns, es.st_size]
        except OSError:
            pass
    hit = _cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    data = fn(path)
    _cache[str(path)] = [sig, data]
    _dirty = True
    return data


def save_cache() -> None:
    global _dirty
    if not _dirty or _cache is None:
        return
    live = {k: v for k, v in _cache.items() if os.path.exists(k)}
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = _cache_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"ver": _CACHE_VER, "files": live}))
        tmp.replace(_cache_file())
        _dirty = False
    except OSError:
        pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
