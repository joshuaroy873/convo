"""Regenerate docs/screenshot.png, or docs/demo.gif, from made-up conversations.

    python docs/screenshot.py           # the screenshot
    python docs/screenshot.py --demo    # the animated tour (needs Firefox and ffmpeg)

Nothing real is read: a throwaway home folder is filled with invented Claude
Code, Codex and Antigravity sessions, and convo is pointed at that. Textual
draws the screen as an SVG; headless Firefox turns it into a PNG at twice the
size, so it stays sharp on a high-resolution display.
"""

from __future__ import annotations

import asyncio
import io
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

from rich.text import Text  # noqa: E402
from textual.app import App  # noqa: E402
from textual.widgets import Static  # noqa: E402

from conftest import fld, jsonl, step  # noqa: E402
from convo.sources import agy, claude  # noqa: E402
from convo.tui import ConvoApp  # noqa: E402

NOW = time.time()
HOUR, DAY = 3600, 86400
TOUR_TURNS = (0, 1, 4, 6)  # which exchanges the conversation opened in the tour has: few enough to fit one screen
# tool, via, title, project, branch, age in seconds, exchanges, first prompt
DEMO = [
    ("claude", "cli", "Fix flaky retry test in payment client", "projects/payments-api", "fix/retry-backoff", 40 * 60, 31,
     "test_retry_backoff fails about one run in five on CI. Find out why and fix it without loosening the assertion."),
    ("codex", "vscode", "Port CSV importer to async", "projects/data-pipeline", "async-import", 3 * HOUR, 58, "Port the importer"),
    ("agy", "ide", "Refactor auth middleware", "projects/dashboard", "", 5 * HOUR, 44, "Split the auth middleware"),
    ("claude", "vscode", "Add dark mode toggle to settings page", "projects/dashboard", "feature/dark-mode", 1 * DAY + 2 * HOUR,
     len(TOUR_TURNS), "Add a dark mode toggle"),
    ("agy", "cli", "Why is the VPN tunnel down?", "", "", 1 * DAY + 6 * HOUR, 6, "The tunnel is down again"),
    ("codex", "cli", "Profile slow startup", "projects/payments-api", "main", 2 * DAY, 17, "Startup takes eight seconds"),
    ("claude", "cli", "Write migration for orders table", "projects/payments-api", "main", 4 * DAY, 12, "Write a migration"),
    ("claude", "vscode", "Explain the caching layer", "projects/data-pipeline", "main", 6 * DAY, 9, "Explain the cache"),
    ("agy", "ide", "Chart weekly signups by region", "projects/dashboard", "", 9 * DAY, 27, "Chart weekly signups"),
    ("codex", "vscode", "Review pull request 214", "projects/data-pipeline", "main", 13 * DAY, 8, "Review this pull request"),
    ("claude", "cli", "Set up pre-commit hooks", "projects/dotfiles", "main", 21 * DAY, 14, "Set up pre-commit"),
    ("agy", "ide", "Draft the release notes", "projects/payments-api", "", 34 * DAY, 11, "Draft release notes"),
]
ASKS = ["Which files does this touch?", "Run the tests and show me what fails.", "Keep the public interface as it is.",
        "Can you make that smaller?", "Add a test for the empty case.", "Explain the trade-off before changing anything.",
        "That looks right. Apply it.", "Summarise what changed."]
# weight, so sizes look like real sessions; left out of the conversation the tour opens
PAD = "\n\n" + "Details of the step, kept for the record. " * 40
OPENED = "Add dark mode toggle to settings page"
REPLIES = [
    "It touches three files:\n\n- `settings/page.tsx` renders the control\n- `theme/provider.tsx` holds the current choice\n"
    "- `theme/tokens.css` defines both palettes",
    "Two tests fail, both for the same reason: the stored preference is read before the provider mounts.\n\n"
    "```\nFAIL  theme/provider.test.tsx\n  ✕ restores the saved choice (41 ms)\n  ✕ follows the system setting (12 ms)\n```",
    "Agreed. The exported names stay the same; only the internals move:\n\n"
    "```ts\nexport function useTheme(): Theme {\n  return useContext(ThemeContext).theme;\n}\n```",
    "Yes. The helper was doing two jobs, so I split it and dropped the unused branch. It is 18 lines shorter.",
    "Added. With nothing saved it falls back to the system setting, and the test covers that path.",
    "The trade-off is start-up flicker against simplicity. Reading the preference early avoids a flash of the wrong "
    "palette but needs an inline script; reading it late is simpler and flickers once on a cold load.",
    "Applied, and the suite passes: 142 tests in 6.1 s.",
    "**Summary**\n\n1. A toggle in settings with three choices: light, dark, system\n2. The choice is saved and restored\n"
    "3. Both palettes come from one token file",
]


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

        def turn(i: int, opened: bool = title == OPENED) -> int:
            return TOUR_TURNS[i] if opened else i  # the conversation the tour opens is short enough to fit one screen

        def reply(i: int, weight: bool = title != OPENED) -> str:
            return REPLIES[turn(i) % len(REPLIES)] + (PAD * (i % 5) if weight else "")

        if tool == "claude":
            # the same fields Claude Code writes, so the real program can resume the file for the demo
            base = {"cwd": str(cwd), "entrypoint": "claude-vscode" if via == "vscode" else "cli", "gitBranch": branch,
                    "sessionId": sid, "isSidechain": False, "userType": "external", "version": "2.1.0"}
            recs, parent = [], None
            for i, ts in enumerate(stamps):
                for kind, at, message in (
                    ("user", ts, {"role": "user", "content": prompt if i == 0 else ASKS[turn(i) % len(ASKS)]}),
                    ("assistant", ts + 30, {"id": f"msg_{n:02d}{i:04d}", "type": "message", "role": "assistant", "model": "claude-opus-4-5",
                                            "content": [{"type": "text", "text": reply(i)}], "stop_reason": "end_turn",
                                            "usage": {"input_tokens": 10, "output_tokens": 10}})):
                    me = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{sid}-{len(recs)}"))
                    recs.append({"type": kind, "uuid": me, "parentUuid": parent, "timestamp": iso(at), **base, "message": message})
                    parent = me
            recs.append({"type": "ai-title", "aiTitle": title, "sessionId": sid})
            jsonl(claude.ROOT / "projects" / claude._encode(str(cwd)) / f"{sid}.jsonl", recs)
        elif tool == "codex":
            def item(ts, kind, text):
                return {"timestamp": iso(ts), "type": "event_msg",
                        "payload": {"type": "item_completed", "item": {"type": kind, "content": [{"type": "text", "text": text}]}}}

            recs = [{"type": "session_meta", "timestamp": iso(stamps[0]),
                     "payload": {"id": sid, "cwd": str(cwd), "originator": "codex_vscode" if via == "vscode" else "codex-tui",
                                 "git": {"branch": branch}, "timestamp": iso(stamps[0])}}]
            for i, ts in enumerate(stamps):
                recs += [item(ts, "UserMessage", prompt if i == 0 else ASKS[i % len(ASKS)]),
                         item(ts + 30, "AgentMessage", reply(i))]
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
                rows.append((2 * i, agy.USER, step(agy.USER, int(ts), fld(19, fld(2, prompt if i == 0 else ASKS[i % len(ASKS)])))))
                rows.append((2 * i + 1, agy.ASSISTANT, step(agy.ASSISTANT, int(ts + 30), fld(20, fld(1, reply(i))))))
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


def caption(svg: Path, key: str, what: str) -> None:
    """Write the key just pressed, and what it does, large in the top right corner of a frame."""
    text = svg.read_text()
    width = float(re.search(r'viewBox="0 0 ([\d.]+)', text).group(1))
    # no box behind it: a dark outline keeps the letters readable over whatever is underneath
    label = (f'<text x="{width - 28}" y="84" text-anchor="end" font-family="Fira Code, monospace" font-size="46" font-weight="bold" '
             f'fill="#fff" stroke="#111" stroke-width="7" stroke-linejoin="round" paint-order="stroke">{key}  {what}</text>')
    svg.write_text(text.replace("</svg>", label + "</svg>"))


def claude_screen(frames: Path, size: tuple[int, int], typed: str, wait: float = 12) -> list[Path]:
    """Record the real `claude --resume` on the invented conversation: its screen, then `typed` going into its prompt.

    It runs against a copy of the conversation in its own throwaway settings folder, with a made-up API key,
    so no real login is used, and what is typed is never sent. Empty if Claude Code or pyte is not installed.
    """
    try:
        import pyte
    except ImportError:
        return []
    if not shutil.which("claude"):
        return []
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

    from rich.console import Console
    from rich.style import Style

    n = next(i for i, d in enumerate(DEMO) if d[2] == OPENED)
    sid, cwd = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"convo-demo-{n}")), HOME / DEMO[n][3]
    settings = HOME / "capture"
    project = settings / "projects" / claude._encode(str(cwd))
    project.mkdir(parents=True)
    shutil.copy(claude.ROOT / "projects" / project.name / f"{sid}.jsonl", project)
    key = "sk-ant-demo-" + "0" * 40
    (settings / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5, "customApiKeyResponses": {"approved": [key[-20:]]},
        "projects": {str(cwd): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    env = {"HOME": str(HOME), "CLAUDE_CONFIG_DIR": str(settings), "ANTHROPIC_API_KEY": key, "PATH": os.environ["PATH"],
           "TERM": "xterm-256color", "LANG": "en_US.UTF-8", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    cols, rows = size
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe("claude", ["claude", "--resume", sid], env)
    tall = rows + 4  # room for the warning removed below, so the top of the screen is not scrolled away
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", tall, cols, 0, 0))
    screen = pyte.Screen(cols, tall)
    stream = pyte.ByteStream(screen)

    def listen(seconds: float) -> None:
        until = time.time() + seconds
        while time.time() < until:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    stream.feed(os.read(fd, 65536))
                except OSError:
                    return

    def colour(name: str) -> str | None:
        if name == "default":
            return None
        if re.fullmatch(r"[0-9a-f]{6}", name):
            return "#" + name
        return name.replace("brown", "yellow").replace("bright", "bright_")

    def snapshot(svg: Path) -> bool:
        lines = []
        for y in range(tall):
            line = Text()
            for x in range(cols):
                cell = screen.buffer[y][x]
                if cell.data:  # the second half of a wide character is empty
                    line.append(cell.data, Style(color=colour(cell.fg), bgcolor=colour(cell.bg), bold=cell.bold, italic=cell.italics,
                                                 underline=cell.underscore, strike=cell.strikethrough, reverse=cell.reverse))
            lines.append(line)
        if not any("Claude Code" in line.plain for line in lines):
            return False
        # the made-up key draws a sign-in warning that a logged-in user never sees; leave those lines out
        start = next((i for i, line in enumerate(lines) if "Remote managed settings failed to load" in line.plain), None)
        if start is not None:
            stop = next(i for i in range(start, tall) if not lines[i].plain.strip()) + 1
            lines = lines[:start] + lines[stop:]
        # the input box is pinned to the bottom of the taller screen: close the gap above it to get back to the frame's height
        box = next((i for i, line in enumerate(lines) if line.plain.startswith("──")), 0)
        while len(lines) > rows and box and not lines[box - 1].plain.strip():
            box -= 1
            del lines[box]
        if any(line.plain.strip() for line in lines[rows:]):
            return False  # it would not fit the frame
        lines = lines[:rows] + [Text()] * (rows - len(lines))
        console = Console(width=cols, height=rows, file=io.StringIO(), force_terminal=True, color_system="truecolor", record=True,
                          legacy_windows=False, safe_box=False)
        for line in lines:
            line.pad_right(cols - line.cell_len)
            console.print(line, overflow="crop", no_wrap=True, highlight=False)
        svg.write_text(console.export_svg(title="claude"))
        return True

    shots: list[Path] = []
    try:
        listen(wait)
        for n in range(len(typed) + 1):  # the screen as it opens, then once more after each character
            if n:
                os.write(fd, typed[n - 1].encode())
                listen(0.4)
            shot = frames / f"claude-{n:02d}.svg"
            if not snapshot(shot):
                return []
            shots.append(shot)
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    return shots


class Handover(App):
    """Stands in for Claude Code's screen when the real program is not there to record."""

    TITLE = "terminal"
    CSS = "Screen { background: #0c0c0c; } Static { padding: 1 2; }"

    def compose(self):
        n = next(i for i, d in enumerate(DEMO) if d[2] == OPENED)
        sid = uuid.uuid5(uuid.NAMESPACE_DNS, f"convo-demo-{n}")
        yield Static(Text.assemble(
            ("$ ", "green"), ("cd ", "bold"), f"~/{DEMO[n][3]}\n", ("$ ", "green"), ("claude --resume ", "bold"), f"{sid}\n\n",
            (f"Claude Code opens here with “{OPENED}”, ready to continue.\n", "grey70"),
            ("Quit it and you are back in convo.", "grey70")))


async def tour(frames: Path) -> list[float]:
    """Drive convo through a short tour, saving a frame after each step; returns how long each frame is shown."""
    shown: list[float] = []
    size = (118, 34)
    asked = "Now update the changelog"
    claude_shots = claude_screen(frames, size, asked)
    if not claude_shots:  # without Claude Code installed, a plain note stands in for its screen
        claude_shots = [frames / "handover.svg"]
        async with Handover().run_test(size=size) as pilot:
            await pilot.pause()
            pilot.app.save_screenshot(str(claude_shots[0]))
    app = ConvoApp()
    async with app.run_test(size=size) as pilot:
        async def frame(seconds: float, says: str = "", picture: Path | None = None) -> None:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            svg = frames / f"{len(shown):03d}.svg"
            if picture:
                shutil.copy(picture, svg)
            else:
                app.save_screenshot(str(svg))
            if says:
                caption(svg, *says.split(" ", 1))
            shown.append(seconds)

        async def keys(names: str | list[str], seconds: float, last: float | None = None, says: str = "") -> None:
            """Press each key; `says` captions the frames with the key and what it does."""
            names = list(names) if isinstance(names, str) else names
            for i, name in enumerate(names):
                await pilot.press(name)
                await frame(last if last is not None and i == len(names) - 1 else seconds, says)

        await frame(1.0)
        await keys(["down"] * 3, 0.5, 1.0)
        await keys(["slash"], 0.8, says="/ search")
        await keys("dashboard", 0.18, 1.2, says="/ search")
        await keys(["enter"], 1.5)
        await keys(["enter"], 2.4, says="enter open conversation")
        await keys(["down"] * 3, 0.5, 0.8)                     # scroll a few rows
        await keys(["escape"], 0.9, says="esc back")
        await keys(["escape"], 1.3, says="esc clear")
        # r itself is not pressed: it would start the real program. Its screen was recorded above.
        resume = "r resume in Claude Code"
        await frame(1.2, says=resume)
        await frame(2.5, says=resume, picture=claude_shots[0])
        for shot in claude_shots[1:-1]:
            await frame(0.09, says=resume, picture=shot)
        if len(claude_shots) > 1:
            await frame(2.0, says=resume, picture=claude_shots[-1])
        await frame(1.2, says="/exit back to convo")
        await keys(["s"], 3.5, says="s stats")
        await keys(["escape"], 1.5, says="esc back")
    return shown


def to_gif(frames: Path, shown: list[float], gif: Path, width: int = 1100) -> None:
    for i in range(len(shown)):
        to_png(frames / f"{i:03d}.svg", frames / f"{i:03d}.png", scale=1)
    lines = [f"file '{i:03d}.png'\nduration {seconds}\n" for i, seconds in enumerate(shown)]
    (frames / "list.txt").write_text("".join(lines) + f"file '{len(shown) - 1:03d}.png'\n")
    look = f"fps=10,scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(frames / "list.txt"),
                    "-vf", look, "-loop", "0", str(gif)], check=True, timeout=300)


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
    if "--demo" in sys.argv:
        if not (shutil.which("firefox") and shutil.which("ffmpeg")):
            sys.exit("the demo needs Firefox and ffmpeg")
        (HOME / "firefox").mkdir()
        (HOME / "frames").mkdir()
        to_gif(HOME / "frames", asyncio.run(tour(HOME / "frames")), HERE / "demo.gif")
        print(HERE / "demo.gif")
    elif shutil.which("firefox"):
        asyncio.run(shoot(svg))
        (HOME / "firefox").mkdir()
        to_png(svg, HERE / "screenshot.png")
        print(HERE / "screenshot.png")
    else:
        asyncio.run(shoot(svg))
        shutil.copy(svg, HERE / "screenshot.svg")
        print(f"{HERE / 'screenshot.svg'}  (install Firefox to get a PNG)")
    shutil.rmtree(HOME, ignore_errors=True)
