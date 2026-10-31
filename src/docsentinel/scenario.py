"""Build a throwaway git repo for a demo / eval case.

Copies the vendored demo repo, commits it as `base`, applies the case's
find/replace edits, commits that as `head`. The vendored snapshot is never touched.

Case file (YAML):
  name: ...
  config: {cli_name: dotenv}          # written as .docsentinel.yml
  edits:
    - files: [src/dotenv/main.py]
      find: "def dotenv_values("
      replace: "def read_dotenv("
  expect: ...                          # used by the eval harness
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parents[1]
DEMO_REPO = SRC / "demo_repo" / "python-dotenv"


def load_case(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def build(case: dict, workdir: Path | None = None) -> Path:
    repo = Path(workdir or tempfile.mkdtemp(prefix="docsentinel-")) / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(DEMO_REPO, repo, ignore=shutil.ignore_patterns("__pycache__"))
    (repo / ".docsentinel.yml").write_text(yaml.safe_dump(case.get("config", {})))

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "docsentinel-demo")
    _git(repo, "config", "user.email", "demo@docsentinel.invalid")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    for edit in case.get("edits", []):
        hit = False
        for rel in edit["files"]:
            f = repo / rel
            text = f.read_text()
            if edit["find"] in text:
                f.write_text(text.replace(edit["find"], edit["replace"]))
                hit = True
        if not hit:
            raise ValueError(f"case {case.get('name')}: edit not applied, text not found: {edit['find']!r}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"head: {case.get('name', 'case')}")
    return repo
