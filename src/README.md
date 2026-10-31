# Code map

```
main.py                 entry point (fixed hash seed, one-line errors) -> docsentinel/cli.py
                        commands: doctor | flag | fix | ask | trial | eval | notify | mcp | mcp-demo | hooks-demo
docsentinel/
  scout.py              1 Change Scout     public API diff between two commits (ast)
  docmap.py             2 Doc Mapper       doc sections that mention a changed thing
  flag.py                 rules            stale / candidate / gap findings
  judge.py              3 Drift Judge      LLM: is this candidate really stale?
  fixer.py              4 Doc Fixer        renames by code, the rest by LLM
  verifier.py           5 Verifier         checks every fix before it is used
  publish.py            6 Publisher        patch, local branch, draft PR
  concierge.py          7 Docs Concierge   agent with read-only tools
  reviewer.py             Fix Reviewer     LLM: is the fix's meaning right?
  hooks.py              outbound           signed webhooks (JSON / Slack)
  mcp_server.py         inbound            MCP server for AI editors (read-only tools)
  llm.py                the only path to the LLM (claude -p), record/replay
  prompts/              system prompts for the four LLM roles
  recordings/           recorded LLM responses used in replay mode
  evals.py, scenario.py eval harness and throwaway-repo builder
  cli.py                the commands; trial = dry run on history, notify = send a report to Slack/webhooks
  config.py, gitio.py, report.py
evals/cases/            11 eval cases (code edits + expected answer)
tests/                  run_all.py runs both suites (no network, no LLM):
                        test_guardrails.py  bad fixes, webhook signing, Slack content
                        test_workflows.py   real workflow shell steps vs a local git remote + stub gh
demo_repo/              python-dotenv v1.2.4 snapshot + settings for the GitHub Action
```
