# DocSentinel

Catch documentation that a code change made wrong, and propose a verified fix for a human to approve.

## The problem

Code changes, docs don't, and readers find out when an example fails. When code changes, DocSentinel finds the docs that became wrong, proposes a verified fix as a draft PR for a human to approve, and answers developers' "how do I…?" questions from docs checked against the current code.

## How it works

Rules (`ast`-based change detection and a code-to-doc map) handle what is certain; an LLM Judge handles what takes reading. The AI can read but never write: a separate job without AI posts comments and opens draft PRs. Architecture diagram and design notes: [docs/README.md](docs/README.md).

| What it does | Shown on a real repo |
|---|---|
| Flags stale doc sections on every PR, with lines and reasons (rules where certain, an LLM Judge where it takes reading) | [PR comment on python-dotenv](https://github.com/shubhamchitte/python-dotenv/pull/3#issuecomment-6191897504) (a 2020 commit, replayed) |
| Fixes them as a draft docs PR after merge: verified, low-confidence parts marked, never merged by the bot | [Draft PR #4](https://github.com/shubhamchitte/python-dotenv/pull/4), [draft PR #8](https://github.com/shubhamchitte/python-dotenv/pull/8) |
| Serves docs through an agent (CLI or any AI editor via MCP) that answers from code + docs and warns where docs are stale | [4 real questions, with permalink citations](docs/evidence.md#docs-concierge-on-the-real-repo-docsentinel-ask-live) |

## Key results

Precision **100%** and recall **94%** on 11 eval cases (rules alone: 45% precision). **32/32** tests pass with one command. On the real python-dotenv it caught 2 of 3 historical drift commits, and on httpx it found [doc drift that is still there today](docs/evidence.md#second-repo-encodehttpx-overfitting-check).

Read next: [design write-up with the diagram](docs/README.md) · [evidence](docs/evidence.md) · [`run.yaml`](run.yaml) (reproducible setup/run/test commands) · [install in your repo](docs/install.md)

## Run it (5 minutes)

### 1. Set up Python first

Supported: Python 3.10 or newer (3.10 to 3.14 tested in CI by running `run.yaml`, with and without `CLAUDE_KEY`) and git. Do this before anything else:
```bash
python3 -m venv .venv && source .venv/bin/activate   # gives you `python` (macOS only has python3)
pip install -r requirements.txt
```

### 2. Install the Claude CLI

Needed for live LLM calls; without it, everything still runs on recorded answers.
```bash
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"                 # add to ~/.zshrc or ~/.bashrc to keep it
export CLAUDE_KEY=<your key>
```
Any Claude key works; DocSentinel passes it to the claude CLI for you. No key? Everything still runs using saved AI answers, and the output says LLM: replay.

This installer always fetches the latest CLI; CI and `run.yaml` pin `@anthropic-ai/claude-code@2.1.299`. (Or `npm install -g @anthropic-ai/claude-code@2.1.299`.) If `CLAUDE_KEY` is set but the CLI isn't found, every command prints: *"CLAUDE_KEY found but no claude CLI, running on recorded answers. To run live: curl -fsSL https://claude.ai/install.sh | bash and add ~/.local/bin to PATH."*

### 3. Check, then run

```bash
python src/main.py doctor                       # first check: should say "mode : live" (or "replay" with no key/CLI)
python src/main.py fix --case src/evals/cases/demo.yaml --out out/demo
python src/main.py ask "How do I read a .env file into a dict without changing os.environ?" \
    --case src/evals/cases/demo.yaml --out out/ask
python src/main.py eval                         # scorecard over 11 cases
python src/tests/run_all.py                     # all tests: guardrails + workflow behaviour + MCP server (32)
```

`--case` builds a throwaway git repo: python-dotenv as `base`, then the case's code edits as `head`. The demo case renames `dotenv_values`, flips the `load_dotenv(override=...)` default, renames the CLI option `list --format`, and renames a private helper (which must not be flagged).

What you get in `out/demo/`:

| File | What it is |
|---|---|
| `report.md` | Which doc sections are stale, the lines involved, and why (this becomes the PR comment) |
| `fix.patch` | Verified line edits to the docs (`git apply`-able) |
| `fix.md` | Draft PR description: what changed and what was left for a human |

## Tests

`python src/tests/run_all.py` runs 32 tests with no network and no LLM: fix guardrails (rejecting new URLs or shell commands), webhook signing, fork-PR handling, the real workflow shell steps against a local git remote, and the MCP server contract (both tools registered and annotated read-only, idempotent and closed-world). CI runs `run.yaml` on Python 3.10 to 3.14.

## Project layout

- `src/main.py`: CLI (`doctor`, `fix`, `ask`, `eval`, ...)
- `src/docsentinel/`: the package
- `src/evals/cases/`: eval cases (recorded LLM answers live in `src/docsentinel/recordings/`)
- `src/tests/`: test suite
- `src/demo_repo/python-dotenv/`: vendored python-dotenv (with its own LICENSE) used as the example repo
- `docs/`: design write-up, evidence, install guide
- `.github/workflows/`: reusable check/fix workflows and CI

## Security

Security in one line each: the AI can read but never write; a separate job without AI posts the comment or opens the draft PR. Settings come from your main branch, not the PR. Versions are pinned. PRs from forks run without AI. Details: [docs/install.md](docs/install.md).

## Install in your repo

One workflow file plus a 4-line config; a free dry run first. See [docs/install.md](docs/install.md).
