# Configuration

Define workflows in `ai-push-hooks.toml` at the repository root. Start with the [README example](../README.md#example-check-your-rules) or the [starter configuration](../ai-push-hooks.toml).

## Installation

The [npm quick start](../README.md#quick-start) installs a repository-local wrapper. Its tested/supported Python range is 3.10-3.13; the Python code requires 3.10+ and the launcher may probe newer interpreters, but newer versions are not part of that compatibility claim. The npm package does not bundle Python. The wrapper starts the selected interpreter in Python isolated mode: it ignores `PYTHONPATH` and the user site directory, then adds only the package's shipped `src` and vendored Tomli wheel. The interpreter's normal system or virtual-environment site-packages remain available.

If a repository callback needs an external dependency, install it with the exact Python executable the wrapper will select (for example, `/path/to/python -m pip install <dependency>`), into that interpreter's normal system or virtual-environment site-packages. The wrapper tests candidates in its ordered list (`python3.14`, `python3.13`, `python3.12`, `python3.11`, `python3.10`, `python3`, then `python`), so activating a virtual environment alone does not guarantee that it wins over a higher-priority executable elsewhere on `PATH`; make the intended candidate discoverable first. Do not rely on `pip install --user` or `PYTHONPATH`; they are intentionally not part of the npm launch path.

For pnpm:

```bash
pnpm add -D ai-push-hooks
pnpm exec ai-push-hooks init --template minimal-docs
pnpm exec ai-push-hooks install
```

For a Python-only installation:

```bash
python -m pip install ai-push-hooks
ai-push-hooks init --template minimal-docs
ai-push-hooks install
```

`uv tool install ai-push-hooks` or `pipx install ai-push-hooks` can replace the pip command.

AI steps need the selected CLI installed and authenticated with an available model. Deterministic-only workflows do not need one. `init` and `install` refuse to overwrite existing files; `--force` explicitly replaces them.

## Modules And Steps

`[workflow].modules` selects the modules to run. Define each module under `modules.<name>` with a non-empty list of steps. Modules are enabled by default; set `enabled = false` to disable one.

Step inputs refer to earlier artifacts in the same module: `collect/push.diff`, for example. Cross-module inputs are not supported. Independent `collect` and `ask` work may run concurrently; built-in `exec`, `assert`, and `apply` steps are serialized. Trusted custom command runners, including project-access `ask` commands, are not enforced read-only.

| Field | Used by | Meaning |
| --- | --- | --- |
| `id` | All | Unique step name within the module. |
| `type` | All | `collect`, `ask`, `apply`, `exec`, or `assert`. |
| `inputs` | Non-collector steps | Earlier module-local artifacts. |
| `when_env` | All | Run only when the named environment variable is true. |
| `collector` | `collect` | Built-in collector name. |
| `prompt` | `ask`, `apply` | Inline instructions. |
| `prompt_file` | `ask`, `apply` | Repository-relative prompt file. |
| `fallback_prompt_id` | `ask`, `apply` | Built-in prompt name. |
| `runner` | `ask`, `apply` | Override the default runner profile. |
| `output` | `ask` | Required response artifact filename. |
| `schema` | `ask` | Optional JSON schema name. Omit for plain text. |
| `allow_paths` | `apply` | Required list of permitted edit globs. |
| `auto_commit` | `apply` | Commit the propagated edits. Default `false`. |
| `auto_push` | `apply` | Push that commit. Requires `auto_commit`. Default `false`. |
| `commit_message` | `apply` | Fixed commit subject. Requires `auto_commit`. |
| `executor` | `exec` | Built-in action name. |
| `assertion` | `assert` | Built-in assertion name. |
| `python` | `collect`, `exec`, `assert` | Repository-local callback reference. |
| `options` | Python steps | JSON-compatible callback options. |
| `command` | `exec`, `assert` | Direct argv command. |
| `stdin` | Command steps | One declared input to send on stdin. |
| `timeout_seconds` | Command steps | Positive timeout; default `60`. |

Choose one handler per deterministic step: a built-in, a callback, or a command. Prompt resolution uses `prompt`, then `prompt_file`, then `fallback_prompt_id`.

`ask` produces a response, not an automatic policy verdict. Use `assert` to evaluate findings when they should block. `apply` can run without a preceding `ask`; an input ending in `issues.json` containing `[]` skips the apply step.

Upgrading from 0.2.1? Rename `type = "llm"` steps to `type = "ask"`.

## Runner Profiles

`[llm].runner` selects the default; a step's `runner` overrides it. Referenced profiles must exist under `[runners.<name>]`, except for the implicit OpenCode default.

OpenCode, Codex, and Claude are first-class adapters. The generic `command` type is for other agentic CLIs and follows the custom runner contract below.

| Profile field | Values / behavior |
| --- | --- |
| `type` | Required: `opencode`, `codex`, `claude`, or `command`. |
| `model` | Runner-specific model identifier. |
| `project_access` | `artifacts` or `project`; OpenCode defaults to `artifacts`, others to `project`. |
| `variant` | Optional legacy OpenCode variant; nonempty values are rejected for the V2 CLI seam. |
| `command` | Required argv array for command runners. |
| `prompt_transport` | Command runners: `stdin` (default) or `argv`. |

Without an explicit OpenCode profile, `[llm].model` and `[llm].variant` configure it. Explicit profiles use their own model settings. `AI_PUSH_HOOKS_MODEL` overrides the selected profile's model.

Artifact mode supplies collected inputs in a scratch directory. Project mode lets analysis inspect the checkout. Apply always uses a temporary staging copy, with propagation limited by `allow_paths`.

The default legacy OpenCode generation runs with isolated configuration and permissions; project/global configuration, external plugins, and MCP servers are not loaded. This claim does **not** extend to the partial V2 CLI seam below. Codex uses read-only analysis and workspace-write apply modes. Claude Code uses separate analysis and editing permissions. Authenticate the CLI before using it in a hook.

Apply requires a single pushed branch whose local commit is the checked-out `HEAD`. Staging excludes Git metadata, `AGENTS.md`, ignored files, symlinks, and special files. These controls are not an OS sandbox or an automatic rollback system. See [Security](../SECURITY.md).

### OpenCode CLI generation (partial source-only)

`[llm].opencode_cli_generation` accepts exactly `"legacy"` (default) or `"v2"`.
It is global for every `type = "opencode"` profile, including implicit, explicit
and per-step selections. There is no per-profile selector, environment override,
version autodetection, probe, server fallback or generic extra-argv setting.
Other runner types are unchanged. For source evaluation only:

```toml
[llm]
opencode_cli_generation = "v2"
```

**This is a partial source-only CLI-generation candidate targeting OpenCode
2.0.25, not production-supported V2 or a security qualification.** Its
`--standalone` selects CLI-owned standalone connection handling; it is **not**
equivalent to legacy `--pure`. Stored well-known configuration can still load
plugins in V2. Inherited isolated HOME/config/cache/state, filtered provider
environment, project-disable flags and inline `plugin: []`/permission payloads
do not establish removal of that gap. Shared provider data/session storage is
retained unchanged; effective V2 config/plugin exclusion, permission enforcement
and native run/export/delete behavior remain unqualified. See [Security](../SECURITY.md).

Unknown selector strings, whitespace/case variants and non-string values fail
validation. V2 rejects nonempty separate `variant` values from `[llm]`, any
OpenCode profile (even unselected), final `AI_PUSH_HOOKS_VARIANT` overrides and
direct adapter requests, including finalize-only requests, before launching a
child. Empty variants are accepted. Models are passed verbatim; there is no
model/variant rewriting. `AI_PUSH_HOOKS_MODEL` still overrides the effective
selected profile's model. More than 100 V2 attachments are rejected before
materialization or launch; legacy attachment behavior is unchanged.

The exact V2 argv order is (brackets below denote optional groups, not literal
arguments):

```text
opencode run --agent AGENT --standalone --format json [--model MODEL] (--session SID | --title TITLE) [--file PATH ...] -- PROMPT
opencode session export --standalone SID
opencode session delete --standalone SID
```

Each attachment uses its own `--file PATH` pair, in input order. Fresh calls get
a title; resumed calls keep the exact session ID. There is exactly one
`--standalone` per call, no `--pure`, `--variant`, server URL or permission-skip
flag. The unchanged legacy vectors use `run --agent AGENT --pure --format json`,
optional model/variant, session-or-title, file pairs and `-- PROMPT`;
`export SID --pure`; and `session delete SID --pure`.
Source syntax evidence is the tagged
[v2.0.25 command definitions](https://github.com/anomalyco/opencode/blob/v2.0.25/packages/cli/src/commands/commands.ts)
and [connection selector](https://github.com/anomalyco/opencode/blob/v2.0.25/packages/cli/src/services/server-connection.ts).
Fake-process source tests establish emitted argv and host staging/environment
policy only, not V2 parser, permission engine, plugin loader, provider or native
lifecycle acceptance. Existing transcript/deletion toggles and best-effort
failure behavior remain unchanged; this setting is not adoption/release approval.

### Live validation snapshot

Live read/review and apply checks on Linux:

| Date | Runner (CLI; model) | Review | Apply |
| --- | --- | --- | --- |
| 2026-09-12 | OpenCode (1.18.29; `openai/gpt-5.6-luna`) | Passed | Passed |
| 2026-09-12 | Claude Code (2.1.220; `sonnet`) | Passed | Passed |
| 2026-09-14 | Codex (0.152.0; `gpt-5.6-luna`) | Passed | Passed |

The Codex check read a random value from a disposable repository, then verified
that only the allowlisted README changed. These checks establish the tested
operations, not compatibility with every model or platform.

#### Local troubleshooting: Codex sandbox

For Codex 0.152, the standalone no-AI sandbox syntax is
`codex sandbox -- /usr/bin/true`. Resolve host restrictions rather than disabling
protections, and use a deterministic postcondition to verify the desired checkout
outcome after an apply.

### Apply and manual commits

`apply` is generic: it can edit any eligible checkout file matching `allow_paths`; it is not limited to Markdown. The runner edits a temporary staging copy, and only validated changes propagate back to the checkout. Those edits never enter the commit already being pushed. `apply` creates no Git commit unless [`auto_commit`](#committing-and-pushing-applied-edits) is set, and it refuses to commit a file that already had uncommitted changes.

The existing `docs_apply_requires_manual_commit` assertion is a workflow gate, not human-review enforcement. It prevents the original push from passing after `apply` changes files; it cannot prove that anyone reviewed the edits or that they conform to policy. Add it after the `apply` step when that gate is desired:

```toml
[[modules.docs.steps]]
id = "manual-commit"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
```

Do not combine this assertion with `auto_commit`. Commits are made only after
every step passes, so this gate fires first and nothing is ever committed. Use
one or the other: this assertion to stop for manual review, or `auto_commit` to
have the commit made for you.

The assertion checks `apply/result.json`'s `changed_files` and intentionally blocks when edits were propagated. Review `git diff`, run the relevant checks, commit the approved changes, and retry the push. On the retry, the assertion passes when the apply step reports no changes.

### Committing and pushing applied edits

`apply` does not commit by default. Two opt-ins change that:

```toml
[[modules.rules.steps]]
id = "fix"
type = "apply"
prompt = "Fix the violations listed in issues.json."
allow_paths = ["src/**"]
auto_commit = true
auto_push = true                               # requires auto_commit
# commit_message = "fix: satisfy house rules"  # omit to let the runner name it
```

**The commit happens after the whole workflow passes, not inside the `apply`
step.** Steps placed after `apply` -- a test suite, a deterministic
postcondition, an `assert` gate -- must retain the ability to reject the edits,
and they cannot do that if the commit has already been made. If any later step
fails, nothing is committed and the applied edits are left in the working tree
for you to inspect. Put your verification after `apply`, as usual:

```toml
[[modules.rules.steps]]
id = "verify"
type = "exec"
command = ["{python}", "-m", "pytest", "-q"]
```

**The push in flight always stops when a commit is made.** Git selects the
commits a push will send before the pre-push hook runs, and passes those SHAs to
the hook on stdin. A commit created during the hook is therefore not part of that
push. If the hook allowed the push to proceed, Git would send the pre-fix commit
and report success while the fix stayed on your machine. So the push stops, and
you run `git push` again -- which then succeeds normally, carrying the fix.

With `auto_push` the hook pushes the fix itself, so one `git push` is all you
run. The command still reports failure, because the update it was holding did not
happen -- so the output is a numbered ledger that says which push is which:

```
The fix is on the remote. Nothing further is needed.

  committed f85b7cf2dad2  refunds: route refund call through the typed client
    src/components/RefundPanel.tsx

  push 1  fb03764fe6c2  FAILED     scoped to the pre-fix commit, superseded
  push 2  f85b7cf2dad2  SUCCEEDED  pushed to origin refs/heads/feature/violations

Git prints `error: failed to push some refs` below. That is push 1.
Push 2 carried the fix and succeeded.
```

Push 1 is the one you ran; it can never carry the fix. Push 2 is the hook's, and
it does. `FAILED` and `SUCCEEDED` are coloured on a terminal, and plain whenever
stderr is not a TTY or `NO_COLOR` is set.

If the nested push fails, the fix stays committed locally and the output falls
back to asking you to push again -- it never claims a push that did not happen.

This stop ignores `allow_push_on_error`. Fail-open is a policy choice about
checks that could not complete; it is not a licence to push code the run has
already superseded locally.

If the fix touches a file you already had uncommitted changes in, nothing is
committed at all. `git commit -- <file>` commits the whole file, so committing it
would capture work you never staged -- and with `auto_push`, publish it. The run
stops instead and names the files, leaving your working tree for you to sort out.

If the repository signs commits (`commit.gpgsign`), the commit is signed like any
other. A signing agent that needs an interactive passphrase cannot prompt from
inside a hook; the commit fails on the 120-second command timeout and blocks the
push rather than hanging. Unlock the agent first, or leave `auto_commit` off in
repositories that sign interactively.

The commit is made by ai-push-hooks, not by the runner. Staging excludes `.git`,
so the runner has no repository to commit to, and post-propagation verification
fails the step if the checkout's index or Git metadata moved while it ran. The
commit is limited by pathspec to the files `apply` actually propagated, so
unrelated modified files in your working tree are never swept in. 
Omit `commit_message` and the runner that made the fix names the commit: it is
asked to end its response with a `COMMIT: <subject>` line. That subject is
untrusted text, held to the same rules as a configured one (single line, no
control characters, at most 72 characters, never leading `-`). A missing or
malformed subject falls back to a generic one rather than failing the step.

#### Driving this from a script or an agent

Nothing here is interactive. The hook exits, `git push` returns non-zero, and
control returns immediately; there is no prompt and nothing to confirm.

Git collapses every nonzero pre-push hook exit to `1`, so exit status cannot
tell "blocked, stop" apart from "fixed, push again". Match the final line of
stderr instead:

```
ai-push-hooks Push Again - b99af43fcdc6 is committed locally but not on the
remote. Run `git push` again to send it.
```

Exactly one marker is emitted, and only when a commit was made:

| Final line begins | Meaning | Caller should |
| --- | --- | --- |
| `ai-push-hooks Push Success` | The fix is on the remote. | Treat as success. Do not push again. |
| `ai-push-hooks Push Again` | The fix is committed but not sent. | Run `git push` again. |
| neither | A check failed. | Stop and read the failure. |

Both phrases are a stable contract; the detail after them may be reworded.

```bash
out=$(git push 2>&1) || true
case "$out" in
  *"ai-push-hooks Push Success"*) echo "fix pushed" ;;
  *"ai-push-hooks Push Again"*)   git push ;;
  *)                              echo "$out"; exit 1 ;;
esac
```

The retry terminates: on the second run the fix is already committed, so `apply`
propagates nothing, no commit is made, and the push proceeds. A check that keeps
failing blocks without the marker, so a retry loop never spins on it.

`auto_commit` does not review the change for you. It shortens the loop between a
finding and a fix; it does not establish that the fix is correct. Pair it with a
deterministic postcondition, below, when the desired result can be checked.

### Deterministic postconditions after apply

An apply process succeeding, or reporting `changed_files = []`, is not proof that
the requested result is present. The latter can simply mean that the apply was a
legitimate no-op because the checkout was already correct. Add a deterministic
postcondition after `apply` and before the manual-commit gate when the desired
file content has a precise representation:

```toml
[general]
require_clean_worktree = true

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = "In the existing README.md, replace 'Release note: DRAFT.' with 'Release note: READY.' and make no other changes."
allow_paths = ["README.md"]

[[modules.docs.steps]]
id = "postcondition"
type = "assert"
command = [
  "{python}",
  "-c",
  "import pathlib, sys; sys.exit(0 if pathlib.Path('README.md').read_text(encoding='utf-8') == 'Release note: READY.\\n' else 1)",
]
inputs = ["apply/result.json"]

[[modules.docs.steps]]
id = "manual-commit"
type = "assert"
assertion = "docs_apply_requires_manual_commit"
inputs = ["apply/result.json"]
```

This is an intentional synthetic example, not a recommendation to overwrite a
real README: its fixture starts with exactly `Release note: DRAFT.\n`, and its
desired full content is exactly `Release note: READY.\n`.
The command is a direct argv vector rather than a Python `assert`; Python
optimization must not be able to remove the check. The postcondition reads the
checkout after apply, not the commit being pushed. Keep the clean-worktree
workflow setting for the hook's starting state and the manual-commit gate for
the intentionally dirty post-apply checkout: review the resulting diff, run
relevant checks, commit it, and retry the push. This gate and postcondition
still do not prove human review, semantic correctness, or that an agent
complied with every instruction.

## Custom Runners

Use a `command` profile to invoke any other agentic CLI, including Pi, directly or through a thin wrapper. A wrapper can normalize JSONL events into a final response. The runner must be noninteractive: read the prompt from stdin or argv, write only the final response to stdout, and use exit codes to report success or failure.

```toml
[runners.custom]
type = "command"
command = ["/absolute/path/to/scripts/review-agent"]
prompt_transport = "stdin"
project_access = "project"
```

Select it with `runner = "custom"` on an `ask` or `apply` step. The runner receives the full instruction and artifact packet. For apply, its working directory is the staging copy.

Commands are argv arrays, not shell strings. Whole-argument placeholders are `{model}`, `{cwd}`, `{stage}`, and `{prompt}`. The `argv` transport requires exactly one `{prompt}` argument; stdin avoids exposing prompts in process listings. Custom programs inherit the user's environment, including authentication variables, and manage their own permissions and session setup/cleanup. For apply, the working directory is the staging copy.

## Commands And Callbacks

Command steps run in the real repository, with no implicit shell. Exit zero means success; a nonzero exit blocks the workflow. stdout, stderr, and a `result.json` report are saved as step artifacts.

```toml
[[modules.verify.steps]]
id = "tests"
type = "exec"
command = ["{python}", "-m", "pytest", "-q"]
timeout_seconds = 300
```

Available whole-argument placeholders are `{repo}`, `{python}`, and `{input:<step/artifact>}`. Input placeholders must also appear in `inputs`. Stdin is closed unless `stdin` names a declared input. Command output is bounded to 16 MiB per stream.

For custom context or policy, reference a top-level synchronous Python function:

```toml
[[modules.policy.steps]]
id = "change-size"
type = "assert"
python = "checks/hooks.py:check_size"
options = { max_files = 25 }
```

In `checks/hooks.py`:

```python
from ai_push_hooks.plugins import PluginContext


def check_size(context: PluginContext) -> dict:
    ok = len(context.push.changed_files) <= context.options["max_files"]
    return {"ok": ok, "message": "Change exceeds the configured file limit." if not ok else ""}
```

Add `policy` to `[workflow].modules` to enable it. Callbacks receive repository and step identifiers, push facts, input paths, options, prior module metadata, and a logger.

| Callback type | Return value |
| --- | --- |
| `collect` | `CollectorResult` from `ai_push_hooks.plugins`, with artifacts and metadata. |
| `exec` | JSON-serializable dictionary, saved as `result.json`. |
| `assert` | Dictionary with boolean `ok` and optional string `message`; false blocks. |

Callbacks run as trusted in-process code with already-installed dependencies. There is no callback timeout or automatic dependency installation. Collectors may run concurrently, so custom collectors must be concurrency-safe.

## Built-In Handlers

| Kind | Name | Purpose |
| --- | --- | --- |
| Collector | `docs_context` | Push diff, changed files, docs excerpts, recent commits. |
| Collector | `beads_status_context` | Branch and Beads task context. |
| Collector | `pr_context` | Context for a pull request. |
| Action | `beads_alignment` | Apply supported `bd update` / `bd close` operations. |
| Action | `gh_pr_create` | Create or reuse a PR through `gh`. |
| Assertion | `docs_apply_requires_manual_commit` | Block after applied docs changes. |
| Assertion | `beads_alignment_clean` | Block on unresolved task alignment. |

Beads steps need the native `bd` CLI; PR creation needs `gh`. Gate optional actions with `when_env = "AI_PUSH_HOOKS_CREATE_PR"`, for example. Beads schema and database maintenance remain separate from hook execution.

| JSON schema | Payload |
| --- | --- |
| `string_array` | Array of strings. |
| `docs_issue_array` | Array of objects with `file` and `description`. |
| `beads_alignment_result` | Object with optional `commands` string array. |
| `pr_create_payload` | Object containing PR creation fields. |

Built-in prompts: `docs-query-basic`, `docs-analysis-basic`, `docs-apply-basic`, `beads-plan-basic`, and `pr-compose-basic`.

## Settings

These optional settings supplement the workflow and runner definitions:

| Section | Key | Default |
| --- | --- | --- |
| `general` | `enabled` | `true` |
| `general` | `allow_push_on_error` | `false` |
| `general` | `require_clean_worktree` | `false` |
| `general` | `skip_on_sync_branch` | `true` |
| `general` | `base_branch` | `"main"` |
| `llm` | `runner` | `"opencode"` |
| `llm` | `opencode_cli_generation` | `"legacy"`; `"v2"` is a partial source-only CLI seam, not qualified V2 isolation |
| `llm` | `model` | `"openai/gpt-5.6-luna"` (implicit OpenCode profile) |
| `llm` | `variant` | `""` |
| `llm` | `timeout_seconds` | `800` |
| `llm` | `max_parallel` | `2` |
| `llm` | `json_max_retries` | `2` |
| `llm` | `invalid_json_feedback_max_chars` | `6000` |
| `llm` | `json_retry_new_session` | `true` |
| `llm` | `delete_session_after_run` | `true` |
| `llm` | `max_diff_bytes` | `180000` |
| `llm` | `session_title_prefix` | `"ai-push-hooks"` |
| `logging` | `level` | `"status"` (`info` and `debug` also available) |
| `logging` | `jsonl` | `true` |
| `logging` | `capture_llm_transcript` | `true` (OpenCode only) |
| `logging` | `print_llm_output` | `false` |
| `logging` | `dir` | `".git/ai-push-hooks/logs"` |
| `logging` | `transcript_dir` | `".git/ai-push-hooks/transcripts"` |
| `logging` | `summary_dir` | `".git/ai-push-hooks/summaries"` |

Model availability depends on the provider. Select one available to your account rather than assuming the starter's model is accessible.

## Environment Overrides

| Variable | Effect |
| --- | --- |
| `AI_PUSH_HOOKS_SKIP=1` | Skip the hook before loading configuration. |
| `AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR=1` | Log errors but allow the push. |
| `AI_PUSH_HOOKS_REQUIRE_CLEAN=1` | Require a clean worktree. |
| `AI_PUSH_HOOKS_ALLOW_DIRTY=1` | Allow a dirty worktree. |
| `AI_PUSH_HOOKS_BASE_BRANCH` | Override the base branch. |
| `AI_PUSH_HOOKS_MODEL` | Override the selected runner's model. |
| `AI_PUSH_HOOKS_VARIANT` | Override the legacy OpenCode variant; nonempty values are rejected for V2. |
| `AI_PUSH_HOOKS_TIMEOUT_SECONDS` | Override the runner timeout. |
| `AI_PUSH_HOOKS_LOG_LEVEL` | Set console verbosity. |
| `AI_PUSH_HOOKS_PRINT_LLM_OUTPUT=1` | Print normalized, redacted final responses; sensitive opt-in. |

Boolean values accept `1/0`, `true/false`, `yes/no`, `y/n`, and `on/off`. Skips and fail-open settings are explicit policy choices, not successful checks.

## Hook Managers

The built-in `install` command creates a repository-local pre-push hook without changing Git configuration. Existing hooks require explicit `--force` replacement; shared/external paths and symlinks are refused. Keep the installed Python executable or local npm package available at its installed location.

For Lefthook, use this in `lefthook.yml` instead of the built-in installer:

```yaml
pre-push:
  commands:
    ai-checks:
      run: npx --no-install ai-push-hooks hook {1} {2}
      use_stdin: true
```

Run `lefthook install`. For a Python installation, omit the `npx --no-install` prefix. Other hook managers must likewise forward Git's remote arguments, ref-update stdin, and exit status. If earlier commands consume stdin, capture it first and replay it to ai-push-hooks.

To remove the generated hook, inspect `git rev-parse --git-path hooks/pre-push` and remove only the ai-push-hooks delegate or its entry in your hook manager. Preserve unrelated hook commands.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Hook does not run | Reinstall with your chosen hook manager and inspect `git config --get core.hooksPath`. |
| Missing runner or authentication failure | Ensure the CLI is on the hook's `PATH`, authenticated, and configured with an available model. |
| Unknown profile | Match `runner` to an existing `[runners.<name>]`. |
| Runner capability error | Update the CLI to a version with the required adapter flags. |
| Invalid JSON | Check the prompt and schema; invalid JSON is retried twice by default. |
| Push blocked after edits | Review `git diff`, run checks, commit approved changes, and retry the push. |
| Need diagnostics | Inspect `.git/ai-push-hooks/logs` and `.git/ai-push-hooks/summaries`. |

For validation commands, see [Contributing](../CONTRIBUTING.md).
