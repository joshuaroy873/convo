"""Regenerate docs/screenshot.png from made-up conversations.

    python docs/screenshot.py

Nothing real is read: a throwaway home folder is filled with invented Claude
Code, Codex and Antigravity sessions, and convo is pointed at that. Textual
draws the screen as an SVG; headless Firefox turns it into a PNG at twice the
size, so it stays sharp on a high-resolution display.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(tempfile.mkdtemp(prefix="convo-demo-"))
os.environ.update(HOME=str(HOME), CLAUDE_CONFIG_DIR=str(HOME / ".claude"), CODEX_HOME=str(HOME / ".codex"),
                  CONVO_AGY_HOME=str(HOME / ".gemini"), CONVO_DATA=str(HOME / "data"), CONVO_CACHE=str(HOME / "cache"),
                  CONVO_CONFIG=str(HOME / "config.toml"))
sys.path.insert(0, str(HERE.parent / "tests"))

from conftest import fld, jsonl, step  # noqa: E402
from convo.sources import agy, claude  # noqa: E402
from convo.tui import ConvoApp  # noqa: E402

NOW = time.time()
HOUR, DAY = 3600, 86400
# tool, via, title, project, branch, age in seconds, exchanges, first prompt
DEMO = [
    ("claude", "cli", "Fix flaky retry test in payment client", "projects/payments-api", "fix/retry-backoff", 40 * 60, 31,
     "test_retry_backoff fails about one run in five on CI. Find out why and fix it without loosening the assertion."),
    ("codex", "vscode", "Port CSV importer to async", "projects/data-pipeline", "async-import", 3 * HOUR, 58, "Port the importer"),
    ("agy", "ide", "Refactor auth middleware", "projects/dashboard", "", 5 * HOUR, 44, "Split the auth middleware"),
    ("claude", "vscode", "Add dark mode toggle to settings page", "projects/dashboard", "feature/dark-mode", 1 * DAY + 2 * HOUR, 22,
     "Add a dark mode toggle"),
    ("agy", "cli", "Why is the VPN tunnel down?", "", "", 1 * DAY + 6 * HOUR, 6, "The tunnel is down again"),
    ("codex", "cli", "Profile slow startup", "projects/payments-api", "main", 2 * DAY, 17, "Startup takes eight seconds"),
    ("claude", "cli", "Write migration for orders table", "projects/payments-api", "main", 4 * DAY, 12, "Write a migration"),
    ("claude", "vscode", "Explain the caching layer", "projects/data-pipeline", "main", 6 * DAY, 9, "Explain the cache"),
    ("agy", "ide", "Chart weekly signups by region", "projects/dashboard", "", 9 * DAY, 27, "Chart weekly signups"),
    ("codex", "vscode", "Review pull request 214", "projects/data-pipeline", "main", 13 * DAY, 8, "Review this pull request"),
    ("claude", "cli", "Set up pre-commit hooks", "projects/dotfiles", "main", 21 * DAY, 14, "Set up pre-commit"),
    ("agy", "ide", "Draft the release notes", "projects/payments-api", "", 34 * DAY, 11, "Draft release notes"),
]
FILLER = "Here is what I found and what I changed. " * 12


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def build() -> None:
    summaries = []
    for n, (tool, via, title, project, branch, age, turns, prompt) in enumerate(DEMO):
        cwd = HOME / project
        cwd.mkdir(parents=True, exist_ok=True)
        end = NOW - age
        stamps = [end - (turns - i) * 90 for i in range(turns)]
        sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"convo-demo-{n}"))
        if tool == "claude":
            base = {"cwd": str(cwd), "entrypoint": "claude-vscode" if via == "vscode" else "cli", "gitBranch": branch}
            recs = []
            for i, ts in enumerate(stamps):
                recs.append({"type": "user", "timestamp": iso(ts), **base,
                             "message": {"role": "user", "content": prompt if i == 0 else f"Follow-up {i}"}})
                recs.append({"type": "assistant", "timestamp": iso(ts + 30), **base,
                             "message": {"role": "assistant", "content": [{"type": "text", "text": FILLER * (3 + i % 5)}]}})
            recs.append({"type": "ai-title", "aiTitle": title})
            jsonl(claude.ROOT / "projects" / claude._encode(str(cwd)) / f"{sid}.jsonl", recs)
        elif tool == "codex":
            def item(ts, kind, text):
                return {"timestamp": iso(ts), "type": "event_msg",
                        "payload": {"type": "item_completed", "item": {"type": kind, "content": [{"type": "text", "text": text}]}}}

            recs = [{"type": "session_meta", "timestamp": iso(stamps[0]),
                     "payload": {"id": sid, "cwd": str(cwd), "originator": "codex_vscode" if via == "vscode" else "codex-tui",
                                 "git": {"branch": branch}, "timestamp": iso(stamps[0])}}]
            for i, ts in enumerate(stamps):
                recs += [item(ts, "UserMessage", prompt if i == 0 else f"Follow-up {i}"),
                         item(ts + 30, "AgentMessage", FILLER * (2 + i % 4))]
            day = time.strftime("%Y/%m/%d", time.gmtime(stamps[0]))
            jsonl(Path(os.environ["CODEX_HOME"]) / "sessions" / day / f"rollout-{iso(stamps[0]).replace(':', '-')}-{sid}.jsonl", recs)
            with open(Path(os.environ["CODEX_HOME"]) / "session_index.jsonl", "a") as f:
                f.write(json.dumps({"id": sid, "thread_name": title, "updated_at": iso(end)}) + "\n")
        else:
            sub = "antigravity" if via == "ide" else "antigravity-cli"
            (agy.ROOT / sub / "conversations").mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(agy.ROOT / sub / "conversations" / f"{sid}.db")
            con.execute("create table steps (idx integer primary key, step_type integer, step_payload blob)")
            rows = []
            for i, ts in enumerate(stamps):
                rows.append((2 * i, agy.USER, step(agy.USER, int(ts), fld(19, fld(2, prompt if i == 0 else f"Follow-up {i}")))))
                rows.append((2 * i + 1, agy.ASSISTANT, step(agy.ASSISTANT, int(ts + 30), fld(20, fld(1, FILLER * (4 + i % 6))))))
            con.executemany("insert into steps values (?,?,?)", rows)
            con.commit()
            con.close()
            summaries.append((sub, sid, title, json.dumps([cwd.as_uri()]), iso(end).replace("T", " ").replace("Z", "+00:00")))
    for sub in {s[0] for s in summaries}:
        con = sqlite3.connect(agy.ROOT / sub / "conversation_summaries.db")
        con.execute("create table conversation_summaries (conversation_id text primary key, title text, "
                    "workspace_uris text, last_modified_time text)")
        con.executemany("insert into conversation_summaries values (?,?,?,?)", [s[1:] for s in summaries if s[0] == sub])
        con.commit()
        con.close()
    old = NOW - DAY  # freshly written files would otherwise all show as in use
    for p in HOME.rglob("*"):
        os.utime(p, (old, old))


async def shoot(svg: Path) -> None:
    app = ConvoApp()
    async with app.run_test(size=(118, 24)) as pilot:
        await pilot.pause()
        app.save_screenshot(str(svg))


def to_png(svg: Path, png: Path, scale: int = 2) -> None:
    width, height = (round(float(v) * scale) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text()).groups())
    page = HOME / "page.html"
    page.write_text(f'<body style="margin:0;background:#000"><img src="{svg.as_uri()}" '
                    f'style="display:block;width:{width}px;height:{height}px"></body>')
    subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(HOME / "firefox"),
                    f"--window-size={width},{height}", "--screenshot", str(png), page.as_uri()],
                   check=True, capture_output=True, timeout=120)


if __name__ == "__main__":
    build()
    svg = HOME / "screenshot.svg"
    asyncio.run(shoot(svg))
    if shutil.which("firefox"):
        (HOME / "firefox").mkdir()
        to_png(svg, HERE / "screenshot.png")
        print(HERE / "screenshot.png")
    else:
        shutil.copy(svg, HERE / "screenshot.svg")
        print(f"{HERE / 'screenshot.svg'}  (install Firefox to get a PNG)")
    shutil.rmtree(HOME, ignore_errors=True)
