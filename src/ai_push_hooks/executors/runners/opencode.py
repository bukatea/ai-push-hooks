"""OpenCode adapter for the internal runner boundary.

The adapter deliberately retains the shipped ``opencode run`` shape.  This
module owns the adapter-level protocol and lifecycle semantics; workflow
retries and JSON/schema handling remain in orchestration.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any

from .opencode_support import (
    OPENCODE_APPLY_AGENT,
    OPENCODE_READ_ONLY_AGENT,
    _transcript_dir,
    build_opencode_security_config,
    non_agent_opencode_config,
    opencode_isolation_env,
    resolve_opencode_executable,
    sanitize_filename_component,
    validate_hook_owned_artifacts,
)
from ...paths import (
    atomic_write_bytes,
    relative_path_parts,
    ensure_private_directory,
    resolve_contained_path,
    write_text_no_follow,
)
from ...types import RequiredRetentionError
from .process import ProcessResult
from .contracts import (
    RunnerCapabilities,
    RunnerContractError,
    RunnerError,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerRequest,
    RunnerResult,
    SessionMetadata,
    bounded_diagnostic,
    require_final_text,
    require_zero_exit,
)
from .process import run_process


@dataclass(frozen=True, repr=False)
class _RetentionState:
    """Evidence owned by one call, never by the registry-cached adapter."""

    request: RunnerRequest
    directory: pathlib.Path
    receipt: dict[str, Any]


@dataclass(frozen=True, repr=False)
class _RetainedResult(RunnerResult):
    # The result bridges run() and finalize(), even on different worker threads.
    retention: _RetentionState | None = field(default=None, repr=False)


class OpenCodeRunner:
    """Run one isolated OpenCode invocation and optionally finalize its session."""

    capabilities = RunnerCapabilities(
        supports_resume=True,
        supports_finalize=True,
        supports_transcript=True,
    )

    def _required(self, request: RunnerRequest) -> bool:
        return bool(
            getattr(
                self._context(request).config.logging,
                "require_opencode_retention",
                False,
            )
        )

    def _receipt(self, state: _RetentionState) -> None:
        atomic_write_bytes(
            state.directory / "receipt.json",
            json.dumps(state.receipt, sort_keys=True).encode("utf-8"),
        )

    def _capture(
        self,
        state: _RetentionState,
        completed: ProcessResult | None,
        action: str,
        error: BaseException | None = None,
    ) -> None:
        metadata = {
            "available": completed is not None,
            "exception": type(error).__name__ if error else None,
        }
        state.receipt[action] = metadata
        if completed is not None:
            metadata.update(
                returncode=completed.returncode,
                stdout_bytes=len(completed.stdout_bytes),
                stderr_bytes=len(completed.stderr_bytes),
                stdout_truncated=completed.stdout_truncated,
                stderr_truncated=completed.stderr_truncated,
            )
            atomic_write_bytes(
                state.directory / f"{action}.stdout", completed.stdout_bytes
            )
            atomic_write_bytes(
                state.directory / f"{action}.stderr", completed.stderr_bytes
            )
        self._receipt(state)

    @staticmethod
    def _complete_process(completed: ProcessResult) -> str:
        if (
            completed.returncode != 0
            or completed.stdout_truncated
            or completed.stderr_truncated
        ):
            raise RequiredRetentionError(
                "Required OpenCode retention: incomplete process output"
            )
        try:
            completed.stderr_bytes.decode("utf-8")
            raw = completed.stdout_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise RequiredRetentionError(
                "Required OpenCode retention: invalid encoding"
            ) from exc
        if (
            raw != completed.stdout
            or completed.stderr_bytes.decode("utf-8") != completed.stderr
        ):
            raise RequiredRetentionError(
                "Required OpenCode retention: original bytes unavailable"
            )
        return raw

    @staticmethod
    def _has_error(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("type") in {"error", "fatal", "session.error"} or any(
                (key == "error" and item is not None) or OpenCodeRunner._has_error(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(OpenCodeRunner._has_error(item) for item in value)
        return False

    def _parse_required(
        self, raw: str, request: RunnerRequest, state: _RetentionState
    ) -> tuple[str, str]:
        ids: set[str] = set()
        message_ids: set[str] = set()
        texts: list[str] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict) or self._has_error(event):
                raise RequiredRetentionError(
                    "Required OpenCode retention: invalid/error event"
                )
            sid = event.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                raise RequiredRetentionError(
                    "Required OpenCode retention: missing root identity"
                )
            ids.add(sid)
            if event.get("type") in {"text", "step_start", "step_finish"}:
                part = event.get("part")
                if (
                    not isinstance(part, dict)
                    or part.get("sessionID") != sid
                    or part.get("type") != event["type"].replace("_", "-")
                ):
                    raise RequiredRetentionError(
                        "Required OpenCode retention: invalid part identity/type"
                    )
                mid = part.get("messageID")
                if not isinstance(mid, str) or not mid.strip():
                    raise RequiredRetentionError(
                        "Required OpenCode retention: missing message identity"
                    )
                message_ids.add(mid)
                if event["type"] == "text":
                    if not isinstance(part.get("text"), str):
                        raise RequiredRetentionError(
                            "Required OpenCode retention: invalid text"
                        )
                    texts.append(part["text"])
        if len(ids) != 1 or (request.resume_session and ids != {request.session_id}):
            raise RequiredRetentionError(
                "Required OpenCode retention: ambiguous session identity"
            )
        text = "\n".join(texts).strip()
        if not text:
            raise RequiredRetentionError(
                "Required OpenCode retention: missing final text"
            )
        sid = next(iter(ids))
        state.receipt.update(
            session_id=sid,
            message_ids=sorted(message_ids),
            run_validated=True,
            outcome="retained",
        )
        self._receipt(state)
        return sid, text

    def _finalize_required(
        self, request: RunnerRequest, result: RunnerResult
    ) -> RunnerResult:
        context = self._context(request)
        if (
            not isinstance(result, _RetainedResult)
            or result.retention is None
            or result.retention.request is not request
            or not result.retention.receipt.get("run_validated")
            or result.returncode != 0
        ):
            raise RequiredRetentionError(
                "Required OpenCode retention: failed run retained; deletion forbidden"
            )
        state = result.retention
        sid = state.receipt["session_id"]
        if result.session is None or result.session.session_id != sid:
            raise RequiredRetentionError(
                "Required OpenCode retention: result identity mismatch; deletion forbidden"
            )
        executable = state.receipt["executable"]
        try:
            try:
                exported = self._lifecycle_process(
                    request, context, [executable, "export", sid, "--pure"], "export"
                )
            except Exception as exc:
                self._capture(
                    state, getattr(exc, "_process_result", None), "export", exc
                )
                raise
            self._capture(state, exported, "export")
            payload = json.loads(self._complete_process(exported))
            if (
                not isinstance(payload, dict)
                or self._has_error(payload)
                or not isinstance(payload.get("info"), dict)
                or payload["info"].get("id") != sid
            ):
                raise RequiredRetentionError(
                    "Required OpenCode retention: invalid export identity"
                )
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise RequiredRetentionError(
                    "Required OpenCode retention: empty export messages"
                )
            assistant_ids = set()
            for message in messages:
                if (
                    not isinstance(message, dict)
                    or not isinstance(message.get("type"), str)
                    or not isinstance(message.get("id"), str)
                ):
                    raise RequiredRetentionError(
                        "Required OpenCode retention: invalid export message"
                    )
                if message["type"] == "assistant":
                    content = message.get("content")
                    if (
                        not isinstance(content, list)
                        or not content
                        or not message["id"].strip()
                    ):
                        raise RequiredRetentionError(
                            "Required OpenCode retention: invalid assistant content"
                        )
                    for part in content:
                        if not isinstance(part, dict) or part.get("type") not in {
                            "text",
                            "reasoning",
                            "tool",
                        }:
                            raise RequiredRetentionError(
                                "Required OpenCode retention: invalid content type"
                            )
                        if part["type"] in {"text", "reasoning"} and not isinstance(
                            part.get("text"), str
                        ):
                            raise RequiredRetentionError(
                                "Required OpenCode retention: invalid content text"
                            )
                        if part["type"] == "tool" and (
                            not isinstance(part.get("id"), str)
                            or not isinstance(part.get("name"), str)
                            or not isinstance(part.get("state"), dict)
                        ):
                            raise RequiredRetentionError(
                                "Required OpenCode retention: invalid tool content"
                            )
                    assistant_ids.add(message["id"])
            if not set(state.receipt["message_ids"]).issubset(assistant_ids):
                raise RequiredRetentionError(
                    "Required OpenCode retention: missing export correlation"
                )
            state.receipt["export_validated"] = True
            deleting = context.config.llm.delete_session_after_run
            state.receipt.update(
                delete_action="intent" if deleting else "not requested",
                outcome="retained",
            )
            self._receipt(state)
            if deleting:
                state.receipt.update(delete_action="attempted", outcome="unknown")
                self._receipt(state)
                try:
                    deleted = self._lifecycle_process(
                        request,
                        context,
                        [executable, "session", "delete", sid, "--pure"],
                        "delete",
                    )
                except Exception as exc:
                    self._capture(
                        state, getattr(exc, "_process_result", None), "delete", exc
                    )
                    raise
                self._capture(state, deleted, "delete")
                self._complete_process(deleted)
                state.receipt["outcome"] = "deleted"
            self._receipt(state)
            return RunnerResult(
                final_text=result.final_text,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                session=SessionMetadata(
                    session_id=sid,
                    state="deleted" if deleting else "persisted",
                    resumable=not deleting,
                    transcript=str(state.directory / "export.stdout"),
                ),
            )
        except Exception as exc:
            state.receipt["lifecycle_error"] = type(exc).__name__
            if state.receipt.get("outcome") == "deleted":
                state.receipt["outcome"] = "unknown"
            try:
                self._receipt(state)
            except Exception as receipt_error:
                error = RequiredRetentionError(
                    "Required OpenCode retention: lifecycle and receipt failed; outcome unknown"
                )
                error.primary_error = exc
                error.secondary_error = receipt_error
                raise error from exc
            error = RequiredRetentionError(
                "Required OpenCode retention: lifecycle failed; receipt/outcome may be unavailable"
            )
            error.secondary_error = exc
            raise error from exc

    def _context(self, request: RunnerRequest) -> Any:
        context = request.integration_context
        if context is None:
            raise RunnerContractError(
                "OpenCode runner requires an integration context for executable and isolation policy"
            )
        return context

    @staticmethod
    def _parse_output(raw: str, request: RunnerRequest) -> tuple[str | None, str]:
        session_id: str | None = None
        text_parts: list[str] = []

        def protocol_failure(
            message: str, *, missing: bool = False
        ) -> RunnerProtocolError:
            error: RunnerProtocolError
            if missing:
                error = RunnerMissingOutputError(message)
            else:
                error = RunnerProtocolError(message)
            # A session may have been announced before a later malformed or
            # error event.  Keep that identity on every protocol failure so
            # the orchestrator can still finalize it.
            known_session = session_id
            if known_session is None and request.resume_session:
                known_session = request.session_id
            setattr(error, "session_id", known_session)
            return error

        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise protocol_failure(
                    "OpenCode emitted malformed JSONL output"
                ) from exc
            if not isinstance(event, dict):
                raise protocol_failure("OpenCode emitted a non-object JSONL event")

            event_type = event.get("type")

            for key in ("sessionID", "session_id"):
                value = event.get(key)
                if session_id is None and isinstance(value, str) and value.strip():
                    session_id = value.strip()

            if event_type in {"error", "fatal", "session.error"} or (
                isinstance(event.get("error"), (str, dict, list))
                and event_type not in {"text", "step_finish"}
            ):
                # Do not serialize the provider event: OpenCode may echo the
                # instruction, artifact bodies, or credential-shaped data.
                raise protocol_failure("OpenCode reported an error event")

            if event_type != "text":
                # New OpenCode event kinds are additive unless they are an
                # explicit error.  Only text parts form the final response.
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                raise protocol_failure("OpenCode text event has no text part")
            text = part.get("text")
            if not isinstance(text, str):
                raise protocol_failure("OpenCode text event has an invalid text value")
            text_parts.append(text)

        final_text = "\n".join(text_parts).strip()
        if not final_text:
            raise protocol_failure("OpenCode produced no final response", missing=True)
        return session_id, final_text

    @staticmethod
    def _session_id_from_partial_output(raw: str) -> str | None:
        """Recover only a complete session identifier from a bounded prefix."""

        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            for key in ("sessionID", "session_id"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _attachment_name(index: int, logical_name: str) -> str:
        # The order prefix prevents collisions after sanitizing path-like
        # logical names while retaining a useful basename for CLI diagnostics.
        basename = pathlib.PurePath(logical_name.replace("\\", "/")).name
        safe_basename = sanitize_filename_component(basename)
        if safe_basename in {"", ".", ".."}:
            safe_basename = "artifact"
        return f"{index:04d}-{safe_basename}"

    def _materialize_attachments(
        self,
        context: Any,
        request: RunnerRequest,
    ) -> tuple[list[pathlib.Path], pathlib.Path | None]:
        original_paths = [artifact.path for artifact in request.artifacts]
        # Validate source ownership and symlink traversal before making any
        # copy.  The source is then never reopened: its logical snapshot is
        # the only content sent through OpenCode's native --file transport.
        validate_hook_owned_artifacts(
            context,
            [path for path in original_paths if path is not None],
        )
        if not request.artifacts:
            return [], None

        run_root = ensure_private_directory(context.run_dir.resolve(strict=True))
        attachment_dir = pathlib.Path(
            tempfile.mkdtemp(prefix="opencode-attachments-", dir=str(run_root))
        )
        try:
            ensure_private_directory(attachment_dir, private_root=run_root)
            paths: list[pathlib.Path] = []
            for index, artifact in enumerate(request.artifacts, start=1):
                target = resolve_contained_path(
                    attachment_dir,
                    self._attachment_name(index, artifact.name),
                    "OpenCode materialized attachment path",
                )
                write_text_no_follow(target, artifact.content)
                paths.append(target)
            return paths, attachment_dir
        except Exception:
            shutil.rmtree(attachment_dir, ignore_errors=True)
            raise

    @staticmethod
    def _cleanup_attachments(
        attachment_dir: pathlib.Path | None,
        *,
        session_id: str | None = None,
    ) -> None:
        if attachment_dir is None:
            return
        # Capture the active exception before entering the cleanup try block;
        # sys.exc_info() inside ``except OSError`` refers to the cleanup error,
        # not the invocation error being unwound.
        unwinding_error = sys.exc_info()[1]
        try:
            shutil.rmtree(attachment_dir)
        except FileNotFoundError:
            return
        except OSError as exc:
            if unwinding_error is not None:
                if session_id and not getattr(unwinding_error, "session_id", None):
                    setattr(unwinding_error, "session_id", session_id)
                return
            error = RunnerError("OpenCode attachment cleanup failed")
            if session_id:
                setattr(error, "session_id", session_id)
            raise error from exc

    def _argv(
        self,
        request: RunnerRequest,
        context: Any,
        executable: str,
        attachments: list[pathlib.Path],
        *,
        prompt: str,
    ) -> list[str]:
        agent = (
            OPENCODE_APPLY_AGENT
            if request.mode == "apply"
            else OPENCODE_READ_ONLY_AGENT
        )
        config = getattr(getattr(context, "config", None), "llm", None)
        title_prefix = getattr(config, "session_title_prefix", "ai-push-hooks")
        argv = [
            executable,
            "run",
            "--agent",
            agent,
            "--pure",
            "--format",
            "json",
        ]
        if request.model:
            argv.extend(["--model", request.model])
        if request.variant:
            argv.extend(["--variant", request.variant])
        if request.resume_session and request.session_id:
            argv.extend(["--session", request.session_id])
        else:
            argv.extend(
                [
                    "--title",
                    f"{title_prefix} {context.run_id} {request.stage}",
                ]
            )
        for path in attachments:
            argv.extend(["--file", str(path)])
        argv.extend(["--", prompt])
        return argv

    @staticmethod
    def _require_staging_directory(
        context: Any, working_directory: pathlib.Path
    ) -> None:
        """Reject repository worktrees as apply targets.

        The apply workflow supplies a disposable, non-VCS staging projection.
        A runner request can identify the repository root and the selected cwd,
        but it cannot turn an arbitrary host directory into a sandbox.  Refuse
        the known unsafe cases at this boundary instead of granting edit
        permissions to the checked-out repository.
        """

        repository_root = context.repo_root.resolve(strict=True)
        if working_directory == repository_root or working_directory.is_relative_to(
            repository_root
        ):
            raise RunnerContractError(
                "OpenCode apply requests require an isolated staging directory"
            )
        if any(
            (parent / ".git").exists()
            for parent in (working_directory, *working_directory.parents)
        ):
            raise RunnerContractError(
                "OpenCode apply requests require a non-VCS isolated staging directory"
            )

    def run(self, request: RunnerRequest) -> RunnerResult:
        if request.runner_type != "opencode":
            raise RunnerContractError(
                f"OpenCode runner cannot handle runner type {request.runner_type!r}"
            )
        context = self._context(request)
        required = self._required(request)
        state: _RetentionState | None = None
        if required:
            if (
                not context.config.logging.capture_llm_transcript
                or context.config.general.allow_push_on_error
            ):
                raise RequiredRetentionError(
                    "Required OpenCode retention: incompatible policy"
                )
            try:
                parts = relative_path_parts(
                    context.config.logging.transcript_dir,
                    "Required transcript directory",
                )
                root = (
                    context.git_dir.joinpath(*parts[1:])
                    if parts[0] == ".git"
                    else context.repo_root.joinpath(*parts)
                )
                ensure_private_directory(root, durable=True)
                directory = (
                    root
                    / f"{sanitize_filename_component(context.run_id)}-{sanitize_filename_component(request.stage)}-{uuid.uuid4().hex}"
                )
                ensure_private_directory(
                    directory, private_root=root, durable=True, exclusive=True
                )
                state = _RetentionState(
                    request,
                    directory,
                    {
                        "run_id": context.run_id,
                        "stage": request.stage,
                        "resume_session": request.session_id
                        if request.resume_session
                        else None,
                        "run_validated": False,
                        "delete_action": "not attempted",
                        "outcome": "unknown",
                    },
                )
                self._receipt(state)
            except Exception as exc:
                raise RequiredRetentionError(
                    "Required OpenCode retention: storage unavailable"
                ) from exc
        if request.mode == "apply" and not request.allow_paths:
            raise RunnerContractError("OpenCode apply requests require allow_paths")
        if request.mode == "apply" and request.project_access == "artifacts":
            # Apply always runs in a host-created staging projection, even for
            # the compatibility profile.
            working_directory = request.cwd
        elif request.project_access == "project":
            working_directory = request.cwd
        else:
            working_directory = None

        if working_directory is not None:
            try:
                working_directory = working_directory.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise RunnerContractError(
                    "OpenCode request cwd must be an existing directory"
                ) from exc
            if not working_directory.is_dir():
                raise RunnerContractError(
                    "OpenCode request cwd must be an existing directory"
                )
            if request.mode == "apply":
                self._require_staging_directory(context, working_directory)

        agent = "apply" if request.mode == "apply" else "read-only"
        _agent_name, security_config = build_opencode_security_config(
            agent,
            request.allow_paths,
            non_vcs_working_directory=(
                working_directory if request.mode == "apply" else None
            ),
            project_read_root=(
                working_directory if request.project_access == "project" else None
            ),
        )
        executable = (
            getattr(context, "opencode_executable", None)
            or resolve_opencode_executable()
        )
        isolated_env = opencode_isolation_env(context, security_config, request.stage)
        if state is not None:
            state.receipt["executable"] = executable
        attachment_dir: pathlib.Path | None = None
        session_id: str | None = None
        try:
            attachments, attachment_dir = self._materialize_attachments(
                context, request
            )
            # Every logical artifact is materialized, including pathless
            # contract artifacts.  Therefore native attachments carry the
            # complete snapshot and the instruction is never duplicated in a
            # prompt packet.
            argv = self._argv(
                request,
                context,
                executable,
                attachments,
                prompt=request.instruction,
            )

            def invoke(cwd: pathlib.Path) -> Any:
                try:
                    completed = run_process(
                        argv,
                        cwd=cwd,
                        timeout_seconds=request.timeout_seconds,
                        env=isolated_env,
                    )
                except Exception as exc:
                    if required:
                        try:
                            self._capture(
                                state, getattr(exc, "_process_result", None), "run", exc
                            )
                        except Exception as secondary:
                            error = RequiredRetentionError(
                                "Required OpenCode retention: process and capture failed"
                            )
                            error.secondary_error = secondary
                            raise error from exc
                        raise RequiredRetentionError(
                            "Required OpenCode retention: process failed"
                        ) from exc
                    raise
                if required:
                    self._capture(state, completed, "run")
                    self._complete_process(completed)
                if getattr(completed, "stdout_truncated", False) or getattr(
                    completed, "stderr_truncated", False
                ):
                    error = RunnerProtocolError("OpenCode process output was truncated")
                    setattr(
                        error,
                        "session_id",
                        self._session_id_from_partial_output(completed.stdout)
                        or (request.session_id if request.resume_session else None),
                    )
                    raise error
                return completed

            try:
                if working_directory is None:
                    with tempfile.TemporaryDirectory(
                        prefix="ai-push-hooks-readonly-"
                    ) as directory:
                        completed = invoke(pathlib.Path(directory).resolve(strict=True))
                else:
                    completed = invoke(working_directory)
            except (RunnerError,) as error:
                process_result = getattr(error, "_process_result", None)
                partial_stdout = (
                    process_result.stdout
                    if isinstance(process_result, ProcessResult)
                    else ""
                )
                setattr(
                    error,
                    "session_id",
                    self._session_id_from_partial_output(partial_stdout)
                    or getattr(error, "session_id", None)
                    or (request.session_id if request.resume_session else None),
                )
                raise

            result = RunnerResult(
                final_text="",
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            # Check the process status before parsing.  A failed child may emit
            # a partial or malformed stream; the process failure is the
            # truthful, normalized terminal diagnosis.
            try:
                require_zero_exit(request, result, env=isolated_env)
            except RunnerError as error:
                setattr(
                    error,
                    "session_id",
                    self._session_id_from_partial_output(completed.stdout)
                    or (request.session_id if request.resume_session else None),
                )
                raise
            session_id, final_text = (
                self._parse_required(completed.stdout, request, state)
                if required
                else self._parse_output(completed.stdout, request)
            )
            if request.resume_session and session_id is None:
                session_id = request.session_id
            result = RunnerResult(
                final_text=require_final_text(final_text, mode=request.mode),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                session=(
                    SessionMetadata(
                        session_id=session_id, state="persisted", resumable=True
                    )
                    if session_id
                    else SessionMetadata()
                ),
            )
            if state is not None:
                return _RetainedResult(
                    final_text=result.final_text,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    session=result.session,
                    retention=state,
                )
            return result
        except Exception as exc:
            if state is not None:
                state.receipt["run_error"] = type(exc).__name__
                try:
                    self._receipt(state)
                except Exception as secondary:
                    error = RequiredRetentionError(
                        "Required OpenCode retention: run and receipt failed; outcome unknown"
                    )
                    error.secondary_error = secondary
                    raise error from exc
            if required and not isinstance(exc, RequiredRetentionError):
                raise RequiredRetentionError(
                    "Required OpenCode retention: run/capture failed; session retained"
                ) from exc
            raise
        finally:
            active_error = sys.exc_info()[1]
            cleanup_session_id = session_id or getattr(active_error, "session_id", None)
            self._cleanup_attachments(
                attachment_dir,
                session_id=(
                    cleanup_session_id if isinstance(cleanup_session_id, str) else None
                ),
            )

    def _lifecycle_process(
        self,
        request: RunnerRequest,
        context: Any,
        argv: list[str],
        action: str,
    ) -> Any:
        # Export/delete must use the original stage isolation root.  Using a
        # different action-specific root makes retained sessions invisible to
        # OpenCode because its HOME/XDG state is isolated per stage.
        env = opencode_isolation_env(
            context,
            non_agent_opencode_config(),
            request.stage,
        )
        with tempfile.TemporaryDirectory(
            prefix=f"ai-push-hooks-session-{action}-"
        ) as directory:
            return run_process(
                argv,
                cwd=pathlib.Path(directory).resolve(strict=True),
                timeout_seconds=request.timeout_seconds,
                env=env,
            )

    def finalize(self, request: RunnerRequest, result: RunnerResult) -> RunnerResult:
        if self._required(request):
            return self._finalize_required(request, result)
        session = result.session
        if session is None or not session.session_id:
            return result
        context = self._context(request)
        session_id = session.session_id
        transcript_path: pathlib.Path | None = None

        if getattr(context.config.logging, "capture_llm_transcript", False):
            transcript_dir = _transcript_dir(context)
            if transcript_dir is not None:
                transcript_path = resolve_contained_path(
                    transcript_dir,
                    (
                        f"{sanitize_filename_component(context.run_id)}-"
                        f"{sanitize_filename_component(request.stage)}-"
                        f"{sanitize_filename_component(session_id)}.json"
                    ),
                    "OpenCode transcript path",
                )
                try:
                    executable = (
                        getattr(context, "opencode_executable", None)
                        or resolve_opencode_executable()
                    )
                    exported = self._lifecycle_process(
                        request,
                        context,
                        [executable, "export", session_id, "--pure"],
                        "export",
                    )
                    if (
                        exported.returncode == 0
                        and not getattr(exported, "stdout_truncated", False)
                        and not getattr(exported, "stderr_truncated", False)
                        and exported.stdout.strip()
                    ):
                        write_text_no_follow(
                            transcript_path, exported.stdout.strip() + "\n"
                        )
                    else:
                        transcript_path = None
                        self._warn_export(
                            context,
                            request,
                            session_id,
                            "export returned no transcript",
                        )
                except Exception as exc:  # noqa: BLE001
                    transcript_path = None
                    self._warn_export(context, request, session_id, type(exc).__name__)

        deleted = False
        if getattr(context.config.llm, "delete_session_after_run", False):
            try:
                executable = (
                    getattr(context, "opencode_executable", None)
                    or resolve_opencode_executable()
                )
                deleted_result = self._lifecycle_process(
                    request,
                    context,
                    [executable, "session", "delete", session_id, "--pure"],
                    "delete",
                )
                deleted = deleted_result.returncode == 0
                if not deleted:
                    self._warn_export(
                        context, request, session_id, "session deletion failed"
                    )
            except Exception as exc:  # noqa: BLE001
                self._warn_export(
                    context, request, session_id, f"delete {type(exc).__name__}"
                )

        state = "deleted" if deleted else "persisted"
        finalized_session = SessionMetadata(
            session_id=session_id,
            state=state,
            resumable=not deleted,
            transcript=str(transcript_path) if transcript_path is not None else None,
        )
        return RunnerResult(
            final_text=result.final_text,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            session=finalized_session,
            transcript=result.transcript,
        )

    @staticmethod
    def _warn_export(
        context: Any, request: RunnerRequest, session_id: str, reason: str
    ) -> None:
        logger = getattr(context, "logger", None)
        if logger is not None:
            logger.warn(
                "llm.transcript_export_failed",
                "Could not capture or delete the OpenCode session cleanly.",
                stage_name=request.stage,
                session_id=session_id,
                reason=bounded_diagnostic(reason, max_chars=120),
            )


def create_runner() -> OpenCodeRunner:
    """Registry factory for the built-in OpenCode adapter."""

    return OpenCodeRunner()
