from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import tempfile
from dataclasses import dataclass
from typing import Any

from ..config import resolve_runner_profile
from ..paths import (
    atomic_write_bytes,
    ensure_private_directory,
    is_path_within,
    normalized_component,
    path_has_symlink,
    path_is_link_or_reparse,
    relative_path_parts,
    sanitize_file_mode,
)
from ..config import COMMIT_SUBJECT_MAX_CHARS
from ..types import (
    HookError,
    RequiredRetentionError,
    ModuleRuntimeState,
    RuntimeContext,
    StepConfig,
)
from .autocommit import register_pending_auto_commit
from ..git_utils import (
    list_repo_changes,
    path_matches,
    resolve_git_common_dir,
    resolve_git_dir,
    run_command,
)
from .runner_workflow import run_runner_once
from .runners.opencode_support import validate_hook_owned_artifacts

METADATA_MAX_FILES = 20_000
METADATA_MAX_BYTES = 64 * 1024 * 1024
STAGING_MAX_FILES = 10_000
STAGING_MAX_BYTES = 256 * 1024 * 1024
SPECIAL_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
PROTECTED_GIT_COMPONENT = ".git"
PROTECTED_INSTRUCTION_FILENAME = "agents.md"

FileSnapshot = tuple[str, int | None, str | None]
MetadataSnapshot = dict[str, FileSnapshot]


@dataclass(frozen=True)
class StagedFile:
    digest: str
    mode: int
    size: int


@dataclass(frozen=True)
class DestinationState:
    kind: str
    mode: int | None = None
    digest: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ApplyOperation:
    relative_path: str
    baseline: DestinationState
    content: bytes | None
    mode: int | None


def _is_protected_path(path: str) -> bool:
    parts = pathlib.PurePosixPath(path).parts
    return any(
        normalized_component(part) == PROTECTED_GIT_COMPONENT for part in parts
    ) or (
        bool(parts)
        and normalized_component(parts[-1]) == PROTECTED_INSTRUCTION_FILENAME
    )


def _open_regular_file(path: pathlib.Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HookError(f"Unable to safely open regular file: {path}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise HookError(f"Path is not a regular file: {path}")
    return descriptor, metadata


def _hash_file(path: pathlib.Path, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    descriptor, metadata = _open_regular_file(path)
    with os.fdopen(descriptor, "rb") as handle:
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise HookError(f"File exceeds bounded read budget before reading: {path}")
        total_bytes = 0
        while True:
            read_size = 1024 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes - total_bytes + 1)
            chunk = handle.read(read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise HookError(
                    f"File grew beyond bounded read budget while reading: {path}"
                )
            digest.update(chunk)
    return digest.hexdigest()


def _read_regular_file(
    path: pathlib.Path, *, max_bytes: int | None = None
) -> tuple[bytes, int]:
    descriptor, metadata = _open_regular_file(path)
    with os.fdopen(descriptor, "rb") as handle:
        if max_bytes is not None and metadata.st_size > max_bytes:
            raise HookError(f"File exceeds bounded read budget before reading: {path}")
        content = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise HookError(
                f"File grew beyond bounded read budget while reading: {path}"
            )
        return content, metadata.st_mode


def _snapshot_destination(
    repo_root: pathlib.Path, relative_path: str
) -> DestinationState:
    destination = _repo_path_from_git(repo_root, relative_path)
    if path_has_symlink(repo_root, destination):
        return DestinationState("symlink")
    if not destination.exists():
        return DestinationState("missing")
    metadata = destination.lstat()
    if stat.S_ISREG(metadata.st_mode):
        return DestinationState(
            "file", metadata.st_mode, _hash_file(destination, STAGING_MAX_BYTES)
        )
    if stat.S_ISDIR(metadata.st_mode):
        return DestinationState("directory", metadata.st_mode)
    return DestinationState("other", metadata.st_mode)


def _repo_path_from_git(repo_root: pathlib.Path, path: str) -> pathlib.Path:
    pure_path = pathlib.PurePosixPath(path)
    if pure_path.is_absolute() or not pure_path.parts or ".." in pure_path.parts:
        raise HookError(f"Git returned an unsafe repository path: {path!r}")
    return repo_root.joinpath(*pure_path.parts)


def _snapshot_repo_files(
    repo_root: pathlib.Path, paths: set[str]
) -> dict[str, FileSnapshot]:
    if len(paths) > STAGING_MAX_FILES:
        raise HookError(
            "Git-visible checkout changes exceed the bounded safety snapshot budget"
        )
    snapshot: dict[str, FileSnapshot] = {}
    total_bytes = 0
    for path in paths:
        full_path = _repo_path_from_git(repo_root, path)
        if path_has_symlink(repo_root, full_path):
            mode = (
                full_path.lstat().st_mode
                if full_path.exists() or full_path.is_symlink()
                else None
            )
            snapshot[path] = (
                "symlink",
                mode,
                os.readlink(full_path) if full_path.is_symlink() else None,
            )
        elif full_path.exists():
            metadata = full_path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                total_bytes += metadata.st_size
                if total_bytes > STAGING_MAX_BYTES:
                    raise HookError(
                        "Git-visible checkout changes exceed the bounded safety snapshot budget"
                    )
                snapshot[path] = (
                    "file",
                    metadata.st_mode,
                    _hash_file(full_path, metadata.st_size),
                )
            else:
                snapshot[path] = ("other", metadata.st_mode, None)
        else:
            snapshot[path] = ("missing", None, None)
    return snapshot


def _runtime_metadata_namespaces(context: RuntimeContext) -> tuple[pathlib.Path, ...]:
    common_dir = resolve_git_common_dir(context.repo_root)
    namespaces = tuple(
        dict.fromkeys(
            (
                (context.git_dir.resolve() / "ai-push-hooks"),
                (common_dir.resolve() / "ai-push-hooks"),
            )
        )
    )
    for namespace in namespaces:
        if path_is_link_or_reparse(namespace):
            raise HookError(
                "ai-push-hooks runtime metadata namespace is a symlink or reparse point: "
                f"{namespace}"
            )
    return namespaces


def _resolve_effective_hooks_path(context: RuntimeContext) -> pathlib.Path:
    raw_hooks_path = run_command(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=context.repo_root,
        check=True,
    ).stdout.strip()
    if not raw_hooks_path:
        raise HookError("Git did not return an effective hooks path")
    hooks_path = pathlib.Path(raw_hooks_path)
    if not hooks_path.is_absolute():
        hooks_path = context.repo_root / hooks_path
    return pathlib.Path(os.path.abspath(hooks_path))


def _snapshot_git_control_metadata(context: RuntimeContext) -> MetadataSnapshot:
    git_dir = context.git_dir.resolve(strict=True)
    common_dir = resolve_git_common_dir(context.repo_root).resolve(strict=True)
    excluded_namespaces = _runtime_metadata_namespaces(context)
    snapshot: MetadataSnapshot = {}
    budget = {"entries": 0, "bytes": 0}

    def excluded(path: pathlib.Path) -> bool:
        lexical = pathlib.Path(os.path.abspath(path))
        return any(
            is_path_within(lexical, namespace) for namespace in excluded_namespaces
        )

    def record(key: str, path: pathlib.Path) -> None:
        budget["entries"] += 1
        if budget["entries"] > METADATA_MAX_FILES:
            raise HookError(
                "Git control metadata exceeds the bounded safety snapshot budget"
            )
        if path_is_link_or_reparse(path):
            raise HookError(
                f"Refusing symlinked monitored Git metadata or reparse point: {key} ({path})"
            )
        elif not path.exists():
            snapshot[key] = ("missing", None, None)
        else:
            metadata = path.lstat()
            detail: str | None = None
            if stat.S_ISREG(metadata.st_mode):
                budget["bytes"] += metadata.st_size
                if budget["bytes"] > METADATA_MAX_BYTES:
                    raise HookError(
                        "Git control metadata exceeds the bounded safety snapshot budget"
                    )
                detail = _hash_file(path, metadata.st_size)
            snapshot[key] = ("metadata", metadata.st_mode, detail)

    def scan_tree(
        label: str,
        root: pathlib.Path,
        *,
        pruned_top_level: set[str] | None = None,
        skipped_root_files: set[str] | None = None,
    ) -> None:
        if not root.exists() and not root.is_symlink():
            record(f"{label}:.", root)
            return
        record(f"{label}:.", root)
        if path_is_link_or_reparse(root) or not root.is_dir():
            return
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            directory_path = pathlib.Path(directory)
            relative_directory = directory_path.relative_to(root)
            retained: list[str] = []
            for name in dirnames:
                path = directory_path / name
                if excluded(path) or (
                    not relative_directory.parts
                    and pruned_top_level is not None
                    and name in pruned_top_level
                ):
                    continue
                record(f"{label}:{path.relative_to(root).as_posix()}", path)
                if not path_is_link_or_reparse(path):
                    retained.append(name)
            dirnames[:] = retained
            for name in filenames:
                path = directory_path / name
                if excluded(path) or (
                    not relative_directory.parts
                    and skipped_root_files is not None
                    and name in skipped_root_files
                ):
                    continue
                record(f"{label}:{path.relative_to(root).as_posix()}", path)

    if git_dir == common_dir:
        scan_tree(
            "current",
            git_dir,
            pruned_top_level={
                "ai-push-hooks",
                "branches",
                "hooks",
                "lfs",
                "logs",
                "objects",
                "refs",
                "worktrees",
            },
            skipped_root_files={
                "HEAD",
                "config",
                "config.worktree",
                "index",
                "packed-refs",
            },
        )
        record("current:logs/HEAD", common_dir / "logs" / "HEAD")
    else:
        scan_tree(
            "current",
            git_dir,
            pruned_top_level={"ai-push-hooks", "lfs", "objects"},
            skipped_root_files={"index"},
        )

    for name in ("HEAD", "config", "config.worktree", "packed-refs"):
        record(f"shared:{name}", common_dir / name)
    scan_tree("shared:refs", common_dir / "refs")
    hooks_path = _resolve_effective_hooks_path(context)
    if any(is_path_within(hooks_path, namespace) for namespace in excluded_namespaces):
        raise HookError(
            "Configured core.hooksPath must not overlap ai-push-hooks runtime metadata: "
            f"{hooks_path}"
        )
    scan_tree("shared:hooks", hooks_path)
    return snapshot


def _git_index_state(repo_root: pathlib.Path) -> tuple[str, str]:
    staged = run_command(
        ["git", "ls-files", "--stage", "-z"], cwd=repo_root, check=True
    ).stdout
    flags = run_command(
        ["git", "ls-files", "-v", "-z"], cwd=repo_root, check=True
    ).stdout
    return staged, flags


def _validate_apply_allowlist(
    repo_root: pathlib.Path, patterns: tuple[str, ...]
) -> None:
    for pattern in patterns:
        parts = relative_path_parts(pattern, "Apply allow_paths entry")
        if any(normalized_component(part) == PROTECTED_GIT_COMPONENT for part in parts):
            raise HookError("Apply allow_paths must not include Git metadata")
        if normalized_component(parts[-1]) == PROTECTED_INSTRUCTION_FILENAME:
            raise HookError("Apply allow_paths must not include AGENTS.md")
        static_parts: list[str] = []
        for part in parts:
            if any(character in part for character in "*?["):
                break
            static_parts.append(part)
        if len(static_parts) == len(parts):
            candidates = [repo_root.joinpath(*parts)]
        else:
            candidates = (
                [repo_root.joinpath(*static_parts)] if static_parts else [repo_root]
            )
        for candidate in candidates:
            if path_has_symlink(repo_root, candidate):
                raise HookError(f"Apply allow_paths traverses a symlink: {pattern}")


def _tracked_and_unignored_paths(repo_root: pathlib.Path) -> set[str]:
    output = run_command(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root,
        check=True,
    ).stdout
    return {path for path in output.split("\x00") if path}


def _copy_checkout_to_staging(
    context: RuntimeContext,
    staging_root: pathlib.Path,
    allow_paths: tuple[str, ...],
    *,
    project_access: str = "artifacts",
) -> dict[str, DestinationState]:
    repo_root = context.repo_root
    git_roots = (
        context.git_dir.resolve(strict=True),
        resolve_git_common_dir(repo_root).resolve(strict=True),
    )
    copied_files = 0
    copied_bytes = 0
    baselines: dict[str, DestinationState] = {}
    resolved_repo_root = repo_root.resolve(strict=True)
    checkout_paths = _tracked_and_unignored_paths(repo_root)
    if project_access == "project":
        projection_candidates = checkout_paths
    else:
        projection_candidates = {
            relative_path
            for relative_path in checkout_paths
            if not _is_protected_path(relative_path)
            and any(path_matches(relative_path, pattern) for pattern in allow_paths)
        }
    ignored_paths = _ignored_changed_paths(repo_root, projection_candidates)
    projection_paths = projection_candidates - ignored_paths
    for relative_path in sorted(projection_paths):
        if _is_protected_path(relative_path):
            continue
        source = _repo_path_from_git(repo_root, relative_path)
        if path_has_symlink(repo_root, source):
            if project_access == "project":
                continue
            raise HookError(
                f"Allowed checkout path is or traverses a symlink or reparse point: {relative_path}"
            )
        resolved_source = source.resolve(strict=False)
        if not is_path_within(resolved_source, resolved_repo_root):
            raise HookError(
                f"Allowed checkout source escapes repository: {relative_path}"
            )
        if any(is_path_within(resolved_source, root) for root in git_roots):
            continue
        if not source.exists():
            continue
        if not stat.S_ISREG(source.lstat().st_mode):
            if project_access == "project":
                continue
            raise HookError(
                f"Allowed checkout path is not a regular file: {relative_path}"
            )
        copied_files += 1
        if copied_files > STAGING_MAX_FILES:
            raise HookError(
                "Allowed apply files exceed the bounded staging workspace budget"
            )
        remaining_bytes = STAGING_MAX_BYTES - copied_bytes
        content, source_mode = _read_regular_file(source, max_bytes=remaining_bytes)
        copied_bytes += len(content)
        if copied_bytes > STAGING_MAX_BYTES:
            raise HookError(
                "Allowed apply files exceed the bounded staging workspace budget"
            )
        baselines[relative_path] = DestinationState(
            "file",
            source_mode,
            hashlib.sha256(content).hexdigest(),
        )
        destination = staging_root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        ensure_private_directory(destination.parent, private_root=staging_root)
        atomic_write_bytes(destination, content, mode=source_mode)
    return baselines


def _inventory_staging(staging_root: pathlib.Path) -> dict[str, StagedFile]:
    inventory: dict[str, StagedFile] = {}
    total_bytes = 0
    total_entries = 0
    for directory, dirnames, filenames in os.walk(staging_root, followlinks=False):
        total_entries += len(dirnames) + len(filenames)
        if total_entries > STAGING_MAX_FILES:
            raise HookError(
                "Apply staging workspace exceeds its bounded inventory budget"
            )
        directory_path = pathlib.Path(directory)
        for name in dirnames:
            path = directory_path / name
            if path_is_link_or_reparse(path):
                relative = path.relative_to(staging_root).as_posix()
                raise HookError(
                    f"Apply staging workspace contains symlink or reparse point: {relative}"
                )
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(staging_root).as_posix()
            metadata = path.lstat()
            if path_is_link_or_reparse(path):
                raise HookError(
                    f"Apply staging workspace contains symlink or reparse point: {relative}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise HookError(
                    f"Apply staging workspace contains non-regular file: {relative}"
                )
            total_bytes += metadata.st_size
            if len(inventory) >= STAGING_MAX_FILES or total_bytes > STAGING_MAX_BYTES:
                raise HookError(
                    "Apply staging workspace exceeds its bounded inventory budget"
                )
            inventory[relative] = StagedFile(
                _hash_file(path, metadata.st_size), metadata.st_mode, metadata.st_size
            )
    return inventory


def _changed_staging_paths(
    before: dict[str, StagedFile],
    after: dict[str, StagedFile],
    allow_paths: tuple[str, ...],
) -> set[str]:
    all_paths = set(before) | set(after)
    unexpected = sorted(
        path
        for path in all_paths
        if before.get(path) != after.get(path)
        and (
            _is_protected_path(path)
            or not any(path_matches(path, pattern) for pattern in allow_paths)
        )
    )
    if unexpected:
        raise HookError(
            "Apply staging workspace contains paths outside allowlist: "
            + ", ".join(unexpected)
        )
    return {path for path in all_paths if before.get(path) != after.get(path)}


def _ignored_changed_paths(
    repo_root: pathlib.Path,
    paths: set[str],
    *,
    work_tree: pathlib.Path | None = None,
) -> set[str]:
    if not paths:
        return set()
    command = ["git"]
    if work_tree is not None:
        command.extend(
            [
                f"--git-dir={resolve_git_dir(repo_root)}",
                f"--work-tree={work_tree}",
            ]
        )
    command.extend(["check-ignore", "--no-index", "--stdin", "-z"])
    completed = run_command(
        command,
        cwd=repo_root,
        input_text="\x00".join(sorted(paths)) + "\x00",
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise HookError((completed.stderr or "").strip() or "git check-ignore failed")
    return {path for path in completed.stdout.split("\x00") if path}


def _safe_destination(context: RuntimeContext, relative_path: str) -> pathlib.Path:
    repo_root = context.repo_root.resolve(strict=True)
    if _is_protected_path(relative_path):
        raise HookError(
            f"Apply destination must not contain Git metadata: {relative_path}"
        )
    destination = _repo_path_from_git(repo_root, relative_path)
    if path_has_symlink(repo_root, destination):
        raise HookError(f"Apply destination is or traverses a symlink: {relative_path}")
    existing_parent = destination.parent
    while not existing_parent.exists() and existing_parent != repo_root:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise HookError(
            f"Apply destination has a non-directory parent: {relative_path}"
        )
    if not is_path_within(
        existing_parent.resolve(strict=True), repo_root.resolve(strict=True)
    ):
        raise HookError(f"Apply destination escapes repository: {relative_path}")
    resolved_destination = destination.resolve(strict=False)
    git_roots = (
        context.git_dir.resolve(strict=True),
        resolve_git_common_dir(context.repo_root).resolve(strict=True),
    )
    if any(is_path_within(resolved_destination, root) for root in git_roots):
        raise HookError(
            f"Apply destination resolves inside Git metadata: {relative_path}"
        )
    hooks_path = _resolve_effective_hooks_path(context).resolve(strict=False)
    if is_path_within(resolved_destination, hooks_path):
        raise HookError(
            f"Apply destination resolves inside configured Git hooks path: "
            f"{relative_path}"
        )
    return destination


def _conservative_propagation_mode(
    baseline: DestinationState,
    source_mode: int,
) -> int:
    source_permissions = sanitize_file_mode(source_mode)
    if baseline.kind == "file" and baseline.mode is not None:
        existing_permissions = sanitize_file_mode(baseline.mode)
        if existing_permissions & 0o022 == 0:
            return existing_permissions
        source_permissions = existing_permissions
    return 0o700 if source_permissions & 0o100 else 0o600


def _preflight_apply_operations(
    context: RuntimeContext,
    operations: list[ApplyOperation],
) -> None:
    # Validate every destination before the propagation loop can create a
    # parent directory or write even the first operation.  In particular, a
    # configured core.hooksPath may not exist yet, so this guard cannot depend
    # on the hooks tree being present in the checkout.
    for operation in operations:
        _safe_destination(context, operation.relative_path)
    conflicts = [
        operation.relative_path
        for operation in operations
        if _snapshot_destination(context.repo_root, operation.relative_path)
        != operation.baseline
    ]
    if conflicts:
        raise HookError(
            "Apply checkout changed concurrently; refusing to overwrite: "
            + ", ".join(conflicts)
        )


def _verify_operation_baseline(
    context: RuntimeContext, operation: ApplyOperation
) -> None:
    if (
        _snapshot_destination(context.repo_root, operation.relative_path)
        != operation.baseline
    ):
        raise HookError(
            "Apply checkout changed concurrently; refusing to overwrite: "
            + operation.relative_path
        )


def _propagate_staging_changes(
    context: RuntimeContext,
    staging_root: pathlib.Path,
    after: dict[str, StagedFile],
    changed_paths: set[str],
    baselines: dict[str, DestinationState],
) -> dict[str, StagedFile | None]:
    ignored = _ignored_changed_paths(context.repo_root, changed_paths)
    ignored.update(
        _ignored_changed_paths(context.repo_root, changed_paths, work_tree=staging_root)
    )
    if ignored:
        raise HookError(
            "Refusing to copy staging output to ignored paths: "
            + ", ".join(sorted(ignored))
        )
    operations: list[ApplyOperation] = []
    expected: dict[str, StagedFile | None] = {}
    for relative_path in sorted(changed_paths):
        baseline = baselines.get(relative_path, DestinationState("missing"))
        staged = after.get(relative_path)
        if staged is None:
            if baseline.kind != "file":
                raise HookError(f"Refusing unsafe staged deletion: {relative_path}")
            operations.append(ApplyOperation(relative_path, baseline, None, None))
            expected[relative_path] = None
            continue
        if staged.mode & SPECIAL_MODE_BITS:
            raise HookError(
                f"Refusing staged output with setuid, setgid, or sticky mode bits: {relative_path}"
            )
        source = staging_root.joinpath(*pathlib.PurePosixPath(relative_path).parts)
        if path_has_symlink(staging_root, source) or not source.is_file():
            raise HookError(f"Refusing unsafe staged output: {relative_path}")
        if not is_path_within(
            source.resolve(strict=True), staging_root.resolve(strict=True)
        ):
            raise HookError(
                f"Refusing staged output that escapes workspace: {relative_path}"
            )
        content, source_mode = _read_regular_file(source, max_bytes=staged.size)
        if hashlib.sha256(content).hexdigest() != staged.digest or stat.S_IMODE(
            source_mode
        ) != stat.S_IMODE(staged.mode):
            raise HookError(f"Staged output changed after validation: {relative_path}")
        approved_mode = _conservative_propagation_mode(baseline, staged.mode)
        operations.append(
            ApplyOperation(relative_path, baseline, content, approved_mode)
        )
        expected[relative_path] = StagedFile(staged.digest, approved_mode, staged.size)

    _preflight_apply_operations(context, operations)
    applied_paths: list[str] = []
    try:
        for operation in operations:
            destination = _safe_destination(context, operation.relative_path)
            _verify_operation_baseline(context, operation)
            if operation.content is None or operation.mode is None:
                destination.unlink()
                applied_paths.append(operation.relative_path)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = _safe_destination(context, operation.relative_path)
            _verify_operation_baseline(context, operation)
            atomic_write_bytes(destination, operation.content, mode=operation.mode)
            applied_paths.append(operation.relative_path)
    except Exception as exc:  # noqa: BLE001
        applied = ", ".join(applied_paths) if applied_paths else "<none>"
        raise HookError(
            f"Apply propagation failed; already-applied paths: {applied}; error: {exc}"
        ) from exc
    return expected


def _verify_propagated_changes(
    repo_root: pathlib.Path,
    expected: dict[str, StagedFile | None],
) -> None:
    mismatches: list[str] = []
    for relative_path, staged in sorted(expected.items()):
        destination = _repo_path_from_git(repo_root, relative_path)
        if staged is None:
            if destination.exists() or destination.is_symlink():
                mismatches.append(relative_path)
            continue
        if path_has_symlink(repo_root, destination) or not destination.is_file():
            mismatches.append(relative_path)
            continue
        metadata = destination.lstat()
        if _hash_file(destination, staged.size) != staged.digest or stat.S_IMODE(
            metadata.st_mode
        ) != stat.S_IMODE(staged.mode):
            mismatches.append(relative_path)
    if mismatches:
        raise HookError(
            "Real checkout does not match validated staging output: "
            + ", ".join(mismatches)
        )


def _metadata_changes(before: MetadataSnapshot, after: MetadataSnapshot) -> list[str]:
    return sorted(
        path for path in set(before) | set(after) if before.get(path) != after.get(path)
    )


def _checkout_changes_from_baseline(
    repo_root: pathlib.Path,
    baseline: set[str],
    baseline_contents: dict[str, FileSnapshot],
) -> tuple[set[str], set[str]]:
    current = list_repo_changes(repo_root)
    current_baseline_contents = _snapshot_repo_files(repo_root, baseline)
    content_changes = {
        path
        for path, before_content in baseline_contents.items()
        if before_content != current_baseline_contents[path]
    }
    return current, content_changes


def _verify_pre_propagation_security_state(
    context: RuntimeContext,
    baseline: set[str],
    baseline_contents: dict[str, FileSnapshot],
    index_before: tuple[str, str],
    metadata_before: MetadataSnapshot,
) -> None:
    current, content_changes = _checkout_changes_from_baseline(
        context.repo_root, baseline, baseline_contents
    )
    checkout_changes = sorted((current ^ baseline) | content_changes)
    if checkout_changes:
        raise HookError(
            "Apply modified the real checkout before propagation: "
            + ", ".join(checkout_changes)
            + ". Changes were not reverted; review them manually."
        )
    if _git_index_state(context.repo_root) != index_before:
        raise HookError(
            "Apply modified the Git index before propagation. "
            "Changes were not reverted; review them manually."
        )
    changed_metadata = _metadata_changes(
        metadata_before, _snapshot_git_control_metadata(context)
    )
    if changed_metadata:
        raise HookError(
            "Apply modified Git control metadata before propagation: "
            + ", ".join(changed_metadata[:20])
            + ". Changes were not reverted; review them manually."
        )


def _verify_post_propagation_security_state(
    context: RuntimeContext,
    baseline: set[str],
    baseline_contents: dict[str, FileSnapshot],
    index_before: tuple[str, str],
    metadata_before: MetadataSnapshot,
    staged_changes: set[str],
    propagated_expected: dict[str, StagedFile | None],
) -> None:
    _verify_propagated_changes(context.repo_root, propagated_expected)
    current, content_changes = _checkout_changes_from_baseline(
        context.repo_root, baseline, baseline_contents
    )
    changed_files = (current - baseline) | content_changes
    unapproved_real_changes = sorted(changed_files - staged_changes)
    if unapproved_real_changes:
        raise HookError(
            "Apply modified the real checkout outside validated staging propagation: "
            + ", ".join(unapproved_real_changes)
            + ". Changes were not reverted; review them manually."
        )
    if _git_index_state(context.repo_root) != index_before:
        raise HookError(
            "Apply modified the Git index after propagation. "
            "Changes were not reverted; review them manually."
        )
    changed_metadata = _metadata_changes(
        metadata_before, _snapshot_git_control_metadata(context)
    )
    if changed_metadata:
        raise HookError(
            "Apply modified Git control metadata after propagation: "
            + ", ".join(changed_metadata[:20])
            + ". Changes were not reverted; review them manually."
        )


def _apply_prompt(
    prompt: str,
    allow_paths: tuple[str, ...],
    *,
    project_access: str = "artifacts",
    request_commit_subject: bool = False,
) -> str:
    rendered_paths = "\n".join(f"- {pattern}" for pattern in allow_paths)
    if project_access == "project":
        projection_text = (
            "This workspace contains the eligible readable project projection and may "
            "include readable repository files beyond the allowlist."
        )
    else:
        projection_text = (
            "This workspace contains only eligible readable files selected by the "
            "allowlist."
        )
    commit_subject_text = ""
    if request_commit_subject:
        commit_subject_text = (
            "\nCOMMIT SUBJECT:\n"
            "Your changes will be committed for you. End your response with a single "
            "final line of the form `COMMIT: <subject>` describing what you changed: "
            "one line, imperative mood, at most "
            f"{COMMIT_SUBJECT_MAX_CHARS} characters, not starting with `-`. "
            "A missing or malformed line is replaced with a generic subject; it does "
            "not fail the step.\n"
        )
    return (
        prompt.rstrip()
        + "\n\nMANDATORY STAGING BOUNDARY:\n"
        + projection_text
        + "\nOnly changes to paths matching the allowlist below propagate to the real "
        "checkout. Any other staging change fails the apply step.\n"
        + "Allowlisted paths:\n"
        + rendered_paths
        + "\nTool availability is controlled by runner and user policy; this instruction "
        + "does not impose a universal tool ban. Do not create symlinks.\n"
        + commit_subject_text
    )


def _assert_apply_targets_checked_out_head(context: RuntimeContext) -> None:
    updates = context.cache.get("pushed_branch_updates")
    if updates is None:
        updates = [
            update
            for update in context.cache.get("push_updates", [])
            if update.ref_kind == "branch" and update.operation != "delete"
        ]
    if not isinstance(updates, (list, tuple)) or len(updates) != 1:
        raise HookError("Apply requires exactly one non-deletion pushed branch update")
    update = updates[0]
    head = run_command(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=context.repo_root,
        check=True,
    ).stdout.strip()
    local_commit = run_command(
        ["git", "rev-parse", "--verify", "--quiet", f"{update.local_sha}^{{commit}}"],
        cwd=context.repo_root,
        check=False,
    ).stdout.strip()
    if not local_commit or local_commit != head:
        raise HookError(
            "Apply requires the single pushed branch's local commit to be the checked-out HEAD"
        )


def run_apply_step(
    context: RuntimeContext,
    state: ModuleRuntimeState,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> dict[str, object]:
    validated_inputs = validate_hook_owned_artifacts(context, input_paths)
    for input_path in validated_inputs:
        if input_path.name.endswith("issues.json"):
            issues = json.loads(input_path.read_text(encoding="utf-8"))
            if isinstance(issues, list) and not issues:
                # Legacy compatibility shortcut; keep until an explicit replacement exists.
                return {"changed": False, "changed_files": [], "skipped": True}

    _assert_apply_targets_checked_out_head(context)
    _validate_apply_allowlist(context.repo_root, step.allow_paths)
    baseline = list_repo_changes(context.repo_root)
    baseline_contents = _snapshot_repo_files(context.repo_root, baseline)
    index_before = _git_index_state(context.repo_root)
    metadata_before = _snapshot_git_control_metadata(context)

    result: Any | None = None
    call_error: Exception | None = None
    staged_changes: set[str] = set()
    propagated_expected: dict[str, StagedFile | None] = {}
    with tempfile.TemporaryDirectory(
        prefix="ai-push-hooks-apply-"
    ) as temporary_directory:
        staging_root = pathlib.Path(temporary_directory).resolve(strict=True)
        profile = resolve_runner_profile(context.config, step)
        destination_baselines = _copy_checkout_to_staging(
            context,
            staging_root,
            step.allow_paths,
            project_access=profile.project_access,
        )
        staged_before = _inventory_staging(staging_root)
        try:
            result = run_runner_once(
                context,
                step,
                _apply_prompt(
                    prompt,
                    step.allow_paths,
                    project_access=profile.project_access,
                    request_commit_subject=step.auto_commit
                    and step.commit_message is None,
                ),
                validated_inputs,
                stage_name,
                working_directory=staging_root,
            )
        except Exception as exc:  # noqa: BLE001
            call_error = exc

        try:
            staged_after = _inventory_staging(staging_root)
            staged_changes = _changed_staging_paths(
                staged_before, staged_after, step.allow_paths
            )
        except Exception as exc:  # noqa: BLE001
            if call_error is None:
                call_error = exc

        _verify_pre_propagation_security_state(
            context,
            baseline,
            baseline_contents,
            index_before,
            metadata_before,
        )
        if call_error is not None:
            error_type = (
                RequiredRetentionError
                if isinstance(call_error, RequiredRetentionError)
                else HookError
            )
            raise error_type(
                f"Apply step failed in isolated staging: {call_error}"
            ) from call_error
        if result is None:
            raise HookError("Apply step failed without a runner result")
        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit code {result.returncode}"
            )
            raise HookError(f"Apply step failed in isolated staging: {details}")
        propagated_expected = _propagate_staging_changes(
            context,
            staging_root,
            staged_after,
            staged_changes,
            destination_baselines,
        )

    try:
        _verify_post_propagation_security_state(
            context,
            baseline,
            baseline_contents,
            index_before,
            metadata_before,
            staged_changes,
            propagated_expected,
        )
    except Exception as exc:  # noqa: BLE001
        applied = ", ".join(sorted(propagated_expected)) or "<none>"
        raise HookError(
            "Apply post-propagation verification failed; "
            f"already-applied paths: {applied}; error: {exc}"
        ) from exc
    outcome: dict[str, object] = {
        "changed": bool(staged_changes),
        "changed_files": sorted(staged_changes),
        "allowed_paths": list(step.allow_paths),
        "skipped": False,
    }
    if not step.auto_commit or not staged_changes:
        return outcome

    # Recorded now, committed only once the whole workflow has passed. Steps that
    # run after this one -- a test suite, a deterministic postcondition, an assert
    # gate -- must be able to reject these edits, and they cannot do that if the
    # commit has already happened here.
    register_pending_auto_commit(
        context,
        step,
        sorted(staged_changes),
        result.stdout if result is not None else None,
        # Files already modified before this run. Committing one would sweep in
        # work the developer never staged, so the commit refuses them.
        preexisting_dirty=sorted(baseline & staged_changes),
    )
    outcome["auto_commit"] = {"pending": True, "auto_push": step.auto_push}
    return outcome
