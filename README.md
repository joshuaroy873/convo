# convo

One terminal view of every conversation you've had with **Claude Code**, **Codex** and **Antigravity (agy)**, whether it started in the CLI or in VS Code. Browse, search, read, resume, rename, trash and export them from one place.

Linux only for now.

> **convo edits other tools' private data.** Rename and trash write to storage that Claude Code, Codex and Antigravity do not document and may change in any release. Trash is reversible until you empty it, and convo refuses to write when a store looks unfamiliar, but keep a backup of anything you cannot afford to lose.

## Install

```sh
pip install git+https://github.com/joshuaroy873/convo
# or from a checkout
pip install .
```

Requires Python 3.11+. The command is `convo`.

## Use

Run `convo` for the interactive view, or `convo some words` to open it already filtered.

| Key | In the list | Inside a conversation |
|---|---|---|
| `enter` | actions for the conversation (or fold a group) | |
| `v` | view the conversation | |
| `/` | filter (see below) | find text |
| `s` | search inside every conversation | |
| `t` | cycle the tool filter | show or hide tool output and thinking |
| `o` | cycle the sort order | |
| `b` | group by project, by date, or not at all | |
| `r` | resume in the tool that owns it | |
| `y` | copy the command that resumes it | |
| `n` | rename | next match (`N` previous) |
| `space` | mark for a bulk trash | |
| `d` / `u` | trash the marked ones (or this one) / undo the last trash | |
| `e` / `E` | export Markdown / JSON | export Markdown |
| `[` `]` | | previous / next prompt |
| `g` | quit, leaving your shell in the conversation's folder | |
| `m` | Claude's memory notes | |
| `c` | clean up orphans | |
| `?` | show all keys | show all keys |
| `esc` | clear marks, then filters | back |
| `q` | quit | back |

### Filter terms

Mix these with plain words, in the `/` box or on the command line:

```text
tool:claude,codex    -tool:agy    tool:claude,!codex
via:cli              via:vscode   via:ide
dir:backend,!test
date:today  date:yesterday  date:week  date:month  date:<2d  date:>1w
```

Plain words match the title, project folder, git branch, tool and id. On the command line, write an exclusion as `tool:!agy` rather than `-tool:agy`, which the shell-side parser would read as an option.

### Commands

```sh
convo [filter]                      # interactive view
convo list [--tool T] [--json] [filter]
convo show <id> [--tools]           # print as Markdown
convo export <id> [-f md|json] [-o FILE] [--tools]
convo rename <id> "New title"
convo resume <id or filter>         # by id, or the most recent match
convo rm <id>                       # move to trash
convo trash [list|restore [N]|empty]
convo rm <id> --dry-run             # show what a trash would move
convo orphans [--trash]             # empty conversations, missing projects, leftover files
convo stats [filter]                # totals by tool and project, and when you were active
convo trash log                     # what was trashed, restored and deleted, and when
convo memory [--project DIR]        # the notes Claude keeps between sessions
convo config [--init]               # settings in use; --init writes a file of defaults
convo setup                         # let convo change your shell's folder (see below)
convo doctor                        # where each tool's data was found, and any problems
```

`<id>` can be any unique prefix of a conversation id. Exports get a new name rather than overwriting an earlier one.

### Settings

Optional, in `~/.config/convo/config.toml` (`convo config --init` writes it):

```toml
sort = "updated"      # updated | created | size | messages | title | project
group = "none"        # none | project | date
enter = "menu"        # what Enter does on a conversation: menu | open
show_tools = false    # show tool output and thinking when a conversation opens
export_dir = ""       # where exports go; empty means ~/Documents/chat-exports
```

### Ending up in a conversation's folder

A program cannot change the folder of the shell that started it, so `g` needs a small shell function. `convo setup` adds one line to your shell profile (zsh, bash or fish); `convo init zsh` prints the function if you would rather add it yourself. Without it, `g` prints the `cd` command instead.

### Claude's memory notes

`m` (or `convo memory`) lists the notes Claude Code keeps per project between sessions, with the conversation that wrote each. Enter reads one; `d` moves it to the trash.

## Trash

`d` (or `convo rm`) moves a conversation's files out of the owning tool's storage into `~/.local/share/convo/trash`, together with the index records that pointed at it, so the tool no longer lists it. `u` (or `convo trash restore`) puts everything back. Nothing is erased until `convo trash empty`.

For Claude Code that covers the transcript, its subagent and tool-result folder, file history, task and environment state, scratch folder and the session's rows in the prompt history. For Codex it is what `codex delete` removes, plus its prompt-history rows.

The confirmation lists every path and index record a trash will move. Each trash, restore and permanent delete is appended to `~/.local/share/convo/log.jsonl`.

### Cleaning up

`c` (or `convo orphans`) lists what is probably safe to remove: conversations with no messages, conversations whose project folder is gone, and state a tool left behind for conversations that no longer exist. Each item says why it is listed, and removing one moves it to the trash like anything else. Claude's scratch folders and prompt history are never offered here.

When you trash the last conversation of a project, convo asks whether to also trash what Claude Code still keeps for that project: its memory notes, and its trust and tool-permission entry in `.claude.json`. The answer defaults to no, and it is restorable like anything else.

A conversation in use cannot be trashed: one open in a running Claude Code session, or a Codex or Antigravity one written to in the last 90 seconds.

## What is read, and how

| Tool | Storage | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/<id>.jsonl` | Rename appends the same title record as `/rename`. |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`, `state_*.sqlite`, `session_index.jsonl` | A thread split across several rollout files is shown as one conversation. Subagent and `codex exec` threads are hidden. |
| Antigravity | `~/.gemini/antigravity{,-cli}/conversations/<id>.db` | Messages are protobuf with no published schema; the field layout was worked out from real data and may shift between agy versions. Rename is best effort. If a database cannot be read, the plain-text log agy keeps under `brain/<id>/.system_generated/logs/` is used instead. |

If a tool is running (for example its VS Code panel), it may keep showing an old title or a trashed conversation until it restarts.

Conversations held on a remote host over VS Code Remote SSH live on that host and are not visible here.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code data |
| `CODEX_HOME` | `~/.codex` | Codex data |
| `CONVO_AGY_HOME` | `~/.gemini` | Antigravity data |
| `CONVO_DATA` | `~/.local/share/convo` | trash |
| `CONVO_EXPORT_DIR` | `~/Documents/chat-exports` | exports |
| `CONVO_CACHE` | `~/.cache/convo` | scan cache |
| `CONVO_CONFIG` | `~/.config/convo/config.toml` | settings file |

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check .
```

The tests build small made-up stores for each tool in a temporary folder; they never read a real home directory.

## Licence

MIT
