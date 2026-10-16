# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Propose opt-in `logging.require_opencode_retention` (default false): private
  byte-preserving OpenCode capture, identity-correlated export, and fail-closed
  lifecycle receipts. Best-effort defaults and other providers are unchanged.
  Strict capture is incompatible with fail-open. Native CLI-generation
  compatibility and maintainer release acceptance remain unverified.
- Added `auto_commit` to `apply` steps, committing the edits a fix propagated.
  Off by default. The commit is made after every later step passes, so tests and
  assert gates can still reject the edits; a failure leaves them uncommitted in
  the working tree.
- Added `auto_push` to `apply` steps, sending that commit in the same `git push`.
  Off by default and requires `auto_commit`.
- Added `commit_message` to `apply` steps. Omit it and the runner that made the
  fix names the commit through a `COMMIT: <subject>` line, validated as untrusted
  input and replaced with a generic subject when malformed.
- Reported a superseded push through a stable final line, `ai-push-hooks Push
  Success` or `ai-push-hooks Push Again`, because Git collapses every nonzero
  pre-push hook exit to `1` and leaves non-interactive callers no usable exit
  status. The accompanying ledger names which push failed and which succeeded.

### Fixed

- Bound required OpenCode retention evidence to each invocation so concurrent
  `ask` steps cannot exchange capture, export, or deletion state on a shared runner.
- Refused to auto-commit a file that already had uncommitted changes. Committing
  it would have captured work the developer never staged, and published it when
  `auto_push` was enabled.
- Stopped a push whose commits this run superseded regardless of
  `allow_push_on_error`. Failing open there would have sent the pre-fix commit
  and reported success.

- Used GitHub's current Actions media type when recovering immutable release
  artifacts after delayed registry publication.
- Created a missing GitHub release directly when recovering from an Actions
  artifact instead of requiring a draft release first.

## [0.3.4] - 2026-09-14

This is the first stable release. Python and npm both use `0.3.4`, the canonical
Git tag is `v0.3.4`, npm uses the `latest` dist-tag, and GitHub publishes a
standard release.

### Fixed

- Failed the clean-worktree gate when Git cannot verify checkout status.
- Rejected apply destinations inside configured Git hooks directories before
  propagating any changes to the checkout.
- Isolated npm's Python startup from consuming-repository modules, inherited
  `PYTHONPATH`, and user-site imports so they cannot replace the installed hook.
- Excluded Git-ignored local documentation from default model context collection.
- Required explicit boolean Beads verdicts before commands, report changes, or
  quality-gate decisions.
- Kept failed command output in private artifacts rather than terminal errors.
- Redacted known sensitive values across diagnostic truncation and partial-output
  boundaries, including timeout errors.
- Collected review diffs independently of external diff drivers, text conversion,
  and color settings.
- Made private runtime directory creation tolerate concurrent OpenCode stages
  while retaining directory and symlink checks.
- Included regular-file, symlink, and other Git type changes in pushed-file
  facts exposed to workflows and Python callbacks.
- Preserved each Python callback source's annotation semantics and registered
  callback modules for dataclasses and runtime type-hint introspection.
- Validated recovered Actions artifacts against their associated push run,
  release workflow, repository, tag, and commit before downloading archives.

## [0.3.3] - 2026-09-14

This is a beta patch release. Python and npm both use `0.3.3`, the canonical
Git tag is `v0.3.3`, npm uses the `beta` dist-tag, and GitHub marks the release
as a prerelease. PyPI does not provide a separate beta channel, so Python users
must select the exact `0.3.3` version.

This is a documentation and validation patch only; it adds no runtime features.

### Documentation

- Added a tested deterministic postcondition recipe that distinguishes successful
  runner execution from a verified fix, while preserving legitimate no-op applies.
- Recorded live runner validation results for OpenCode, Claude Code, and Codex.
- Clarified the focused pre-push use case and how the tool complements hook
  managers, background reviewers, and broader agent workflow frameworks.
- Documented custom-agent CLI support through the custom command runner.
- Updated the Codex conformance check to 0.152.0 and verified live project reads
  and allowlisted edits with `gpt-5.6-luna`.

## [0.3.2] - 2026-09-12

This is a beta patch release. Python and npm both use `0.3.2`, the canonical
Git tag is `v0.3.2`, npm uses the `beta` dist-tag, and GitHub marks the release
as a prerelease. PyPI does not provide a separate beta channel, so Python users
must select the exact `0.3.2` version.

### Changed

- Added a pinned Ruff 0.13.3 formatting policy for Python sources, tests, and scripts.
- Updated the default OpenCode model and README positioning to
  `openai/gpt-5.6-luna`.
- Clarified that built-in mutating steps are serialized, while trusted custom
  command runners, including project-access `ask` commands, are not enforced
  read-only.
- Removed the unused secondary shell launcher in favor of the canonical Python
  console script and npm wrapper.

### Fixed

- Review initial publication of the configured base branch against the empty
  tree, while retaining configured-base comparison for new feature branches.
- Persist bounded diffs as valid UTF-8, truncating at character boundaries and
  representing malformed bytes with replacement characters within the budget.
- Hardened pull-request creation by validating generated payload types,
  reconciling failed `gh` calls against GitHub, and keeping remote credentials
  out of diagnostics.
- Bounded config, prompt, and callback source reads and rejected unsafe config
  file types.
- Rejected duplicate workflow identifiers and simplified workflow step result
  handling.
- Made source distributions complete and added validation of their unpacked
  test suite and wheel builds with the minimum supported setuptools 77 backend.
- Standardized development and release validation on the isolated project
  `.[dev]` extra instead of repeating tool lists.
- Restored the PyPI release job's checkout permission and pinned the OpenCode
  contract test's base container image.

### Documentation

- Added an agent setup skill and CI/npm beta badges.
- Explained the manual-commit assertion and why applied edits are not part of
  the commit already being pushed.
- Included the configuration guide in the npm package and removed its link to
  the local-only runner verification report.

## [0.3.1] - 2026-09-10

### Changed

- Rewrote the README around npm-first setup, modular workflows, and AI
  ask/apply examples; moved detailed configuration into a separate guide.

### Fixed

- Bundled the Python 3.10 TOML dependency in the npm package, removing the
  separate `tomli` installation step even for offline, script-disabled installs.
- Hardened release recovery around source-bound checksum manifests, safe
  artifact paths, and exact artifact identity; recovery remains npm-only after
  PyPI is complete and does not rebuild or republish the Python artifacts.
- Tightened bounded child-process output handling and cleanup when a stream
  reaches its limit.

### Performance

- Reduced documentation context-search work by reading each candidate document
  once and bounding retained query-match metadata.
- Allowed independent runner types and callback sources to initialize in
  parallel while preserving per-source/per-adapter initialization safety.

### Cleanup

- Consolidated hook-owned artifact validation and shared process handling across
  runner adapters, removing duplicated OpenCode-specific plumbing.

## [0.3.0] - 2026-09-09

This is a beta feature release. Python and npm both use `0.3.0`, the canonical
Git tag is `v0.3.0`, npm uses the `beta` dist-tag, and GitHub marks the release
as a prerelease. PyPI does not provide a separate beta channel, so Python users
must select the exact `0.3.0` version.

### Added

- Added strict named runner profiles for OpenCode, Codex, Claude, and direct
  shell-free command adapters, with global and per-`ask`/`apply` selection.
- Added explicit project-aware analysis and broad, bounded project apply staging
  while retaining allowlisted propagation and the artifact-only OpenCode default.
- Added repository-local Python callbacks for `collect`, `exec`, and `assert`,
  plus direct argv commands for `exec` and `assert`.

### Changed

- Made model/schema retries runner-neutral and session-optional, with fresh
  invocation fallback when reuse is unavailable; added truthful session,
  transcript, colored status, JSONL, and normalized redacted-output reporting.
- Documented authentication ownership, provider data exposure, custom-command
  and Pi trust/concurrency, protected and ignored apply inputs, and the
  no-sandbox boundary.
- Renamed the workflow step spelling from `llm` to `ask` without an `llm` or
  `agent` alias. The shared `[llm]` model policy keeps its historical name.

### Fixed

- Fixed bounded child-process capture and timeout cleanup for descendant process
  trees on POSIX where possible, while preventing pipe descriptors from being
  reused and closed by a later invocation.

### Compatibility and deferred work

- Published `0.2.1` remains the historical beta baseline and used `llm` as the
  model-backed workflow-step spelling. Upgrading to `0.3.0` requires changing
  that step spelling to `ask`; the old spelling is not accepted.
- [Deferred/proposed] Automatic discovery, an SDK, installed-module hooks,
  sandboxing, and a universal external-agent observer remain out of scope.

## [0.2.1] - 2026-09-08

This is a beta patch release. Python and npm both use `0.2.1`, the canonical
Git tag is `v0.2.1`, npm uses the `beta` dist-tag, and GitHub marks the release
as a prerelease.

### Fixed

- Restored OpenCode's built-in plugins, including built-in authentication such as
  Codex OAuth, during isolated runs. `--pure` still excludes external plugins,
  and project/global configuration and plugins remain isolated.
- Restored forwarding of recognized provider environment variables, including
  `OPENAI_API_KEY`, so OpenCode itself selects the authentication path.
- Prevented hook-launched Beads alignment updates from inheriting schema and
  remote-migration safety overrides.
- Made post-publication PyPI verification tolerate bounded 404/partial metadata
  propagation while retaining fail-closed artifact hash and authentication checks.

### Documentation

- Documented the native `bd` maintenance, backup, rehearsal, and remote
  publication boundaries; Beads-Rust (`br`) is not supported.

## [0.2.0] - 2026-09-06

This is a beta release. Python and npm both use `0.2.0`, the canonical Git tag
is `v0.2.0`, npm uses the `beta` dist-tag, and GitHub marks the release as a
prerelease. PyPI does not provide package channels, so Python users must select
the exact `0.2.0` version rather than a PyPI beta channel.

### Added

- Added a safe repo-local `ai-push-hooks install [--force]` delegate that
  preserves hook arguments, stdin, and exit status, refuses unsafe or shared
  hook targets, and never changes Git configuration.

### Changed

- Hardened OpenCode execution with isolated configuration, denied tools for read-only work, and allowlisted temporary workspaces for apply steps.
- Added fail-closed path, symlink, file-mode, artifact, Git metadata, and concurrent-change validation around model-assisted edits.
- Restricted prompt files and runtime storage to validated repository-owned locations and private filesystem permissions.
- Updated the default OpenCode model and release workflow dependencies.
- Added CI/package validation, community health files, public package metadata, and post-publication GitHub Releases.
- Documented the shortest installed wheel/npm onboarding path, the full-featured
  Lefthook alternative, safe removal, failure/fail-open/skip semantics, and the
  Python requirement for npm installs.
- Recorded a limited synthetic provider preview: OpenCode 1.18.29 with
  `opencode/muse-spark-1.3-contributor-free`, which was listed as free at test
  time; synthetic query/analyze passed at zero reported cost. Free-model
  availability can change. This does not claim all providers/models or live
  apply.

### Compatibility and limitations

- The candidate evidence includes macOS Darwin 24.6.0 arm64 with Python
  3.12.13, Node 24.19.0, npm 10.9.2, Git 2.55.0, Ruff 0.13.3, OpenCode
  1.18.29, `gh` 2.93.0, and `bd` 1.2.2. Python 3.10–3.13 and Node 18+ remain
  declared ranges; Windows has no native beta evidence and is untested/not
  supported for this beta.
- The published `0.1.19` registry artifacts retain their historical provenance;
  this release does not republish, move, or overwrite that version.

## [0.1.19] - 2026-07-13

### Added

- Added configurable base-branch support for pull-request workflows.
- Added packed npm-package smoke coverage.

### Fixed

- Added Python 3.10 TOML compatibility and fixed npm-local hook command resolution.
- Preserved Git porcelain paths and protected pre-existing dirty allowlisted files during apply steps.
- Clarified module-local artifact references and standardized the repository hook integration.

[Unreleased]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.3.4...HEAD
[0.3.4]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.19...v0.2.0
[0.1.19]: https://github.com/shanebishop1/ai-push-hooks/compare/v0.1.18...v0.1.19
