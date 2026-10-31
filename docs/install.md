# Install DocSentinel in a repo

## Trial in 5 minutes

Replace `<SHA>` everywhere below with the full 40-character commit SHA of DocSentinel you want to pin: the latest commit on `main` of [shubhamchitte/docsentinel](https://github.com/shubhamchitte/docsentinel) (`git ls-remote https://github.com/shubhamchitte/docsentinel main`, which needs read access). Use the same SHA in `uses:` and `docsentinel_ref`.

### 0. Dry run first, at no cost

What would it have flagged on your last 20 commits? This is rules only and writes nothing:
```bash
git clone https://github.com/shubhamchitte/docsentinel docsentinel  # needs read access
pip install ./docsentinel
docsentinel trial --repo . --last 20            # add --llm --max-calls 5 to let the Judge decide a few
```

### 1. Install = one workflow file + a 4-line config

([real example](https://github.com/shubhamchitte/python-dotenv/commit/41a95ec)):

`.docsentinel.yml`
```yaml
code: ["src/yourpkg/*.py"]
docs: ["README.md", "docs/*.md"]
exclude: ["CHANGELOG*", "tests/**"]
cli_name: yourcli          # optional (click CLIs): commands match only as "yourcli <cmd>"
```

`.github/workflows/docsentinel.yml`
```yaml
name: DocSentinel
on:
  pull_request:
  push:
    branches: [main]
permissions: {}
concurrency:
  group: docsentinel-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: true
jobs:
  check:   # flag mode: one comment per PR
    if: vars.DOCSENTINEL_ENABLED != 'false' && github.event_name == 'pull_request'
    uses: shubhamchitte/docsentinel/.github/workflows/docsentinel-check.yml@<SHA>
    permissions: { contents: read, pull-requests: write }
    with: { docsentinel_ref: <SHA> }
    secrets: { CLAUDE_KEY: "${{ secrets.CLAUDE_KEY }}", SLACK_WEBHOOK_URL: "${{ secrets.SLACK_WEBHOOK_URL }}" }
  # fix:   # fix mode: draft docs PR after merge. Uncomment, and see the note below.
  #   if: vars.DOCSENTINEL_ENABLED != 'false' && github.event_name == 'push'
  #   uses: shubhamchitte/docsentinel/.github/workflows/docsentinel-fix.yml@<SHA>
  #   permissions: { contents: write, pull-requests: write, actions: read }
  #   with: { docsentinel_ref: <SHA> }
  #   secrets: { CLAUDE_KEY: "${{ secrets.CLAUDE_KEY }}", SLACK_WEBHOOK_URL: "${{ secrets.SLACK_WEBHOOK_URL }}" }
```

Optional cost cap: `max_sections_per_run: 20` (default). Sections over the cap are reported as "skipped: cap reached", never silently dropped.

### 2. Protect the install

Add a CODEOWNERS rule so no PR can change the pinned DocSentinel version, turn on fix mode, or widen its scope without owner review ([example](https://github.com/shubhamchitte/python-dotenv/blob/main/.github/CODEOWNERS)):
```
/.github/workflows/  @your-team
/.docsentinel.yml    @your-team
```
Then turn on branch protection for `main` with "Require review from Code Owners". Without it, the CODEOWNERS file only suggests reviewers.

### 3. Add the secret

`CLAUDE_KEY` (from `claude setup-token`). Optional: `SLACK_WEBHOOK_URL` for a Slack message per result (counts and links only; the URL is the credential, so keep it a secret) and pass it under `secrets:` in the workflow. For an org, set it once as an org secret scoped to selected repositories.

| Stage | What you change | What it can write |
|---|---|---|
| Dry run | nothing (`docsentinel trial`) | nothing |
| Flag | the two files and the secret | one PR comment |
| Fix | uncomment `fix:` and turn on Settings → Actions → "Allow GitHub Actions to create and approve pull requests" | draft PRs on `docs-sentinel/*` branches; a human merges |
| Pause | repo variable `DOCSENTINEL_ENABLED=false` (instant, no commit) | nothing |
| Uninstall | delete the workflow file | nothing |

### What's safe about it
- Pinned: DocSentinel by commit SHA, the `claude` CLI version in CI and `run.yaml`, top-level Python deps to exact versions (`requirements.txt`; transitive deps of `mcp` are not locked), and third-party actions (SHA).
- The LLM job has a read-only token; writes happen in a separate job with no LLM.
- The config is read from the PR's base branch, so a PR can't change its own scope.
- PR code is parsed, never executed.
- Fork and Dependabot PRs (which get no secrets) run rules-only and pass. They're never failed.
- At merge, fix mode reuses the PR check's verdicts (only from this repo's workflow, and only for an identical change), so each change gets one decision.

From the command line on any repo:
```bash
docsentinel flag --repo /path/to/repo --base main --head my-branch       # report only
docsentinel fix  --repo /path/to/repo --base HEAD~1 --head HEAD --branch  # + local docs-sentinel/* branch
```

### Access: DocSentinel is a private repo

The reusable workflows check out DocSentinel at the pinned commit and install it from that checkout, so nothing is fetched from a public URL.
- Same repo (this one): `github.token` can read it, so nothing extra is needed.
- Other private repos of the same owner or org: in DocSentinel's settings, set *Actions → General → Access* to "Accessible from repositories owned by…", so they can call the workflows. Then give them read access to the code: either a GitHub App installation token (recommended for production: scoped, short-lived, auditable) or a fine-grained token with *Contents: read* on this one repo, stored as the secret `DOCSENTINEL_READ_TOKEN`.
- Public repos can't call workflows in a private repo. Publish versioned releases to a private package index instead, and install them pinned by version and hash.

## Plug it into other systems

```bash
# Outbound: signed webhook (Slack, Jira automation, Zapier, any HTTPS endpoint)
export DOCSENTINEL_WEBHOOK_SECRET=...            # shared with the receiver; never in config
python src/main.py fix --repo . --webhook https://hooks.example.com/docsentinel
python src/main.py hooks-demo                    # local receiver: genuine accepted, tampered/replayed rejected

# Inbound: MCP server for AI editors (read-only tools: ask_docs, check_docs)
claude mcp add docsentinel -- python /path/to/src/main.py mcp --repo /path/to/repo
python src/main.py mcp-demo                      # an MCP client lists and calls the tools
```

Receivers verify with `hooks.verify(secret, headers, body)`: HMAC-SHA256 of `"<timestamp>.<body>"`, rejected if older than 5 minutes. Configure several targets with `webhooks:` in `.docsentinel.yml` (`format: slack` for Slack incoming webhooks).

## LLM access

Every LLM call is `claude -p`. The CLI does not read `CLAUDE_KEY` itself, so DocSentinel copies it into the variable the CLI does read. It tries `CLAUDE_CODE_OAUTH_TOKEN` (for `sk-ant-oat…` tokens from `claude setup-token`) and `ANTHROPIC_API_KEY`, in that order.

| Situation | What happens |
|---|---|
| `CLAUDE_KEY` set and valid | Live calls |
| No `CLAUDE_KEY`, CLI already logged in | Live calls using that login |
| No CLI, or key invalid | Replay: recorded responses from `src/docsentinel/recordings/`; output says `LLM: replay` |

Force a mode with `DOCSENTINEL_LLM=live` (never use recordings) or `DOCSENTINEL_LLM=replay`. Recordings are made with `DOCSENTINEL_LLM=record`. If `claude` is not on PATH, set `DOCSENTINEL_CLAUDE_BIN=/path/to/claude`.
