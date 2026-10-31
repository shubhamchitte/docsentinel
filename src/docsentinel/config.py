"""Per-repo settings. Defaults work for a typical Python repo; override with .docsentinel.yml at the repo root."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    code: list[str] = field(default_factory=lambda: ["src/*.py", "src/**/*.py", "*.py"])
    docs: list[str] = field(default_factory=lambda: ["README.md", "docs/*.md", "docs/**/*.md"])
    # Changelogs mention old names on purpose -- never treat them as stale.
    exclude: list[str] = field(default_factory=lambda: ["CHANGELOG*", "docs/changelog*", "tests/**", "**/tests/**"])
    tests: list[str] = field(default_factory=lambda: ["tests/**", "**/test_*.py", "**/*_test.py"])  # shown to the Judge
    cli_name: str | None = None  # e.g. "dotenv": CLI commands match only as `dotenv <cmd>`
    max_sections_per_run: int = 20  # cap on LLM work per run
    min_confidence_to_fix: float = 0.7
    webhooks: list[dict] = field(default_factory=list)  # see hooks.py
    review_sections: str = "draft"  # REVIEW fixes: "draft" (in the draft PR, flagged) | "comment_only"
    notify_slack: str = "changes"   # "changes": skip runs with nothing to report | "always"

    @classmethod
    def load(cls, repo: Path, path: Path | None = None) -> "Config":
        path = Path(path) if path else repo / ".docsentinel.yml"
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)
