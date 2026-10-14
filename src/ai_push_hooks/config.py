from __future__ import annotations

import errno
import math
import os
import pathlib
import re
import stat
from collections.abc import Mapping
from typing import Any

from .git_utils import env_bool, resolve_git_common_dir, resolve_git_dir
from .paths import (
    is_path_within,
    normalized_component,
    path_has_symlink,
    relative_path_parts,
    resolve_contained_path,
    validate_path_component,
)
from .prompts_builtin import BUILTIN_PROMPTS
from .types import (
    DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS,
    SUPPORTED_STEP_TYPES,
    GeneralConfig,
    HookConfig,
    HookError,
    LlmConfig,
    LoggingConfig,
    ModuleConfig,
    RunnerProfile,
    StepConfig,
    WorkflowConfig,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

ALLOWED_TOP_LEVEL_KEYS = {"general", "llm", "logging", "workflow", "modules", "runners"}
STORAGE_NAMESPACE_PARTS = (".git", "ai-push-hooks")
GENERAL_KEYS = {
    "enabled",
    "allow_push_on_error",
    "require_clean_worktree",
    "skip_on_sync_branch",
    "base_branch",
}
LLM_KEYS = {
    "runner",
    "model",
    "variant",
    "timeout_seconds",
    "max_parallel",
    "json_max_retries",
    "invalid_json_feedback_max_chars",
    "json_retry_new_session",
    "delete_session_after_run",
    "max_diff_bytes",
    "session_title_prefix",
}
LOGGING_KEYS = {
    "level",
    "jsonl",
    "dir",
    "capture_llm_transcript",
    "require_opencode_retention",
    "transcript_dir",
    "summary_dir",
    "print_llm_output",
}
STEP_KEYS = {
    "id",
    "type",
    "inputs",
    "output",
    "schema",
    "prompt",
    "prompt_file",
    "fallback_prompt_id",
    "collector",
    "allow_paths",
    "executor",
    "assertion",
    "python",
    "options",
    "command",
    "stdin",
    "timeout_seconds",
    "when_env",
    "runner",
    "auto_commit",
    "auto_push",
    "commit_message",
}
COMMIT_SUBJECT_MAX_CHARS = 72
RUNNER_TYPES = frozenset({"opencode", "codex", "claude", "command"})
PROJECT_ACCESS_VALUES = frozenset({"artifacts", "project"})
PROMPT_TRANSPORT_VALUES = frozenset({"stdin", "argv"})
RUNNER_KEYS = {
    "type",
    "model",
    "variant",
    "project_access",
    "command",
    "prompt_transport",
}
RUNNER_PLACEHOLDERS = frozenset({"{prompt}", "{model}", "{cwd}", "{stage}"})
PYTHON_CALLABLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
COMMAND_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z0-9_./:-]+)\}\Z")
EMBEDDED_COMMAND_PLACEHOLDER_PATTERN = re.compile(
    r"\{(?:repo|python|input:[A-Za-z0-9_./:-]+)\}"
)
COMMAND_PLACEHOLDER_NAMES = frozenset({"repo", "python"})
RUNNER_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]*\}")
CONFIG_MAX_BYTES = 1 * 1024 * 1024
PROMPT_MAX_BYTES = 256 * 1024
_FILE_READ_CHUNK_BYTES = 64 * 1024


def _read_descriptor_limited(
    descriptor: int, max_bytes: int, oversize_message: str
) -> bytes:
    """Read an already-validated descriptor without exceeding ``max_bytes``."""

    if os.fstat(descriptor).st_size > max_bytes:
        raise HookError(oversize_message)
    content = bytearray()
    while True:
        read_limit = min(
            _FILE_READ_CHUNK_BYTES,
            max_bytes - len(content) + 1,
        )
        chunk = os.read(descriptor, max(1, read_limit))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > max_bytes:
            raise HookError(oversize_message)


def _require_table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HookError(f"{label} must be a table")
    return value


def _validate_unknown_keys(
    table: dict[str, Any], allowed: set[str], label: str
) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise HookError(f"Unknown field(s) in {label}: {', '.join(sorted(unknown))}")


def _validate_bool(table: dict[str, Any], key: str, label: str) -> None:
    if key in table and type(table[key]) is not bool:
        raise HookError(f"{label}.{key} must be a TOML boolean")


def _validate_string(
    table: dict[str, Any], key: str, label: str, *, allow_none: bool = False
) -> None:
    if key in table and (table[key] is None and allow_none):
        return
    if key in table and not isinstance(table[key], str):
        raise HookError(f"{label}.{key} must be a string")


def _validate_string_list(table: dict[str, Any], key: str, label: str) -> None:
    if key not in table:
        return
    value = table[key]
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise HookError(f"{label}.{key} must be an array of strings")


def _validate_non_empty_string(table: dict[str, Any], key: str, label: str) -> None:
    _validate_string(table, key, label)
    if key in table and not table[key].strip():
        raise HookError(f"{label}.{key} must be a non-empty string")


def _validate_no_control_chars(value: str, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HookError(f"{label} must not contain NUL or control characters")


def _validate_model_override(value: str | None) -> None:
    if value is not None and not value.strip():
        raise HookError("AI_PUSH_HOOKS_MODEL must be a non-empty model identifier")
    if value is not None:
        _validate_no_control_chars(value, "AI_PUSH_HOOKS_MODEL")


def _validate_variant_override(value: str | None) -> None:
    if value is not None:
        _validate_no_control_chars(value, "AI_PUSH_HOOKS_VARIANT")


def _validate_runner_command_placeholders(
    command: list[str] | tuple[str, ...],
    label: str,
    transport: str,
    model: Any,
    effective_model: str | None = None,
) -> None:
    prompt_count = 0
    for index, argument in enumerate(command, start=1):
        argument_label = f"{label}.command[{index}]"
        _validate_no_control_chars(argument, argument_label)
        for placeholder in RUNNER_PLACEHOLDER_PATTERN.findall(argument):
            if placeholder not in RUNNER_PLACEHOLDERS:
                raise HookError(
                    f"Unknown placeholder {placeholder!r} in {argument_label}"
                )
        if ("{" in argument or "}" in argument) and argument not in RUNNER_PLACEHOLDERS:
            raise HookError(
                f"Placeholders in {argument_label} must be whole argv elements"
            )
        if argument == "{prompt}":
            prompt_count += 1
    if transport == "stdin" and prompt_count:
        raise HookError(
            f"{label}.command must not contain {{prompt}} with stdin transport"
        )
    if transport == "argv" and prompt_count != 1:
        raise HookError(
            f"{label}.command must contain exactly one {{prompt}} with argv transport"
        )
    if "{model}" in command and not (model or effective_model):
        raise HookError(
            f"{label}.command uses {{model}} but {label}.model is not configured"
        )


def _validate_runner_profiles(
    raw: dict[str, Any], effective_model: str | None = None
) -> None:
    runners = _require_table(raw.get("runners", {}), "runners")
    for name, profile_value in runners.items():
        if not isinstance(name, str) or not name.strip():
            raise HookError("runners profile names must be non-empty strings")
        _validate_no_control_chars(name, f"runners profile name `{name}`")
        label = f"runners.{name}"
        profile = _require_table(profile_value, label)
        _validate_unknown_keys(profile, RUNNER_KEYS, label)
        if "type" not in profile:
            raise HookError(f"{label}.type is required")
        _validate_non_empty_string(profile, "type", label)
        runner_type = profile["type"].strip()
        _validate_no_control_chars(profile["type"], f"{label}.type")
        if runner_type not in RUNNER_TYPES:
            raise HookError(
                f"{label}.type must be one of: {', '.join(sorted(RUNNER_TYPES))}"
            )
        _validate_string(profile, "model", label)
        if "model" in profile and not profile["model"].strip():
            raise HookError(f"{label}.model must be a non-empty string when provided")
        if "model" in profile:
            _validate_no_control_chars(profile["model"], f"{label}.model")
        _validate_string(profile, "variant", label)
        if "variant" in profile:
            _validate_no_control_chars(profile["variant"], f"{label}.variant")
        _validate_string(profile, "project_access", label)
        _validate_string(profile, "prompt_transport", label)
        if (
            "project_access" in profile
            and profile["project_access"] not in PROJECT_ACCESS_VALUES
        ):
            raise HookError(
                f"{label}.project_access must be one of: artifacts, project"
            )

        type_specific_keys = {
            "variant": runner_type == "opencode",
            "command": runner_type == "command",
            "prompt_transport": runner_type == "command",
        }
        for key, applicable in type_specific_keys.items():
            if key in profile and not applicable:
                raise HookError(
                    f"{label}.{key} is only valid for runner type {('opencode' if key == 'variant' else 'command')}"
                )

        if runner_type != "command":
            continue
        if "command" not in profile:
            raise HookError(f"{label}.command is required for runner type command")
        _validate_string_list(profile, "command", label)
        command = profile["command"]
        if not command:
            raise HookError(f"{label}.command must be a non-empty array")
        for index, argument in enumerate(command, start=1):
            if not argument.strip():
                raise HookError(f"{label}.command[{index}] must be a non-empty string")
        transport = profile.get("prompt_transport", "stdin")
        if transport not in PROMPT_TRANSPORT_VALUES:
            raise HookError(f"{label}.prompt_transport must be one of: argv, stdin")
        _validate_runner_command_placeholders(
            command,
            label,
            transport,
            profile.get("model"),
            effective_model,
        )


def _validate_integer(
    table: dict[str, Any], key: str, label: str, *, minimum: int | None = None
) -> None:
    if key not in table:
        return
    value = table[key]
    if type(value) is not int:
        raise HookError(f"{label}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise HookError(f"{label}.{key} must be at least {minimum}")


def _validate_json_options(value: Any, label: str, *, path: str = "") -> None:
    """Validate TOML options as finite, null-free JSON data."""

    location = f"{label}{path}"
    if value is None:
        raise HookError(f"{location} must not be null")
    if isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HookError(f"{location} must be a finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            _validate_json_options(item, label, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HookError(f"{location} must use string keys")
            _validate_json_options(item, label, path=f"{path}.{key}")
        return
    raise HookError(f"{location} must contain only JSON-compatible null-free values")


def _validate_python_reference(
    value: str,
    label: str,
    repo_root: pathlib.Path | None = None,
) -> str:
    """Validate a repository-local callback reference without importing it."""

    if value.count(":") != 1:
        raise HookError(
            f"{label} must be a repository-relative .py path followed by :callable"
        )
    path_value, callable_name = value.split(":", 1)
    if not callable_name or not PYTHON_CALLABLE_PATTERN.fullmatch(callable_name):
        raise HookError(f"{label} callable must be one top-level identifier")
    try:
        parts = relative_path_parts(path_value, f"{label} path")
    except HookError as exc:
        raise HookError(str(exc)) from exc
    if not parts[-1].endswith(".py"):
        raise HookError(f"{label} path must name a .py file")
    normalized = "/".join(parts) + ":" + callable_name

    if repo_root is None:
        return normalized

    root = pathlib.Path(repo_root).resolve(strict=False)
    lexical_path = root.joinpath(*parts)
    if path_has_symlink(root, lexical_path):
        raise HookError(f"{label} path must not traverse a symlink or reparse point")
    try:
        callback_path = resolve_contained_path(root, "/".join(parts), f"{label} path")
    except HookError as exc:
        raise HookError(str(exc)) from exc
    try:
        metadata = callback_path.lstat()
    except FileNotFoundError as exc:
        raise HookError(
            f"{label} path must reference an existing regular file"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    ):
        raise HookError(f"{label} path must not be a symlink or reparse point")
    if not stat.S_ISREG(metadata.st_mode):
        raise HookError(f"{label} path must reference an ordinary regular file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(callback_path, flags)
    except OSError as exc:
        raise HookError(f"{label} path could not be opened safely") from exc
    try:
        descriptor_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_metadata.st_mode):
            raise HookError(f"{label} path must reference an ordinary regular file")
    finally:
        os.close(descriptor)
    return normalized


def validate_commit_subject(value: str, label: str) -> str:
    """Return a single-line commit subject, or raise if it cannot be one.

    Shared by config validation and the runner-authored message path, so a
    subject proposed by a model is held to exactly the same rules as one written
    by hand: one line, no control characters, bounded length, and never able to
    start with `-` where Git would read it as an option.
    """

    subject = value.strip()
    if not subject:
        raise HookError(f"{label} must not be empty")
    if "\n" in subject or "\r" in subject:
        raise HookError(f"{label} must be a single line")
    _validate_no_control_chars(subject, label)
    if len(subject) > COMMIT_SUBJECT_MAX_CHARS:
        raise HookError(
            f"{label} must be at most {COMMIT_SUBJECT_MAX_CHARS} characters"
        )
    if subject.startswith("-"):
        raise HookError(f"{label} must not start with `-`")
    return subject


def _validate_auto_commit_extensions(step: dict[str, Any], label: str) -> None:
    """Validate the apply-step commit/push opt-ins and their dependencies."""

    for key in ("auto_commit", "auto_push"):
        if key in step and not isinstance(step[key], bool):
            raise HookError(f"{label}.{key} must be a boolean")

    auto_commit = bool(step.get("auto_commit", False))
    if step.get("auto_push", False) and not auto_commit:
        raise HookError(f"{label}.auto_push requires {label}.auto_commit")
    if "commit_message" in step:
        value = step["commit_message"]
        if not isinstance(value, str):
            raise HookError(f"{label}.commit_message must be a string")
        if not auto_commit:
            raise HookError(f"{label}.commit_message requires {label}.auto_commit")
        validate_commit_subject(value, f"{label}.commit_message")


def _validate_step_extensions(
    step: dict[str, Any],
    label: str,
    *,
    repo_root: pathlib.Path | None = None,
) -> None:
    """Validate the implementation seams shared by config type checking/building."""

    step_type = step.get("type")
    if not isinstance(step_type, str):
        return

    implementation_keys = {
        "collect": ("collector", "python"),
        "exec": ("executor", "python", "command"),
        "assert": ("assertion", "python", "command"),
    }
    implementations = implementation_keys.get(step_type)
    if implementations is not None:
        for key in implementations:
            if key in {"collector", "executor", "assertion", "python"}:
                value = step.get(key)
                if isinstance(value, str) and not value.strip():
                    raise HookError(f"{label}.{key} must be a non-empty string")
        if "command" in step and not step["command"]:
            raise HookError(f"{label}.command must be a non-empty array")
        selected = [key for key in implementations if step.get(key) not in (None, "")]
        if len(selected) != 1:
            choices = ", ".join(implementations)
            raise HookError(
                f"{label} requires exactly one implementation field: {choices}"
            )

    applicable = {
        "collector": {"collect"},
        "executor": {"exec"},
        "assertion": {"assert"},
        "python": {"collect", "exec", "assert"},
        "command": {"exec", "assert"},
        "stdin": {"exec", "assert"},
        "timeout_seconds": {"exec", "assert"},
        "options": {"collect", "exec", "assert"},
        "auto_commit": {"apply"},
        "auto_push": {"apply"},
        "commit_message": {"apply"},
    }
    for key, allowed_types in applicable.items():
        if key in step and step_type not in allowed_types:
            raise HookError(f"{label}.{key} is not valid for {step_type} steps")

    _validate_auto_commit_extensions(step, label)

    has_python = step.get("python") not in (None, "")
    has_command = bool(step.get("command"))
    if "options" in step:
        if not has_python:
            raise HookError(f"{label}.options requires {label}.python")
        _validate_json_options(step["options"], f"{label}.options")
    if "stdin" in step and not has_command:
        raise HookError(f"{label}.stdin is only valid with {label}.command")
    if "timeout_seconds" in step and not has_command:
        raise HookError(f"{label}.timeout_seconds is only valid with {label}.command")

    if has_python:
        inputs = step.get("inputs", [])
        if len(inputs) != len(set(inputs)):
            raise HookError(
                f"{label}.inputs must not contain duplicate references for Python steps"
            )
        _validate_python_reference(step["python"], f"{label}.python", repo_root)

    if has_command:
        command = step["command"]
        declared_inputs = set(step.get("inputs", []))
        for index, argument in enumerate(command, start=1):
            match = COMMAND_PLACEHOLDER_PATTERN.fullmatch(argument)
            if match is not None:
                token = match.group(1)
                if token in COMMAND_PLACEHOLDER_NAMES:
                    continue
                if token.startswith("input:"):
                    logical_ref = token.removeprefix("input:")
                    if logical_ref not in declared_inputs:
                        raise HookError(
                            f"{label}.command[{index}] references undeclared input "
                            f"`{logical_ref}`"
                        )
                    continue
                raise HookError(
                    f"Unknown command placeholder {{{token}}} in {label}.command[{index}]"
                )
            if EMBEDDED_COMMAND_PLACEHOLDER_PATTERN.search(argument):
                raise HookError(
                    f"Recognized command placeholders in {label}.command[{index}] "
                    "must be whole argv elements"
                )

    if "stdin" in step and step.get("stdin") not in step.get("inputs", []):
        raise HookError(f"{label}.stdin must exactly match a declared input")


def _reject_legacy_step_type(step_type: str, label: str) -> None:
    if step_type in {"llm", "agent"}:
        raise HookError(
            f"Legacy workflow step type `{step_type}` is not supported at {label}.type; "
            "use `ask` instead"
        )


def _validate_config_types(raw: dict[str, Any]) -> None:
    unknown = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise HookError(
            "Legacy or unsupported config keys are not allowed: "
            + ", ".join(sorted(unknown))
        )

    general = _require_table(raw.get("general", {}), "general")
    _validate_unknown_keys(general, GENERAL_KEYS, "general")
    for key in (
        "enabled",
        "allow_push_on_error",
        "require_clean_worktree",
        "skip_on_sync_branch",
    ):
        _validate_bool(general, key, "general")
    _validate_string(general, "base_branch", "general")

    llm = _require_table(raw.get("llm", {}), "llm")
    _validate_unknown_keys(llm, LLM_KEYS, "llm")
    for key in ("runner", "model", "variant", "session_title_prefix"):
        _validate_string(llm, key, "llm")
    _validate_non_empty_string(llm, "runner", "llm")
    if "runner" in llm:
        _validate_no_control_chars(llm["runner"], "llm.runner")
    for key in ("model", "variant"):
        if key in llm:
            _validate_no_control_chars(llm[key], f"llm.{key}")
    for key in ("json_retry_new_session", "delete_session_after_run"):
        _validate_bool(llm, key, "llm")
    _validate_integer(llm, "timeout_seconds", "llm", minimum=1)
    _validate_integer(llm, "max_parallel", "llm", minimum=1)
    _validate_integer(llm, "json_max_retries", "llm", minimum=0)
    _validate_integer(llm, "invalid_json_feedback_max_chars", "llm", minimum=1)
    _validate_integer(llm, "max_diff_bytes", "llm", minimum=1)

    logging = _require_table(raw.get("logging", {}), "logging")
    _validate_unknown_keys(logging, LOGGING_KEYS, "logging")
    for key in ("level", "dir", "transcript_dir", "summary_dir"):
        _validate_string(logging, key, "logging")
    for key in (
        "jsonl",
        "capture_llm_transcript",
        "print_llm_output",
        "require_opencode_retention",
    ):
        _validate_bool(logging, key, "logging")

    workflow = _require_table(raw.get("workflow", {}), "workflow")
    _validate_unknown_keys(workflow, {"modules"}, "workflow")
    _validate_string_list(workflow, "modules", "workflow")
    if "modules" in workflow:
        module_ids: set[str] = set()
        for module_id in workflow["modules"]:
            normalized_module_id = module_id.strip()
            if normalized_module_id in module_ids:
                raise HookError(
                    f"Duplicate module id `{normalized_module_id}` in workflow.modules"
                )
            module_ids.add(normalized_module_id)

    modules = _require_table(raw.get("modules", {}), "modules")
    for module_id, module_value in modules.items():
        if not isinstance(module_id, str):
            raise HookError("modules keys must be strings")
        module = _require_table(module_value, f"modules.{module_id}")
        _validate_unknown_keys(module, {"enabled", "steps"}, f"modules.{module_id}")
        _validate_bool(module, "enabled", f"modules.{module_id}")
        if "steps" in module:
            steps = module["steps"]
            if not isinstance(steps, (list, tuple)):
                raise HookError(f"modules.{module_id}.steps must be an array of tables")
            step_ids: set[str] = set()
            for index, step_value in enumerate(steps, start=1):
                step = _require_table(step_value, f"modules.{module_id}.steps[{index}]")
                label = f"modules.{module_id}.steps[{index}]"
                _validate_unknown_keys(step, STEP_KEYS, label)
                for key in ("id", "type"):
                    _validate_string(step, key, label)
                step_id = step.get("id")
                if isinstance(step_id, str):
                    normalized_step_id = step_id.strip()
                    if normalized_step_id in step_ids:
                        raise HookError(
                            f"Duplicate step id `{normalized_step_id}` at {label}.id"
                        )
                    step_ids.add(normalized_step_id)
                if isinstance(step.get("type"), str):
                    _reject_legacy_step_type(step["type"].strip(), label)
                for key in (
                    "collector",
                    "executor",
                    "assertion",
                    "python",
                    "output",
                    "schema",
                    "prompt",
                    "prompt_file",
                    "fallback_prompt_id",
                    "when_env",
                    "runner",
                ):
                    _validate_string(step, key, label, allow_none=True)
                for key in ("inputs", "allow_paths", "command"):
                    _validate_string_list(step, key, label)
                if "options" in step and not isinstance(step["options"], dict):
                    raise HookError(f"{label}.options must be a table")
                _validate_string(step, "stdin", label, allow_none=True)
                _validate_integer(step, "timeout_seconds", label, minimum=1)
                if (
                    "runner" in step
                    and step["runner"] is not None
                    and not step["runner"].strip()
                ):
                    raise HookError(f"{label}.runner must be a non-empty string")
                if "runner" in step and step["runner"] is not None:
                    _validate_no_control_chars(step["runner"], f"{label}.runner")
                if (
                    step.get("runner") is not None
                    and isinstance(step.get("type"), str)
                    and step["type"] in {"collect", "exec", "assert"}
                ):
                    raise HookError(
                        f"{label}.runner is only valid on ask and apply steps"
                    )
                _validate_step_extensions(step, label)


def _normalize_runner_profile(name: str, raw: dict[str, Any]) -> RunnerProfile:
    runner_type = str(raw["type"]).strip()
    return RunnerProfile(
        name=name,
        type=runner_type,
        model=str(raw["model"]) if raw.get("model") is not None else None,
        variant=str(raw["variant"]) if raw.get("variant") is not None else None,
        project_access=str(
            raw.get(
                "project_access",
                "artifacts" if runner_type == "opencode" else "project",
            )
        ),
        command=tuple(str(item) for item in raw.get("command", []) or []),
        prompt_transport=str(raw.get("prompt_transport", "stdin")),
    )


def _normalize_step(
    raw: dict[str, Any], label: str, *, repo_root: pathlib.Path | None = None
) -> StepConfig:
    step_type = str(raw.get("type", "")).strip()
    _reject_legacy_step_type(step_type, label)
    if step_type not in SUPPORTED_STEP_TYPES:
        raise HookError(f"Unknown step type at {label}.type: {step_type}")
    _validate_step_extensions(raw, label, repo_root=repo_root)
    step = StepConfig(
        id=str(raw.get("id", "")).strip(),
        type=step_type,
        inputs=tuple(str(item) for item in raw.get("inputs", []) or []),
        output=str(raw.get("output")).strip()
        if raw.get("output") is not None
        else None,
        schema=str(raw.get("schema")).strip()
        if raw.get("schema") is not None
        else None,
        prompt=str(raw.get("prompt")).strip()
        if raw.get("prompt") is not None
        else None,
        prompt_file=str(raw.get("prompt_file")).strip()
        if raw.get("prompt_file") is not None
        else None,
        fallback_prompt_id=(
            str(raw.get("fallback_prompt_id")).strip()
            if raw.get("fallback_prompt_id") is not None
            else None
        ),
        collector=str(raw.get("collector")).strip()
        if raw.get("collector") is not None
        else None,
        allow_paths=tuple(str(item) for item in raw.get("allow_paths", []) or []),
        executor=str(raw.get("executor")).strip()
        if raw.get("executor") is not None
        else None,
        assertion=str(raw.get("assertion")).strip()
        if raw.get("assertion") is not None
        else None,
        python=str(raw.get("python")).strip()
        if raw.get("python") is not None
        else None,
        options=dict(raw.get("options", {}) or {}),
        command=tuple(str(item) for item in raw.get("command", []) or []),
        stdin=str(raw.get("stdin")).strip() if raw.get("stdin") is not None else None,
        timeout_seconds=(
            int(raw["timeout_seconds"])
            if raw.get("timeout_seconds") is not None
            else (DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS if raw.get("command") else None)
        ),
        when_env=str(raw.get("when_env")).strip()
        if raw.get("when_env") is not None
        else None,
        runner=str(raw.get("runner")).strip()
        if raw.get("runner") is not None
        else None,
        auto_commit=bool(raw.get("auto_commit", False)),
        auto_push=bool(raw.get("auto_push", False)),
        commit_message=str(raw.get("commit_message")).strip()
        if raw.get("commit_message") is not None
        else None,
    )
    if not step.id:
        raise HookError("Every workflow step requires a non-empty id")
    validate_path_component(step.id, "Workflow step id")
    if step.output:
        validate_path_component(step.output, f"Output for step `{step.id}`")
    for pattern in step.allow_paths:
        parts = relative_path_parts(pattern, f"allow_paths entry for step `{step.id}`")
        if any(normalized_component(part) == ".git" for part in parts):
            raise HookError(f"Apply step `{step.id}` may not allow Git metadata paths")
        if normalized_component(parts[-1]) == "agents.md":
            raise HookError(f"Apply step `{step.id}` may not allow AGENTS.md")
    if step.is_promptable and not any(
        [step.prompt, step.prompt_file, step.fallback_prompt_id]
    ):
        raise HookError(
            f"Promptable step `{step.id}` requires prompt, prompt_file, or fallback_prompt_id"
        )
    if step.type == "collect" and not (step.collector or step.python):
        raise HookError(f"Collect step `{step.id}` requires collector or python")
    if step.type == "ask" and not step.output:
        raise HookError(f"Ask step `{step.id}` requires output")
    if step.type == "apply" and not step.allow_paths:
        raise HookError(f"Apply step `{step.id}` requires allow_paths")
    if step.type == "exec" and not (step.executor or step.python or step.command):
        raise HookError(f"Exec step `{step.id}` requires executor, python, or command")
    if step.type == "assert" and not (step.assertion or step.python or step.command):
        raise HookError(
            f"Assert step `{step.id}` requires assertion, python, or command"
        )
    return step


def _build_config(
    raw: dict[str, Any],
    *,
    effective_model: str | None = None,
    repo_root: pathlib.Path | None = None,
) -> HookConfig:
    if not isinstance(raw, dict):
        raise HookError("Config document must contain a top-level table")
    _validate_config_types(raw)
    _validate_runner_profiles(raw, effective_model)
    unknown = set(raw) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise HookError(
            "Legacy or unsupported config keys are not allowed: "
            + ", ".join(sorted(unknown))
        )

    workflow_modules = tuple(
        str(item) for item in raw.get("workflow", {}).get("modules", []) or []
    )
    if not workflow_modules:
        raise HookError("workflow.modules must define at least one module id")

    module_payload = raw.get("modules", {})
    if not isinstance(module_payload, dict):
        raise HookError("modules must be a table")

    modules: dict[str, ModuleConfig] = {}
    for module_id in workflow_modules:
        validate_path_component(module_id, "Workflow module id")
        if module_id not in module_payload:
            raise HookError(f"workflow.modules references unknown module `{module_id}`")
        module_raw = module_payload[module_id]
        steps_raw = module_raw.get("steps", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            raise HookError(f"Module `{module_id}` must define a non-empty steps array")
        modules[module_id] = ModuleConfig(
            id=module_id,
            enabled=bool(module_raw.get("enabled", True)),
            steps=tuple(
                _normalize_step(
                    step,
                    f"modules.{module_id}.steps[{index}]",
                    # Repository filesystem checks are performed once below,
                    # after environment overrides have been resolved.
                    repo_root=None,
                )
                for index, step in enumerate(steps_raw, start=1)
            ),
        )

    # Validate repository-local callback paths in disabled/unselected modules too,
    # while preserving the existing runtime model that only workflow modules are
    # materialized in HookConfig.modules.
    if repo_root is not None:
        validated_python_references: set[str] = set()
        for module_id, module_raw in module_payload.items():
            for index, step_raw in enumerate(
                module_raw.get("steps", []) or [], start=1
            ):
                python_ref = step_raw.get("python")
                if python_ref is not None:
                    if python_ref in validated_python_references:
                        continue
                    validated_python_references.add(python_ref)
                    _validate_python_reference(
                        python_ref,
                        f"modules.{module_id}.steps[{index}].python",
                        repo_root,
                    )

    general = GeneralConfig(**raw.get("general", {}))
    llm = LlmConfig(**raw.get("llm", {}))
    logging = LoggingConfig(**raw.get("logging", {}))
    if logging.require_opencode_retention and (
        not logging.capture_llm_transcript or general.allow_push_on_error
    ):
        from .types import RequiredRetentionError

        raise RequiredRetentionError(
            "Required OpenCode retention requires capture_llm_transcript=true and allow_push_on_error=false"
        )
    runner_payload = raw.get("runners", {})
    runners = {
        name: _normalize_runner_profile(name, profile)
        for name, profile in runner_payload.items()
    }
    if llm.runner != "opencode" and llm.runner not in runners:
        raise HookError(f"llm.runner references missing runner profile `{llm.runner}`")
    for module_id, module_raw in module_payload.items():
        for index, step_raw in enumerate(module_raw.get("steps", []) or [], start=1):
            step_runner = step_raw.get("runner")
            if step_runner is None or step_runner == "opencode":
                continue
            if step_runner not in runners:
                raise HookError(
                    f"modules.{module_id}.steps[{index}].runner references missing runner profile "
                    f"`{step_runner}`"
                )
    for label, storage_path in (
        ("logging.dir", logging.dir),
        ("logging.transcript_dir", logging.transcript_dir),
        ("logging.summary_dir", logging.summary_dir),
    ):
        parts = relative_path_parts(storage_path, label)
        if parts[:2] != STORAGE_NAMESPACE_PARTS or len(parts) < 3:
            raise HookError(f"{label} must be inside .git/ai-push-hooks/")
    return HookConfig(
        general=general,
        llm=llm,
        logging=logging,
        workflow=WorkflowConfig(modules=workflow_modules),
        modules=modules,
        runners=runners,
    )


def resolve_runner_profile(
    config: HookConfig,
    step: StepConfig,
    env: Mapping[str, str] | None = None,
) -> RunnerProfile:
    """Resolve the runner selected by a promptable step and apply final env overrides."""
    if step.type not in {"ask", "apply"} and step.runner is not None:
        raise HookError(f"Step `{step.id}` may not select a runner")

    selected_name = step.runner or config.llm.runner
    profile = config.runners.get(selected_name)
    if profile is None:
        if selected_name != "opencode":
            raise HookError(f"Runner profile `{selected_name}` does not exist")
        profile = RunnerProfile(
            name="opencode",
            type="opencode",
            model=config.llm.model,
            variant=config.llm.variant,
            project_access="artifacts",
        )

    environment = os.environ if env is None else env
    model = profile.model
    model_override = environment.get("AI_PUSH_HOOKS_MODEL")
    _validate_model_override(model_override)
    if model_override is not None:
        model = model_override

    variant = profile.variant
    if profile.type == "opencode":
        variant_override = environment.get("AI_PUSH_HOOKS_VARIANT")
        _validate_variant_override(variant_override)
        if variant_override is not None:
            variant = variant_override.strip()

    return RunnerProfile(
        name=selected_name,
        type=profile.type,
        model=model,
        variant=variant,
        project_access=profile.project_access,
        command=profile.command,
        prompt_transport=profile.prompt_transport,
    )


def _apply_env_overrides(
    config: HookConfig,
    *,
    repo_root: pathlib.Path | None = None,
    raw_modules: dict[str, Any] | None = None,
) -> HookConfig:
    raw = {
        "general": {
            "enabled": config.general.enabled,
            "allow_push_on_error": config.general.allow_push_on_error,
            "require_clean_worktree": config.general.require_clean_worktree,
            "skip_on_sync_branch": config.general.skip_on_sync_branch,
            "base_branch": config.general.base_branch,
        },
        "llm": config.llm.__dict__.copy(),
        "logging": config.logging.__dict__.copy(),
        "workflow": {"modules": list(config.workflow.modules)},
        "modules": {},
        "runners": {},
    }
    for module_id, module in config.modules.items():
        step_payloads: list[dict[str, Any]] = []
        for step in module.steps:
            step_payload = step.__dict__.copy()
            for key in (
                "collector",
                "executor",
                "assertion",
                "python",
                "stdin",
                "timeout_seconds",
                "when_env",
                "runner",
            ):
                if step_payload[key] is None:
                    step_payload.pop(key)
            if not step_payload["options"]:
                step_payload.pop("options")
            if not step_payload["command"]:
                step_payload.pop("command")
            step_payloads.append(step_payload)
        raw["modules"][module_id] = {
            "enabled": module.enabled,
            "steps": step_payloads,
        }
    if raw_modules is not None:
        # Keep disabled and unselected modules in the final build so their
        # callback references are validated without materializing them.
        raw["modules"] = raw_modules
    for name, profile in config.runners.items():
        runner_raw: dict[str, Any] = {
            "type": profile.type,
            "project_access": profile.project_access,
        }
        if profile.model is not None:
            runner_raw["model"] = profile.model
        if profile.variant is not None:
            runner_raw["variant"] = profile.variant
        if profile.type == "command":
            runner_raw["command"] = list(profile.command)
            runner_raw["prompt_transport"] = profile.prompt_transport
        raw["runners"][name] = runner_raw

    def read_env_bool(name: str) -> bool | None:
        value = os.getenv(name)
        if value is None:
            return None
        parsed = env_bool(name)
        if parsed is None:
            raise HookError(
                f"Invalid boolean environment override {name}: expected true/false"
            )
        return parsed

    skip = read_env_bool("AI_PUSH_HOOKS_SKIP")
    if skip is True:
        raw["general"]["enabled"] = False
    allow_on_error = read_env_bool("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR")
    if allow_on_error is not None:
        raw["general"]["allow_push_on_error"] = allow_on_error
    require_clean = read_env_bool("AI_PUSH_HOOKS_REQUIRE_CLEAN")
    if require_clean is not None:
        raw["general"]["require_clean_worktree"] = require_clean
    allow_dirty = read_env_bool("AI_PUSH_HOOKS_ALLOW_DIRTY")
    if allow_dirty is True:
        raw["general"]["require_clean_worktree"] = False
    base_branch = os.getenv("AI_PUSH_HOOKS_BASE_BRANCH")
    if base_branch:
        raw["general"]["base_branch"] = base_branch.strip() or "main"

    logging_level = os.getenv("AI_PUSH_HOOKS_LOG_LEVEL")
    if logging_level:
        raw["logging"]["level"] = logging_level.strip().lower()
    print_output = read_env_bool("AI_PUSH_HOOKS_PRINT_LLM_OUTPUT")
    if print_output is not None:
        raw["logging"]["print_llm_output"] = print_output
    model = os.getenv("AI_PUSH_HOOKS_MODEL")
    _validate_model_override(model)
    if model is not None:
        raw["llm"]["model"] = model
    variant = os.getenv("AI_PUSH_HOOKS_VARIANT")
    _validate_variant_override(variant)
    if variant is not None:
        raw["llm"]["variant"] = variant.strip()
    timeout = os.getenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout.strip())
        except ValueError as exc:
            raise HookError(
                "Invalid numeric environment override AI_PUSH_HOOKS_TIMEOUT_SECONDS: "
                f"{timeout!r}"
            ) from exc
        if parsed_timeout < 1:
            raise HookError(
                "Invalid numeric environment override AI_PUSH_HOOKS_TIMEOUT_SECONDS: "
                "must be at least 1"
            )
        raw["llm"]["timeout_seconds"] = parsed_timeout
    return _build_config(raw, effective_model=model, repo_root=repo_root)


def load_config(repo_root: pathlib.Path) -> tuple[HookConfig, pathlib.Path]:
    config_path = repo_root / "ai-push-hooks.toml"
    try:
        path_metadata = config_path.lstat()
    except FileNotFoundError:
        raise HookError(
            "Missing required config file `ai-push-hooks.toml` in repo root. "
            "Run `ai-push-hooks init --template minimal-docs` first"
        )
    except OSError as exc:
        raise HookError(f"Could not inspect config file {config_path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(path_metadata.st_mode) or bool(
        getattr(path_metadata, "st_file_attributes", 0) & reparse_flag
    ):
        raise HookError(
            f"Config file must not be a symlink or reparse point: {config_path}"
        )
    if not stat.S_ISREG(path_metadata.st_mode):
        raise HookError(f"Config file must be a regular file: {config_path}")

    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(config_path, flags)
        descriptor_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_metadata.st_mode):
            raise HookError(f"Config file must be a regular file: {config_path}")
        content = _read_descriptor_limited(
            descriptor,
            CONFIG_MAX_BYTES,
            f"Config file exceeds maximum size of {CONFIG_MAX_BYTES} bytes: {config_path}",
        )
        text = content.decode("utf-8")
    except HookError:
        raise
    except UnicodeDecodeError as exc:
        raise HookError(f"Config file is not valid UTF-8: {config_path}") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise HookError(
                f"Config file must not be a symlink or reparse point: {config_path}"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise HookError(
                "Missing required config file `ai-push-hooks.toml` in repo root. "
                "Run `ai-push-hooks init --template minimal-docs` first"
            ) from exc
        raise HookError(f"Could not read config file {config_path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        loaded = tomllib.loads(text)
    except ValueError as exc:
        location = ""
        if hasattr(exc, "lineno") and hasattr(exc, "colno"):
            location = f" at line {exc.lineno}, column {exc.colno}"
        raise HookError(f"Invalid TOML in {config_path}{location}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise HookError(
            f"Invalid config format in {config_path}: expected a top-level table"
        )
    model_override = os.getenv("AI_PUSH_HOOKS_MODEL")
    variant_override = os.getenv("AI_PUSH_HOOKS_VARIANT")
    logging_raw = loaded.get("logging")
    required = (
        isinstance(logging_raw, dict)
        and logging_raw.get("require_opencode_retention") is True
    )
    try:
        if required and isinstance(loaded.get("general", {}), dict):
            # Validate policy against effective precedence, not the TOML intermediate.
            override = env_bool("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR")
            if override is not None:
                loaded = {
                    **loaded,
                    "general": {
                        **loaded.get("general", {}),
                        "allow_push_on_error": override,
                    },
                }
        _validate_model_override(model_override)
        _validate_variant_override(variant_override)
        return _apply_env_overrides(
            _build_config(loaded, effective_model=model_override, repo_root=None),
            repo_root=repo_root,
            raw_modules=loaded.get("modules", {}),
        ), config_path
    except HookError as exc:
        if required:
            from .types import RequiredRetentionError

            raise RequiredRetentionError(
                "Required OpenCode retention: effective configuration rejected"
            ) from exc
        raise


def resolve_prompt_text(repo_root: pathlib.Path, step: StepConfig) -> str:
    if step.prompt and step.prompt.strip():
        return step.prompt.strip()
    if step.prompt_file:
        parts = relative_path_parts(
            step.prompt_file, f"Prompt file for step `{step.id}`"
        )
        if any(normalized_component(part) == ".git" for part in parts):
            raise HookError(
                f"Prompt file for step `{step.id}` must not reference Git metadata"
            )
        lexical_prompt_path = repo_root.joinpath(*parts)
        if path_has_symlink(repo_root, lexical_prompt_path):
            raise HookError(
                f"Prompt file for step `{step.id}` must not traverse a symlink"
            )
        prompt_path = resolve_contained_path(
            repo_root,
            step.prompt_file,
            f"Prompt file for step `{step.id}`",
        )
        resolved_prompt_path = prompt_path.resolve(strict=False)
        try:
            git_roots = (
                resolve_git_dir(repo_root).resolve(strict=True),
                resolve_git_common_dir(repo_root).resolve(strict=True),
            )
        except HookError:
            git_roots = ()
        if any(
            is_path_within(resolved_prompt_path, git_root) for git_root in git_roots
        ):
            raise HookError(
                f"Prompt file for step `{step.id}` must not resolve inside Git metadata"
            )
        if prompt_path.exists():
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            try:
                descriptor = os.open(prompt_path, flags)
            except OSError as exc:
                raise HookError(
                    f"Prompt file could not be opened safely for step `{step.id}`: {prompt_path}"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise HookError(
                        f"Prompt file is not a regular file for step `{step.id}`: {prompt_path}"
                    )
                content = _read_descriptor_limited(
                    descriptor,
                    PROMPT_MAX_BYTES,
                    f"Prompt file exceeds maximum size of {PROMPT_MAX_BYTES} bytes "
                    f"for step `{step.id}`: {prompt_path}",
                )
                try:
                    text = content.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise HookError(
                        f"Prompt file is not valid UTF-8 for step `{step.id}`: {prompt_path}"
                    ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if text:
                return text
        if step.fallback_prompt_id:
            return resolve_builtin_prompt(step.fallback_prompt_id)
        raise HookError(
            f"Prompt file not found or empty for step `{step.id}`: {prompt_path}"
        )
    if step.fallback_prompt_id:
        return resolve_builtin_prompt(step.fallback_prompt_id)
    raise HookError(f"No prompt source available for step `{step.id}`")


def resolve_builtin_prompt(prompt_id: str) -> str:
    prompt = BUILTIN_PROMPTS.get(prompt_id)
    if not prompt:
        raise HookError(f"Unknown built-in prompt id: {prompt_id}")
    return prompt
