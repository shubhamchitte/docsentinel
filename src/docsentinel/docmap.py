"""Doc Mapper: which doc sections talk about a changed thing.

Deterministic. Docs are split into sections at Markdown headings. A section
mentions an entity only in a code context, to keep plain English words like
"list" or "get" from matching:
  py       name inside `backticks` or a code fence, or written as name(
  cli_cmd  "<cli> <cmd>" on one line, e.g. "dotenv list"
  cli_opt  the flag itself, e.g. --format
  env      the variable name
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import gitio
from .config import Config
from .scout import Change


@dataclass
class Section:
    doc: str
    heading: str
    start: int  # 1-based line numbers, inclusive
    end: int
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Mention:
    section: Section
    term: str
    lines: list[tuple[int, str]]  # (line number, line text)
    params_mentioned: list[str]


def split_sections(doc: str, text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = []
    heading, start, in_fence = "(top)", 1, False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and re.match(r"#{1,6} ", line):
            if i > start:
                sections.append(Section(doc, heading, start, i - 1, lines[start - 1:i - 1]))
            heading, start = line.lstrip("#").strip(), i
    sections.append(Section(doc, heading, start, len(lines), lines[start - 1:]))
    return sections


def load_sections(repo: Path, ref: str, cfg: Config) -> list[Section]:
    out: list[Section] = []
    for path in gitio.files_at(repo, ref):
        if path.endswith(".md") and gitio.match_any(path, cfg.docs) and not gitio.match_any(path, cfg.exclude):
            out += split_sections(path, gitio.read_at(repo, ref, path) or "")
    return out


def _code_mask(lines: list[str]) -> list[str]:
    """Each line reduced to its code parts: whole line inside a fence, else only `inline code`."""
    out, in_fence = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
        elif in_fence:
            out.append(line)
        else:
            out.append(" ".join(re.findall(r"`([^`]*)`", line)))
    return out


def _term_regex(kind: str, name: str, cli_name: str | None) -> tuple[re.Pattern, bool]:
    """(pattern, code_only). code_only means: match only within code parts of the line."""
    if kind == "py":
        leaf = re.escape(name)
        return re.compile(rf"(?<![\w.]){leaf}\b"), True
    if kind == "cli_cmd":
        cli = re.escape(cli_name or "")
        return re.compile(rf"\b{cli}\b(?:\s+-\S+(?:\s+\S+)?)*\s+{re.escape(name)}\b"), False
    if kind == "cli_opt":
        flag = name.split()[-1]
        return re.compile(rf"(?<![\w-]){re.escape(flag)}(?![\w-])"), False
    return re.compile(rf"\b{re.escape(name)}\b"), False


def find_mentions(change: Change, sections: list[Section], cfg: Config) -> list[Mention]:
    if change.kind == "cli_cmd" and not cfg.cli_name:
        return []
    pattern, code_only = _term_regex(change.kind, change.name, cfg.cli_name)
    call = re.compile(rf"(?<![\w.]){re.escape(change.name)}\(") if change.kind == "py" else None
    out: list[Mention] = []
    for s in sections:
        masked = _code_mask(s.lines) if code_only else s.lines
        hits = [(s.start + i, s.lines[i]) for i, m in enumerate(masked)
                if pattern.search(m) or (call and call.search(s.lines[i]))]
        if not hits:
            continue
        params = [p for p in change.params_touched if re.search(rf"\b{re.escape(p)}\b", s.text)]
        out.append(Mention(s, change.name, hits, params))
    return out
