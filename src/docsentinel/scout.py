"""Change Scout: which public things changed between two revisions.

Deterministic. Parses both versions of each changed Python file with `ast` and
compares the public surface a doc might describe:
  py       public functions, classes, methods (with parameters and defaults)
  cli_cmd  click subcommands, e.g. "list"
  cli_opt  click options, e.g. "list --format"
  env      environment variables the code reads, e.g. PYTHON_DOTENV_DISABLED
Private names (leading underscore) are ignored: internal refactors never reach the docs.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitio
from .config import Config


@dataclass(frozen=True)
class Entity:
    kind: str
    name: str
    file: str
    line: int
    signature: str = ""
    params: tuple = ()  # ((name, default_source_or_None), ...)
    body: str = ""  # behaviour fingerprint (see _body_fingerprint); "" = not tracked


@dataclass
class Change:
    kind: str
    name: str
    change: str  # removed | added | renamed | changed | behavior
    file: str
    line: int
    before: str = ""
    after: str = ""
    new_name: str = ""
    old_line: int = 0  # definition line in the base version (for code diffs)
    details: list[str] = field(default_factory=list)
    params_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- extraction ----------

def _body_fingerprint(fn: ast.FunctionDef) -> str:
    """What the function does, ignoring formatting, comments, docstrings and type hints.
    Two versions with the same fingerprint behave the same, so no doc can have gone stale."""
    fn = copy.deepcopy(fn)
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # docstring
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.arg):
            node.annotation = None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Constant(None)
    return hashlib.sha1("".join(ast.dump(n) for n in body).encode()).hexdigest()[:12]


def _params(fn: ast.FunctionDef) -> tuple:
    a = fn.args
    positional = a.posonlyargs + a.args
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    out = [(p.arg, ast.unparse(d) if d is not None else None) for p, d in zip(positional, defaults)]
    out += [(p.arg, ast.unparse(d) if d is not None else None) for p, d in zip(a.kwonlyargs, a.kw_defaults)]
    return tuple((n, d) for n, d in out if n not in ("self", "cls", "ctx"))


def _render(params: tuple) -> str:
    return ", ".join(n if d is None else f"{n}={d}" for n, d in params)


def _deco_call(d: ast.expr, attr: str) -> ast.Call | None:
    """Match @something.<attr>(...) e.g. @click.option(...), @cli.command(...)."""
    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == attr:
        return d
    return None


def _cli_entities(fn: ast.FunctionDef, path: str) -> list[Entity]:
    out: list[Entity] = []
    cmd = None
    for d in fn.decorator_list:
        if (c := _deco_call(d, "group")) is not None:
            cmd = ""  # root group: options are global
        elif (c := _deco_call(d, "command")) is not None:
            names = [k.value.value for k in c.keywords if k.arg == "name" and isinstance(k.value, ast.Constant)]
            cmd = names[0] if names else fn.name.replace("_", "-")
    if cmd is None:
        return out
    if cmd:
        out.append(Entity("cli_cmd", cmd, path, fn.lineno, signature=_render(_params(fn)),
                          body=_body_fingerprint(fn)))
    for d in fn.decorator_list:
        if (c := _deco_call(d, "option")) is None:
            continue
        flags = [a.value for a in c.args if isinstance(a, ast.Constant) and str(a.value).startswith("-")]
        if not flags:
            continue
        flag = max(flags, key=len).split("/")[0]
        kw = {k.arg: ast.unparse(k.value) for k in c.keywords if k.arg in ("default", "type")}
        sig = " ".join(f"{k}={v}" for k, v in sorted(kw.items()))
        out.append(Entity("cli_opt", f"{cmd} {flag}".strip(), path, c.lineno, signature=sig))
    return out


def _env_vars(tree: ast.AST, path: str) -> list[Entity]:
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
            key = node.slice
        elif isinstance(node, ast.Compare) and any(ast.unparse(c).endswith("environ") for c in node.comparators):
            key = node.left
        elif isinstance(node, ast.Call) and ast.unparse(node.func).endswith(("getenv", "environ.get")) and node.args:
            key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.isupper():
            found.setdefault(key.value, node.lineno)
    return [Entity("env", k, path, ln) for k, ln in found.items()]


def extract(source: str, path: str) -> dict[tuple[str, str], Entity]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    ents: list[Entity] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cli = _cli_entities(node, path)
            ents += cli
            if not cli and not node.name.startswith("_"):
                p = _params(node)
                ents.append(Entity("py", node.name, path, node.lineno, _render(p), p, _body_fingerprint(node)))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            ents.append(Entity("py", node.name, path, node.lineno))
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    p = _params(m)
                    ents.append(Entity("py", f"{node.name}.{m.name}", path, m.lineno, _render(p), p,
                                       _body_fingerprint(m)))
    ents += _env_vars(tree, path)
    return {(e.kind, e.name): e for e in ents}


# ---------- diff ----------

def _param_details(old: Entity, new: Entity) -> tuple[list[str], list[str]]:
    o, n = dict(old.params), dict(new.params)
    details, touched = [], []
    # sorted(): set order varies per process; prompts (and replay keys) must not.
    for name in sorted(o.keys() - n.keys()):
        details.append(f"parameter `{name}` removed")
        touched.append(name)
    for name in sorted(n.keys() - o.keys()):
        details.append(f"parameter `{name}` added" + (f" (default {n[name]})" if n[name] else " (required)"))
        touched.append(name)
    for name in sorted(o.keys() & n.keys()):
        if o[name] != n[name]:
            details.append(f"default of `{name}` changed: {o[name]} -> {n[name]}")
            touched.append(name)
    return details, sorted(touched)


def diff_entities(old: dict, new: dict) -> list[Change]:
    changes: list[Change] = []
    removed = {k: old[k] for k in sorted(old.keys() - new.keys())}
    added = {k: new[k] for k in sorted(new.keys() - old.keys())}

    # Rename = one removed + one added of the same kind, file and signature.
    for rk, r in list(removed.items()):
        match = [ak for ak, a in added.items()
                 if a.kind == r.kind and a.file == r.file and a.signature == r.signature]
        if len(match) == 1:
            a = added.pop(match[0])
            del removed[rk]
            changes.append(Change(r.kind, r.name, "renamed", a.file, a.line, r.signature, a.signature,
                                  new_name=a.name, details=[f"`{r.name}` renamed to `{a.name}`"]))

    for r in removed.values():
        changes.append(Change(r.kind, r.name, "removed", r.file, r.line, r.signature,
                              details=[f"`{r.name}` removed"]))
    for a in added.values():
        changes.append(Change(a.kind, a.name, "added", a.file, a.line, after=a.signature,
                              details=[f"`{a.name}` added"]))
    for k in sorted(old.keys() & new.keys()):
        o, n = old[k], new[k]
        if o.signature != n.signature:
            details, touched = _param_details(o, n) if o.kind == "py" else ([f"`{o.name}`: {o.signature} -> {n.signature}"], [])
            changes.append(Change(o.kind, o.name, "changed", n.file, n.line, o.signature, n.signature,
                                  old_line=o.line, details=details, params_touched=touched))
        elif o.body and n.body and o.body != n.body:
            # R5: same signature, different behaviour. The Judge sees the code and test diffs.
            changes.append(Change(o.kind, o.name, "behavior", n.file, n.line, o.signature, n.signature,
                                  old_line=o.line, details=[f"behaviour of `{o.name}` changed (same signature)"]))
    return sorted(changes, key=lambda c: (c.file, c.line))


def scout(repo: Path, base: str, head: str, cfg: Config) -> list[Change]:
    """All public changes between base and head, across changed code files."""
    old_all: dict = {}
    new_all: dict = {}
    for path in gitio.changed_files(repo, base, head):
        if not path.endswith(".py") or not gitio.match_any(path, cfg.code) or gitio.match_any(path, cfg.exclude):
            continue
        old_all.update(extract(gitio.read_at(repo, base, path) or "", path))
        new_all.update(extract(gitio.read_at(repo, head, path) or "", path))
    # An entity re-exported in several files (e.g. __init__) is keyed once; the first definition wins.
    return diff_entities(old_all, new_all)
