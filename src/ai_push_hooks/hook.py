from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, Sequence

from .artifacts import ArtifactStore, generate_run_id
from .config import load_config
from .engine import WorkflowEngine
from .executors.autocommit import (
    combined_superseded_message,
    finalize_pending_auto_commits,
)
from .paths import (
    ensure_private_directory,
    resolve_contained_path,
    write_text_no_follow,
)
from .git_utils import (
    collect_changed_files,
    collect_diff,
    collect_revision_ranges,
    current_branch,
    ensure_dir,
    env_bool,
    parse_push_updates,
    resolve_git_dir,
    resolve_repo_root,
    resolve_storage_path,
    run_command,
    should_skip_for_sync_branch,
    unique_range_expressions,
)
from .types import (
    HookConfig,
    HookError,
    HookLogger,
    PushSupersededError,
    RequiredRetentionError,
    RuntimeContext,
)


def _ensure_hook_namespace(git_dir: pathlib.Path, config: HookConfig) -> None:
    required = config.logging.require_opencode_retention
    try:
        ensure_private_directory(git_dir / "ai-push-hooks", durable=required)
    except (HookError, OSError) as exc:
        if required:
            raise RequiredRetentionError(
                "Required OpenCode retention: hook namespace durability failed"
            ) from exc
        raise


def _build_logger(
    repo_root: pathlib.Path, git_dir: pathlib.Path, config: HookConfig
) -> HookLogger:
    _ensure_hook_namespace(git_dir, config)
    jsonl_path = None
    if config.logging.jsonl:
        log_dir = ensure_dir(
            resolve_storage_path(repo_root, git_dir, config.logging.dir)
        )
        if log_dir is not None:
            jsonl_path = resolve_contained_path(log_dir, "hook.jsonl", "JSONL log path")
    return HookLogger(jsonl_path=jsonl_path, console_level=config.logging.level)


def _write_summary(context: RuntimeContext, result: dict[str, object]) -> None:
    summary_dir = ensure_dir(
        resolve_storage_path(
            context.repo_root, context.git_dir, context.config.logging.summary_dir
        )
    )
    if summary_dir is None:
        return
    summary_path = resolve_contained_path(
        summary_dir,
        f"{context.run_id}.json",
        "Summary output path",
    )
    write_text_no_follow(
        summary_path, json.dumps(result, ensure_ascii=True, indent=2) + "\n"
    )


def _assert_clean_worktree(repo_root: pathlib.Path) -> None:
    completed = run_command(["git", "status", "--short"], cwd=repo_root, check=False)
    if completed.returncode != 0:
        raise HookError("Hook requires a clean worktree but Git status failed")
    status = (completed.stdout or "").strip()
    if status:
        raise HookError("Hook requires a clean worktree but local changes are present")


def _run_hook_impl(
    remote_name: str = "",
    remote_url: str = "",
    stdin_lines: Sequence[str] | None = None,
    cwd: pathlib.Path | None = None,
) -> int:
    current_dir = cwd or pathlib.Path.cwd()
    repo_root = resolve_repo_root(current_dir)
    git_dir = resolve_git_dir(repo_root)
    config, _config_path = load_config(repo_root)
    _ensure_hook_namespace(git_dir, config)
    logger = _build_logger(repo_root, git_dir, config)

    if not config.general.enabled:
        logger.status("hook.disabled", "AI push hooks disabled")
        return 0
    if config.general.require_clean_worktree:
        _assert_clean_worktree(repo_root)

    actual_stdin = (
        list(stdin_lines)
        if stdin_lines is not None
        else [line.rstrip("\n") for line in sys.stdin]
    )
    push_updates = parse_push_updates(actual_stdin)
    pushed_branch_updates = [
        update
        for update in push_updates
        if update.ref_kind == "branch" and update.operation != "delete"
    ]
    if len(pushed_branch_updates) > 1:
        pushed_refs = ", ".join(update.remote_ref for update in pushed_branch_updates)
        raise HookError(
            "Ambiguous push contains multiple branch updates; refusing to skip branch gates: "
            + pushed_refs
        )
    pushed_branches = list(
        dict.fromkeys(
            update.branch_name
            for update in pushed_branch_updates
            if update.branch_name is not None
        )
    )
    if config.general.skip_on_sync_branch:
        skip_sync, reason = should_skip_for_sync_branch(
            repo_root, pushed_branches, push_updates
        )
        if skip_sync:
            logger.status("hook.skip_sync_branch", f"Skipping AI push hooks: {reason}")
            return 0

    revision_ranges = collect_revision_ranges(
        repo_root, remote_name or "origin", push_updates, config.general.base_branch
    )
    ranges = unique_range_expressions(revision_ranges)
    changed_files = collect_changed_files(repo_root, ranges) if ranges else []
    diff_text = (
        collect_diff(repo_root, ranges, config.llm.max_diff_bytes) if ranges else ""
    )
    if len(pushed_branches) == 1:
        branch_name = pushed_branches[0]
        branch_selection_reason = "single pushed branch"
        branch_revision_ranges = [
            item for item in revision_ranges if item.update.branch_name == branch_name
        ]
        branch_ranges = unique_range_expressions(branch_revision_ranges)
        if branch_ranges == ranges:
            branch_changed_files = changed_files
            branch_diff_text = diff_text
        else:
            branch_changed_files = (
                collect_changed_files(repo_root, branch_ranges) if branch_ranges else []
            )
            branch_diff_text = (
                collect_diff(repo_root, branch_ranges, config.llm.max_diff_bytes)
                if branch_ranges
                else ""
            )
        branch_is_new = any(
            update.branch_name == branch_name and update.operation == "create"
            for update in push_updates
        )
    else:
        branch_name = ""
        branch_selection_reason = "no pushed branch updates"
        branch_revision_ranges = []
        branch_ranges = []
        branch_changed_files = []
        branch_diff_text = ""
        branch_is_new = False
    run_id = generate_run_id()
    run_dir = resolve_storage_path(
        repo_root, git_dir, f".git/ai-push-hooks/runs/{run_id}"
    )

    context = RuntimeContext(
        repo_root=repo_root,
        git_dir=git_dir,
        config=config,
        logger=logger,
        remote_name=remote_name or "origin",
        remote_url=remote_url,
        stdin_lines=actual_stdin,
        run_id=run_id,
        run_dir=run_dir,
        opencode_executable=None,
        cache={
            "ranges": ranges,
            "revision_ranges": revision_ranges,
            "changed_files": changed_files,
            "diff_text": diff_text,
            "push_updates": push_updates,
            "pushed_branch_updates": pushed_branch_updates,
            "pushed_branches": pushed_branches,
            "branch_name": branch_name,
            "branch_selection_reason": branch_selection_reason,
            "branch_revision_ranges": branch_revision_ranges,
            "branch_ranges": branch_ranges,
            "branch_changed_files": branch_changed_files,
            "branch_diff_text": branch_diff_text,
            "branch_is_new": branch_is_new,
            "checked_out_branch": current_branch(repo_root),
            "base_branch": config.general.base_branch,
            "sync_branch": os.getenv("BEADS_SYNC_BRANCH", "beads-sync"),
        },
    )
    logger.status(
        "hook.start",
        "Starting AI push hooks workflow",
        branch=context.cache["branch_name"],
        checked_out_branch=context.cache["checked_out_branch"],
        branch_selection_reason=branch_selection_reason,
        changed_files=len(changed_files),
        ranges=ranges,
        push_updates=[
            {
                "local_ref": update.local_ref,
                "local_sha": update.local_sha,
                "remote_ref": update.remote_ref,
                "remote_sha": update.remote_sha,
                "ref_kind": update.ref_kind,
                "operation": update.operation,
            }
            for update in push_updates
        ],
    )
    engine = WorkflowEngine(context=context, artifacts=ArtifactStore(run_dir))
    try:
        workflow_result = engine.run()
        # Only now, with every check passed, are deferred commits made. A failure
        # anywhere above leaves the applied edits uncommitted in the working tree.
        commit_outcomes = finalize_pending_auto_commits(context)
        logger.llm_summary()
        summary: dict[str, Any] = {
            "run_dir": str(workflow_result.run_dir),
            "modules": workflow_result.modules,
        }
        if commit_outcomes:
            summary["auto_commit"] = commit_outcomes
        _write_summary(context, summary)
        commit_outcomes = [
            outcome
            for outcome in commit_outcomes
            if outcome.get("committed") or outcome.get("blocked")
        ]
        if commit_outcomes:
            logger.status(
                "hook.superseded",
                "Applied fixes; the original push was superseded",
                run_dir=str(workflow_result.run_dir),
            )
            raise PushSupersededError(combined_superseded_message(commit_outcomes))
        logger.status(
            "hook.complete",
            "AI push hooks workflow completed",
            run_dir=str(workflow_result.run_dir),
        )
        return 0
    except (PushSupersededError, RequiredRetentionError):
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc).strip() or exc.__class__.__name__
        logger.error("hook.failed", "AI push hooks workflow failed", error=message)
        if config.general.allow_push_on_error:
            logger.warn(
                "hook.fail_open",
                "Allowing push because allow_push_on_error=true",
                error=message,
            )
            return 0
        raise


def run_hook(
    remote_name: str = "",
    remote_url: str = "",
    stdin_lines: Sequence[str] | None = None,
    cwd: pathlib.Path | None = None,
) -> int:
    if env_bool("AI_PUSH_HOOKS_SKIP") is True:
        return 0
    try:
        return _run_hook_impl(remote_name, remote_url, stdin_lines, cwd)
    except (PushSupersededError, RequiredRetentionError):
        # Not a failed check, so fail-open does not apply: this run committed a
        # fix locally, which means the commits Git is holding for this push are
        # stale. Allowing it through would push the unfixed code and report
        # success, the one outcome auto_commit exists to prevent.
        raise
    except Exception as exc:  # noqa: BLE001
        allow_on_error = env_bool("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR")
        if allow_on_error is None:
            try:
                repo_root = resolve_repo_root(cwd or pathlib.Path.cwd())
                config, _ = load_config(repo_root)
                allow_on_error = config.general.allow_push_on_error
            except Exception:  # noqa: BLE001
                allow_on_error = False
        if allow_on_error:
            message = str(exc).strip() or exc.__class__.__name__
            sys.stderr.write(
                "[ai-push-hooks] Allowing push because allow_push_on_error=true: "
                + message
                + "\n"
            )
            return 0
        raise
