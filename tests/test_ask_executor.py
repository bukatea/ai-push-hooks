from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.ask import run_ask_step, validate_schema
from ai_push_hooks.executors.runners import (
    RunnerAdapterUnavailableError,
    RunnerCapabilities,
    RunnerResult,
    RunnerTimeoutError,
    SessionMetadata,
)
from ai_push_hooks.types import HookError

from .conftest import build_context, init_repo


@pytest.mark.parametrize("reuse", [True, False])
def test_strict_ask_repair_never_bypasses_export(tmp_path, monkeypatch, reuse):
    import json
    from ai_push_hooks.executors.runners import ProcessResult
    from ai_push_hooks.types import RequiredRetentionError, StepConfig

    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, require_opencode_retention=True),
        llm=replace(config.llm, json_retry_new_session=not reuse),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/fixture/opencode"
    calls = []

    def process(argv, **kwargs):
        calls.append(argv[1])
        event = {
            "type": "text",
            "sessionID": "s1",
            "part": {
                "type": "text",
                "sessionID": "s1",
                "messageID": "msg_1",
                "text": "invalid JSON",
            },
        }
        raw = json.dumps(event).encode() if argv[1] == "run" else b"{}"
        return ProcessResult(0, raw.decode(), "", stdout_bytes=raw)

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    with pytest.raises(RequiredRetentionError):
        run_ask_step(
            context,
            StepConfig(id="strict", type="ask", schema="string_array"),
            "instruction",
            [],
            "docs.strict",
        )
    assert calls == ([] if reuse else ["run", "export"])


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", True), ("body", 123), ("draft", "false"), ("draft", 1)],
)
def test_pr_create_payload_rejects_type_coercion_inputs(
    field: str, value: object
) -> None:
    payload = {field: value}

    with pytest.raises(HookError, match=rf"pr_create_payload\.{field}"):
        validate_schema("pr_create_payload", payload)


def test_pr_create_payload_allows_optional_fields_to_be_omitted() -> None:
    assert validate_schema("pr_create_payload", {"extra": "preserved"}) == {
        "extra": "preserved"
    }


@pytest.mark.parametrize(
    "unresolved",
    [None, "false", [], {}, 1],
)
def test_beads_alignment_result_requires_explicit_boolean_unresolved(
    unresolved: object,
) -> None:
    with pytest.raises(HookError, match=r"beads_alignment_result\.unresolved"):
        validate_schema(
            "beads_alignment_result",
            {"commands": [], "unresolved": unresolved},
        )


def test_beads_alignment_result_requires_unresolved() -> None:
    with pytest.raises(HookError, match=r"beads_alignment_result\.unresolved"):
        validate_schema("beads_alignment_result", {"commands": []})


@pytest.mark.parametrize(
    ("field", "value"),
    [("commands", None), ("commands", "bd close issue-1"), ("report_markdown", None)],
)
def test_beads_alignment_result_rejects_malformed_optional_fields(
    field: str, value: object
) -> None:
    with pytest.raises(HookError, match=rf"beads_alignment_result\.{field}"):
        validate_schema(
            "beads_alignment_result",
            {"unresolved": False, field: value},
        )


def _use_runner_boundary_logger(context, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def llm_call(stage, purpose, model, attempt=None, total_attempts=None, **fields):
        number = len(calls) + 1
        calls.append(
            {
                "call_number": number,
                "stage": stage,
                "purpose": purpose,
                "model": model,
                "attempt": attempt,
                "total_attempts": total_attempts,
                **fields,
            }
        )
        return number

    completions: list[dict[str, object]] = []

    def llm_complete(call_number, stage, profile, runner_type, **fields):
        completions.append(
            {
                "call_number": call_number,
                "stage": stage,
                "profile": profile,
                "runner_type": runner_type,
                **fields,
            }
        )

    monkeypatch.setattr(context.logger, "llm_call", llm_call)
    monkeypatch.setattr(context.logger, "llm_complete", llm_complete, raising=False)
    monkeypatch.setattr(context.logger, "completions", completions, raising=False)


def test_run_ask_step_accepts_array_for_docs_issue_schema(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    analyze_step = next(
        step for step in config.modules["docs"].steps if step.id == "analyze"
    )
    analyze_step = replace(analyze_step, inputs=())

    class FakeRunner:
        capabilities = RunnerCapabilities()

        def run(self, request):
            assert request.mode == "ask"
            return RunnerResult("[]", 0, "", "")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    assert run_ask_step(context, analyze_step, "prompt", [], "docs.analyze") == []


def test_run_ask_step_selects_the_configured_runner_profile(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(
        step for step in config.modules["docs"].steps if step.id == "query"
    )
    query_step = replace(query_step, inputs=())
    requests = []

    class FakeRunner:
        capabilities = RunnerCapabilities()

        def run(self, request):
            requests.append(request)
            return RunnerResult("[]", 0, "", "")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert requests[0].mode == "ask"


def test_json_retry_new_session_finalizes_each_attempt(tmp_path, monkeypatch) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    query_step = next(
        step for step in config.modules["docs"].steps if step.id == "query"
    )
    query_step = replace(query_step, inputs=())
    results = iter(
        [
            RunnerResult(
                "not json", 0, "", "", SessionMetadata("session-1", "persisted", True)
            ),
            RunnerResult(
                "[]", 0, "", "", SessionMetadata("session-2", "persisted", True)
            ),
        ]
    )
    requests = []
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, request):
            requests.append(request)
            return next(results)

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert [request.session_id for request in requests] == [None, None]
    assert finalized == ["session-1", "session-2"]


def test_json_retry_reused_session_finalizes_only_after_last_attempt(
    tmp_path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, json_retry_new_session=False))
    context = build_context(repo, config)
    query_step = next(
        step for step in config.modules["docs"].steps if step.id == "query"
    )
    query_step = replace(query_step, inputs=())
    results = iter(
        [
            RunnerResult(
                "not json", 0, "", "", SessionMetadata("session-1", "persisted", True)
            ),
            RunnerResult(
                "[]", 0, "", "", SessionMetadata("session-1", "persisted", True)
            ),
        ]
    )
    requests = []
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, request):
            requests.append(request)
            return next(results)

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    assert run_ask_step(context, query_step, "prompt", [], "docs.query") == []
    assert [request.session_id for request in requests] == [None, "session-1"]
    assert [request.resume_session for request in requests] == [False, True]
    assert finalized == ["session-1"]


@pytest.mark.parametrize(
    "second_outcome", [RunnerTimeoutError("timed out"), "missing-output"]
)
def test_reused_session_is_finalized_when_retry_fails_without_session_metadata(
    tmp_path: pathlib.Path, monkeypatch, second_outcome
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, json_retry_new_session=False))
    context = build_context(repo, config)
    query_step = next(
        step for step in config.modules["docs"].steps if step.id == "query"
    )
    query_step = replace(query_step, inputs=())
    finalized: list[str | None] = []
    outcomes = iter(
        [
            RunnerResult(
                "not json", 0, "", "", SessionMetadata("session-1", "persisted", True)
            ),
            second_outcome,
        ]
    )

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, _request):
            outcome = next(outcomes)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "missing-output":
                return RunnerResult("", 0, "", "")
            return outcome

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", lambda _type: FakeRunner()
    )

    with pytest.raises(HookError, match=r"opencode.*docs\.query"):
        run_ask_step(context, query_step, "prompt", [], "docs.query")
    assert finalized == ["session-1"]


def test_reused_session_is_finalized_when_next_runner_construction_fails(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(config, llm=replace(config.llm, json_retry_new_session=False))
    context = build_context(repo, config)
    query_step = next(
        step for step in config.modules["docs"].steps if step.id == "query"
    )
    query_step = replace(query_step, inputs=())
    finalized: list[str | None] = []

    class FakeRunner:
        capabilities = RunnerCapabilities(supports_resume=True, supports_finalize=True)

        def run(self, _request):
            return RunnerResult(
                "not json", 0, "", "", SessionMetadata("session-1", "persisted", True)
            )

        def finalize(self, _request, result):
            if result.session:
                finalized.append(result.session.session_id)
            return result

    calls = 0

    def get_runner(_type):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeRunner()
        raise RunnerAdapterUnavailableError("construction failed")

    _use_runner_boundary_logger(context, monkeypatch)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runner_workflow.get_runner", get_runner
    )

    with pytest.raises(HookError, match=r"opencode.*docs\.query"):
        run_ask_step(context, query_step, "prompt", [], "docs.query")
    assert finalized == ["session-1"]
