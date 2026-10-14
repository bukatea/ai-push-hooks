from __future__ import annotations

import json
import pathlib
import subprocess
import threading
from dataclasses import replace

import pytest

from ai_push_hooks.executors.runners import (
    ProcessResult,
    RunnerRequest,
    RunnerArtifact,
    RunnerContractError,
    RunnerError,
    RunnerMissingOutputError,
    RunnerProtocolError,
    RunnerResult,
    RunnerTimeoutError,
    SessionMetadata,
)
from ai_push_hooks.executors.runners.opencode import OpenCodeRunner, create_runner
from ai_push_hooks.executors.runners.opencode_support import opencode_isolation_env
from ai_push_hooks.config import load_config
from ai_push_hooks.types import HookError, HookLogger

from .conftest import build_context, init_repo


def _request(
    context, *, project_access: str = "artifacts", **overrides: object
) -> RunnerRequest:
    artifact = context.run_dir / "input.txt"
    artifact.write_text("artifact body\n", encoding="utf-8")
    values: dict[str, object] = {
        "profile_id": "opencode",
        "runner_type": "opencode",
        "stage": "docs.query",
        "purpose": "ask:query",
        "mode": "ask",
        "instruction": "Return a JSON array.",
        "artifacts": (RunnerArtifact("input.txt", "artifact body\n", artifact),),
        "cwd": context.repo_root,
        "timeout_seconds": 3,
        "model": "profile/model",
        "variant": "profile-variant",
        "project_access": project_access,
        "integration_context": context,
    }
    values.update(overrides)
    return RunnerRequest(**values)


def test_required_retention_rejects_unidentified_output(tmp_path, monkeypatch):
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    # Set the proposed flag without requiring the new dataclass field for RED.
    object.__setattr__(config.logging, "require_opencode_retention", True)
    context = build_context(repo, config)
    context.opencode_executable = "/fixture/opencode"
    raw = b'{"type":"text","part":{"text":"[]"}}\n'
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *args, **kwargs: ProcessResult(0, raw.decode(), "", stdout_bytes=raw),
    )
    with pytest.raises(HookError, match="retention"):
        OpenCodeRunner().run(_request(context))


def _strict_fixture(tmp_path):
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config, logging=replace(config.logging, require_opencode_retention=True)
    )
    context = build_context(repo, config)
    context.opencode_executable = "/fixture/opencode"
    event = {
        "type": "text",
        "timestamp": 1,
        "sessionID": "s1",
        "part": {"type": "text", "sessionID": "s1", "messageID": "msg_1", "text": "[]"},
    }
    export = {
        "info": {"id": "s1"},
        "messages": [
            {
                "type": "assistant",
                "id": "msg_1",
                "content": [{"type": "text", "text": "prefix []"}],
            }
        ],
    }
    return context, event, export


def _bytes_result(raw, code=0, **kwargs):
    return ProcessResult(
        code, raw.decode("utf-8", "surrogateescape"), "", stdout_bytes=raw, **kwargs
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "export_nonzero",
        "export_empty",
        "export_list",
        "export_identity",
        "export_correlation",
        "export_error",
        "delete_nonzero",
        "delete_timeout",
        "no_delete",
    ],
)
def test_required_lifecycle_exact_bytes_and_no_unsafe_deletion(
    tmp_path, monkeypatch, failure
):
    context, event, export = _strict_fixture(tmp_path)
    if failure == "no_delete":
        context.config = replace(
            context.config,
            llm=replace(context.config.llm, delete_session_after_run=False),
        )
    raw = ("  " + json.dumps(event) + "\n\n").encode()
    if failure == "export_identity":
        export["info"]["id"] = "other"
    if failure == "export_correlation":
        export["messages"][0]["id"] = "msg_other"
    if failure == "export_error":
        export["error"] = {"private": "body"}
    exported = ("\n" + json.dumps(export, indent=2) + "\n").encode()
    if failure == "export_empty":
        exported = b""
    if failure == "export_list":
        exported = b"[]"
    calls = []

    def process(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "run":
            return _bytes_result(raw)
        if argv[1] == "export":
            return _bytes_result(exported, 1 if failure == "export_nonzero" else 0)
        if failure == "delete_timeout":
            raise RunnerTimeoutError("timeout")
        return _bytes_result(b"", 1 if failure == "delete_nonzero" else 0)

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    runner = OpenCodeRunner()
    request = _request(context)
    result = runner.run(request)
    if failure not in {None, "no_delete"}:
        with pytest.raises(HookError, match="retention"):
            runner.finalize(request, result)
    else:
        result = runner.finalize(request, result)
        assert result.session.state == ("persisted" if failure else "deleted")
    directory = runner._retention_dir
    assert (directory / "run.stdout").read_bytes() == raw
    assert (directory / "export.stdout").read_bytes() == exported
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    assert len(calls) == (
        2 if failure and (failure.startswith("export") or failure == "no_delete") else 3
    )
    assert all(call[0][0] == "/fixture/opencode" for call in calls)
    assert len({call[1]["env"]["XDG_DATA_HOME"] for call in calls}) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "conflict",
        "part_conflict",
        "missing_message",
        "step_only",
        "malformed",
        "error",
        "invalid_bytes",
        "truncated",
        "nonzero",
        "resume",
    ],
)
def test_required_run_failure_keeps_bytes_and_never_deletes(
    tmp_path, monkeypatch, failure
):
    context, event, _ = _strict_fixture(tmp_path)
    if failure == "part_conflict":
        event["part"]["sessionID"] = "other"
    if failure == "missing_message":
        del event["part"]["messageID"]
    if failure == "step_only":
        event["type"] = "step_finish"
        event["part"]["type"] = "step-finish"
    if failure == "error":
        event["part"]["error"] = {"private": "body"}
    raw = (json.dumps(event) + "\n").encode()
    if failure == "conflict":
        event["sessionID"] = "other"
        raw += json.dumps(event).encode()
    if failure == "malformed":
        raw += b"{"
    if failure == "invalid_bytes":
        raw += b"\xff"
    calls = []

    def process(argv, **kwargs):
        calls.append(argv)
        return _bytes_result(
            raw,
            1 if failure == "nonzero" else 0,
            stdout_truncated=failure == "truncated",
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    runner = OpenCodeRunner()
    request = _request(
        context,
        resume_session=failure == "resume",
        session_id="other" if failure == "resume" else None,
    )
    with pytest.raises(HookError, match="retention"):
        runner.run(request)
    with pytest.raises(HookError, match="retention"):
        runner.finalize(request, RunnerResult("", 1, "", ""))
    assert len(calls) == 1
    assert (runner._retention_dir / "run.stdout").read_bytes() == raw


@pytest.mark.parametrize(
    "write_failure", ["run.stdout", "export.stdout", "intent", "completion"]
)
def test_required_storage_failure_blocks_success_without_retry(
    tmp_path, monkeypatch, write_failure
):
    from ai_push_hooks.executors.runners import opencode as module

    context, event, export = _strict_fixture(tmp_path)
    original = module.atomic_write_bytes
    calls = []

    def write(path, content, **kwargs):
        receipt = json.loads(content) if path.name == "receipt.json" else {}
        if (
            path.name == write_failure
            or (write_failure == "intent" and receipt.get("delete_action") == "intent")
            or (write_failure == "completion" and receipt.get("outcome") == "deleted")
        ):
            raise OSError("fixture write failure")
        original(path, content, **kwargs)

    def process(argv, **kwargs):
        calls.append(argv)
        return _bytes_result(
            json.dumps(event if argv[1] == "run" else export).encode()
            if argv[1] != "session"
            else b""
        )

    monkeypatch.setattr(module, "atomic_write_bytes", write)
    monkeypatch.setattr(module, "run_process", process)
    runner = OpenCodeRunner()
    request = _request(context)
    with pytest.raises(HookError, match="retention"):
        result = runner.run(request)
        runner.finalize(request, result)
    assert (
        len(calls)
        == {"run.stdout": 1, "export.stdout": 2, "intent": 2, "completion": 3}[
            write_failure
        ]
    )
    if write_failure == "completion":
        assert (
            json.loads((runner._retention_dir / "receipt.json").read_bytes())["outcome"]
            == "unknown"
        )


@pytest.mark.parametrize("mode", ["ask", "apply"])
def test_required_real_workflow_finalization_failure_no_model_retry(
    tmp_path, monkeypatch, mode
):
    from ai_push_hooks.executors.runner_workflow import run_runner_once
    from ai_push_hooks.types import RequiredRetentionError, StepConfig, RunnerProfile

    context, event, _ = _strict_fixture(tmp_path)
    context.config = replace(
        context.config,
        runners={"native": RunnerProfile(type="opencode", name="native")},
    )
    stage = tmp_path / "isolated-stage"
    stage.mkdir()
    calls = []

    def process(argv, **kwargs):
        calls.append(argv)
        return _bytes_result(json.dumps(event).encode() if argv[1] == "run" else b"{}")

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    with pytest.raises(RequiredRetentionError):
        run_runner_once(
            context,
            StepConfig(
                id="strict", type=mode, runner="native", allow_paths=("README.md",)
            ),
            "instruction",
            [],
            "docs.strict",
            working_directory=stage if mode == "apply" else context.repo_root,
        )
    assert [argv[1] for argv in calls] == ["run", "export"]


@pytest.mark.parametrize(
    "failure",
    [
        "stdout",
        "stderr",
        "encoding",
        "bytes",
        "nonzero",
        "timeout",
        "attempted_receipt",
    ],
)
def test_strict_delete_ambiguous_outcome(tmp_path, monkeypatch, failure):
    from ai_push_hooks.executors.runners import opencode as module
    from ai_push_hooks.types import RequiredRetentionError

    context, event, export = _strict_fixture(tmp_path)
    calls = []
    original = module.atomic_write_bytes

    def write(path, content, **kwargs):
        if (
            failure == "attempted_receipt"
            and path.name == "receipt.json"
            and json.loads(content).get("delete_action") == "attempted"
        ):
            raise OSError("attempt receipt unavailable")
        return original(path, content, **kwargs)

    def process(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] != "session":
            return _bytes_result(
                json.dumps(event if argv[1] == "run" else export).encode()
            )
        if failure == "timeout":
            raise RunnerTimeoutError("fixture timeout")
        if failure == "bytes":
            return ProcessResult(0, "not captured", "")
        return _bytes_result(
            b"\xff" if failure == "encoding" else b"",
            1 if failure == "nonzero" else 0,
            stdout_truncated=failure == "stdout",
            stderr_truncated=failure == "stderr",
        )

    monkeypatch.setattr(module, "atomic_write_bytes", write)
    monkeypatch.setattr(module, "run_process", process)
    runner = OpenCodeRunner()
    request = _request(context)
    result = runner.run(request)
    with pytest.raises(RequiredRetentionError):
        runner.finalize(request, result)
    assert calls == (
        ["run", "export"]
        if failure == "attempted_receipt"
        else ["run", "export", "session"]
    )
    if failure != "attempted_receipt":
        assert (
            json.loads((runner._retention_dir / "receipt.json").read_bytes())["outcome"]
            == "unknown"
        )


def test_atomic_writer_ancestor_replacement_never_writes_outside(tmp_path, monkeypatch):
    import os
    from ai_push_hooks.paths import atomic_write_bytes

    root = tmp_path / "owned"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = os.open
    switched = False

    def open_with_race(*args, **kwargs):
        nonlocal switched
        if not switched and pathlib.Path(args[0]).name.startswith(".ai-push-hooks-"):
            switched = True
            root.rename(tmp_path / "moved")
            root.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(os, "open", open_with_race)
    with pytest.raises((HookError, OSError)):
        atomic_write_bytes(root / "receipt.json", b"private")
    assert list(outside.iterdir()) == []


def test_strict_invocation_parent_durability_failure_prevents_model(
    tmp_path, monkeypatch
):
    import os
    from ai_push_hooks.executors.runners import opencode as module

    context, _, _ = _strict_fixture(tmp_path)
    transcript_root = context.git_dir / "ai-push-hooks" / "transcripts"
    transcript_root.mkdir(parents=True, exist_ok=True)
    root_identity = (transcript_root.stat().st_dev, transcript_root.stat().st_ino)
    calls = []
    monkeypatch.setattr(
        module, "run_process", lambda *args, **kwargs: calls.append(args)
    )
    original = os.fsync
    root_syncs = 0

    def fsync(descriptor):
        nonlocal root_syncs
        import stat

        metadata = os.fstat(descriptor)
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == root_identity
        ):
            root_syncs += 1
            if root_syncs == 2:
                raise OSError("invocation parent durability failure")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(HookError, match="retention"):
        OpenCodeRunner().run(_request(context))
    assert calls == []
    assert root_syncs == 2


@pytest.mark.parametrize("kind", ["symlink", "nonregular", "foreign"])
def test_atomic_writer_rejects_unsafe_targets(tmp_path, monkeypatch, kind):
    import os
    from types import SimpleNamespace
    from ai_push_hooks.paths import atomic_write_bytes

    target = tmp_path / "evidence"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"unchanged")
        target.symlink_to(outside)
    elif kind == "nonregular":
        target.mkdir()
    else:
        original = os.fstat

        def foreign(descriptor):
            metadata = original(descriptor)
            return SimpleNamespace(
                st_uid=os.geteuid() + 10000,
                st_mode=metadata.st_mode,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
            )

        monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(HookError):
        atomic_write_bytes(target, b"private")
    if kind == "symlink":
        assert outside.read_bytes() == b"unchanged"


@pytest.mark.parametrize("resume", [True, False])
def test_strict_invocation_collision_preserves_prior_evidence(
    tmp_path, monkeypatch, resume
):
    from types import SimpleNamespace
    from ai_push_hooks.executors.runners import opencode as module

    context, event, _ = _strict_fixture(tmp_path)
    calls = []

    def process(argv, **kwargs):
        calls.append(argv)
        return _bytes_result(json.dumps(event).encode())

    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    monkeypatch.setattr(module, "run_process", process)
    first = OpenCodeRunner()
    first.run(_request(context, stage="docs/a"))
    directory = first._retention_dir
    sentinel = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(HookError, match="retention"):
        OpenCodeRunner().run(
            _request(
                context,
                stage="docs:a",
                resume_session=resume,
                session_id="s1" if resume else None,
            )
        )
    assert len(calls) == 1
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == sentinel
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())


@pytest.mark.parametrize("action", ["run", "export", "delete"])
@pytest.mark.parametrize("kind", ["timeout", "signal"])
def test_strict_failed_process_preserves_original_prefixes(
    tmp_path, monkeypatch, action, kind
):
    from ai_push_hooks.executors.runners import RunnerSignalError

    context, event, export = _strict_fixture(tmp_path)
    stdout = b'  {"sessionID":"s1","partial":\xff\n'
    stderr = b"private stderr prefix\x00\xff\n"
    failed = ProcessResult(
        -9,
        stdout.decode("utf-8", "surrogateescape"),
        stderr.decode("utf-8", "surrogateescape"),
        stdout_truncated=True,
        stderr_truncated=False,
        stdout_bytes=stdout,
        stderr_bytes=stderr,
    )
    primary = (RunnerTimeoutError if kind == "timeout" else RunnerSignalError)(
        "fixture failure"
    )
    primary._process_result = failed
    calls = []

    def process(argv, **kwargs):
        boundary = "delete" if argv[1] == "session" else argv[1]
        calls.append(boundary)
        if boundary == action:
            raise primary
        return _bytes_result(
            json.dumps(event if boundary == "run" else export).encode()
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    runner = OpenCodeRunner()
    request = _request(context)
    with pytest.raises(HookError, match="retention"):
        result = runner.run(request)
        runner.finalize(request, result)
    directory = runner._retention_dir
    assert (directory / f"{action}.stdout").read_bytes() == stdout
    assert (directory / f"{action}.stderr").read_bytes() == stderr
    receipt = json.loads((directory / "receipt.json").read_bytes())
    assert receipt[action] == {
        "available": True,
        "exception": type(primary).__name__,
        "returncode": -9,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_truncated": True,
        "stderr_truncated": False,
    }
    assert receipt["outcome"] == ("retained" if action == "export" else "unknown")
    assert (
        calls
        == {
            "run": ["run"],
            "export": ["run", "export"],
            "delete": ["run", "export", "delete"],
        }[action]
    )


def test_strict_process_and_capture_failure_preserves_both_causes(
    tmp_path, monkeypatch
):
    from ai_push_hooks.executors.runners import opencode as module

    context, _, _ = _strict_fixture(tmp_path)
    primary = RunnerTimeoutError("fixture timeout")
    primary._process_result = ProcessResult(
        -9, "prefix", "stderr", stdout_bytes=b"prefix", stderr_bytes=b"stderr"
    )
    secondary = OSError("fixture capture failure")
    original = module.atomic_write_bytes
    calls = []

    def process(*args, **kwargs):
        calls.append(args)
        raise primary

    def write(path, content, **kwargs):
        if path.name == "run.stdout":
            raise secondary
        return original(path, content, **kwargs)

    monkeypatch.setattr(module, "run_process", process)
    monkeypatch.setattr(module, "atomic_write_bytes", write)
    with pytest.raises(HookError) as caught:
        OpenCodeRunner().run(_request(context))
    assert caught.value.__cause__ is primary
    assert caught.value.secondary_error is secondary
    assert len(calls) == 1


def test_create_runner_exposes_resume_finalize_and_transcript_capabilities() -> None:
    runner = create_runner()

    assert isinstance(runner, OpenCodeRunner)
    assert runner.capabilities.supports_resume
    assert runner.capabilities.supports_finalize
    assert runner.capabilities.supports_transcript


def test_project_request_uses_effective_model_variant_and_rooted_read_policy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return ProcessResult(
            returncode=0,
            stdout='{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            stderr="",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    result = OpenCodeRunner().run(_request(context, project_access="project"))

    assert result.final_text == "[]"
    assert context.logger.llm_calls == []
    argv = captured["argv"]
    assert argv[0:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        "ai-push-hooks-readonly",
        "--pure",
        "--format",
        "json",
    ]
    assert ["--model", "profile/model"] == argv[7:9]
    assert "--variant" in argv and "profile-variant" in argv
    assert captured["cwd"] == repo.resolve()
    import json

    security = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    permissions = security["agent"]["ai-push-hooks-readonly"]["permission"]
    assert permissions["edit"] == "deny"
    for tool in ("read", "list", "glob", "grep"):
        assert permissions[tool] == "allow"
    for tool in (
        "bash",
        "task",
        "webfetch",
        "websearch",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[tool] == "deny"
    assert security["plugin"] == []
    assert security["mcp"] == {}


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("not json\n", RunnerProtocolError),
        ('{"type":"step_start"}\n', RunnerMissingOutputError),
    ],
)
def test_malformed_or_incomplete_output_fails_closed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: type[Exception],
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, stdout, ""),
    )

    with pytest.raises(expected):
        OpenCodeRunner().run(_request(context))


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (
            '{"type":"session.created","sessionID":"session-1"}\nnot json\n',
            RunnerProtocolError,
        ),
        (
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"step_start"}\n',
            RunnerMissingOutputError,
        ),
        (
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"error","error":"model-generated-secret"}\n',
            RunnerProtocolError,
        ),
    ],
)
def test_protocol_failures_preserve_announced_session_identity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: type[Exception],
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, stdout, ""),
    )

    with pytest.raises(expected) as error:
        OpenCodeRunner().run(_request(context))

    assert getattr(error.value, "session_id", None) == "session-1"
    assert "model-generated-secret" not in str(error.value)


def test_resumed_protocol_failure_uses_the_exact_known_session_fallback(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(0, '{"type":"step_start"}\n', ""),
    )

    with pytest.raises(RunnerMissingOutputError) as error:
        OpenCodeRunner().run(
            _request(context, session_id="known-session", resume_session=True)
        )

    assert getattr(error.value, "session_id", None) == "known-session"


def test_truncated_process_output_fails_closed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
            stdout_truncated=True,
        ),
    )

    with pytest.raises(RunnerProtocolError, match="truncated") as error:
        OpenCodeRunner().run(_request(context))
    assert getattr(error.value, "session_id", None) is None


def test_opencode_materializes_exact_artifact_snapshots_and_cleans_them(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    source = context.run_dir / "input.txt"
    source.write_text("source-before-request\n", encoding="utf-8")
    request = _request(
        context,
        artifacts=(
            RunnerArtifact("logical/input.txt", "snapshot-one\n", source),
            RunnerArtifact("../second.txt", "snapshot-two\n"),
        ),
    )
    source.write_text("source-mutated-after-request\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = list(argv)
        attachment_paths = [
            pathlib.Path(argv[index + 1])
            for index, value in enumerate(argv)
            if value == "--file"
        ]
        captured["attachment_paths"] = attachment_paths
        captured["attachment_contents"] = [
            path.read_text(encoding="utf-8") for path in attachment_paths
        ]
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"snapshot-session"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    result = OpenCodeRunner().run(request)

    paths = captured["attachment_paths"]
    assert captured["attachment_contents"] == ["snapshot-one\n", "snapshot-two\n"]
    assert all(path.is_relative_to(context.run_dir) for path in paths)
    assert all(not path.exists() for path in paths)
    assert captured["argv"][-1] == request.instruction
    assert "snapshot-one" not in captured["argv"][-1]
    assert result.final_text == "[]"


def test_opencode_still_rejects_symlinked_or_escaping_source_artifacts(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    external = tmp_path / "external.txt"
    external.write_text("outside\n", encoding="utf-8")
    linked = context.run_dir / "linked.txt"
    linked.symlink_to(external)

    with pytest.raises(HookError, match="symlink"):
        OpenCodeRunner().run(
            _request(
                context, artifacts=(RunnerArtifact("linked.txt", "snapshot", linked),)
            )
        )
    with pytest.raises(HookError, match="not a hook-owned artifact"):
        OpenCodeRunner().run(
            _request(
                context,
                artifacts=(RunnerArtifact("external.txt", "snapshot", external),),
            )
        )


def test_timeout_preserves_session_for_lifecycle_cleanup_and_attachment_cleanup(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []
    attachment_paths: list[pathlib.Path] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "run":
            attachment_paths.extend(
                pathlib.Path(argv[index + 1])
                for index, value in enumerate(argv)
                if value == "--file"
            )
            error = RunnerTimeoutError("runner process exceeded its timeout")
            error._process_result = ProcessResult(  # type: ignore[attr-defined]
                -9,
                '{"type":"session.created","sessionID":"timeout-session"}\n',
                "secret=do-not-leak",
            )
            raise error
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    request = _request(context)

    with pytest.raises(RunnerTimeoutError) as error:
        OpenCodeRunner().run(request)

    assert getattr(error.value, "session_id", None) == "timeout-session"
    assert "do-not-leak" not in str(error.value)
    assert all(not path.exists() for path in attachment_paths)

    failed = RunnerResult(
        "", 1, "", "", SessionMetadata("timeout-session", "persisted", True)
    )
    finalized = OpenCodeRunner().finalize(request, failed)
    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert [call[1:3] for call in calls] == [
        ["run", "--agent"],
        ["export", "timeout-session"],
        ["session", "delete"],
    ]


def test_successful_run_blocks_on_attachment_cleanup_failure_without_raw_details(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"type":"session.created","sessionID":"cleanup-session"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        ),
    )

    import shutil

    original_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if "opencode-attachments-" in str(path):
            raise OSError("cleanup-secret /private/temporary/path")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.shutil.rmtree", fail_cleanup
    )

    with pytest.raises(RunnerError) as error:
        OpenCodeRunner().run(_request(context))

    assert getattr(error.value, "session_id", None) == "cleanup-session"
    assert "cleanup-secret" not in str(error.value)
    assert "/private/temporary/path" not in str(error.value)
    assert "cleanup-secret" not in repr(error.value)


def test_invocation_error_survives_attachment_cleanup_failure_with_session_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    invocation_error = RunnerProtocolError("invocation failed")
    invocation_error.session_id = "invocation-session"  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(invocation_error),
    )

    import shutil

    original_rmtree = shutil.rmtree

    def fail_cleanup(path, *args, **kwargs):
        if "opencode-attachments-" in str(path):
            raise OSError("cleanup-secret /private/temporary/path")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.shutil.rmtree", fail_cleanup
    )

    with pytest.raises(RunnerProtocolError) as error:
        OpenCodeRunner().run(_request(context))

    assert error.value is invocation_error
    assert getattr(error.value, "session_id", None) == "invocation-session"
    assert "cleanup-secret" not in str(error.value)
    assert "/private/temporary/path" not in str(error.value)


def test_finalize_reports_deleted_session_and_private_transcript(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(0, '{"session":"session-1"}\n', "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-1", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.resumable is False
    assert finalized.session.transcript is not None
    assert pathlib.Path(finalized.session.transcript).is_relative_to(context.git_dir)
    assert (
        pathlib.Path(finalized.session.transcript).read_text(encoding="utf-8")
        == '{"session":"session-1"}\n'
    )
    assert [call[1:3] for call in calls] == [
        ["export", "session-1"],
        ["session", "delete"],
    ]


def test_finalize_never_persists_truncated_export(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        logging=replace(config.logging, capture_llm_transcript=True),
        llm=replace(config.llm, delete_session_after_run=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"

    def fake_run_process(argv, **kwargs):
        if argv[1] == "export":
            return ProcessResult(
                0,
                '{"session":"session-truncated"}\n',
                "",
                stdout_truncated=True,
            )
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-truncated", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert list((context.git_dir / "ai-push-hooks" / "transcripts").iterdir()) == []


def test_readonly_run_uses_private_isolation_and_provider_auth_only(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    host_home = tmp_path / "host-home"
    host_config = tmp_path / "host-config"
    (host_home / ".opencode").mkdir(parents=True)
    (host_home / ".opencode" / "opencode.json").write_text(
        '{"share":"auto","mcp":{"unsafe":{"type":"local","command":["sh"]}}}',
        encoding="utf-8",
    )
    (host_config / "opencode").mkdir(parents=True)
    (host_config / "opencode" / "opencode.json").write_text(
        '{"share":"auto","plugin":["unsafe-plugin"]}', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(host_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "opencode-data"))
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "malicious.json"))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"share":"auto"}')
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://secret@proxy.invalid")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        captured["cwd_entries"] = list(kwargs["cwd"].iterdir())
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"session-1"}\n'
            '{"type":"text","part":{"text":"[]"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    OpenCodeRunner().run(_request(context, artifacts=()))

    assert captured["argv"][:7] == [
        "/usr/local/bin/opencode",
        "run",
        "--agent",
        "ai-push-hooks-readonly",
        "--pure",
        "--format",
        "json",
    ]
    env = captured["env"]
    assert env["OPENCODE_PURE"] == "true"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "true"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
    assert env["OPENCODE_DISABLE_LSP_DOWNLOAD"] == "true"
    assert env["OPENCODE_DISABLE_SHARE"] == "true"
    assert "OPENCODE_CONFIG" not in env
    assert pathlib.Path(env["HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_CONFIG_HOME"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["OPENCODE_CONFIG_DIR"]).is_relative_to(context.run_dir)
    assert pathlib.Path(env["XDG_DATA_HOME"]) == (tmp_path / "opencode-data").resolve()
    assert env["OPENAI_API_KEY"] == "provider-key"
    assert "HTTPS_PROXY" not in env
    assert "UNRELATED_SECRET" not in env
    assert captured["cwd_entries"] == []
    permissions = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"][
        "ai-push-hooks-readonly"
    ]["permission"]
    assert permissions["*"] == "deny"
    for tool in (
        "read",
        "glob",
        "grep",
        "list",
        "edit",
        "bash",
        "task",
        "external_directory",
        "webfetch",
        "websearch",
        "skill",
        "todowrite",
        "question",
    ):
        assert permissions[tool] == "deny"


def test_concurrent_opencode_isolation_initialization_shares_fresh_parent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    isolation_parent = context.run_dir / "opencode-isolation"
    assert not isolation_parent.exists()
    mkdir_barrier = threading.Barrier(2)
    original_mkdir = pathlib.Path.mkdir

    def synchronize_shared_parent(self, *args, **kwargs):
        if self == isolation_parent:
            mkdir_barrier.wait(timeout=5)
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", synchronize_shared_parent)
    results: list[dict[str, str | None]] = []
    errors: list[BaseException] = []

    def initialize(stage_name: str) -> None:
        try:
            results.append(opencode_isolation_env(context, {}, stage_name))
        except BaseException as error:  # pragma: no cover - diagnostic path
            errors.append(error)

    threads = [
        threading.Thread(target=initialize, args=(stage_name,))
        for stage_name in ("docs.query", "docs.collect")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert sorted(pathlib.Path(result["HOME"]).parent.name for result in results) == [
        "docs.collect",
        "docs.query",
    ]
    assert isolation_parent.is_dir()


def test_apply_run_uses_allowlisted_edit_permissions_for_staging(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    staging = tmp_path / "Temp Root" / "OpenCode-Staging"
    staging.mkdir(parents=True)
    staging_link = tmp_path / "staging-link"
    staging_link.symlink_to(staging, target_is_directory=True)
    captured: dict[str, object] = {}

    def fake_run_process(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        pathlib.Path(kwargs["cwd"]).joinpath("README.md").write_text(
            "updated in isolated staging\n", encoding="utf-8"
        )
        return ProcessResult(
            0,
            '{"type":"session.created","sessionID":"apply-session"}\n'
            '{"type":"text","part":{"text":"done"}}\n',
            "",
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    OpenCodeRunner().run(
        _request(
            context,
            mode="apply",
            artifacts=(),
            cwd=staging_link,
            allow_paths=("README.md", "docs/**/*.md"),
        )
    )

    assert captured["argv"][2:6] == [
        "--agent",
        "ai-push-hooks-apply",
        "--pure",
        "--format",
    ]
    assert (staging / "README.md").read_text(
        encoding="utf-8"
    ) == "updated in isolated staging\n"
    permissions = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])["agent"][
        "ai-push-hooks-apply"
    ]["permission"]
    prefix = staging.resolve().relative_to(pathlib.Path(staging.anchor)).as_posix()
    assert permissions["read"] == "allow"
    assert permissions["edit"] == {
        "*": "deny",
        f"{prefix}/README.md": "allow",
        f"{prefix}/docs/*.md": "allow",
        f"{prefix}/docs/**/*.md": "allow",
        f"{prefix}/.git": "deny",
        f"{prefix}/.git/**": "deny",
    }
    assert "write" not in permissions
    for tool in ("bash", "task", "external_directory", "webfetch", "websearch"):
        assert permissions[tool] == "deny"


@pytest.mark.parametrize("directory_kind", ["missing", "file"])
def test_apply_run_rejects_missing_or_non_directory_cwd(
    tmp_path: pathlib.Path, directory_kind: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    cwd = tmp_path / "not-a-directory"
    if directory_kind == "file":
        cwd.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(RunnerContractError, match="existing directory"):
        OpenCodeRunner().run(
            _request(
                context, mode="apply", artifacts=(), cwd=cwd, allow_paths=("README.md",)
            )
        )


def test_apply_run_rejects_repository_cwd_as_missing_isolated_staging(
    tmp_path: pathlib.Path,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)

    with pytest.raises(RunnerContractError, match="isolated staging directory"):
        OpenCodeRunner().run(
            _request(
                context,
                mode="apply",
                artifacts=(),
                cwd=context.repo_root,
                allow_paths=("README.md",),
            )
        )


@pytest.mark.parametrize(
    ("return_code", "stdout"),
    [(1, "export failed\n"), (0, "")],
)
def test_finalize_warns_and_deletes_when_export_has_no_transcript(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    return_code: int,
    stdout: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(return_code, stdout, "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    result = RunnerResult(
        "[]", 0, "", "", SessionMetadata("session-failed", "persisted", True)
    )

    finalized = OpenCodeRunner().finalize(_request(context), result)

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert [argv[1:3] for argv in calls] == [
        ["export", "session-failed"],
        ["session", "delete"],
    ]
    assert (
        "Could not capture or delete the OpenCode session cleanly"
        in capsys.readouterr().err
    )


def test_finalize_export_process_exception_still_deletes_session(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    warning_log = tmp_path / "warning.jsonl"
    context.logger = HookLogger(warning_log)
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            raise subprocess.TimeoutExpired(
                ["opencode", "export", "session-exception", "token=paid-secret"], 1
            )
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )

    finalized = OpenCodeRunner().finalize(
        _request(context),
        RunnerResult(
            "[]", 0, "", "", SessionMetadata("session-exception", "persisted", True)
        ),
    )

    assert finalized.session is not None
    assert finalized.session.state == "deleted"
    assert finalized.session.transcript is None
    assert [argv[1:3] for argv in calls] == [
        ["export", "session-exception"],
        ["session", "delete"],
    ]
    warning = capsys.readouterr().err
    assert "Could not capture or delete the OpenCode session cleanly" in warning
    assert "paid-secret" not in warning
    record = json.loads(warning_log.read_text(encoding="utf-8").strip())
    assert record["reason"] == "TimeoutExpired"
    assert record["session_id"] == "session-exception"
    assert "paid-secret" not in json.dumps(record)


def test_finalize_deletes_when_transcript_write_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config,
        llm=replace(config.llm, delete_session_after_run=True),
        logging=replace(config.logging, capture_llm_transcript=True),
    )
    context = build_context(repo, config)
    context.opencode_executable = "/usr/local/bin/opencode"
    calls: list[list[str]] = []

    def fake_run_process(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "export":
            return ProcessResult(0, '{"session":"session-write"}\n', "")
        return ProcessResult(0, "", "")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", fake_run_process
    )
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.write_text_no_follow",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("simulated transcript failure")
        ),
    )

    OpenCodeRunner().finalize(
        _request(context),
        RunnerResult(
            "[]", 0, "", "", SessionMetadata("session-write", "persisted", True)
        ),
    )

    assert [argv[1:3] for argv in calls] == [
        ["export", "session-write"],
        ["session", "delete"],
    ]
    assert (
        "Could not capture or delete the OpenCode session cleanly"
        in capsys.readouterr().err
    )
