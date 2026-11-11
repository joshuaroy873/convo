"""convo: terminal manager for Claude Code, Codex and Antigravity (agy) conversations."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("convo-tui")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
