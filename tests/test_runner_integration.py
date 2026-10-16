from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import replace

import pytest

from ai_push_hooks.artifacts import ArtifactStore
from ai_push_hooks.config import load_config
from ai_push_hooks.engine import WorkflowEngine
from ai_push_hooks.executors.runners import ProcessResult
from ai_push_hooks.executors.runners.registry import DEFAULT_RUNNER_REGISTRY
from ai_push_hooks.types import CollectorResult, HookError

from .conftest import build_context, init_repo


CLAUDE_HELP = """
Usage: claude [options] [prompt]
  -p, --print
  --output-format <format> (json)
  --no-session-persistence
  --model <model>
  --permission-mode <mode> (dontAsk, acceptEdits)
  --tools <tools...>
  --allowedTools <tools...>
"""


def _write_custom_runner(tmp_path: pathlib.Path) -> pathlib.Path:
    script = tmp_path / "trusted-custom-runner.py"
    script.write_text(
        """
import json
import os
import pathlib
import sys

stage = sys.argv[2]
packet = sys.stdin.read()
record_path = pathlib.Path(os.environ["AI_PUSH_HOOKS_TEST_RECORD"])
with record_path.open("a", encoding="utf-8") as record:
    record.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": packet}) + "\\n")

if stage.endswith(".apply"):
    pathlib.Path("README.md").write_text("# Applied by custom runner\\n", encoding="utf-8")
    if os.environ.get("AI_PUSH_HOOKS_TEST_NONALLOWLISTED") == "1":
        pathlib.Path("src/app.py").write_text("must not propagate\\n", encoding="utf-8")

print("custom response prompt-secret token=custom-child-secret")
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def _write_mixed_config(repo: pathlib.Path, custom_script: pathlib.Path) -> None:
    repo.joinpath("ai-push-hooks.toml").write_text(
        f"""
[llm]
runner = "codex-global"
model = "opaque/config-codex-model::v1"
max_parallel = 1
json_max_retries = 0

[logging]
print_llm_output = true
jsonl = false

[runners.codex-global]
type = "codex"
model = "opaque/profile-codex-model::v2"
project_access = "project"

[runners.claude-step]
type = "claude"
model = "opaque/profile-claude-model::v3"
project_access = "project"

[runners.custom-apply]
type = "command"
model = "opaque/profile-custom-model::v4"
project_access = "project"
prompt_transport = "stdin"
command = [{sys.executable!r}, {str(custom_script)!r}, "{{model}}", "{{stage}}"]

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "fixture"

[[modules.docs.steps]]
id = "codex"
type = "ask"
inputs = ["collect/first.txt", "collect/second.txt"]
prompt = "codex prompt-secret"
output = "codex.txt"

[[modules.docs.steps]]
id = "claude"
type = "ask"
inputs = ["collect/second.txt", "codex/codex.txt"]
prompt = "claude prompt-secret"
output = "claude.txt"
runner = "claude-step"

[[modules.docs.steps]]
id = "apply"
type = "apply"
inputs = ["collect/first.txt", "claude/claude.txt"]
prompt = "apply prompt-secret"
allow_paths = ["README.md"]
runner = "custom-apply"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _codex_stream(text: str, thread_id: str = "codex-thread") -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": text},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )


def _reset_runner_cache(*runner_types: str) -> None:
    for runner_type in runner_types:
        DEFAULT_RUNNER_REGISTRY._loaded.pop(runner_type, None)


def test_config_engine_neutral_orchestration_reaches_mixed_real_adapters(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path, branch="feature/runners")
    custom_script = _write_custom_runner(tmp_path)
    record_path = tmp_path / "custom-record.jsonl"
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_RECORD", str(record_path))
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_TOKEN", "environment-secret")
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", "opaque/env-override-model::exact")
    _write_mixed_config(repo, custom_script)
    config, _ = load_config(repo)
    context = build_context(repo, config)

    codex_calls: list[dict[str, object]] = []
    claude_calls: list[dict[str, object]] = []

    def fake_codex(argv, *, cwd, input_text, timeout_seconds, env=None):
        codex_calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
                "env": env,
            }
        )
        return ProcessResult(
            0,
            _codex_stream("codex prompt-secret api_key=codex-child-secret"),
            "",
        )

    def fake_claude(argv, *, cwd, input_text, timeout_seconds, env=None):
        claude_calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "input_text": input_text,
                "timeout_seconds": timeout_seconds,
                "env": env,
            }
        )
        if list(argv) == ["claude", "--help"]:
            return ProcessResult(0, CLAUDE_HELP, "")
        return ProcessResult(
            0,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "claude artifact-secret token=claude-child-secret",
                    "session_id": "claude-ephemeral-session",
                }
            ),
            "",
        )

    _reset_runner_cache("codex", "claude", "command")
    monkeypatch.setattr("ai_push_hooks.executors.runners.codex.run_process", fake_codex)
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.claude.run_process", fake_claude
    )
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.claude.shutil.which",
        lambda name: "claude" if name == "claude" else None,
    )

    def collect(_context, _state):
        return CollectorResult(
            artifacts={
                "first.txt": "first artifact-secret",
                "second.txt": "second body",
            }
        )

    result = WorkflowEngine(
        context=context,
        artifacts=ArtifactStore(context.run_dir),
        collectors={"fixture": collect},
    ).run()

    assert result.modules == {"docs": "completed"}
    assert (repo / "README.md").read_text(
        encoding="utf-8"
    ) == "# Applied by custom runner\n"

    codex_packet = str(codex_calls[0]["input_text"])
    assert codex_packet.index("first.txt") < codex_packet.index("second.txt")
    assert codex_calls[0]["cwd"] == repo.resolve()
    assert codex_calls[0]["argv"] == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--cd",
        str(repo.resolve()),
        "--model",
        "opaque/env-override-model::exact",
        "-",
    ]

    claude_invocation = claude_calls[-1]
    claude_packet = str(claude_invocation["input_text"])
    assert claude_packet.index("second.txt") < claude_packet.index("codex.txt")
    assert claude_invocation["cwd"] == repo.resolve()
    assert claude_invocation["argv"] == [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--model",
        "opaque/env-override-model::exact",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Glob,Grep",
        "--allowedTools",
        "Read,Glob,Grep",
    ]

    custom_record = json.loads(record_path.read_text(encoding="utf-8").splitlines()[0])
    assert custom_record["argv"] == [
        "opaque/env-override-model::exact",
        "docs.apply",
    ]
    assert pathlib.Path(custom_record["cwd"]).name.startswith("ai-push-hooks-apply-")
    assert custom_record["cwd"] != str(repo.resolve())
    assert custom_record["stdin"].index("first.txt") < custom_record["stdin"].index(
        "claude.txt"
    )

    docs_dir = context.run_dir / "docs"
    assert (
        (docs_dir / "01-codex" / "codex.txt")
        .read_text(encoding="utf-8")
        .startswith("codex prompt-secret")
    )
    assert (
        (docs_dir / "02-claude" / "claude.txt")
        .read_text(encoding="utf-8")
        .startswith("claude artifact-secret")
    )
    assert json.loads(
        (docs_dir / "03-apply" / "result.json").read_text(encoding="utf-8")
    )["changed_files"] == ["README.md"]

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "environment-secret" not in output
    for secret in (
        "prompt-secret",
        "artifact-secret",
        "codex-child-secret",
        "claude-child-secret",
        "custom-child-secret",
    ):
        assert secret not in output
    assert output.count("[REDACTED]") >= 3
    assert [
        (call["runner_profile"], call["runner_type"], call["model"])
        for call in context.logger.llm_calls
    ] == [
        ("codex-global", "codex", "opaque/env-override-model::exact"),
        ("claude-step", "claude", "opaque/env-override-model::exact"),
        ("custom-apply", "command", "opaque/env-override-model::exact"),
    ]


def test_nonallowlisted_custom_apply_mutation_fails_closed_before_propagation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/security")
    custom_script = _write_custom_runner(tmp_path)
    record_path = tmp_path / "custom-record.jsonl"
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_RECORD", str(record_path))
    monkeypatch.setenv("AI_PUSH_HOOKS_TEST_NONALLOWLISTED", "1")
    _write_mixed_config(repo, custom_script)
    config, _ = load_config(repo)
    context = build_context(repo, config)
    apply_only = replace(
        config,
        workflow=replace(config.workflow, modules=("docs",)),
        modules={
            "docs": replace(
                config.modules["docs"],
                steps=(replace(config.modules["docs"].steps[-1], inputs=()),),
            )
        },
    )
    context.config = apply_only

    with pytest.raises(HookError, match="outside allowlist"):
        WorkflowEngine(context, ArtifactStore(context.run_dir)).run()

    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.parametrize("runner_type", ["codex", "claude"])
def test_full_engine_json_retries_use_fresh_ephemeral_runner_invocations(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_type: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/retry")
    runner_name = f"retry-{runner_type}"
    model = "opaque/retry-model::no-parsing"
    claude_profile = (
        f'\n[runners.{runner_name}]\ntype = "{runner_type}"\nmodel = {model!r}\n'
    )
    repo.joinpath("ai-push-hooks.toml").write_text(
        f"""
[llm]
runner = "{runner_name}"
model = "{model}"
json_max_retries = 1
json_retry_new_session = false

{claude_profile}
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "query"
type = "ask"
schema = "string_array"
prompt = "retry prompt-secret"
output = "result.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    completions: list[dict[str, object]] = []
    monkeypatch.setattr(
        context.logger,
        "llm_complete",
        lambda _number, _stage, _profile, _type, **fields: completions.append(fields),
    )
    calls: list[list[str]] = []
    inputs: list[str | None] = []

    if runner_type == "codex":
        _reset_runner_cache("codex")

        def fake_process(argv, *, cwd, input_text, timeout_seconds, env=None):
            calls.append(list(argv))
            inputs.append(input_text)
            text = "not-json" if len(calls) == 1 else '["accepted"]'
            return ProcessResult(0, _codex_stream(text, f"thread-{len(calls)}"), "")

        monkeypatch.setattr(
            "ai_push_hooks.executors.runners.codex.run_process", fake_process
        )
    else:
        _reset_runner_cache("claude")
        monkeypatch.setattr(
            "ai_push_hooks.executors.runners.claude.shutil.which",
            lambda name: "claude" if name == "claude" else None,
        )

        def fake_process(argv, *, cwd, input_text, timeout_seconds, env=None):
            calls.append(list(argv))
            inputs.append(input_text)
            if list(argv) == ["claude", "--help"]:
                return ProcessResult(0, CLAUDE_HELP, "")
            invocation_number = len(
                [call for call in calls if call != ["claude", "--help"]]
            )
            text = "not-json" if invocation_number == 1 else '["accepted"]'
            return ProcessResult(
                0,
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": text,
                        "session_id": f"claude-{invocation_number}",
                    }
                ),
                "",
            )

        monkeypatch.setattr(
            "ai_push_hooks.executors.runners.claude.run_process", fake_process
        )

    WorkflowEngine(context, ArtifactStore(context.run_dir)).run()

    invocation_calls = [call for call in calls if call != ["claude", "--help"]]
    assert len(invocation_calls) == 2
    assert all(
        "--session" not in call and "--resume" not in call for call in invocation_calls
    )
    assert len(completions) == 2
    assert all(fields["session_state"] == "ephemeral" for fields in completions)
    assert all(fields["resumable"] is False for fields in completions)
    assert inputs[-1] is not None and "previous response was invalid JSON" in inputs[-1]
    result_path = context.run_dir / "docs" / "00-query" / "result.json"
    assert json.loads(result_path.read_text(encoding="utf-8")) == ["accepted"]


def test_opencode_retry_reuses_the_exact_retained_session_id(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/opencode-retry")
    repo.joinpath("ai-push-hooks.toml").write_text(
        """
[llm]
runner = "opencode"
model = "opaque/opencode-model::exact"
json_max_retries = 1
json_retry_new_session = false
delete_session_after_run = false

[logging]
capture_llm_transcript = false

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "query"
type = "ask"
schema = "string_array"
prompt = "opencode retry prompt-secret"
output = "result.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/disposable/opencode"
    _reset_runner_cache("opencode")
    calls: list[list[str]] = []

    def fake_process(argv, **_kwargs):
        calls.append(list(argv))
        text = "not-json" if len(calls) == 1 else '["accepted"]'
        return ProcessResult(
            0,
            "\n".join(
                [
                    json.dumps(
                        {"type": "session.created", "sessionID": "session-opaque-42"}
                    ),
                    json.dumps({"type": "text", "part": {"text": text}}),
                ]
            ),
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_process
    )

    WorkflowEngine(context, ArtifactStore(context.run_dir)).run()

    assert len(calls) == 2
    assert "--title" in calls[0]
    assert "--session" not in calls[0]
    assert calls[1][calls[1].index("--session") + 1] == "session-opaque-42"
    assert "--title" not in calls[1]
    assert json.loads(
        context.run_dir.joinpath("docs/00-query/result.json").read_text(
            encoding="utf-8"
        )
    ) == ["accepted"]


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("retries", [0, 1, 2])
@pytest.mark.parametrize("outcome", ["correction", "exhausted", "process-failure"])
def test_opencode_cli_generation_real_workflow_retry_matrix(
    tmp_path, monkeypatch, generation, new_session, retries, outcome
):
    from ai_push_hooks.executors.runners.opencode import OpenCodeRunner

    repo = init_repo(tmp_path, branch="feature/cli-generation")
    selector = "" if generation is None else f'opencode_cli_generation = "{generation}"'
    # Exercise implicit default, explicit global and per-step selections with
    # real config rebuilding and final environment override precedence.
    selected = "opencode" if retries == 0 else "selected"
    global_runner = selected if retries != 2 else "opencode"
    step_runner = 'runner = "selected"' if retries == 2 else ""
    profiles = (
        ""
        if retries == 0
        else '[runners.selected]\ntype = "opencode"\nmodel = "profile/model"\nvariant = ""'
    )
    repo.joinpath("ai-push-hooks.toml").write_text(
        f'''
[llm]
{selector}
runner = "{global_runner}"
model = "global/model"
json_max_retries = {retries}
json_retry_new_session = {str(new_session).lower()}
invalid_json_feedback_max_chars = 13
delete_session_after_run = true
{profiles}
[logging]
capture_llm_transcript = true
[workflow]
modules = ["docs"]
[[modules.docs.steps]]
id = "query"
type = "ask"
schema = "string_array"
prompt = "--exact workflow prompt"
output = "result.json"
{step_runner}
''',
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", "env/model#native")
    monkeypatch.setenv("AI_PUSH_HOOKS_VARIANT", "")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/fake-only/workflow-opencode"
    _reset_runner_cache("opencode")
    assert isinstance(DEFAULT_RUNNER_REGISTRY.get("opencode"), OpenCodeRunner)
    calls = []
    run_count = 0
    bad = "invalid-response-" * 10
    feedback = (
        "--exact workflow prompt\n\nIMPORTANT: Your previous response was invalid JSON and could not be parsed.\n"
        "Parse error: Could not find JSON array in model output\n"
        "Return ONLY valid JSON array.\nPrevious invalid output:\n```text\n"
        + bad[:13]
        + "\n```"
    )
    sid = ""

    def lifecycle(action, session):
        if generation == "v2":
            return [
                context.opencode_executable,
                "session",
                action,
                "--standalone",
                session,
            ]
        return (
            [context.opencode_executable, "export", session, "--pure"]
            if action == "export"
            else [context.opencode_executable, "session", "delete", session, "--pure"]
        )

    def fake(argv, **kwargs):
        nonlocal run_count, sid
        calls.append(list(argv))
        if argv[1] != "run":
            action = "export" if "export" in argv else "delete"
            assert argv == lifecycle(action, sid)
            return ProcessResult(
                0,
                json.dumps({"info": {"id": sid}, "messages": []})
                if action == "export"
                else "deleted",
                "",
            )
        run_count += 1
        sid = (
            f"opaque-session-{run_count}"
            if new_session or run_count == 1
            else "opaque-session-1"
        )
        expected = [
            context.opencode_executable,
            "run",
            "--agent",
            "ai-push-hooks-readonly",
            "--standalone" if generation == "v2" else "--pure",
            "--format",
            "json",
            "--model",
            "env/model#native",
        ]
        expected += (
            ["--session", sid]
            if run_count > 1 and not new_session
            else ["--title", "ai-push-hooks test-run docs.query"]
        )
        expected += ["--", "--exact workflow prompt" if run_count == 1 else feedback]
        assert argv == expected
        assert kwargs["timeout_seconds"] == config.llm.timeout_seconds
        assert list(kwargs["cwd"].iterdir()) == []
        text = '["accepted"]' if outcome == "correction" and run_count == 2 else bad
        return ProcessResult(
            7 if outcome == "process-failure" else 0,
            json.dumps({"type": "text", "sessionID": sid, "part": {"text": text}}),
            "",
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    succeeds = outcome == "correction" and retries > 0
    if succeeds:
        WorkflowEngine(context, ArtifactStore(context.run_dir)).run()
        assert json.loads(
            context.run_dir.joinpath("docs/00-query/result.json").read_text()
        ) == ["accepted"]
    else:
        with pytest.raises(
            HookError,
            match=rf"runner '{selected}' \(opencode\) failed"
            if outcome == "process-failure"
            else "invalid JSON",
        ):
            WorkflowEngine(context, ArtifactStore(context.run_dir)).run()
    attempts = 1 if outcome == "process-failure" else (2 if succeeds else retries + 1)
    assert run_count == attempts
    expected_order = []
    for attempt in range(1, attempts + 1):
        expected_order.append("run")
        if new_session or attempt == attempts:
            expected_order.extend(["export", "delete"])
    assert [
        "run" if argv[1] == "run" else "export" if "export" in argv else "delete"
        for argv in calls
    ] == expected_order
    assert len(calls) == attempts + 2 * (attempts if new_session else 1)
