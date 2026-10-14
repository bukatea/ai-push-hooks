from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

READ_ONLY_STEP_TYPES = frozenset({"collect", "ask"})
PROMPTABLE_STEP_TYPES = frozenset({"ask", "apply"})
SUPPORTED_STEP_TYPES = frozenset({"collect", "ask", "apply", "exec", "assert"})
DEFAULT_STEP_COMMAND_TIMEOUT_SECONDS = 60
FEATURE_BRANCH_PREFIXES = ("feat/", "feature/")
ZERO_OID_LENGTHS = frozenset({40, 64})

_ANSI_ESCAPE_PATTERN = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])"
)
_UNSAFE_TERMINAL_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class HookError(RuntimeError):
    pass


class RequiredRetentionError(HookError):
    """Required OpenCode evidence failed; fail-open must not override this policy."""


class PushSupersededError(HookError):
    """The in-flight push was invalidated by a commit this run created.

    Git selects the commits a push will send before the pre-push hook runs, so a
    commit created during the hook is not part of the push already in flight.
    Allowing that push to proceed would send the pre-fix commit and leave the fix
    behind, reporting success. This is therefore raised to stop the push, and it
    deliberately ignores ``allow_push_on_error``: fail-open is a policy choice
    about checks that could not complete, not a licence to push code that this
    run has already superseded locally.
    """


def is_zero_oid(value: str) -> bool:
    return len(value) in ZERO_OID_LENGTHS and not value.strip("0")


@dataclass(frozen=True)
class PushRefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def ref_kind(self) -> str:
        if self.remote_ref.startswith("refs/heads/"):
            return "branch"
        if self.remote_ref.startswith("refs/tags/"):
            return "tag"
        return "other"

    @property
    def operation(self) -> str:
        if is_zero_oid(self.local_sha):
            return "delete"
        if is_zero_oid(self.remote_sha):
            return "create"
        return "update"

    @property
    def branch_name(self) -> str | None:
        if self.ref_kind != "branch" or self.operation == "delete":
            return None
        return self.remote_ref.removeprefix("refs/heads/")


@dataclass(frozen=True)
class PushRevisionRange:
    update: PushRefUpdate
    expression: str
    strategy: str


@dataclass(frozen=True)
class GeneralConfig:
    enabled: bool = True
    allow_push_on_error: bool = False
    require_clean_worktree: bool = False
    skip_on_sync_branch: bool = True
    base_branch: str = "main"


@dataclass(frozen=True)
class LlmConfig:
    runner: str = "opencode"
    model: str = "openai/gpt-5.6-luna"
    variant: str = ""
    timeout_seconds: int = 800
    max_parallel: int = 2
    json_max_retries: int = 2
    invalid_json_feedback_max_chars: int = 6000
    json_retry_new_session: bool = True
    delete_session_after_run: bool = True
    max_diff_bytes: int = 180000
    session_title_prefix: str = "ai-push-hooks"


@dataclass(frozen=True)
class RunnerProfile:
    type: str
    name: str = ""
    model: str | None = None
    variant: str | None = None
    project_access: str = "artifacts"
    command: tuple[str, ...] = ()
    prompt_transport: str = "stdin"


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "status"
    jsonl: bool = True
    dir: str = ".git/ai-push-hooks/logs"
    capture_llm_transcript: bool = True
    require_opencode_retention: bool = False
    transcript_dir: str = ".git/ai-push-hooks/transcripts"
    summary_dir: str = ".git/ai-push-hooks/summaries"
    print_llm_output: bool = False


@dataclass(frozen=True)
class StepConfig:
    id: str
    type: str
    inputs: tuple[str, ...] = ()
    output: str | None = None
    schema: str | None = None
    prompt: str | None = None
    prompt_file: str | None = None
    fallback_prompt_id: str | None = None
    collector: str | None = None
    allow_paths: tuple[str, ...] = ()
    executor: str | None = None
    assertion: str | None = None
    python: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    stdin: str | None = None
    timeout_seconds: int | None = None
    when_env: str | None = None
    runner: str | None = None
    auto_commit: bool = False
    auto_push: bool = False
    commit_message: str | None = None

    @property
    def is_read_only(self) -> bool:
        return self.type in READ_ONLY_STEP_TYPES

    @property
    def is_promptable(self) -> bool:
        return self.type in PROMPTABLE_STEP_TYPES


@dataclass(frozen=True)
class ModuleConfig:
    id: str
    enabled: bool
    steps: tuple[StepConfig, ...]


@dataclass(frozen=True)
class WorkflowConfig:
    modules: tuple[str, ...]


@dataclass(frozen=True)
class HookConfig:
    general: GeneralConfig
    llm: LlmConfig
    logging: LoggingConfig
    workflow: WorkflowConfig
    modules: dict[str, ModuleConfig]
    runners: dict[str, RunnerProfile] = field(default_factory=dict)


@dataclass
class CollectorResult:
    artifacts: dict[str, str | dict[str, Any] | list[Any]] = field(default_factory=dict)
    skip_module: bool = False
    skip_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModuleRuntimeState:
    module: ModuleConfig
    step_index: int = 0
    status: str = "pending"
    active_step_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, pathlib.Path] = field(default_factory=dict)
    error: str | None = None

    @property
    def next_step(self) -> StepConfig | None:
        if self.step_index >= len(self.module.steps):
            return None
        return self.module.steps[self.step_index]


@dataclass
class RuntimeContext:
    repo_root: pathlib.Path
    git_dir: pathlib.Path
    config: HookConfig
    logger: HookLogger
    remote_name: str
    remote_url: str
    stdin_lines: list[str]
    run_id: str
    run_dir: pathlib.Path
    opencode_executable: str | None = None
    cache: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRunResult:
    run_dir: pathlib.Path
    modules: dict[str, str]


@dataclass
class HookLogger:
    jsonl_path: pathlib.Path | None
    console_level: str = "status"
    jsonl_write_failed: bool = False
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    _verbosity_order: ClassVar[dict[str, int]] = {"status": 0, "info": 1, "debug": 2}

    def _level_is_enabled(self, level: str) -> bool:
        if level in {"warn", "error"}:
            return True
        configured = self._verbosity_order.get(self.console_level, 0)
        required = self._verbosity_order.get(level, 0)
        return configured >= required

    @staticmethod
    def _safe_text(value: object) -> str:
        """Remove terminal escape sequences without changing ordinary text."""

        return _UNSAFE_TERMINAL_CONTROL_PATTERN.sub(
            "", _ANSI_ESCAPE_PATTERN.sub("", str(value))
        )

    @classmethod
    def _safe_json_value(cls, value: Any) -> Any:
        """Keep structured log values JSON-compatible and terminal-safe."""

        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {
                cls._safe_text(key): cls._safe_json_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._safe_json_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return cls._safe_text(value)

    @staticmethod
    def _colors_enabled() -> bool:
        """Resolve color policy at emission time so tests and embedding callers can override it."""

        if "NO_COLOR" in os.environ:
            return False
        force_color = os.environ.get("FORCE_COLOR")
        if force_color == "0":
            return False
        if force_color:
            return True
        if os.environ.get("TERM") == "dumb":
            return False
        try:
            return bool(sys.stderr.isatty())
        except (AttributeError, OSError):
            return False

    @classmethod
    def _style(cls, value: str, code: str, colors_enabled: bool) -> str:
        if not colors_enabled:
            return value
        return f"\x1b[{code}m{value}\x1b[0m"

    @classmethod
    def _stage_for_console(cls, stage_name: object, colors_enabled: bool) -> str:
        safe_stage = cls._safe_text(stage_name).replace("\n", "\\n").replace("\t", " ")
        if not colors_enabled:
            return safe_stage
        if "." not in safe_stage:
            return cls._style(safe_stage, "36", colors_enabled)
        module, step = safe_stage.split(".", 1)
        return (
            cls._style(module, "36", colors_enabled)
            + cls._style(".", "2", colors_enabled)
            + cls._style(step, "35", colors_enabled)
        )

    @classmethod
    def _semantic_body(
        cls,
        event: str,
        message: str,
        fields: dict[str, Any],
        colors_enabled: bool,
        level: str,
    ) -> str:
        safe_message = cls._safe_text(message).replace("\n", "\\n").replace("\t", " ")
        if not colors_enabled:
            return safe_message

        if event == "llm.call" and {
            "call_number",
            "stage_name",
            "purpose",
        }.issubset(fields):
            call_number = cls._safe_text(fields.get("call_number", ""))
            stage = cls._stage_for_console(fields.get("stage_name", ""), colors_enabled)
            purpose = cls._style(
                cls._safe_text(fields.get("purpose", ""))
                .replace("\n", "\\n")
                .replace("\t", " "),
                "34",
                colors_enabled,
            )
            return (
                "LLM call "
                + cls._style(f"#{call_number}", "1", colors_enabled)
                + cls._style(":", "2", colors_enabled)
                + " "
                + stage
                + cls._style(" - ", "2", colors_enabled)
                + purpose
            )

        if event == "llm.complete" and {
            "call_number",
            "stage_name",
            "runner_profile",
            "runner_type",
        }.issubset(fields):
            call_number = cls._safe_text(fields.get("call_number", ""))
            stage = cls._stage_for_console(fields.get("stage_name", ""), colors_enabled)
            profile = cls._style(
                cls._safe_text(fields.get("runner_profile", ""))
                .replace("\n", "\\n")
                .replace("\t", " "),
                "34",
                colors_enabled,
            )
            runner_type = cls._style(
                cls._safe_text(fields.get("runner_type", ""))
                .replace("\n", "\\n")
                .replace("\t", " "),
                "34",
                colors_enabled,
            )
            failed = bool(fields.get("failed", False)) or level == "error"
            label = "LLM failed" if failed else "LLM complete"
            label_color = "31" if failed else "32"
            body = (
                cls._style(label, label_color, colors_enabled)
                + " "
                + cls._style(f"#{call_number}", "1", colors_enabled)
                + cls._style(":", "2", colors_enabled)
                + " "
                + stage
                + cls._style(" (", "2", colors_enabled)
                + profile
                + cls._style("/", "2", colors_enabled)
                + runner_type
                + cls._style(")", "2", colors_enabled)
            )
            if "; " in safe_message:
                body += cls._style(
                    "; " + safe_message.split("; ", 1)[1], "2", colors_enabled
                )
            return body

        return safe_message

    @classmethod
    def _console_prefix(cls, level: str, event: str, fields: dict[str, Any]) -> str:
        prefix = "[ai-push-hooks]"
        colors_enabled = cls._colors_enabled()
        if not colors_enabled:
            return prefix
        color = {
            "warn": "\x1b[33m",
            "error": "\x1b[31m",
        }.get(level, "\x1b[36m")
        if event == "llm.complete" and {
            "call_number",
            "stage_name",
            "runner_profile",
            "runner_type",
        }.issubset(fields):
            color = (
                "\x1b[31m"
                if level == "error" or fields.get("failed", False)
                else "\x1b[32m"
            )
        return f"{color}{prefix}\x1b[0m"

    @classmethod
    def _console_message(
        cls,
        level: str,
        message: str,
        *,
        event: str = "",
        fields: dict[str, Any] | None = None,
    ) -> str:
        # A log event is deliberately one physical line.  This prevents an
        # untrusted stage/profile/message from creating a fake prompt or log line.
        safe_fields = fields or {}
        colors_enabled = cls._colors_enabled()
        body = cls._semantic_body(event, message, safe_fields, colors_enabled, level)
        prefix = cls._console_prefix(level, event, safe_fields)
        return f"{prefix} {body}\n"

    def _emit(self, level: str, event: str, message: str, **fields: Any) -> None:
        with self._lock:
            if not self._level_is_enabled(level):
                return
            sys.stderr.write(
                self._console_message(level, message, event=event, fields=fields)
            )
            if self.jsonl_path is None or self.jsonl_write_failed:
                return
            stamp = datetime.now(timezone.utc).isoformat()
            safe_fields = self._safe_json_value(fields)
            record = {
                **safe_fields,
                "ts": stamp,
                "level": self._safe_text(level),
                "event": self._safe_text(event),
                "message": self._safe_text(message),
            }
            try:
                try:
                    initial_metadata = self.jsonl_path.lstat()
                except FileNotFoundError:
                    initial_metadata = None
                if initial_metadata is not None:
                    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if stat.S_ISLNK(initial_metadata.st_mode) or bool(
                        getattr(initial_metadata, "st_file_attributes", 0)
                        & reparse_flag
                    ):
                        raise HookError(
                            "JSONL log target must not be a symlink or reparse point: "
                            f"{self.jsonl_path}"
                        )
                flags = (
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                )
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.jsonl_path, flags, 0o600)
                try:
                    descriptor_metadata = os.fstat(descriptor)
                    path_metadata = self.jsonl_path.lstat()
                    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if (
                        not stat.S_ISREG(descriptor_metadata.st_mode)
                        or stat.S_ISLNK(path_metadata.st_mode)
                        or bool(
                            getattr(path_metadata, "st_file_attributes", 0)
                            & reparse_flag
                        )
                        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
                        != (path_metadata.st_dev, path_metadata.st_ino)
                    ):
                        raise HookError(
                            f"JSONL log target is not a regular file: {self.jsonl_path}"
                        )
                    os.fchmod(descriptor, 0o600)
                    os.write(
                        descriptor,
                        (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8"),
                    )
                finally:
                    os.close(descriptor)
            except Exception as exc:  # noqa: BLE001
                self.jsonl_write_failed = True
                sys.stderr.write(
                    self._console_message(
                        "error", f"JSONL logging disabled after write failure: {exc}"
                    )
                )

    def debug(self, event: str, message: str, **fields: Any) -> None:
        self._emit("debug", event, message, **fields)

    def info(self, event: str, message: str, **fields: Any) -> None:
        self._emit("info", event, message, **fields)

    def status(self, event: str, message: str, **fields: Any) -> None:
        self._emit("status", event, message, **fields)

    def warn(self, event: str, message: str, **fields: Any) -> None:
        self._emit("warn", event, message, **fields)

    def error(self, event: str, message: str, **fields: Any) -> None:
        self._emit("error", event, message, **fields)

    def llm_call(
        self,
        stage_name: str,
        purpose: str,
        model: str,
        attempt: int | None = None,
        total_attempts: int | None = None,
        *,
        runner_profile: str | None = None,
        runner_type: str | None = None,
    ) -> int:
        with self._lock:
            call_number = len(self.llm_calls) + 1
            safe_stage = self._safe_text(stage_name)
            safe_purpose = self._safe_text(purpose)
            record: dict[str, Any] = {
                "call_number": call_number,
                "stage_name": safe_stage,
                "purpose": safe_purpose,
                "model": self._safe_text(model),
                "module": safe_stage.split(".", 1)[0],
                "step": safe_stage.split(".", 1)[1]
                if "." in safe_stage
                else safe_stage,
            }
            if attempt is not None:
                record["attempt"] = attempt
            if total_attempts is not None:
                record["total_attempts"] = total_attempts
            if runner_profile is not None:
                record["runner_profile"] = self._safe_text(runner_profile)
            if runner_type is not None:
                record["runner_type"] = self._safe_text(runner_type)
            self.llm_calls.append(record)
            self.status(
                "llm.call",
                f"LLM call #{call_number}: {safe_stage} - {safe_purpose}",
                **record,
            )
            return call_number

    def llm_complete(
        self,
        call_number: int,
        stage_name: str,
        runner_profile: str,
        runner_type: str,
        *,
        session_id: str | None = None,
        session_state: str | None = None,
        resumable: bool = False,
        transcript: str | None = None,
        resume_command: str | None = None,
        failed: bool = False,
    ) -> None:
        """Record truthful completion/session details without inventing resume data."""

        safe_stage = self._safe_text(stage_name)
        safe_profile = self._safe_text(runner_profile)
        safe_type = self._safe_text(runner_type)
        safe_state = (
            self._safe_text(session_state) if session_state is not None else None
        )
        safe_session_id = (
            self._safe_text(session_id) if session_id is not None else None
        )
        safe_transcript = (
            self._safe_text(transcript) if transcript is not None else None
        )
        safe_resume_command = (
            self._safe_text(resume_command) if resume_command is not None else None
        )
        effective_resume_command = (
            safe_resume_command if safe_state == "persisted" and resumable else None
        )
        session_details: list[str] = []
        if safe_state == "persisted":
            if safe_session_id:
                session_details.append(f"session persisted: {safe_session_id}")
            else:
                session_details.append("session persisted")
            if effective_resume_command:
                session_details.append(f"resume: {effective_resume_command}")
            elif not resumable:
                session_details.append("not resumable")
            if safe_transcript:
                session_details.append(f"transcript: {safe_transcript}")
        elif safe_state == "deleted":
            if safe_session_id:
                session_details.append(f"session deleted: {safe_session_id}")
            else:
                session_details.append("session deleted")
            if safe_transcript:
                session_details.append(f"transcript: {safe_transcript}")
        elif safe_state == "ephemeral":
            if safe_session_id:
                session_details.append(f"session: {safe_session_id}")
            session_details.append("not resumable")
        elif safe_state is not None:
            session_details.append(f"session {safe_state}")
        elif safe_session_id:
            session_details.append(f"session: {safe_session_id}")

        message = (
            f"LLM failed #{call_number}: {safe_stage} ({safe_profile}/{safe_type})"
            if failed
            else f"LLM complete #{call_number}: {safe_stage} ({safe_profile}/{safe_type})"
        )
        if session_details:
            message += "; " + "; ".join(session_details)
        fields: dict[str, Any] = {
            "call_number": call_number,
            "stage_name": safe_stage,
            "module": safe_stage.split(".", 1)[0],
            "step": safe_stage.split(".", 1)[1] if "." in safe_stage else safe_stage,
            "runner_profile": safe_profile,
            "runner_type": safe_type,
            "failed": failed,
        }
        if safe_session_id is not None:
            fields["session_id"] = safe_session_id
        if safe_state is not None:
            fields["session_state"] = safe_state
        if safe_transcript is not None:
            fields["transcript"] = safe_transcript
        if effective_resume_command is not None:
            fields["resume_command"] = effective_resume_command
        if (
            any(
                value is not None
                for value in (
                    safe_session_id,
                    safe_state,
                    safe_transcript,
                    effective_resume_command,
                )
            )
            or resumable
        ):
            fields["resumable"] = resumable
        self.status("llm.complete", message, **fields)

    def llm_summary(self) -> None:
        with self._lock:
            stage_counts: dict[str, int] = {}
            for call in self.llm_calls:
                stage_name = str(call.get("stage_name", "")).strip() or "<unknown>"
                stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
            self.status(
                "llm.calls_total",
                f"Total LLM calls this run: {len(self.llm_calls)}",
                total_calls=len(self.llm_calls),
                stage_counts=stage_counts,
            )
