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
    ensure_private_directory,
    resolve_contained_path,
    write_text_no_follow,
)
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


class OpenCodeRunner:
    """Run one isolated OpenCode invocation and optionally finalize its session."""

    capabilities = RunnerCapabilities(
        supports_resume=True,
        supports_finalize=True,
        supports_transcript=True,
    )

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
            "--standalone"
            if self._cli_generation(request, context) == "v2"
            else "--pure",
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

    @staticmethod
    def _cli_generation(request: RunnerRequest, context: Any) -> str:
        """Validate before any executable, attachment or best-effort lifecycle work.

        This selector changes CLI syntax only, not the inherited host policy or
        qualification of the selected OpenCode version's isolation guarantees.
        """
        llm = getattr(getattr(context, "config", None), "llm", None)
        generation = getattr(llm, "opencode_cli_generation", "legacy")
        if not isinstance(generation, str) or generation not in ("legacy", "v2"):
            raise RunnerContractError("OpenCode CLI generation must be legacy or v2")
        if generation == "v2":
            if request.variant:
                raise RunnerContractError(
                    "OpenCode v2 does not support a separate variant option"
                )
            if len(request.artifacts) > 100:
                raise RunnerContractError(
                    "OpenCode v2 supports at most 100 attachments"
                )
        return generation

    def run(self, request: RunnerRequest) -> RunnerResult:
        if request.runner_type != "opencode":
            raise RunnerContractError(
                f"OpenCode runner cannot handle runner type {request.runner_type!r}"
            )
        context = self._context(request)
        self._cli_generation(request, context)
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
                completed = run_process(
                    argv,
                    cwd=cwd,
                    timeout_seconds=request.timeout_seconds,
                    env=isolated_env,
                )
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
            session_id, final_text = self._parse_output(completed.stdout, request)
            if request.resume_session and session_id is None:
                session_id = request.session_id
            return RunnerResult(
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
        executable: str,
        session_id: str,
        action: str,
    ) -> Any:
        if self._cli_generation(request, context) == "v2":
            argv = [executable, "session", action, "--standalone", session_id]
        elif action == "export":
            argv = [executable, "export", session_id, "--pure"]
        else:
            argv = [executable, "session", "delete", session_id, "--pure"]
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
        self._cli_generation(request, request.integration_context)
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
                        executable,
                        session_id,
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
                    executable,
                    session_id,
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
