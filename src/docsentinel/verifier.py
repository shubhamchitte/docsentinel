"""Verifier: deterministic checks on every proposed fix. A fix that fails any check is dropped
(its findings stay as flags). Checks:
  scope       edits only inside the stale section of an allowed doc file
  size        changed lines <= cap
  old-name    a removed/renamed name no longer appears in the section
  python      ```python blocks that parsed before still parse
  imports     `from <pkg> import X` in the section only names things that exist in the new code
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitio
from .config import Config
from .fixer import SectionFix
from .scout import extract

MAX_CHANGED_LINES = 15
URL = re.compile(r"(?:https?://|www\.)[^\s)>\]`'\"]+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|sh|ai|co|app)\b", re.I)
SHELL = re.compile(r"\bcurl\b|\bwget\b|\|\s*(?:ba)?sh\b|\bpip3?\s+install\b", re.I)


@dataclass
class Verdict:
    ok: bool
    checks: dict[str, str] = field(default_factory=dict)  # name -> "pass" / failure message


def _python_blocks(lines: list[str]) -> list[str]:
    blocks, cur, lang = [], None, None
    for line in lines:
        s = line.strip()
        if s.startswith("```"):
            if cur is None:
                cur, lang = [], s[3:].strip()
            else:
                if lang == "python":
                    blocks.append("\n".join(cur))
                cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def public_names(repo: Path, head: str, cfg: Config) -> tuple[set[str], set[str]]:
    """(public names in the new code, the project's own package names)."""
    names: set[str] = set()
    packages: set[str] = set()
    for path in gitio.files_at(repo, head):
        if path.endswith(".py") and gitio.match_any(path, cfg.code) and not gitio.match_any(path, cfg.exclude):
            names |= {k[1] for k in extract(gitio.read_at(repo, head, path) or "", path)}
            if path.endswith("__init__.py"):
                packages.add(Path(path).parent.name)
    return names, packages


def verify(fix: SectionFix, findings: list[dict], cfg: Config, known: tuple[set[str], set[str]]) -> Verdict:
    known_names, packages = known
    v = Verdict(ok=True)

    def check(name: str, passed: bool, msg: str) -> None:
        v.checks[name] = "pass" if passed else msg
        v.ok &= passed

    new_lines = [fix.edits.get(fix.start + i, line) for i, line in enumerate(fix.old_lines)]

    check("has-edits", bool(fix.edits), fix.error or "no edits proposed")
    in_scope = all(fix.start <= n <= fix.end for n in fix.edits)
    check("scope", in_scope and gitio.match_any(fix.doc, cfg.docs), "edit outside the stale section or doc allowlist")
    check("size", len(fix.edits) <= MAX_CHANGED_LINES, f"{len(fix.edits)} lines changed (cap {MAX_CHANGED_LINES})")

    leftovers = []
    for f in findings:
        ch = f["change"]
        if ch["change"] in ("removed", "renamed"):
            old = ch["name"].split()[-1]
            if any(re.search(rf"(?<![\w.-]){re.escape(old)}(?![\w-])", line) for line in new_lines):
                leftovers.append(old)
    check("old-name", not leftovers, f"still mentions {', '.join(sorted(set(leftovers)))}")

    before = _python_blocks(fix.old_lines)
    after = _python_blocks(new_lines)
    broken = [i for i, (b, a) in enumerate(zip(before, after)) if _parses(b) and not _parses(a)]
    check("python", not broken and len(before) == len(after), "a python example no longer parses")

    imported = set()
    for line in new_lines:
        m = re.match(r"\s*from (\w+) import (.+)", line)
        if m and m.group(1) in packages:
            imported |= {x.strip().split(" as ")[0] for x in m.group(2).split(",")}
    unknown = sorted(n for n in imported if n not in known_names)
    check("imports", not unknown, f"imports unknown name(s): {', '.join(unknown)}")

    # New links or shell commands are never something a doc fix needs to invent.
    before, after = "\n".join(fix.old_lines), "\n".join(new_lines)
    new_urls = sorted(set(URL.findall(after)) - set(URL.findall(before)))
    new_shell = sorted({m.lower() for m in SHELL.findall(after)} - {m.lower() for m in SHELL.findall(before)})
    check("no-new-url-or-shell", not new_urls and not new_shell,
          f"adds {', '.join(new_urls + new_shell)}")
    return v


def check_all(fixes: list[SectionFix], result: dict, repo: Path, head: str, cfg: Config) -> tuple[list, list]:
    """Verifier on every fix, then the Fix Reviewer (LLM) on fixes the LLM wrote.
    Returns (applied, rejected) as lists of (fix, verdict)."""
    from .reviewer import review  # local import: reviewer -> judge -> ... keeps module load light

    by_id = {f["id"]: f for f in result["findings"]}
    known = public_names(repo, head, cfg)
    applied, rejected = [], []
    for fx in fixes:
        findings = [by_id[i] for i in fx.finding_ids]
        v = verify(fx, findings, cfg, known)
        if v.ok and "llm" in fx.by:
            ok, problem = review(fx, findings, repo, head)
            v.checks["review"] = "pass" if ok else (problem or "rejected by reviewer")
            v.ok = ok
        (applied if v.ok else rejected).append((fx, v))
    return applied, rejected
