"""Runner-neutral request, result, lifecycle, and diagnostic contracts.

This package is intentionally independent from workflow configuration.  The
configuration layer resolves a profile into the flat :class:`RunnerRequest`;
runner adapters only consume that request and return a :class:`RunnerResult`.
"""

from __future__ import annotations

import json
import math
import pathlib
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol, Sequence


RunnerMode = Literal["ask", "apply"]
ProjectAccess = Literal["artifacts", "project"]
PromptTransport = Literal["stdin", "argv"]
SessionState = Literal["persisted", "ephemeral", "deleted", "unknown"]

MAX_DIAGNOSTIC_CHARS = 4_000
DIAGNOSTIC_TRUNCATION_MARKER = "[truncated]"
# Request bodies can be much larger than a useful error excerpt.  Do not let
# redaction turn each word in a diff into another full-stream scan.
MAX_DIAGNOSTIC_REQUEST_CHARS = MAX_DIAGNOSTIC_CHARS * 2


class RunnerContractError(ValueError):
    """The caller supplied a request that does not satisfy the runner contract."""


class RunnerError(RuntimeError):
    """Base class for fail-closed runner and process errors.

    Error details are deliberately bounded and redacted at construction time.
    Request prompts, artifact bodies, and environments are not accepted by this
    class and therefore cannot accidentally appear in its message.
    """

    def __init__(self, message: str, *, details: str = "") -> None:
        safe_message = bounded_diagnostic(message, max_chars=MAX_DIAGNOSTIC_CHARS)
        safe_details = bounded_diagnostic(details, max_chars=MAX_DIAGNOSTIC_CHARS)
        if safe_details:
            safe_message = f"{safe_message}: {safe_details}"
        super().__init__(safe_message)


class RunnerExecutableNotFoundError(RunnerError):
    """The selected executable could not be spawned."""


class RunnerTimeoutError(RunnerError):
    """The selected process exceeded its timeout and was terminated."""


class RunnerSignalError(RunnerError):
    """The selected process terminated because of a signal."""


class RunnerNonzeroExitError(RunnerError):
    """A process returned a non-zero status when success was required."""


class RunnerProtocolError(RunnerError):
    """A runner emitted malformed or incomplete protocol output."""


class RunnerMissingOutputError(RunnerProtocolError):
    """An analysis request completed without a final response."""


class RunnerAdapterUnavailableError(RunnerError):
    """A known adapter has not been installed/implemented in this build."""


def _validate_text(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    allow_line_breaks: bool = False,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise RunnerContractError(f"{label} must be a non-empty string")
    if allow_controls:
        return value
    allowed_controls = "\r\n\t" if allow_line_breaks else ""
    if "\x00" in value or any(
        ord(character) < 32 and character not in allowed_controls for character in value
    ):
        raise RunnerContractError(f"{label} contains control characters")
    return value


def _validate_argv(values: Sequence[str], label: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise RunnerContractError(f"{label} must be a non-empty argv sequence")
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            raise RunnerContractError(f"{label}[{index}] must be a non-empty string")
        if "\x00" in value:
            raise RunnerContractError(f"{label}[{index}] contains a NUL character")
        result.append(value)
    return tuple(result)


@dataclass(frozen=True, repr=False)
class RunnerArtifact:
    """One ordered logical input artifact.

    ``path`` is an already validated hook-owned path for adapters with native
    attachment support.  It is intentionally not used to render prompt
    packets, so adapters without attachments receive the same name and body.
    """

    name: str
    content: str = field(repr=False)
    path: pathlib.Path | None = None

    def __post_init__(self) -> None:
        _validate_text(self.name, "artifact name")
        _validate_text(
            self.content,
            "artifact content",
            allow_empty=True,
            allow_line_breaks=True,
            allow_controls=True,
        )
        if self.path is not None and not isinstance(self.path, pathlib.Path):
            object.__setattr__(self, "path", pathlib.Path(self.path))
        if self.path is not None and "\x00" in str(self.path):
            raise RunnerContractError("artifact path contains a NUL character")

    def __repr__(self) -> str:
        path = f", path={self.path!r}" if self.path is not None else ""
        return f"RunnerArtifact(name={self.name!r}, content=<redacted>{path})"


@dataclass(frozen=True, repr=False)
class PromptPacket:
    """Deterministic prompt representation for adapters without attachments."""

    instruction: str = field(repr=False)
    artifacts: tuple[RunnerArtifact, ...] = ()

    def __post_init__(self) -> None:
        _validate_text(
            self.instruction,
            "instruction",
            allow_empty=True,
            allow_line_breaks=True,
            allow_controls=True,
        )
        if not isinstance(self.artifacts, (tuple, list)):
            raise RunnerContractError("prompt packet artifacts must be ordered")
        if not all(isinstance(item, RunnerArtifact) for item in self.artifacts):
            raise RunnerContractError(
                "prompt packet artifacts must contain RunnerArtifact values"
            )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))

    def render(self) -> str:
        """Render names and bodies in order without exposing attachment paths."""

        sections = [self.instruction]
        for artifact in self.artifacts:
            sections.append(
                f"\n\n--- ai-push-hooks artifact: {artifact.name} ---\n"
                f"{artifact.content}\n"
                f"--- end ai-push-hooks artifact: {artifact.name} ---"
            )
        return "".join(sections)

    @property
    def text(self) -> str:
        return self.render()


@dataclass(frozen=True, repr=False)
class RunnerRequest:
    """The complete, resolved input to one runner invocation."""

    profile_id: str
    runner_type: str
    stage: str
    purpose: str
    mode: RunnerMode
    instruction: str = field(repr=False)
    artifacts: tuple[RunnerArtifact, ...] = ()
    cwd: pathlib.Path = pathlib.Path(".")
    timeout_seconds: float = 0
    model: str | None = None
    variant: str | None = None
    project_access: ProjectAccess = "artifacts"
    allow_paths: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    prompt_transport: PromptTransport = "stdin"
    # These fields exist for the current OpenCode session/lifecycle integration
    # only.  Other adapters may ignore them and must remain session-optional.
    session_id: str | None = field(default=None, repr=False)
    resume_session: bool = False
    integration_context: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_text(self.profile_id, "profile_id")
        _validate_text(self.runner_type, "runner_type")
        _validate_text(self.stage, "stage")
        _validate_text(self.purpose, "purpose")
        if self.mode not in {"ask", "apply"}:
            raise RunnerContractError("mode must be 'ask' or 'apply'")
        _validate_text(
            self.instruction, "instruction", allow_empty=True, allow_line_breaks=True
        )
        if not isinstance(self.artifacts, (tuple, list)):
            raise RunnerContractError("artifacts must be an ordered sequence")
        normalized_artifacts: list[RunnerArtifact] = []
        for artifact in self.artifacts:
            if not isinstance(artifact, RunnerArtifact):
                raise RunnerContractError(
                    "artifacts must contain RunnerArtifact values"
                )
            normalized_artifacts.append(artifact)
        object.__setattr__(self, "artifacts", tuple(normalized_artifacts))
        if not isinstance(self.cwd, pathlib.Path):
            object.__setattr__(self, "cwd", pathlib.Path(self.cwd))
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
        ):
            raise RunnerContractError("timeout_seconds must be finite")
        if self.timeout_seconds <= 0:
            raise RunnerContractError("timeout_seconds must be greater than zero")
        for value, label in ((self.model, "model"), (self.variant, "variant")):
            if value is not None:
                _validate_text(value, label, allow_empty=True)
        if self.project_access not in {"artifacts", "project"}:
            raise RunnerContractError("project_access must be 'artifacts' or 'project'")
        if not isinstance(self.allow_paths, (tuple, list)):
            raise RunnerContractError("allow_paths must be an ordered sequence")
        object.__setattr__(self, "allow_paths", tuple(self.allow_paths))
        for index, path in enumerate(self.allow_paths):
            _validate_text(path, f"allow_paths[{index}]")
        if not isinstance(self.command, (tuple, list)):
            raise RunnerContractError("command must be an argv sequence")
        if self.command:
            object.__setattr__(self, "command", _validate_argv(self.command, "command"))
        else:
            object.__setattr__(self, "command", ())
        if self.prompt_transport not in {"stdin", "argv"}:
            raise RunnerContractError("prompt_transport must be 'stdin' or 'argv'")
        if self.session_id is not None:
            _validate_text(self.session_id, "session_id")

    def prompt_packet(self) -> PromptPacket:
        """Build the ordered logical packet shared by non-attachment adapters."""

        return PromptPacket(self.instruction, self.artifacts)

    def __repr__(self) -> str:
        return (
            "RunnerRequest("
            f"profile_id={self.profile_id!r}, runner_type={self.runner_type!r}, "
            f"stage={self.stage!r}, purpose={self.purpose!r}, mode={self.mode!r}, "
            f"artifact_count={len(self.artifacts)}, cwd={str(self.cwd)!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, model={self.model!r}, "
            f"variant={self.variant!r}, project_access={self.project_access!r}, "
            f"allow_path_count={len(self.allow_paths)}, command_arg_count={len(self.command)})"
        )


@dataclass(frozen=True, repr=False)
class SessionMetadata:
    """Truthful lifecycle state reported by an adapter."""

    session_id: str | None = None
    state: SessionState = "ephemeral"
    resumable: bool = False
    transcript: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.session_id is not None:
            _validate_text(self.session_id, "session_id")
        if self.state not in {"persisted", "ephemeral", "deleted", "unknown"}:
            raise RunnerContractError("session state is invalid")
        if self.resumable and self.state != "persisted":
            raise RunnerContractError("only persisted sessions can be resumable")
        if self.transcript is not None:
            _validate_text(
                self.transcript,
                "transcript",
                allow_empty=True,
                allow_line_breaks=True,
                allow_controls=True,
            )

    def __repr__(self) -> str:
        return (
            "SessionMetadata("
            f"session_id={self.session_id!r}, state={self.state!r}, "
            f"resumable={self.resumable}, transcript=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class RunnerResult:
    """Normalized output from an adapter."""

    final_text: str
    returncode: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    session: SessionMetadata | None = None
    transcript: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_text(
            self.final_text, "final_text", allow_empty=True, allow_controls=True
        )
        if isinstance(self.returncode, bool) or not isinstance(self.returncode, int):
            raise RunnerContractError("returncode must be an integer")
        _validate_text(self.stdout, "stdout", allow_empty=True, allow_controls=True)
        _validate_text(self.stderr, "stderr", allow_empty=True, allow_controls=True)
        if self.session is not None and not isinstance(self.session, SessionMetadata):
            raise RunnerContractError("session must be SessionMetadata or None")
        if self.transcript is not None:
            _validate_text(
                self.transcript, "transcript", allow_empty=True, allow_line_breaks=True
            )

    def __repr__(self) -> str:
        return (
            "RunnerResult("
            f"returncode={self.returncode!r}, session={self.session!r}, "
            "final_text=<redacted>, stdout=<redacted>, stderr=<redacted>)"
        )


@dataclass(frozen=True)
class RunnerCapabilities:
    """Optional adapter features; invocation never depends on them."""

    supports_resume: bool = False
    supports_finalize: bool = False
    supports_transcript: bool = False


class Runner(Protocol):
    """Minimal adapter API shared by all runner types."""

    capabilities: RunnerCapabilities

    def run(self, request: RunnerRequest) -> RunnerResult: ...


class RunnerLifecycle(Protocol):
    """Optional lifecycle API, implemented only by adapters that need it."""

    def finalize(
        self, request: RunnerRequest, result: RunnerResult
    ) -> RunnerResult: ...


def finalize_runner(
    runner: Runner, request: RunnerRequest, result: RunnerResult
) -> RunnerResult:
    """Finalize a result when the adapter explicitly advertises that capability."""

    capabilities = getattr(runner, "capabilities", RunnerCapabilities())
    finalizer = getattr(runner, "finalize", None)
    if not capabilities.supports_finalize or not callable(finalizer):
        return result
    finalized = finalizer(request, result)
    if not isinstance(finalized, RunnerResult):
        raise RunnerProtocolError("runner finalizer returned an invalid result")
    return finalized


_ANSI_ESCAPE_PATTERN = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])"
)
_UNSAFE_TERMINAL_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_DIAGNOSTIC_FRAGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{3,}")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)([\"']?(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|authorization|credential|password|secret|token)[a-z0-9_-]*[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
    ),
    re.compile(
        r"(?i)(?<![a-z0-9])[a-z0-9][a-z0-9_.:/+\-]*(?:secret|api[_-]?key|access[_-]?token|auth[_-]?token|credential|password)[a-z0-9_.:/+\-]*(?![a-z0-9])"
    ),
)


def strip_terminal_controls(value: str) -> str:
    """Remove ANSI/terminal controls while retaining ordinary text and newlines."""

    without_ansi = _ANSI_ESCAPE_PATTERN.sub("", value)
    return _UNSAFE_TERMINAL_CONTROL_PATTERN.sub("", without_ansi)


def _diagnostic_redaction_values(secrets: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    fragment_count = 0
    for secret in secrets:
        if not isinstance(secret, str) or not secret or secret in seen:
            continue
        seen.add(secret)
        values.append(secret)
        # A bounded fragment set catches a child echoing one prompt token,
        # while skipping a whole large diff avoids the old O(words * stream)
        # behavior.  The request-sensitive helper additionally suppresses
        # excerpts for genuinely large request bodies.
        if len(secret) <= MAX_DIAGNOSTIC_REQUEST_CHARS:
            for fragment in _DIAGNOSTIC_FRAGMENT_PATTERN.findall(secret):
                if fragment not in seen:
                    seen.add(fragment)
                    values.append(fragment)
                    fragment_count += 1
                    if fragment_count >= 512:
                        break
            if fragment_count >= 512:
                continue
    return tuple(values)


def _longest_prefix_suffix(value: str, prefix: str) -> int:
    """Return the longest suffix of ``value`` that is a prefix of ``prefix``."""

    limit = min(len(value), len(prefix))
    if limit == 0:
        return 0
    pattern = prefix[:limit]
    failure = [0] * limit
    matched = 0
    for index in range(1, limit):
        while matched and pattern[matched] != pattern[index]:
            matched = failure[matched - 1]
        if pattern[matched] == pattern[index]:
            matched += 1
        failure[index] = matched

    matched = 0
    for character in value[-limit:]:
        while matched and (matched == limit or pattern[matched] != character):
            matched = failure[matched - 1]
        if pattern[matched] == character:
            matched += 1
    return matched


def _boundary_redaction_start(value: str, secrets: Sequence[str]) -> int | None:
    """Find the earliest visible prefix of a known value at a cutoff."""

    earliest: int | None = None
    for secret in secrets:
        if len(secret) <= 1:
            continue
        # The captured child stream may itself end while a known value is
        # being emitted, so do not require the value's unseen suffix to exist
        # in ``value``.  This comparison is bounded by the preview and secret
        # prefix, rather than scanning an unbounded stream tail.
        prefix = secret[: min(len(value), len(secret) - 1)]
        overlap = _longest_prefix_suffix(value, prefix)
        if overlap:
            start = len(value) - overlap
            if earliest is None or start < earliest:
                earliest = start
    return earliest


def redact_diagnostic(value: str, *, secrets: Sequence[str] = ()) -> str:
    """Redact supplied secrets and common credential-shaped values."""

    redacted = strip_terminal_controls(value)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]"
            ),
            redacted,
        )
    redaction_values = _diagnostic_redaction_values(secrets)
    for secret in sorted(redaction_values, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def bounded_diagnostic(
    value: str,
    *,
    max_chars: int = MAX_DIAGNOSTIC_CHARS,
    secrets: Sequence[str] = (),
) -> str:
    """Return a bounded, redacted diagnostic excerpt; never an environment dump."""

    if max_chars <= 0:
        return ""
    raw = str(value)
    # Redact only the bounded preview.  Anything after this point cannot be
    # present in the returned diagnostic, and therefore does not need a scan.
    # A known value can start in the preview and finish after it, though; mask
    # that visible prefix after ordinary redaction so a complete match cannot
    # be destroyed first.  Boundary matching is bounded even for large
    # streams/secrets.
    preview = strip_terminal_controls(raw[:max_chars])
    redaction_values = _diagnostic_redaction_values(secrets)
    safe = redact_diagnostic(preview, secrets=secrets)
    boundary_start = _boundary_redaction_start(safe, redaction_values)
    if boundary_start is not None:
        # Several known values can overlap at the cutoff.  Replace their
        # union once so insertion cannot invalidate another span's index.
        safe = safe[:boundary_start] + "[REDACTED]"
    if len(raw) <= max_chars and len(safe) <= max_chars:
        return safe
    if max_chars <= len(DIAGNOSTIC_TRUNCATION_MARKER):
        return DIAGNOSTIC_TRUNCATION_MARKER[:max_chars]
    return (
        safe[: max_chars - len(DIAGNOSTIC_TRUNCATION_MARKER)]
        + DIAGNOSTIC_TRUNCATION_MARKER
    )


def bounded_redacted_diagnostics(
    stdout: str = "",
    stderr: str = "",
    *,
    max_chars: int = MAX_DIAGNOSTIC_CHARS,
    secrets: Sequence[str] = (),
) -> str:
    """Format bounded child streams without including prompt or environment data."""

    parts: list[str] = []
    for label, value in (("stdout", stdout), ("stderr", stderr)):
        if value:
            parts.append(
                f"{label}: {bounded_diagnostic(value, max_chars=max_chars, secrets=secrets)}"
            )
    combined = "\n".join(parts)
    return bounded_diagnostic(combined, max_chars=max_chars, secrets=secrets)


def _credential_environment_values(env: Mapping[str, str] | None) -> tuple[str, ...]:
    if env is None:
        return ()
    credential_markers = (
        "API_KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "AUTH",
        "CREDENTIAL",
    )
    return tuple(
        value
        for name, value in env.items()
        if isinstance(name, str)
        and isinstance(value, str)
        and value
        and any(marker in name.upper() for marker in credential_markers)
    )


def _request_sensitive_values(
    request: RunnerRequest,
    *,
    env: Mapping[str, str] | None = None,
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return a small, deduplicated set of values safe to use for redaction.

    In particular, this must not manufacture one secret per whitespace word
    in a request.  A large diff would otherwise make every diagnostic scan the
    full child stream once per word.  JSON-escaped forms cover a child that
    embeds the packet in a JSON error object without retaining the packet as a
    second unbounded secret.
    """

    values = (
        request.instruction,
        *(artifact.content for artifact in request.artifacts),
        request.model or "",
        request.variant or "",
        *_credential_environment_values(env),
        *extra,
    )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        # The encoded form is bounded independently below.  It is useful for
        # escaped newlines/quotes in JSON diagnostics, but not worth retaining
        # for a request-sized value.
        if len(value) <= MAX_DIAGNOSTIC_REQUEST_CHARS:
            encoded = json.dumps(value, ensure_ascii=True)
            if encoded not in seen:
                seen.add(encoded)
                result.append(encoded)

    return tuple(result)


def request_sensitive_diagnostics(
    request: RunnerRequest,
    stdout: str = "",
    stderr: str = "",
    *,
    max_chars: int = MAX_DIAGNOSTIC_CHARS,
    env: Mapping[str, str] | None = None,
    extra: Sequence[str] = (),
) -> str:
    """Build a bounded diagnostic while keeping request/environment data out.

    If a request contains a body larger than the diagnostic budget, suppress
    child excerpts entirely.  It is not possible to prove that a short model
    excerpt is unrelated to an arbitrary large prompt without an unbounded
    substring search; a fixed explanation is safer and cheaper.
    """

    request_values = (
        request.instruction,
        *(artifact.content for artifact in request.artifacts),
    )
    if any(len(value) > MAX_DIAGNOSTIC_REQUEST_CHARS for value in request_values):
        return "diagnostic output suppressed for a large request"
    return bounded_redacted_diagnostics(
        stdout,
        stderr,
        max_chars=max_chars,
        secrets=_request_sensitive_values(request, env=env, extra=extra),
    )


def require_final_text(final_text: str, *, mode: RunnerMode) -> str:
    """Enforce the runner-neutral output rule for analysis versus apply."""

    if mode == "ask" and not final_text.strip():
        raise RunnerMissingOutputError("runner produced no final response")
    return final_text


def build_prompt_packet(request: RunnerRequest) -> PromptPacket:
    """Return a packet preserving the exact instruction and artifact order."""

    return request.prompt_packet()


def require_zero_exit(
    request: RunnerRequest,
    result: RunnerResult,
    *,
    diagnostic_limit: int = MAX_DIAGNOSTIC_CHARS,
    secrets: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> RunnerResult:
    """Raise a bounded non-zero diagnostic while preserving normalized results."""

    if result.returncode != 0:
        details = (
            request_sensitive_diagnostics(
                request,
                result.stdout,
                result.stderr,
                max_chars=diagnostic_limit,
                env=env,
                extra=secrets,
            )
            or f"exit code {result.returncode}"
        )
        raise RunnerNonzeroExitError(
            f"runner {request.profile_id!r} ({request.runner_type}) failed at {request.stage!r}",
            details=details,
        )
    return result
