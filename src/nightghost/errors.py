from __future__ import annotations


class NightGhostError(Exception):
    """A user-facing error: the CLI prints the message on one line and exits with code 1."""


class GitError(NightGhostError):
    """A git operation failed in a way the user needs to know about."""
