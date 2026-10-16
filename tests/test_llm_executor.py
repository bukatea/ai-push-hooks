from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import replace

import pytest

from ai_push_hooks.config import load_config
from ai_push_hooks.executors.llm import (
    OpenCodeRunResult,
    call_opencode,
    delete_opencode_session,
    export_opencode_session_json,
    finalize_opencode_session,
    run_llm_step,
)
from ai_push_hooks.types import HookError, LlmConfig, StepConfig

from .conftest import build_context, init_repo


def test_run_llm_step_accepts_array_for_docs_issue_schema(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    analyze_step = next(step for step in config.modules["docs"].steps if step.id == "analyze")

    def fake_call_opencode(*args, **kwargs):
        return OpenCodeRunResult(
            output_text="[]",
            session_id=None,
            stdout="",
            stderr="",
            return_code=0,
        )

    monkeypatch.setattr("ai_push_hooks.executors.llm.call_opencode", fake_call_opencode)
    monkeypatch.setattr("ai_push_hooks.executors.llm.finalize_opencode_session", lambda *args, **kwargs: None)

    payload = run_llm_step(context, analyze_step, "prompt", [], "docs.analyze")

    assert payload == []


@pytest.fixture
def fake_process(monkeypatch):
    calls = []
    responses = []

    def fake_run_command(args, **kwargs):
        calls.append((list(args), kwargs))
        assert responses, f"Unexpected process call: {args}"
        code, stdout, stderr = responses.pop(0)
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr("ai_push_hooks.executors.llm.run_command", fake_run_command)
    return calls, responses


@pytest.fixture
def cli_context(repo, monkeypatch):
    for name in ("MODEL", "VARIANT", "TIMEOUT_SECONDS"):
        monkeypatch.delenv(f"AI_PUSH_HOOKS_{name}", raising=False)
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/synthetic/opencode-cli"
    return context


def select_cli(context, generation="v2", **kwargs):
    context.config = replace(
        context.config,
        llm=replace(context.config.llm, opencode_cli_generation=generation, **kwargs),
    )


def text_event(text, session_id="ses-unit"):
    return json.dumps({"type": "text", "sessionID": session_id, "part": {"text": text}})


def test_cli_generation_toml_effective_environment_roundtrip(repo, monkeypatch, fake_process):
    path = repo / "ai-push-hooks.toml"
    path.write_text(path.read_text().replace("[llm]\n", '[llm]\nopencode_cli_generation = "v2"\n'))
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", "test/provider-model")
    monkeypatch.setenv("AI_PUSH_HOOKS_VARIANT", "")
    monkeypatch.setenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS", "17")
    config, loaded_path = load_config(repo)
    assert loaded_path == path
    assert config.llm.opencode_cli_generation == "v2"
    assert config.llm.model == "test/provider-model"
    assert config.llm.variant == ""
    assert config.llm.timeout_seconds == 17
    assert fake_process[0] == []
    context = build_context(repo, config)
    context.opencode_executable = "/synthetic/opencode-cli"
    calls, responses = fake_process
    responses.append((0, text_event("answer"), ""))
    call_opencode(context, "stage", "purpose", "prompt", [])
    assert calls[0][0][:7] == [context.opencode_executable, "run", "--format", "json",
                               "--model", "test/provider-model", "--standalone"]
    assert calls[0][1]["timeout"] == 17


@pytest.mark.parametrize("value", [None, True, 2, 2.0, [], {}, "", "V2", "auto", "v2 "])
def test_cli_generation_rejects_invalid_values_before_process(value, fake_process):
    with pytest.raises(HookError, match="opencode_cli_generation"):
        LlmConfig(opencode_cli_generation=value)
    assert fake_process[0] == []


@pytest.mark.parametrize("raw", ['"unknown"', "true", "2", "2.0", "[]", "{}"])
def test_cli_generation_rejects_invalid_toml_before_process(repo, raw, fake_process):
    path = repo / "ai-push-hooks.toml"
    path.write_text(path.read_text().replace("[llm]\n", f"[llm]\nopencode_cli_generation = {raw}\n"))
    with pytest.raises(HookError, match="opencode_cli_generation"):
        load_config(repo)
    assert fake_process[0] == []


@pytest.mark.parametrize("source", ["dataclass", "toml", "environment"])
def test_v2_variant_rejected_before_process(repo, monkeypatch, source, fake_process):
    if source == "dataclass":
        with pytest.raises(HookError, match="variant"):
            LlmConfig(opencode_cli_generation="v2", variant="high")
    else:
        path = repo / "ai-push-hooks.toml"
        text = path.read_text().replace("[llm]\n", '[llm]\nopencode_cli_generation = "v2"\n')
        if source == "toml":
            text = text.replace('variant = ""', 'variant = "high"')
        path.write_text(text)
        if source == "environment":
            monkeypatch.setenv("AI_PUSH_HOOKS_VARIANT", "high")
        with pytest.raises(HookError, match="variant"):
            load_config(repo)
    assert fake_process[0] == []


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
@pytest.mark.parametrize("session_id", [None, "ses-existing"])
def test_cli_run_exact_argv(cli_context, fake_process, generation, session_id):
    context = cli_context
    if generation is not None:
        select_cli(context, generation)
    calls, responses = fake_process
    responses.append((0, text_event("answer"), "diagnostic"))
    files = [context.repo_root / "input with spaces.txt", context.repo_root / "diff.txt"]
    prompt = "--server fake; $(not-a-command)\nreal prompt"
    result = call_opencode(context, "docs.analyze", "llm:analyze", prompt, files,
                           existing_session_id=session_id)
    expected = [context.opencode_executable, "run", "--format", "json", "--model", context.config.llm.model]
    if generation == "v2":
        expected += ["--standalone"]
    expected += ["--session", session_id] if session_id else ["--title", "ai-push-hooks test-run docs.analyze"]
    expected += ["--file", str(files[0]), "--file", str(files[1]), "--", prompt]
    assert calls == [(expected, {"cwd": context.repo_root, "timeout": 800,
                                 "check": False, "env": {"OPENCODE_SERVER_PASSWORD": None}})]
    assert result.output_text == "answer"
    assert result.session_id == "ses-unit"
    assert result.stderr == "diagnostic"
    assert result.return_code == 0


def test_legacy_variant_argv_unchanged(cli_context, fake_process):
    select_cli(cli_context, "legacy", variant="high")
    calls, responses = fake_process
    responses.append((0, "plain output", ""))
    result = call_opencode(cli_context, "stage", "purpose", "prompt", [], existing_session_id="ses-old")
    assert calls[0][0] == [cli_context.opencode_executable, "run", "--format", "json", "--model",
                           cli_context.config.llm.model, "--variant", "high", "--session", "ses-old", "--", "prompt"]
    assert result.session_id == "ses-old"
    assert result.output_text == "plain output"


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
def test_cli_export_delete_exact_argv(cli_context, fake_process, generation):
    context = cli_context
    if generation is not None:
        select_cli(context, generation, timeout_seconds=17)
    calls, responses = fake_process
    responses.extend([(0, '{"info": {}, "messages": []}', ""), (0, "", "")])
    target = context.run_dir / "session.json"
    assert export_opencode_session_json(context, "ses-unit", target)
    delete_opencode_session(context, "ses-unit")
    prefix = [context.opencode_executable]
    export = ["session", "export", "--standalone"] if generation == "v2" else ["export"]
    delete = ["session", "delete", "--standalone"] if generation == "v2" else ["session", "delete"]
    kwargs = {"cwd": context.repo_root, "timeout": context.config.llm.timeout_seconds, "check": False}
    assert calls == [(prefix + export + ["ses-unit"], kwargs), (prefix + delete + ["ses-unit"], kwargs)]
    assert target.read_text() == '{"info": {}, "messages": []}\n'


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_v2_real_step_success_preserves_lifecycle(cli_context, fake_process, capture, delete):
    context = cli_context
    select_cli(context, delete_session_after_run=delete)
    context.config = replace(context.config, logging=replace(context.config.logging, capture_llm_transcript=capture))
    calls, responses = fake_process
    responses.append((0, text_event('["valid"]'), ""))
    if capture:
        responses.append((0, '{"info": {}, "messages": []}', ""))
    if delete:
        responses.append((0, "", ""))
    step = StepConfig(id="query", type="llm", schema="string_array")
    assert run_llm_step(context, step, "prompt", [], "docs.query") == ["valid"]
    assert len(calls) == 1 + int(capture) + int(delete)
    for args, _ in calls:
        assert args.count("--standalone") == 1
        assert "--server" not in args
    if capture:
        assert calls[1][0] == [context.opencode_executable, "session", "export", "--standalone", "ses-unit"]
    if delete:
        assert calls[-1][0] == [context.opencode_executable, "session", "delete", "--standalone", "ses-unit"]
    assert responses == []


@pytest.mark.parametrize("retries", [0, 1, 2])
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("bad_output, error", [("not json", "Could not find JSON array"), ("[1]", "Expected schema string_array")])
def test_v2_real_step_retries_and_propagates_failure(cli_context, fake_process, new_session, retries, bad_output, error):
    context = cli_context
    select_cli(context, json_max_retries=retries, json_retry_new_session=new_session,
               invalid_json_feedback_max_chars=4)
    calls, responses = fake_process
    # Existing schema validation finalizes BEFORE validation. Preserve that policy,
    # including the final extra finalization on exhausted schema failures.
    for _ in range(retries + 1):
        responses.append((0, text_event(bad_output), ""))
        if bad_output == "[1]":
            responses.extend([(0, "{}", ""), (0, "", "")])
    responses.extend([(0, "{}", ""), (0, "", "")])
    step = StepConfig(id="query", type="llm", schema="string_array")
    with pytest.raises(HookError, match=f"Model failed to return valid JSON for docs.query: {error}"):
        run_llm_step(context, step, "original prompt", [], "docs.query")
    runs = [args for args, _ in calls if args[1] == "run"]
    assert len(runs) == retries + 1
    assert "--title" in runs[0]
    assert runs[0][-2:] == ["--", "original prompt"]
    for retry in runs[1:]:
        if new_session:
            assert "--title" in retry
            assert "--session" not in retry
        else:
            assert retry[retry.index("--session") + 1] == "ses-unit"
        assert "Parse error: " + error in retry[-1]
        assert "```text\n" + bad_output[:4] + "\n```" in retry[-1]
    assert [call["attempt"] for call in context.logger.llm_calls] == list(range(1, retries + 2))
    assert all(call["total_attempts"] == retries + 1 for call in context.logger.llm_calls)
    for args, _ in calls:
        assert args.count("--standalone") == 1
        assert "--server" not in args
        if args[1] == "session":
            assert args[-1] == "ses-unit"
    assert responses == []


def test_v2_retry_recovers_using_resolved_executable(cli_context, fake_process, monkeypatch):
    context = cli_context
    select_cli(context, json_retry_new_session=False, timeout_seconds=17)
    context.opencode_executable = None
    monkeypatch.setattr("ai_push_hooks.executors.llm.resolve_opencode_executable", lambda: "/synthetic/resolved")
    calls, responses = fake_process
    responses.extend([(0, text_event("malformed"), ""), (0, text_event('["recovered"]'), ""),
                      (0, "{}", ""), (0, "", "")])
    step = StepConfig(id="query", type="llm", schema="string_array")
    assert run_llm_step(context, step, "prompt", [], "docs.query") == ["recovered"]
    assert len(calls) == 4
    assert calls[1][0][calls[1][0].index("--session") + 1] == "ses-unit"
    for args, kwargs in calls:
        assert args[0] == "/synthetic/resolved"
        assert args.count("--standalone") == 1
        assert "--server" not in args
        assert kwargs["cwd"] == context.repo_root
        assert kwargs["timeout"] == 17
    assert responses == []


def test_v2_real_step_command_failure_does_not_retry(cli_context, fake_process):
    select_cli(cli_context)
    calls, responses = fake_process
    responses.extend([(7, text_event("failed"), "synthetic failure"), (0, "{}", ""), (0, "", "")])
    step = StepConfig(id="query", type="llm", schema="string_array")
    with pytest.raises(HookError, match="OpenCode command failed: synthetic failure"):
        run_llm_step(cli_context, step, "prompt", [], "docs.query")
    assert len([args for args, _ in calls if args[1] == "run"]) == 1
    assert responses == []


def test_v2_existing_export_delete_failure_policy_unchanged(cli_context, fake_process):
    select_cli(cli_context)
    calls, responses = fake_process
    responses.extend([(1, "", "export failed"), (1, "", "delete failed")])
    # This proposal selects CLI syntax only; it does not make retention fail closed.
    finalize_opencode_session(cli_context, "stage", "ses-unit")
    assert len(calls) == 2
    assert list((cli_context.git_dir / "ai-push-hooks/transcripts").glob("*.json")) == []
    assert responses == []
