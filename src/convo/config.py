"""Optional settings file. Everything has a default; a bad value is reported
by `convo doctor` and ignored rather than stopping convo."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .common import env_path

PATH = env_path("CONVO_CONFIG", "~/.config/convo/config.toml")
CHOICES = {
    "sort": ("updated", "created", "size", "messages", "title", "project"),
    "group": ("none", "project", "date"),
}
TEMPLATE = '''\
# convo settings. Every line is optional; these are the defaults.

sort = "updated"      # updated | created | size | messages | title | project
group = "none"        # none | project | date
show_tools = false    # show tool output and thinking when a conversation opens
export_dir = ""       # where exports go; empty means ~/Documents/chat-exports
'''


@dataclass
class Config:
    sort: str = "updated"
    group: str = "none"
    show_tools: bool = False
    export_dir: str = ""
    problems: list[str] = field(default_factory=list)


def load(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or PATH
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return cfg
    except (OSError, tomllib.TOMLDecodeError) as e:
        cfg.problems.append(f"{path}: {e}")
        return cfg
    for key, value in data.items():
        if key in CHOICES:
            if value in CHOICES[key]:
                setattr(cfg, key, value)
            else:
                cfg.problems.append(f"{key} = {value!r}: expected one of {', '.join(CHOICES[key])}")
        elif key == "show_tools" and isinstance(value, bool):
            cfg.show_tools = value
        elif key == "export_dir" and isinstance(value, str):
            cfg.export_dir = value
        else:
            cfg.problems.append(f"{key}: not a setting convo knows, or the wrong kind of value")
    return cfg
