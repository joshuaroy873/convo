"""Command line entry point. With no subcommand it opens the TUI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, orphans, stats, store, trash
from .common import CACHE, human_size, one_line, short_path, when
from .query import parse


def _load():
    convs, errors = store.load_all()
    for e in errors:
        print(f"warning: {e}", file=sys.stderr)
    return sorted(convs, key=lambda c: -c.updated)


def cmd_list(a):
    q = parse(" ".join(a.query))
    convs = [c for c in _load() if (not a.tool or c.tool == a.tool) and q.match(c)]
    if a.json:
        print(json.dumps([{**c.__dict__, "key": c.key} for c in convs], indent=1))
        return
    for c in convs:
        flags = ("L" if c.live else "") + ("A" if c.archived else "")
        print(f"{c.tool:6} {c.origin:6} {c.id[:8]} {when(c.updated):>11} {c.nmsg:5} {human_size(c.size):>6} "
              f"{flags:2} {one_line(c.title, 60):60} {short_path(c.cwd)}")
    print(f"\n{len(convs)} conversations", file=sys.stderr)


def cmd_show(a):
    c = store.resolve(_load(), a.id)
    sys.stdout.write(store.to_markdown(c, store.messages(c), a.tools) + "\n")


def cmd_export(a):
    c = store.resolve(_load(), a.id)
    print(store.export(c, a.format, Path(a.output) if a.output else None, a.tools))


def cmd_rename(a):
    c = store.resolve(_load(), a.id)
    store.rename(c, a.title)
    print(f"renamed {c.key}: {c.title!r} -> {a.title!r}")


def cmd_rm(a):
    c = store.resolve(_load(), a.id)
    if a.dry_run:
        print(f"would move to trash: {c.key} {c.title!r}")
        for line in trash.preview(store.SOURCES[c.tool].trash_plan(c), c.id):
            print(f"  {line}")
        return
    trash.put(c)
    print(f"moved to trash: {c.key} {c.title!r} (restore with: convo trash restore)")


def cmd_trash(a):
    ents = trash.entries()
    if a.action == "log":
        for r in trash.read_log()[-(a.n or 30):]:
            print(f"{r['when']}  {r['action']:8} {r['tool']:6} {r['id'][:8]} {one_line(r['title'], 60)}")
        return
    if a.action == "list":
        for i, (_, m) in enumerate(ents, 1):
            print(f"{i:3} {when(m['when'], True)} {m['tool']:6} {m['id'][:8]} {one_line(m['title'], 60)}")
        if not ents:
            print("trash is empty")
        return
    if not ents:
        raise SystemExit("trash is empty")
    if a.action == "restore":
        d, m = ents[(a.n or len(ents)) - 1]
        trash.restore(d)
        print(f"restored {m['tool']}:{m['id']} {m['title']!r}")
    elif a.action == "empty":
        if not a.yes:
            ans = input(f"Permanently delete {len(ents)} trashed conversation(s)? [y/N] ")
            if ans.strip().lower() != "y":
                return
        for d, m in ents:
            try:
                trash.purge(d)
                print(f"deleted {m['tool']}:{m['id']} {m['title']!r}")
            except (RuntimeError, OSError) as e:
                print(f"could not delete {m['tool']}:{m['id']}: {e}", file=sys.stderr)


def resume(c) -> None:
    """Hand the terminal to the owning tool, in the conversation's own directory."""
    cmd = store.SOURCES[c.tool].resume_cmd(c)
    if not shutil.which(cmd[0]):
        raise RuntimeError(f"{cmd[0]} is not on PATH")
    if c.live and c.tool == "claude":
        raise RuntimeError("this conversation is already open in a running session")
    cwd = c.cwd if c.cwd and os.path.isdir(c.cwd) else None
    if c.cwd and not cwd and c.tool == "claude":
        raise RuntimeError(f"project folder no longer exists: {c.cwd}")
    subprocess.run(cmd, cwd=cwd)


def cmd_stats(a):
    q = parse(" ".join(a.query))
    print(stats.report([c for c in _load() if q.match(c)], len(trash.entries())))


def cmd_orphans(a):
    words = " ".join(a.match).lower().split()
    found = [o for o in orphans.find(_load()) if all(w in f"{o.tool} {o.id} {o.reason} {o.label}".lower() for w in words)]
    for o in found:
        print(f"{o.tool:6} {o.id[:8]} {human_size(o.size):>6}  {o.reason}: {one_line(o.label, 50)}")
    if not found:
        print("nothing to clean up")
        return
    if not a.trash:
        print(f"\n{len(found)} item(s). Move them to the trash with: convo orphans --trash", file=sys.stderr)
        return
    if not a.yes and input(f"Move these {len(found)} item(s) to the trash? [y/N] ").strip().lower() != "y":
        return
    for o in found:
        try:
            orphans.remove(o)
            print(f"moved to trash: {o.key}")
        except (RuntimeError, OSError) as e:
            print(f"could not move {o.key}: {e}", file=sys.stderr)


WRAPPERS = {
    "sh": '''convo() {
  local convo_cd; convo_cd="$(mktemp)" || return
  CONVO_CD_FILE="$convo_cd" command convo "$@"
  local convo_rc=$?
  [ -s "$convo_cd" ] && cd -- "$(cat "$convo_cd")"
  rm -f "$convo_cd"
  return $convo_rc
}''',
    "fish": '''function convo
    set -l convo_cd (mktemp)
    CONVO_CD_FILE=$convo_cd command convo $argv
    set -l convo_rc $status
    test -s $convo_cd; and cd (cat $convo_cd)
    rm -f $convo_cd
    return $convo_rc
end''',
}
PROFILES = {"zsh": ("~/.zshrc", 'eval "$(convo init zsh)"'), "bash": ("~/.bashrc", 'eval "$(convo init bash)"'),
            "fish": ("~/.config/fish/config.fish", "convo init fish | source")}
MARK = "# convo shell integration"


def _shell(name: str | None) -> str:
    name = name or os.path.basename(os.environ.get("SHELL", ""))
    if name not in PROFILES:
        raise SystemExit(f"shell {name or '(unknown)'!r} is not supported; pass --shell zsh, bash or fish")
    return name


def cmd_init(a):
    print(WRAPPERS["fish" if _shell(a.shell) == "fish" else "sh"])


def cmd_setup(a):
    shell = _shell(a.shell)
    profile, line = PROFILES[shell]
    path = Path(profile).expanduser()
    if path.exists() and MARK in path.read_text():
        print(f"already set up in {profile}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"\n{MARK}\n{line}\n")
    print(f"added to {profile}. Open a new terminal, then g in convo leaves the shell in a conversation's folder.")


def cmd_memory(a):
    from .sources import claude
    notes = [m for m in claude.memories() if not a.project or os.path.abspath(a.project) == m["project"]]
    for m in notes:
        gone = "  (its conversation is gone)" if m["origin_gone"] else ""
        print(f"{m['type'] or '-':10} {when(m['updated']):>11} {human_size(m['size']):>6}  {one_line(m['name'], 44):44} "
              f"{short_path(m['project'])}{gone}")
    print(f"\n{len(notes)} note(s)", file=sys.stderr)


def cmd_config(a):
    cfg = config.load()
    if a.init:
        if config.PATH.exists():
            raise SystemExit(f"{short_path(str(config.PATH))} already exists")
        config.PATH.parent.mkdir(parents=True, exist_ok=True)
        config.PATH.write_text(config.TEMPLATE)
        print(f"wrote {short_path(str(config.PATH))}")
        return
    print(f"{short_path(str(config.PATH))}  ({'found' if config.PATH.exists() else 'not created; defaults in use'})")
    for key in ("sort", "group", "enter", "show_tools", "export_dir"):
        print(f"  {key:11} {getattr(cfg, key)!r}")
    for problem in cfg.problems:
        print(f"  problem     {problem}")
    if not config.PATH.exists():
        print("create it with: convo config --init", file=sys.stderr)


def command_line(c) -> str:
    """The shell command that reopens a conversation, for pasting elsewhere."""
    cmd = shlex.join(store.SOURCES[c.tool].resume_cmd(c))
    return f"cd {shlex.quote(c.cwd)} && {cmd}" if c.cwd else cmd


def cmd_doctor(a):
    from .sources import agy, claude, codex
    convs, errors = store.load_all()

    def agy_layout():
        for c in convs:
            if c.tool == "agy":
                agy._check_layout(c)

    checks = {"claude": (claude.ROOT, lambda: None), "codex": (codex.ROOT, codex._check_layout), "agy": (agy.ROOT, agy_layout)}
    for tool, (root, check) in checks.items():
        n = sum(1 for c in convs if c.tool == tool)
        exe = shutil.which(tool)  # each tool's program shares its name
        print(f"{tool}")
        print(f"  data      {short_path(str(root))}  ({'found' if root.is_dir() else 'not found'})")
        print(f"  program   {short_path(exe) if exe else 'not on PATH (resume will not work)'}")
        print(f"  listed    {n} conversation(s)")
        if tool == "codex" and codex.hidden:
            print(f"  hidden    {codex.hidden} subagent or non-interactive thread(s)")
        if root.is_dir():
            try:
                check()
                print("  changes   rename and trash available")
            except Exception as e:
                print(f"  changes   refused: {e}")
        for e in errors:
            if e.startswith(tool + ":"):
                print(f"  problem   {e}")
    ents = trash.entries()
    print("convo")
    print(f"  trash     {short_path(str(trash.TRASH))}  ({len(ents)} conversation(s))")
    print(f"  exports   {short_path(str(store.EXPORTS))}")
    print(f"  cache     {short_path(str(CACHE))}")
    cfg = config.load()
    print(f"  settings  {short_path(str(config.PATH))}  ({'found' if config.PATH.exists() else 'not created'})")
    for problem in cfg.problems:
        print(f"  problem   {problem}")
    print(f"  shell     {'wrapper active' if os.environ.get('CONVO_CD_FILE') else 'wrapper not active (see: convo setup)'}")


def cmd_resume(a):
    try:
        c = store.find(_load(), " ".join(a.query))
        print(f"resuming {c.tool}: {c.title}", file=sys.stderr)
        resume(c)
    except RuntimeError as e:
        raise SystemExit(str(e)) from None


def main(argv=None):
    p = argparse.ArgumentParser(prog="convo", description="Manage Claude, Codex and Antigravity conversations. "
                                "Run with no arguments for the interactive view.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("list", help="list conversations")
    s.add_argument("--tool", choices=list(store.SOURCES))
    s.add_argument("--json", action="store_true")
    s.add_argument("query", nargs="*", help="filter, e.g. dir:backend date:<2d auth")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show", help="print a conversation as Markdown")
    s.add_argument("id")
    s.add_argument("--tools", action="store_true", help="include tool output and thinking")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("export", help="export a conversation to a file")
    s.add_argument("id")
    s.add_argument("-f", "--format", choices=["md", "json"], default="md")
    s.add_argument("-o", "--output")
    s.add_argument("--tools", action="store_true", help="include tool output and thinking (Markdown)")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("rename", help="rename a conversation")
    s.add_argument("id")
    s.add_argument("title")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("rm", help="move a conversation to the trash")
    s.add_argument("id")
    s.add_argument("--dry-run", action="store_true", help="show what would be moved, and change nothing")
    s.set_defaults(fn=cmd_rm)
    s = sub.add_parser("trash", help="list, restore or empty the trash, or show its log")
    s.add_argument("action", choices=["list", "restore", "empty", "log"], nargs="?", default="list")
    s.add_argument("n", type=int, nargs="?", help="entry to restore (default: most recent), or how many log lines to show")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_trash)
    s = sub.add_parser("resume", help="reopen a conversation by id, or the most recent one matching a filter")
    s.add_argument("query", nargs="+")
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("stats", help="totals by tool and project, and when you were active")
    s.add_argument("query", nargs="*", help="filter, e.g. tool:claude date:month")
    s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("orphans", help="list empty conversations, ones whose project is gone, and leftover files")
    s.add_argument("match", nargs="*", help="only items whose tool, reason or name contains these words")
    s.add_argument("--trash", action="store_true", help="move the listed items to the trash")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_orphans)
    s = sub.add_parser("memory", help="list the notes Claude keeps between sessions")
    s.add_argument("--project", help="only this project folder")
    s.set_defaults(fn=cmd_memory)
    s = sub.add_parser("config", help="show the settings in use")
    s.add_argument("--init", action="store_true", help="write a settings file with the defaults")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("init", help="print the shell function that lets convo change your shell's folder")
    s.add_argument("shell", nargs="?", choices=list(PROFILES))
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("setup", help="add that shell function to your shell profile")
    s.add_argument("--shell", choices=list(PROFILES))
    s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("doctor", help="show where each tool's data was found, and any problems")
    s.set_defaults(fn=cmd_doctor)
    argv = sys.argv[1:] if argv is None else list(argv)
    cfg = config.load()
    if cfg.export_dir and not os.environ.get("CONVO_EXPORT_DIR"):
        store.EXPORTS = Path(cfg.export_dir).expanduser()
    if not argv or (argv[0] not in sub.choices and not argv[0].startswith("-")):
        from .tui import ConvoApp
        folder = ConvoApp(" ".join(argv), cfg).run()  # `convo some words` opens the list already filtered
        if isinstance(folder, str):  # g was pressed: hand the folder to the shell wrapper
            if os.environ.get("CONVO_CD_FILE"):
                Path(os.environ["CONVO_CD_FILE"]).write_text(folder)
            else:
                print(f"cd {shlex.quote(folder)}\n(run `convo setup` once and convo will do this for you)")
        return
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return
    try:
        a.fn(a)
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"error: {e}") from None
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
