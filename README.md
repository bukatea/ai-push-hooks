# ai-push-hooks

[![CI](https://github.com/shanebishop1/ai-push-hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/shanebishop1/ai-push-hooks/actions/workflows/ci.yml) [![npm](https://img.shields.io/npm/v/ai-push-hooks)](https://www.npmjs.com/package/ai-push-hooks)

**Agentic linting for the rules your coding agent forgot.**

`AGENTS.md` tells an agent how to work. **ai-push-hooks checks whether it followed through.** Think of it as the inverse of `AGENTS.md`: a second pass over outgoing changes before `git push`, catching guidelines the agent forgot or neglected.

Use **OpenCode, Codex, Claude Code, or your own agentic CLI** to check rules that need judgment, not just a regex. Report violations, apply scoped fixes, and block pushes with explicit checks. It's a hedge against missed instructions, not a guarantee that AI catches everything.

## Rules Worth Checking

| Your rule | What the check looks for |
| --- | --- |
| No empty marketing speak. | Vague claims and filler in changed pages. |
| Use our typed API client. | Components making direct backend requests. |
| Test behavior, including failures. | Changed behavior without meaningful test coverage. |
| Keep migrations backward-compatible. | Destructive changes that break a rolling deployment. |

Write your own rules in prompts or supply a rules file as context. These are examples of checks you configure, not built-in guarantees.

## Why This Tool?

Keep your coding workflow; add a check before Git sends the commits. ai-push-hooks combines repository-specific AI checks, scripts, and explicit pass/fail gates around the outgoing diff, using your existing coding CLIs. No daemon or hosted review service is required.

Use it alongside [Lefthook](https://github.com/evilmartians/lefthook) for hook management and [roborev](https://github.com/kenn-io/roborev) for background reviews. [Archon](https://github.com/coleam00/Archon) and [TAKT](https://github.com/nrslib/takt) orchestrate development tasks; this tool deliberately stays narrower. Its job is to check your rules before a push, not take over how you build the project.

## Quick Start

Install the [ai-push-hooks skill](https://github.com/shanebishop1/ai-push-hooks/blob/main/skills/ai-push-hooks/SKILL.md), tell your agent what intelligent checks you want before pushes, and let it set up the modules for you.

```bash
npm install --save-dev ai-push-hooks
npx --no-install ai-push-hooks init
npx --no-install ai-push-hooks install
```

Install and authenticate your chosen AI CLI. The current `init` starter checks documentation; replace its configuration with the rules-checking example below to check `AGENTS.md` instead. Then push normally.

[Other installation options](docs/configuration.md#installation) | [Existing hook managers](docs/configuration.md#hook-managers)

## Example: Check Your Rules

This workflow supplies the outgoing diff and your root `AGENTS.md` explicitly, asks for violations, and blocks if any are reported. It does not rely on the runner automatically loading agent instructions.

In `checks/hooks.py`:

```python
import json

from ai_push_hooks.plugins import CollectorResult, PluginContext


def collect_rules(context: PluginContext) -> CollectorResult:
    return CollectorResult(artifacts={
        "push.diff": context.push.diff_text,
        "rules.txt": (context.repo_root / "AGENTS.md").read_text(encoding="utf-8"),
    })


def assert_rules(context: PluginContext) -> dict:
    issues = json.loads(context.inputs["review/issues.json"].read_text(encoding="utf-8"))
    return {"ok": issues == [], "message": "Review findings in review/issues.json."}
```

In `ai-push-hooks.toml`:

```toml
[llm]
runner = "opencode"

[runners.opencode]
type = "opencode"
model = "openai/gpt-5.6-luna"
project_access = "project"

[workflow]
modules = ["rules"]

[[modules.rules.steps]]
id = "collect"
type = "collect"
python = "checks/hooks.py:collect_rules"

[[modules.rules.steps]]
id = "review"
type = "ask"
prompt = "Check the outgoing diff against rules.txt. Inspect repository context as needed. Report only concrete violations introduced by these changes, with file and description fields. Return a JSON array, or [] if none. Do not edit files."
inputs = ["collect/push.diff", "collect/rules.txt"]
output = "issues.json"
schema = "docs_issue_array"

[[modules.rules.steps]]
id = "gate"
type = "assert"
python = "checks/hooks.py:assert_rules"
inputs = ["review/issues.json"]
```

`docs_issue_array` is the existing schema name for `{file, description}` findings; it works for code rules too. A missing rules file or a failed check blocks the push by default. Findings live in the run artifacts under `.git/ai-push-hooks/`.

**Want fixes too?** Add an `apply` step with the findings, your rules, and an explicit `allow_paths` list, then recheck and run tests. [Runner completion is not proof that a fix landed](docs/configuration.md#deterministic-postconditions-after-apply): verify the result. By default applied edits are left uncommitted: review the diff, commit approved changes, and retry the push. To shorten that loop, [`auto_commit`](docs/configuration.md#committing-applied-edits) commits the fix for you; the push still stops, because Git had already scoped it to the pre-fix commit, so you re-run `git push` to send it.

## Build Your Workflow

Each module combines the steps it needs:

| Step | Purpose |
| --- | --- |
| `collect` | Gather the outgoing diff and relevant context. |
| `ask` | Ask AI for findings or another response. |
| `apply` | Edit a temporary workspace; propagate only allowlisted changes. |
| `exec` | Run scripts, tests, or other actions. |
| `assert` | Evaluate a verdict and block the push if it fails. |

`ask` alone does not block on findings; add an `assert` gate. Combine AI review with your existing linters and tests, not instead of them. Independent analysis can run concurrently; built-in `exec`, `assert`, and `apply` steps are serialized. Trusted custom command runners, including project-access `ask` commands, are not enforced read-only.

## Choose Your AI

OpenCode, Codex, and Claude Code have first-class adapters. OpenCode defaults to **`openai/gpt-5.6-luna`**. Explicit runner profiles use their own model settings. To use Codex or Claude Code, add its profile and change `[llm].runner`:

```toml
[runners.codex]
type = "codex"

[runners.claude]
type = "claude"
```

Each `ask` or `apply` step can override the runner, so one tool can review and another can fix. **Any other agentic CLI, including Pi, can integrate through a [custom command runner](docs/configuration.md#custom-runners)**: supply a noninteractive command or a thin wrapper. Use model identifiers available to your provider.

For **source evaluation only**, the partial OpenCode V2 CLI-generation candidate
targets `2.0.25` with this global opt-in (the default remains `"legacy"`):

```toml
[llm]
opencode_cli_generation = "v2"
```

This selects `--standalone`, **not `--pure` equivalence or production-supported
V2**. V2's stored well-known configuration/plugin-loading gap remains unresolved;
the inherited environment and inline config payloads do not remove it. V2
permission/plugin isolation and native lifecycle behavior are not qualified.
See [scope, syntax and restrictions](docs/configuration.md#opencode-cli-generation-partial-source-only)
and [Security](SECURITY.md). Existing examples continue to use legacy behavior.

## Control And Safety

- Errors block pushes by default. AI judgments can still miss violations or report false positives.
- `apply` validates file and Git state and limits propagated edits to `allow_paths`. It does not commit unless you set `auto_commit`, and never pushes.
- Runners, scripts, and callbacks are trusted local programs, not an OS sandbox.
- Local hooks can be bypassed; retain CI for required enforcement.
- Repository content may be sent to your model provider. Review its privacy and billing terms.

Logs and run summaries live under `.git/ai-push-hooks/`. OpenCode transcripts are captured there by default. To intentionally skip one push: `AI_PUSH_HOOKS_SKIP=1 git push`.

[Configuration](docs/configuration.md) | [Security](SECURITY.md) | [Contributing](CONTRIBUTING.md) | [Changelog](CHANGELOG.md) | [MIT License](LICENSE)
