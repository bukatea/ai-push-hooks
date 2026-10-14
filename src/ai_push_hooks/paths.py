from __future__ import annotations

import os
import pathlib
import stat
import tempfile
import unicodedata
import uuid

from .types import HookError

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
ORDINARY_FILE_MODE_MASK = 0o777


def relative_path_parts(raw: str, label: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.strip():
        raise HookError(f"{label} must be a non-empty relative path")
    if "\x00" in raw or any(ord(character) < 32 for character in raw):
        raise HookError(f"{label} contains invalid control characters")

    normalized = raw.replace("\\", "/")
    windows_path = pathlib.PureWindowsPath(raw)
    if normalized.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        raise HookError(f"{label} must be relative: {raw}")

    raw_parts = normalized.split("/")
    if any(part == ".." for part in raw_parts):
        raise HookError(f"{label} must not contain '..': {raw}")
    parts = tuple(part for part in raw_parts if part not in {"", "."})
    if not parts:
        raise HookError(f"{label} must be a non-empty relative path")
    return parts


def validate_path_component(raw: str, label: str) -> str:
    parts = relative_path_parts(raw, label)
    if len(parts) != 1 or "/" in raw or "\\" in raw or parts[0] in {".", ".."}:
        raise HookError(f"{label} must be a single path component: {raw}")
    return parts[0]


def is_path_within(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def normalized_component(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def sanitize_file_mode(mode: int) -> int:
    return mode & ORDINARY_FILE_MODE_MASK


def path_is_link_or_reparse(path: pathlib.Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def path_has_symlink(root: pathlib.Path, path: pathlib.Path) -> bool:
    lexical_root = pathlib.Path(os.path.abspath(root))
    lexical_path = pathlib.Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        return True
    current = lexical_root
    if path_is_link_or_reparse(current):
        return True
    for part in relative.parts:
        current = current / part
        if path_is_link_or_reparse(current):
            return True
        if not current.exists():
            break
    return False


def resolve_contained_path(base: pathlib.Path, raw: str, label: str) -> pathlib.Path:
    parts = relative_path_parts(raw, label)
    lexical_base = pathlib.Path(os.path.abspath(base))
    lexical_candidate = lexical_base.joinpath(*parts)
    if not is_path_within(lexical_candidate, lexical_base):
        raise HookError(f"{label} escapes its intended directory: {raw}")
    if path_has_symlink(lexical_base, lexical_candidate):
        raise HookError(f"{label} traverses a symlink or reparse point: {raw}")

    resolved_base = lexical_base.resolve(strict=False)
    resolved_candidate = lexical_candidate.resolve(strict=False)
    if not is_path_within(resolved_candidate, resolved_base):
        raise HookError(
            f"{label} escapes its intended directory through a symlink: {raw}"
        )
    return resolved_candidate


def _ensure_private_directory_component(directory: pathlib.Path) -> None:
    try:
        directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    except FileExistsError:
        # Tolerate a concurrent creator winning the mkdir race, then validate
        # the result before changing its permissions.
        pass
    if path_is_link_or_reparse(directory) or not directory.is_dir():
        raise HookError(f"Private runtime path is not a safe directory: {directory}")
    os.chmod(directory, PRIVATE_DIRECTORY_MODE)


def ensure_private_directory(
    path: pathlib.Path,
    *,
    private_root: pathlib.Path | None = None,
    durable: bool = False,
    exclusive: bool = False,
) -> pathlib.Path:
    target = pathlib.Path(os.path.abspath(path))
    if durable:
        if os.name != "posix":
            raise HookError(
                "Required durable storage needs POSIX no-follow directory descriptors"
            )
        if private_root is not None and not is_path_within(
            target, pathlib.Path(os.path.abspath(private_root))
        ):
            raise HookError("Private runtime directory escapes its namespace")
        descriptor = _open_directory(target, create=True, exclusive_leaf=exclusive)
        try:
            if os.fstat(descriptor).st_uid != os.geteuid():
                raise HookError("Private runtime directory is not owned by this user")
            os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
            os.fsync(descriptor)
            # An existing entry may survive mkdir after a previous parent-sync
            # failure. Require its parent durability on every strict invocation.
            parent_descriptor = _open_directory(target.parent)
            try:
                child = os.fstat(descriptor)
                entry = os.stat(
                    target.name, dir_fd=parent_descriptor, follow_symlinks=False
                )
                if (child.st_dev, child.st_ino) != (entry.st_dev, entry.st_ino):
                    raise HookError("Private runtime directory changed concurrently")
                os.fsync(parent_descriptor)
                _check_directory_identity(target, descriptor)
            finally:
                os.close(parent_descriptor)
        finally:
            os.close(descriptor)
        return target
    if private_root is None:
        missing: list[pathlib.Path] = []
        current = target
        while not current.exists() and not path_is_link_or_reparse(current):
            missing.append(current)
            current = current.parent
        if path_is_link_or_reparse(current) or not current.is_dir():
            raise HookError(f"Private runtime directory has an unsafe parent: {path}")
        for directory in reversed(missing):
            _ensure_private_directory_component(directory)
        if path_is_link_or_reparse(target) or not target.is_dir():
            raise HookError(f"Private runtime directory is unsafe: {path}")
        os.chmod(target, PRIVATE_DIRECTORY_MODE)
        return target

    root = pathlib.Path(os.path.abspath(private_root))
    if not is_path_within(target, root):
        raise HookError(f"Private runtime directory escapes its namespace: {path}")
    if path_is_link_or_reparse(root.parent) or not root.parent.is_dir():
        raise HookError(f"Private runtime namespace has an unsafe parent: {root}")
    directories = [root]
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        directories.append(current)
    for directory in directories:
        if path_is_link_or_reparse(directory):
            raise HookError(
                f"Private runtime directory traverses a symlink or reparse point: {directory}"
            )
        if directory.exists():
            if not directory.is_dir():
                raise HookError(f"Private runtime path is not a directory: {directory}")
            os.chmod(directory, PRIVATE_DIRECTORY_MODE)
        else:
            _ensure_private_directory_component(directory)
    return target


def _open_directory(
    path: pathlib.Path, *, create: bool = False, exclusive_leaf: bool = False
) -> int:
    """Pin every ancestor without following links; persist newly created entries."""
    absolute = pathlib.Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for index, component in enumerate(absolute.parts[1:], start=1):
            if exclusive_leaf and index == len(absolute.parts) - 1:
                # The invocation leaf must never reuse an earlier evidence namespace.
                os.mkdir(component, PRIVATE_DIRECTORY_MODE, dir_fd=descriptor)
                os.fsync(descriptor)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, PRIVATE_DIRECTORY_MODE, dir_fd=descriptor)
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if metadata.st_uid not in {0, os.geteuid()}:
                os.close(child)
                raise HookError("Runtime storage ancestor is foreign-owned")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_directory_identity(path: pathlib.Path, descriptor: int) -> None:
    current = _open_directory(path)
    try:
        original_stat, current_stat = os.fstat(descriptor), os.fstat(current)
        if (original_stat.st_dev, original_stat.st_ino) != (
            current_stat.st_dev,
            current_stat.st_ino,
        ):
            raise HookError("Runtime storage directory changed concurrently")
    finally:
        os.close(current)


def atomic_write_bytes(
    path: pathlib.Path,
    content: bytes,
    *,
    mode: int = PRIVATE_FILE_MODE,
) -> None:
    if os.name == "posix":
        parent_fd = _open_directory(path.parent)
        temporary_name = ".ai-push-hooks-" + uuid.uuid4().hex
        descriptor = -1
        try:
            if os.fstat(parent_fd).st_uid != os.geteuid():
                raise HookError("Atomic write parent is foreign-owned")

            def validate_target() -> None:
                try:
                    metadata = os.stat(
                        path.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    return
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    raise HookError(
                        "Atomic write target must be an owned regular file, not symlink or reparse point"
                    )

            validate_target()
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), sanitize_file_mode(mode))
                os.fsync(handle.fileno())
            _check_directory_identity(path.parent, parent_fd)
            validate_target()
            os.replace(
                temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            os.fsync(parent_fd)
            _check_directory_identity(path.parent, parent_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_fd)
        return
    lexical_parent = pathlib.Path(os.path.abspath(path.parent))
    if any(
        path_is_link_or_reparse(p) for p in (lexical_parent, *lexical_parent.parents)
    ):
        raise HookError("Atomic write parent traverses a symlink or reparse point")
    parent = lexical_parent.resolve(strict=True)
    target = parent / path.name
    if path_is_link_or_reparse(target):
        raise HookError(f"Refusing to replace symlink or reparse point: {target}")
    if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
        raise HookError("Atomic write target must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ai-push-hooks-", dir=parent)
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, sanitize_file_mode(mode))
        if path_is_link_or_reparse(target):
            raise HookError(f"Refusing to replace symlink or reparse point: {target}")
        os.replace(temporary_path, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_text_no_follow(
    path: pathlib.Path, content: str, *, encoding: str = "utf-8"
) -> None:
    atomic_write_bytes(path, content.encode(encoding))
