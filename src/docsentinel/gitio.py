"""Read-only git access. DocSentinel reads two revisions; it never writes to the repo here."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", f"{base}..{head}")
    return [line for line in out.splitlines() if line]


def files_at(repo: Path, ref: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()


def read_at(repo: Path, ref: str, path: str) -> str | None:
    """File content at a revision, or None if it doesn't exist there."""
    try:
        return git(repo, "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--short", ref).strip()


def match_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)
