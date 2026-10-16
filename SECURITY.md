# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Report it privately with a [GitHub Security Advisory](https://github.com/shanebishop1/ai-push-hooks/security/advisories/new), including affected versions, impact, reproduction details, and any suggested mitigation. Remove credentials, proprietary source, and unneeded transcript content. Maintainers will acknowledge the report, investigate it, and coordinate disclosure and a fix through the advisory. Only the latest release is actively supported with security fixes.

## Threat model and data handling

ai-push-hooks treats repository content, Git paths and metadata, configuration, model output, and concurrent local filesystem changes as potentially unsafe. Its controls constrain model-visible inputs and apply destinations, protect Git metadata and instruction files, validate filesystem state before propagation, and fail closed by default. They are designed to prevent accidental or model-directed changes outside configured boundaries, not to protect against a malicious user or process with the same operating-system permissions.

Every selected runner is a separate local process. Diffs, changed-file context,
prompts, artifacts, and (in project mode) more repository content can therefore
leave the machine under the selected provider's terms. OpenCode retains its
existing authentication data directory and forwards recognized provider
environment variables, including `OPENAI_API_KEY`; OpenCode itself chooses the
authentication path. Codex, Claude, and custom commands inherit the user's
normal environment/home needed by their tooling. Authentication is user-owned:
ai-push-hooks does not log environment values, manage credentials, invoke login,
or provide a credential broker. Do not commit secrets, and review provider
retention, privacy, and billing policies before use on sensitive repositories.

The default legacy OpenCode generation's `--pure` and isolated configuration exclude external plugins,
project/global configuration, MCP servers, instructions, and global custom
providers while retaining built-in plugins such as Codex OAuth. The default
OpenCode profile remains artifact-only; project access is an explicit opt-in.

**The opt-in `[llm].opencode_cli_generation = "v2"` is a partial source-only
CLI-generation candidate targeting 2.0.25, not production-supported V2 or
qualified `--pure`/permission/plugin isolation equivalence.** `--standalone`
selects connection ownership, not the legacy pure boundary. V2 can still load
stored well-known configuration and its plugins from shared data/session storage.
Isolated HOME/config/cache/state, project-disable flags, `OPENCODE_PURE`, inline
`plugin: []` and deny/allow permission payloads do **not** establish exclusion of
that configuration/plugin-loading path. Shared provider data is intentionally
unchanged; no credential copying or security-policy redesign is included.
The inherited host-side environment, staging, allowlist and private-artifact
controls are retained, but effective V2 permission enforcement, config/plugin
exclusion and native lifecycle behavior are unqualified. Fake-process source
checks are not native isolation/retention acceptance. Default legacy behavior
and its guarantees are unchanged; this candidate does not authorize adoption or
release. See [the limited setting](docs/configuration.md#opencode-cli-generation-partial-source-only).

Hook logs, summaries, run artifacts, and OpenCode transcripts are stored locally
under `.git/ai-push-hooks/` with private runtime permissions. Transcript capture
defaults to **on** at `.git/ai-push-hooks/transcripts`; set
`logging.capture_llm_transcript = false` to disable it. OpenCode session deletion
defaults to on, but provider-side retention is controlled by the provider.
Codex and Claude are ephemeral/no-persistence by default, and command profiles
have no inferred transcript lifecycle.

Transcript export is best effort. If export fails or produces no usable output,
the run emits a warning and still applies the configured session-deletion
policy; it does not claim that a transcript was captured. A provider may have
already received the request even when local export fails. Do not use local
transcript files as proof that provider-side data was deleted.

## Repository callbacks and commands

The npm launcher uses Python isolated mode to prevent the consuming checkout,
inherited `PYTHONPATH`, and the user site directory from shadowing the shipped
launcher or standard-library modules. It retains the selected interpreter's
normal system/virtual-environment site-packages for callback dependencies; use
that environment rather than `--user` installs or `PYTHONPATH`. This is launch
path isolation, not an operating-system sandbox, and trusted interpreter
startup customization or callback code is not made safe by it.

The published `0.3.0` beta includes the `ask` spelling, repository Python
callbacks, and direct `exec`/`assert` commands. The previous `0.2.1` beta used
`llm` for model-backed workflow steps; there is no compatibility alias, so
configurations must be updated when upgrading.

A callback reference is one contained, no-follow regular `.py` file plus one
top-level callable. It is loaded lazily only after module, environment, and
input gates, and cached once per run. Loading does not mutate `sys.path`, cwd,
or the environment. A single-file callback may import dependencies already
installed in the interpreter running the hook, but the host never runs `pip`;
sibling/package-relative imports and installed-module references are not a
supported loading mechanism. The callback runs in-process as trusted user code:
there is no SDK, sandbox, filesystem write prevention, or in-process timeout.
The configured timeout applies to child runner processes, not callback code.
Its `PluginContext` has frozen mappings/snapshots and validated `Path`
values, but those paths do not make file contents read-only. Callback prints and
direct writes can disclose or modify host data and are outside host
sanitization. Use a separately managed low-privilege process/container/VM when
that boundary is required.

Command steps use direct argv with no implicit shell, repository-root cwd,
inherited environment, and EOF on stdin unless an exact declared input is
selected. `{repo}`, `{python}`, and `{input:<logical-ref>}` are substituted only
as whole argv elements; unknown tokens in the reserved grammar and embedded
recognized tokens are rejected, while ordinary brace text is preserved. The
default command timeout is 60 seconds. stdout/stderr are private, unredacted
`stdout.txt`/`stderr.txt` artifacts and are not printed by default. Each stream
is capped at 16 MiB; invalid UTF-8, timeout, signal, missing executable, or
truncation fails closed. A command may explicitly invoke `bash -c`, and
`exec`/`assert` commands may modify the real checkout, so these are user-policy
choices rather than host isolation guarantees. Failed command diagnostics name
the step, status/error class, and private artifact namespace only; they never
include captured stream text. `assert` saves its report before blocking on a
false/nonzero result. Only the workflow-level fail-open setting overrides that
block; there is no per-command override.

Read-only `collect` callback work may overlap up to `max_parallel`; trusted
callback/command authors must provide their own concurrency safety. `exec` and
`assert` remain serialized, and `apply` remains a separate staged, allowlisted
operation.

## Boundary and apply limitations

OpenCode permissions and temporary-workspace isolation are **not an
operating-system sandbox**. There is no mandatory command allowlist, shell
parser, container, credential broker, or trust prompt. Custom commands are
arbitrary user-authorized argv programs and a nominally read-only custom `ask`
profile is not enforced as read-only. `collect`/`ask` work may overlap up to
`max_parallel`; trusted custom commands must tolerate that. `apply` is globally
serialized, but this does not prevent a same-user process from changing the
host.

Project apply uses a point-in-time staging projection. It excludes ignored files
including tracked-but-ignored files, Git metadata, casefolded/Unicode-normalized
`AGENTS.md` paths, symlinks/reparse points, and special files. Propagation still
requires `allow_paths` plus destination and Git-state checks. Runner inputs and
captured stdout/stderr are each bounded to 16 MiB; staging is bounded to 10,000
entries/256 MiB and Git metadata snapshots to 20,000 entries/64 MiB. These
limits are resource and scope controls, not isolation. Existing baseline checks
are not an atomic CAS against arbitrary external writers, and automatic rollback
is avoided to protect pre-existing user changes. See [runner profiles and
access modes](docs/configuration.md#runner-profiles).

Apply intentionally repeats integrity and state scans: it snapshots the checkout
and Git metadata, inventories staging before and after the runner, checks each
propagation operation against its baseline, and verifies the propagated result
and protected state afterward. These repeated checks are defense in depth, not
an atomic CAS or an automatic rollback.

Timeout cleanup has platform limits: POSIX uses a private process group on a
best-effort basis, while Windows can terminate only the direct child. Neither
is a sandbox; Windows has no native beta evidence.

## Historical 0.3.0 security evidence

The [0.3.0 release record](CHANGELOG.md#030---2026-09-09) documented a pinned
Lefthook suite reporting **407 tests with no skips**, an OpenCode **1.18.29**
contract smoke test with an in-process loopback mock provider and no external
model call, and version/help-only checks for Codex **0.148.0** and Claude
**2.1.220**. The bounded validation snapshot also records Codex **0.152.0** on
2026-09-14: its synthetic fixture verified a nonce read and an allowlisted marker
apply followed by a deterministic postcondition. See the [runner validation
snapshot](docs/configuration.md#live-validation-snapshot). Together, these records
are historical or bounded evidence, not a current suite result or proof for every
provider, model, authentication mode, platform, live `apply` path, or a general
security guarantee.
Treat generated-hook path checks and the Lefthook runner as integration
safeguards, not isolation boundaries.
