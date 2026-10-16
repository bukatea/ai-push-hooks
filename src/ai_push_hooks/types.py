from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

READ_ONLY_STEP_TYPES = frozenset({"collect", "llm"})
PROMPTABLE_STEP_TYPES = frozenset({"llm", "apply"})
SUPPORTED_STEP_TYPES = frozenset({"collect", "llm", "apply", "exec", "assert"})
FEATURE_BRANCH_PREFIXES = ("feat/", "feature/")


class HookError(RuntimeError):
    pass


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
    model: str = "openai/gpt-5.5"
    variant: str = ""
    timeout_seconds: int = 800
    max_parallel: int = 2
    json_max_retries: int = 2
    invalid_json_feedback_max_chars: int = 6000
    json_retry_new_session: bool = True
    delete_session_after_run: bool = True
    max_diff_bytes: int = 180000
    session_title_prefix: str = "ai-push-hooks"
    opencode_cli_generation: str = "legacy"

    def __post_init__(self) -> None:
        if not isinstance(self.opencode_cli_generation, str) or self.opencode_cli_generation not in (
            "legacy",
            "v2",
        ):
            raise HookError("llm.opencode_cli_generation must be 'legacy' or 'v2'")
        if self.opencode_cli_generation == "v2" and self.variant:
            raise HookError("llm.variant must be empty for opencode_cli_generation 'v2'")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "status"
    jsonl: bool = True
    dir: str = ".git/ai-push-hooks/logs"
    capture_llm_transcript: bool = True
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
    when_env: str | None = None

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


@dataclass
class CollectorResult:
    artifacts: dict[str, str | dict[str, Any] | list[Any]] = field(default_factory=dict)
    skip_module: bool = False
    skip_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    status: str = "completed"
    artifacts: dict[str, pathlib.Path] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str = ""


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
    logger: "HookLogger"
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

    _verbosity_order = {"status": 0, "info": 1, "debug": 2}

    def _level_is_enabled(self, level: str) -> bool:
        if level in {"warn", "error"}:
            return True
        configured = self._verbosity_order.get(self.console_level, 0)
        required = self._verbosity_order.get(level, 0)
        return configured >= required

    def _emit(self, level: str, event: str, message: str, **fields: Any) -> None:
        if not self._level_is_enabled(level):
            return
        stamp = datetime.now(timezone.utc).isoformat()
        sys.stderr.write(f"[ai-push-hooks] {message}\n")
        if self.jsonl_path is None or self.jsonl_write_failed:
            return
        record = {"ts": stamp, "level": level, "event": event, "message": message, **fields}
        try:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            self.jsonl_write_failed = True
            sys.stderr.write(f"[ai-push-hooks] JSONL logging disabled after write failure: {exc}\n")

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
    ) -> None:
        call_number = len(self.llm_calls) + 1
        record: dict[str, Any] = {
            "call_number": call_number,
            "stage_name": stage_name,
            "purpose": purpose,
            "model": model,
        }
        if attempt is not None:
            record["attempt"] = attempt
        if total_attempts is not None:
            record["total_attempts"] = total_attempts
        self.llm_calls.append(record)
        self.status(
            "llm.call",
            f"LLM call #{call_number}: {stage_name} - {purpose}",
            **record,
        )

    def llm_summary(self) -> None:
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
