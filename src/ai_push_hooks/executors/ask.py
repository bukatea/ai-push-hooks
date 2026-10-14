from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any

from ..types import HookError, RuntimeContext, StepConfig


def extract_json_array(text: str) -> list[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise HookError("Could not find JSON array in model output")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HookError(f"Failed to parse JSON array from model output: {exc}") from exc
    if not isinstance(payload, list):
        raise HookError("Model output JSON is not an array")
    return payload


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise HookError("Could not find JSON object in model output")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HookError(
            f"Failed to parse JSON object from model output: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise HookError("Model output JSON is not an object")
    return payload


def validate_schema(schema: str | None, payload: Any) -> Any:
    if schema is None:
        return payload
    if schema == "string_array":
        if not isinstance(payload, list) or not all(
            isinstance(item, str) for item in payload
        ):
            raise HookError("Expected schema string_array")
        return payload
    if schema == "docs_issue_array":
        if not isinstance(payload, list):
            raise HookError("Expected schema docs_issue_array")
        for item in payload:
            if not isinstance(item, dict):
                raise HookError("docs_issue_array items must be objects")
            if (
                not str(item.get("file", "")).strip()
                or not str(item.get("description", "")).strip()
            ):
                raise HookError("docs_issue_array items require file and description")
        return payload
    if schema == "beads_alignment_result":
        if not isinstance(payload, dict):
            raise HookError("Expected schema beads_alignment_result")
        unresolved = payload.get("unresolved")
        if type(unresolved) is not bool:
            raise HookError("beads_alignment_result.unresolved must be a boolean")
        if "commands" in payload and (
            not isinstance(payload["commands"], list)
            or not all(isinstance(item, str) for item in payload["commands"])
        ):
            raise HookError(
                "beads_alignment_result.commands must be an array of strings"
            )
        if "report_markdown" in payload and not isinstance(
            payload["report_markdown"], str
        ):
            raise HookError("beads_alignment_result.report_markdown must be a string")
        return payload
    if schema == "pr_create_payload":
        if not isinstance(payload, dict):
            raise HookError("Expected schema pr_create_payload")
        for field in ("title", "body"):
            if field in payload and not isinstance(payload[field], str):
                raise HookError(f"pr_create_payload.{field} must be a string")
        if "draft" in payload and not isinstance(payload["draft"], bool):
            raise HookError("pr_create_payload.draft must be a boolean")
        return payload
    raise HookError(f"Unsupported schema: {schema}")


def _safe_invalid_output(invocation: Any, output: str) -> str:
    from .runners.contracts import request_sensitive_diagnostics

    request = invocation.request
    return request_sensitive_diagnostics(request, output, max_chars=400, env=os.environ)


def run_ask_step(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> Any:
    from .runner_workflow import _finalize_invocation, _invoke_runner, _named_error
    from ..config import resolve_runner_profile

    total_attempts = context.config.llm.json_max_retries + 1
    prompt_text = prompt
    last_error = ""
    last_output = ""
    wants_json = bool(step.schema)
    expects_json_array = step.schema in {"string_array", "docs_issue_array"}
    session_id: str | None = None
    resume_session = False
    selected_profile = step.runner or context.config.llm.runner
    try:
        profile = resolve_runner_profile(context.config, step)
    except Exception as exc:  # noqa: BLE001
        raise _named_error(selected_profile, "unknown", stage_name, exc) from exc
    retained_invocation = None
    if (
        profile.type == "opencode"
        and context.config.logging.require_opencode_retention
        and wants_json
        and context.config.llm.json_max_retries > 0
        and not context.config.llm.json_retry_new_session
    ):
        from ..types import RequiredRetentionError

        raise RequiredRetentionError(
            "Required OpenCode retention: JSON repair requires json_retry_new_session=true"
        )

    # Artifact-only analysis must retain its historical empty scratch cwd;
    # project-aware analysis gets the actual checkout root so the adapter can
    # provide the selected runner's project-read behavior.
    if profile.project_access == "project":
        working_directory = context.repo_root.resolve(strict=True)
        temporary_directory = None
    else:
        temporary_directory = tempfile.TemporaryDirectory(prefix="ai-push-hooks-ask-")
        working_directory = pathlib.Path(temporary_directory.name).resolve(strict=True)

    try:
        for attempt in range(1, total_attempts + 1):
            invocation = _invoke_runner(
                context,
                step,
                prompt_text,
                input_paths,
                stage_name,
                working_directory=working_directory,
                session_id=session_id,
                resume_session=resume_session,
                attempt=attempt,
                total_attempts=total_attempts,
                prior_invocation=retained_invocation,
            )
            retained_invocation = None
            result = invocation.result
            try:
                if not wants_json:
                    payload = result.final_text
                else:
                    if expects_json_array:
                        payload = extract_json_array(result.final_text)
                    else:
                        payload = extract_json_object(result.final_text)
                    payload = validate_schema(step.schema, payload)
            except HookError as exc:
                last_error = str(exc)
                last_output = result.final_text
                if attempt >= total_attempts:
                    _finalize_invocation(context, invocation, failed=True)
                    safe_error = _safe_invalid_output(invocation, last_error)
                    raise HookError(
                        f"Runner profile `{profile.name}` ({profile.type}) failed at stage "
                        f"`{stage_name}`: invalid JSON: {safe_error}. "
                        f"{_safe_invalid_output(invocation, last_output)}"
                    ) from exc

                snippet = last_output[
                    : context.config.llm.invalid_json_feedback_max_chars
                ]
                suffix = (
                    "Return ONLY valid JSON array."
                    if expects_json_array
                    else "Return ONLY valid JSON object."
                )
                prompt_text = (
                    prompt
                    + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                    + f"Parse error: {last_error}\n"
                    + suffix
                    + "\nPrevious invalid output:\n```text\n"
                    + snippet
                    + "\n```"
                )

                session = result.session
                can_resume = bool(
                    getattr(
                        getattr(invocation.runner, "capabilities", None),
                        "supports_resume",
                        False,
                    )
                    and session is not None
                    and session.session_id
                    and session.resumable
                )
                if context.config.llm.json_retry_new_session or not can_resume:
                    if not session or not session.session_id:
                        retry_reason = "session absent"
                    elif context.config.llm.json_retry_new_session:
                        retry_reason = "fresh session configured"
                    else:
                        retry_reason = "runner does not support resume"
                    retry_message = "Retrying with a fresh runner invocation."
                    if retry_reason == "runner does not support resume":
                        retry_message = "Retrying with a fresh runner invocation; unsupported session reuse."
                    elif retry_reason == "session absent":
                        retry_message = "Retrying with a fresh runner invocation; no reusable session was captured."
                    context.logger.status(
                        "llm.retry_fresh_session",
                        retry_message,
                        stage_name=stage_name,
                        runner_profile=profile.name,
                        runner_type=profile.type,
                        reason=retry_reason,
                    )
                    _finalize_invocation(context, invocation, failed=True)
                    session_id = None
                    resume_session = False
                else:
                    # Keep the exact captured session ID.  Do not invent a
                    # provider-specific resume command for completion output.
                    session_id = session.session_id
                    resume_session = True
                    retained_invocation = invocation
                    # The invocation itself completed, although its response
                    # failed downstream validation; report truthful metadata
                    # before reusing the retained session.
                    from .runner_workflow import _completion

                    _completion(context, invocation, failed=True)
                continue

            _finalize_invocation(context, invocation, failed=False)
            return payload
        raise HookError(
            f"Runner profile `{profile.name}` ({profile.type}) failed at stage `{stage_name}`: "
            "model did not return a valid result"
        )  # pragma: no cover
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
