from __future__ import annotations

import json
import pathlib
import re
import shutil
from dataclasses import dataclass
from typing import Any

from ..types import HookError, RuntimeContext, StepConfig
from .exec import ensure_dir, extract_pr_url, resolve_storage_path, run_command


@dataclass
class OpenCodeRunResult:
    output_text: str
    session_id: str | None
    stdout: str
    stderr: str
    return_code: int


def sanitize_filename_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "value"


def resolve_opencode_executable() -> str:
    opencode_path = shutil.which("opencode")
    if opencode_path:
        return opencode_path
    cli_path = shutil.which("opencode-cli")
    if cli_path:
        return cli_path
    raise HookError("opencode is required but not installed")


def parse_opencode_json_run_output(raw: str) -> tuple[str | None, str]:
    session_id: str | None = None
    parts: list[str] = []
    for line in raw.splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if session_id is None and isinstance(event.get("sessionID"), str):
            session_id = str(event["sessionID"]).strip()
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
            parts.append(part["text"])
    return session_id, "\n".join(parts).strip()


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
        raise HookError(f"Failed to parse JSON object from model output: {exc}") from exc
    if not isinstance(payload, dict):
        raise HookError("Model output JSON is not an object")
    return payload


def validate_schema(schema: str | None, payload: Any) -> Any:
    if schema is None:
        return payload
    if schema == "string_array":
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise HookError("Expected schema string_array")
        return payload
    if schema == "docs_issue_array":
        if not isinstance(payload, list):
            raise HookError("Expected schema docs_issue_array")
        for item in payload:
            if not isinstance(item, dict):
                raise HookError("docs_issue_array items must be objects")
            if not str(item.get("file", "")).strip() or not str(item.get("description", "")).strip():
                raise HookError("docs_issue_array items require file and description")
        return payload
    if schema == "beads_alignment_result":
        if not isinstance(payload, dict):
            raise HookError("Expected schema beads_alignment_result")
        commands = payload.get("commands", [])
        if commands is not None and (
            not isinstance(commands, list) or not all(isinstance(item, str) for item in commands)
        ):
            raise HookError("beads_alignment_result.commands must be an array of strings")
        return payload
    if schema == "pr_create_payload":
        if not isinstance(payload, dict):
            raise HookError("Expected schema pr_create_payload")
        return payload
    raise HookError(f"Unsupported schema: {schema}")


def _transcript_dir(context: RuntimeContext) -> pathlib.Path | None:
    if not context.config.logging.capture_llm_transcript:
        return None
    return ensure_dir(
        resolve_storage_path(
            context.repo_root,
            context.git_dir,
            context.config.logging.transcript_dir,
        )
    )


def _opencode_session_command(context: RuntimeContext, action: str, session_id: str) -> list[str]:
    executable = context.opencode_executable or resolve_opencode_executable()
    if context.config.llm.opencode_cli_generation == "v2":
        return [executable, "session", action, "--standalone", session_id]
    if action == "export":
        return [executable, "export", session_id]
    return [executable, "session", action, session_id]


def export_opencode_session_json(
    context: RuntimeContext,
    session_id: str,
    export_path: pathlib.Path,
) -> bool:
    completed = run_command(
        _opencode_session_command(context, "export", session_id),
        cwd=context.repo_root,
        timeout=context.config.llm.timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        return False
    payload = (completed.stdout or "").strip()
    if not payload:
        return False
    export_path.write_text(payload + "\n", encoding="utf-8")
    return True


def delete_opencode_session(context: RuntimeContext, session_id: str) -> None:
    run_command(
        _opencode_session_command(context, "delete", session_id),
        cwd=context.repo_root,
        timeout=context.config.llm.timeout_seconds,
        check=False,
    )


def finalize_opencode_session(context: RuntimeContext, stage_name: str, session_id: str | None) -> None:
    if not session_id:
        return
    transcript_dir = _transcript_dir(context)
    if transcript_dir is not None:
        export_name = (
            f"{sanitize_filename_component(context.run_id)}-"
            f"{sanitize_filename_component(stage_name)}-"
            f"{sanitize_filename_component(session_id)}.json"
        )
        export_opencode_session_json(context, session_id, transcript_dir / export_name)
    if context.config.llm.delete_session_after_run:
        delete_opencode_session(context, session_id)


def call_opencode(
    context: RuntimeContext,
    stage_name: str,
    purpose: str,
    prompt: str,
    files: list[pathlib.Path],
    attempt: int | None = None,
    total_attempts: int | None = None,
    existing_session_id: str | None = None,
) -> OpenCodeRunResult:
    executable = context.opencode_executable or resolve_opencode_executable()
    context.logger.llm_call(stage_name, purpose, context.config.llm.model, attempt, total_attempts)
    cmd = [
        executable,
        "run",
        "--format",
        "json",
        "--model",
        context.config.llm.model,
    ]
    if context.config.llm.opencode_cli_generation == "v2":
        cmd.append("--standalone")
    if context.config.llm.variant:
        cmd.extend(["--variant", context.config.llm.variant])
    if existing_session_id:
        cmd.extend(["--session", existing_session_id])
    else:
        cmd.extend(["--title", f"{context.config.llm.session_title_prefix} {context.run_id} {stage_name}"])
    for file_path in files:
        cmd.extend(["--file", str(file_path)])
    cmd.extend(["--", prompt])

    completed = run_command(
        cmd,
        cwd=context.repo_root,
        timeout=context.config.llm.timeout_seconds,
        check=False,
        env={"OPENCODE_SERVER_PASSWORD": None},
    )
    session_id, text_output = parse_opencode_json_run_output(completed.stdout or "")
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if context.config.logging.print_llm_output and stdout.strip():
        print(stdout)
    return OpenCodeRunResult(
        output_text=text_output if text_output else stdout.strip(),
        session_id=session_id or existing_session_id,
        stdout=stdout,
        stderr=stderr,
        return_code=completed.returncode,
    )


def run_llm_step(
    context: RuntimeContext,
    step: StepConfig,
    prompt: str,
    input_paths: list[pathlib.Path],
    stage_name: str,
) -> Any:
    total_attempts = context.config.llm.json_max_retries + 1
    session_id: str | None = None
    prompt_text = prompt
    last_error = ""
    last_output = ""
    wants_json = bool(step.schema)
    expects_json_array = step.schema in {"string_array", "docs_issue_array"}
    for attempt in range(1, total_attempts + 1):
        result = call_opencode(
            context,
            stage_name=stage_name,
            purpose=f"{step.type}:{step.id}",
            prompt=prompt_text,
            files=input_paths,
            attempt=attempt,
            total_attempts=total_attempts,
            existing_session_id=session_id,
        )
        session_id = result.session_id
        if result.return_code != 0:
            finalize_opencode_session(context, stage_name, session_id)
            details = result.stderr.strip() or result.stdout.strip() or f"exit code {result.return_code}"
            raise HookError(f"OpenCode command failed: {details}")
        try:
            if not wants_json:
                finalize_opencode_session(context, stage_name, session_id)
                return result.output_text
            if expects_json_array:
                payload = extract_json_array(result.output_text)
            else:
                payload = extract_json_object(result.output_text)
            finalize_opencode_session(context, stage_name, session_id)
            return validate_schema(step.schema, payload)
        except HookError as exc:
            last_error = str(exc)
            last_output = result.output_text
            if attempt >= total_attempts:
                break
            snippet = last_output[: context.config.llm.invalid_json_feedback_max_chars]
            if expects_json_array:
                suffix = "Return ONLY valid JSON array."
            else:
                suffix = "Return ONLY valid JSON object."
            prompt_text = (
                prompt
                + "\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
                + f"Parse error: {last_error}\n"
                + suffix
                + "\nPrevious invalid output:\n```text\n"
                + snippet
                + "\n```"
            )
            if context.config.llm.json_retry_new_session:
                session_id = None
            pr_url = extract_pr_url(last_output)
            if pr_url:
                context.logger.info(
                    "llm.invalid_json_pr_url_hint",
                    "Detected PR URL in invalid JSON output",
                    stage_name=stage_name,
                    url=pr_url,
                )
    finalize_opencode_session(context, stage_name, session_id)
    raise HookError(f"Model failed to return valid JSON for {stage_name}: {last_error}. {last_output[:400]}")
