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


@pytest.mark.parametrize("generation", ["legacy", "v2"])
def test_successful_run_blocks_on_attachment_cleanup_failure_without_raw_details(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config, llm=replace(config.llm, opencode_cli_generation=generation)
    )
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
        OpenCodeRunner().run(
            _request(context, variant="" if generation == "v2" else "profile-variant")
        )

    assert getattr(error.value, "session_id", None) == "cleanup-session"
    assert "cleanup-secret" not in str(error.value)
    assert "/private/temporary/path" not in str(error.value)
    assert "cleanup-secret" not in repr(error.value)


@pytest.mark.parametrize("generation", ["legacy", "v2"])
def test_invocation_error_survives_attachment_cleanup_failure_with_session_identity(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config, llm=replace(config.llm, opencode_cli_generation=generation)
    )
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
        OpenCodeRunner().run(
            _request(context, variant="" if generation == "v2" else "profile-variant")
        )

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


# CLI-generation proofs exercise the real adapter and host policy. Only the
# process boundary is fake; these are not native parser or isolation proofs.
def _cli_generation_context(tmp_path, generation):
    repo = init_repo(tmp_path, branch="feature/cli-generation")
    config, _ = load_config(repo)
    if generation is not None:
        config = replace(
            config, llm=replace(config.llm, opencode_cli_generation=generation)
        )
    context = build_context(repo, config)
    context.opencode_executable = "/fake-only/selected-opencode"
    return context


@pytest.mark.parametrize("generation", ["legacy", "v2"])
@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "nonzero-export",
        "empty-export",
        "truncated-export",
        "export-exception",
        "delete-exception",
        "nonzero-delete",
    ],
)
def test_opencode_cli_generation_finalize_only_exact_argv_and_best_effort(
    tmp_path, monkeypatch, capsys, generation, failure
):
    context = _cli_generation_context(tmp_path, generation)
    request = _request(context, variant="", artifacts=())
    calls = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        action = "export" if "export" in argv else "delete"
        assert argv == _cli_generation_lifecycle(
            context.opencode_executable, generation, action, "known-sid"
        )
        if failure == action + "-exception":
            raise RunnerTimeoutError("synthetic lifecycle timeout")
        if failure == "nonzero-" + action:
            return ProcessResult(3, "", "")
        return ProcessResult(
            0,
            ""
            if failure == "empty-export"
            else '{"info":{"id":"known-sid"},"messages":[]}',
            "",
            stdout_truncated=failure == "truncated-export" and action == "export",
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    finalized = OpenCodeRunner().finalize(
        request,
        RunnerResult("[]", 0, "", "", SessionMetadata("known-sid", "persisted", True)),
    )
    assert calls == [
        _cli_generation_lifecycle(
            context.opencode_executable, generation, action, "known-sid"
        )
        for action in ("export", "delete")
    ]
    delete_failed = failure in {"delete-exception", "nonzero-delete"}
    assert finalized.session.state == ("persisted" if delete_failed else "deleted")
    assert finalized.session.resumable is delete_failed
    assert (finalized.session.transcript is None) == (
        failure not in {"none", "delete-exception", "nonzero-delete"}
    )
    warning = capsys.readouterr().err
    assert ("Could not capture or delete" in warning) == (failure != "none")


@pytest.mark.parametrize("generation", ["legacy", "v2"])
@pytest.mark.parametrize(
    "failure", ["nonzero", "malformed", "missing", "truncated", "timeout"]
)
def test_opencode_cli_generation_run_failure_session_and_cleanup(
    tmp_path, monkeypatch, generation, failure
):
    context = _cli_generation_context(tmp_path, generation)
    request = _request(context, variant="", resume_session=True, session_id="known-sid")
    calls = []
    paths = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        paths.extend(
            pathlib.Path(argv[i + 1]) for i, item in enumerate(argv) if item == "--file"
        )
        assert argv[4] == ("--standalone" if generation == "v2" else "--pure")
        prefix = '{"type":"session.created","sessionID":"known-sid"}\n'
        if failure == "timeout":
            error = RunnerTimeoutError("synthetic timeout")
            error._process_result = ProcessResult(-9, prefix, "")
            raise error
        stdout = prefix + (
            "not-json"
            if failure in {"malformed", "nonzero"}
            else '{"type":"step_start"}'
        )
        return ProcessResult(
            7 if failure == "nonzero" else 0,
            stdout,
            "",
            stdout_truncated=failure == "truncated",
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    expected = (
        RunnerTimeoutError
        if failure == "timeout"
        else RunnerMissingOutputError
        if failure == "missing"
        else RunnerError
        if failure == "nonzero"
        else RunnerProtocolError
    )
    with pytest.raises(expected) as error:
        OpenCodeRunner().run(request)
    assert getattr(error.value, "session_id", None) == "known-sid"
    assert len(calls) == 1
    assert paths and all(not path.exists() for path in paths)


def _cli_generation_lifecycle(executable, generation, action, sid):
    if generation == "v2":
        return [executable, "session", action, "--standalone", sid]
    if action == "export":
        return [executable, "export", sid, "--pure"]
    return [executable, "session", "delete", sid, "--pure"]


def test_opencode_cli_generation_shared_adapter_keeps_request_local_selection(
    tmp_path, monkeypatch
):
    contexts = [
        _cli_generation_context(tmp_path / generation, generation)
        for generation in ("legacy", "v2")
    ]
    requests = [_request(context, variant="", artifacts=()) for context in contexts]
    runner = OpenCodeRunner()
    calls = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        index = 0 if "--pure" in argv else 1
        assert ("--standalone" in argv) is (index == 1)
        assert kwargs["env"]["HOME"].startswith(str(contexts[index].run_dir))
        if argv[1] == "run":
            return ProcessResult(
                0,
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": f"sid-{index}",
                        "part": {"text": "[]"},
                    }
                ),
                "",
            )
        action = "export" if "export" in argv else "delete"
        assert argv == _cli_generation_lifecycle(
            contexts[index].opencode_executable,
            ("legacy", "v2")[index],
            action,
            f"sid-{index}",
        )
        return ProcessResult(0, '{"info":{},"messages":[]}', "")

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    results = [runner.run(request) for request in requests]
    # Finalizing legacy AFTER a V2 run must not inherit another invocation's
    # generation, stage root or identity on the singleton-compatible adapter.
    for request, result in zip(requests, results):
        assert runner.finalize(request, result).session.state == "deleted"
    assert len(calls) == 6


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
@pytest.mark.parametrize("mode", ["artifacts", "project", "apply"])
@pytest.mark.parametrize("resumed", [False, True])
def test_opencode_cli_generation_exact_run_and_unchanged_host_policy(
    tmp_path, monkeypatch, generation, mode, resumed
):
    import os
    import stat

    context = _cli_generation_context(tmp_path, generation)
    staging = tmp_path / "non-vcs-stage"
    staging.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "fake-provider-data"))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-only")
    monkeypatch.setenv("OPENCODE_CONFIG", "/fake-only/unwanted.json")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-filter-me")
    request = _request(
        context,
        variant="" if generation == "v2" else "profile-variant",
        instruction="--leading-dash prompt\nexact",
        mode="apply" if mode == "apply" else "ask",
        project_access="project" if mode == "project" else "artifacts",
        cwd=staging if mode == "apply" else context.repo_root,
        allow_paths=("README.md", "docs/**/*.md") if mode == "apply" else (),
        model="opaque/model#native",
        resume_session=resumed,
        session_id="opaque-sid" if resumed else None,
        artifacts=(
            RunnerArtifact("first.txt", "snapshot-1\n"),
            RunnerArtifact("second.txt", "snapshot-2\n"),
        ),
    )
    calls = []
    environments = []
    attachment_paths = []

    def fake(argv, **kwargs):
        calls.append(list(argv))
        environments.append(dict(kwargs["env"]))
        paths = [
            pathlib.Path(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--file"
        ]
        attachment_paths.extend(paths)
        assert len(paths) == 2
        assert [path.name for path in paths] == ["0001-first.txt", "0002-second.txt"]
        assert [path.read_bytes() for path in paths] == [
            b"snapshot-1\n",
            b"snapshot-2\n",
        ]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
        agent = "ai-push-hooks-apply" if mode == "apply" else "ai-push-hooks-readonly"
        expected = [
            context.opencode_executable,
            "run",
            "--agent",
            agent,
            "--standalone" if generation == "v2" else "--pure",
            "--format",
            "json",
            "--model",
            "opaque/model#native",
        ]
        if generation != "v2":
            expected += ["--variant", "profile-variant"]
        expected += (
            ["--session", "opaque-sid"]
            if resumed
            else ["--title", "ai-push-hooks test-run docs.query"]
        )
        expected += [
            "--file",
            str(paths[0]),
            "--file",
            str(paths[1]),
            "--",
            request.instruction,
        ]
        assert argv == expected
        assert kwargs["timeout_seconds"] == 3
        if mode == "artifacts":
            assert kwargs["cwd"] != context.repo_root
            assert list(kwargs["cwd"].iterdir()) == []
        else:
            assert (
                kwargs["cwd"]
                == (staging if mode == "apply" else context.repo_root).resolve()
            )
        return ProcessResult(
            0, '{"type":"text","sessionID":"opaque-sid","part":{"text":"[]"}}\n', ""
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    runner = OpenCodeRunner()
    result = runner.run(request)
    assert result.session.session_id == "opaque-sid"
    assert len(calls) == 1
    assert all(not path.exists() for path in attachment_paths)
    env = environments[0]
    isolation = context.run_dir / "opencode-isolation" / "docs.query"
    roots = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "OPENCODE_CONFIG_DIR": "config",
    }
    for key, leaf in roots.items():
        assert env[key] == str(isolation / leaf)
        assert stat.S_IMODE(pathlib.Path(env[key]).stat().st_mode) == 0o700
    permissions = {
        tool: "deny"
        for tool in (
            "*",
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
            "lsp",
            "skill",
            "todowrite",
            "question",
            "doom_loop",
        )
    }
    if mode == "project":
        permissions.update({tool: "allow" for tool in ("read", "list", "glob", "grep")})
    if mode == "apply":
        prefix = staging.relative_to(staging.anchor).as_posix()
        permissions["read"] = "allow"
        permissions["edit"] = {
            "*": "deny",
            f"{prefix}/README.md": "allow",
            f"{prefix}/docs/**/*.md": "allow",
            f"{prefix}/docs/*.md": "allow",
            f"{prefix}/.git": "deny",
            f"{prefix}/.git/**": "deny",
        }
    agent = "ai-push-hooks-apply" if mode == "apply" else "ai-push-hooks-readonly"
    policy = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "mcp": {},
        "share": "disabled",
        "instructions": [],
        "formatter": False,
        "lsp": False,
        "command": {},
        "agent": {
            agent: {
                "mode": "primary",
                "description": "Path-restricted ai-push-hooks apply agent"
                if mode == "apply"
                else "Read-only ai-push-hooks analysis agent",
                "permission": permissions,
            }
        },
    }
    assert env["OPENCODE_CONFIG_CONTENT"] == json.dumps(policy, ensure_ascii=True)
    expected_env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {"PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"}
        or key.startswith(
            (
                "ANTHROPIC_",
                "AWS_",
                "AZURE_",
                "COHERE_",
                "DEEPSEEK_",
                "GEMINI_",
                "GOOGLE_",
                "GROQ_",
                "MISTRAL_",
                "OPENAI_",
                "OPENROUTER_",
                "VERTEX_",
                "XAI_",
            )
        )
    }
    expected_env.update({key: str(isolation / leaf) for key, leaf in roots.items()})
    expected_env.update(
        XDG_DATA_HOME=str(tmp_path / "fake-provider-data"),
        OPENCODE_CONFIG_CONTENT=json.dumps(policy, ensure_ascii=True),
    )
    for key in (
        "OPENCODE_PURE",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
        "OPENCODE_DISABLE_SHARE",
        "OPENCODE_DISABLE_AUTOUPDATE",
    ):
        expected_env[key] = "true"
    assert env == expected_env


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
@pytest.mark.parametrize(
    "capture,delete", [(False, False), (False, True), (True, False), (True, True)]
)
def test_opencode_cli_generation_exact_lifecycle_and_stage_continuity(
    tmp_path, monkeypatch, generation, capture, delete
):
    context = _cli_generation_context(tmp_path, generation)
    context.config = replace(
        context.config,
        logging=replace(context.config.logging, capture_llm_transcript=capture),
        llm=replace(context.config.llm, delete_session_after_run=delete),
    )
    request = _request(context, variant="", artifacts=())
    calls = []
    envs = []
    export = '{"info":{"id":"opaque-sid"},"messages":[]}'

    def fake(argv, **kwargs):
        calls.append(list(argv))
        envs.append(dict(kwargs["env"]))
        assert kwargs["timeout_seconds"] == request.timeout_seconds
        assert list(kwargs["cwd"].iterdir()) == []
        if argv[1] == "run":
            assert argv == [
                context.opencode_executable,
                "run",
                "--agent",
                "ai-push-hooks-readonly",
                "--standalone" if generation == "v2" else "--pure",
                "--format",
                "json",
                "--model",
                "profile/model",
                "--title",
                "ai-push-hooks test-run docs.query",
                "--",
                request.instruction,
            ]
            return ProcessResult(
                0, '{"type":"text","sessionID":"opaque-sid","part":{"text":"[]"}}', ""
            )
        action = "export" if "export" in argv else "delete"
        assert argv == _cli_generation_lifecycle(
            context.opencode_executable, generation, action, "opaque-sid"
        )
        return ProcessResult(0, export if action == "export" else "deleted", "")

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    runner = OpenCodeRunner()
    finalized = runner.finalize(request, runner.run(request))
    assert calls[1:] == [
        _cli_generation_lifecycle(
            context.opencode_executable, generation, action, "opaque-sid"
        )
        for action, enabled in (("export", capture), ("delete", delete))
        if enabled
    ]
    non_agent = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "mcp": {},
        "share": "disabled",
        "instructions": [],
        "formatter": False,
        "lsp": False,
        "command": {},
        "permission": {"*": "deny"},
    }
    for env in envs[1:]:
        expected = dict(
            envs[0], OPENCODE_CONFIG_CONTENT=json.dumps(non_agent, ensure_ascii=True)
        )
        assert env == expected
    assert finalized.session.session_id == "opaque-sid"
    assert finalized.session.state == ("deleted" if delete else "persisted")
    assert finalized.session.resumable is not delete
    if capture:
        transcript = pathlib.Path(finalized.session.transcript)
        assert transcript.read_text() == export + "\n"
        assert transcript.stat().st_mode & 0o777 == 0o600
    else:
        assert finalized.session.transcript is None


@pytest.mark.parametrize("entry", ["run", "finalize", "finalize-without-session"])
@pytest.mark.parametrize("invalid", ["generation", "variant"])
def test_opencode_cli_generation_direct_invalid_options_launch_nothing(
    tmp_path, monkeypatch, entry, invalid
):
    from types import SimpleNamespace

    context = _cli_generation_context(tmp_path, "v2")
    if invalid == "generation":
        context.config = replace(
            context.config,
            llm=SimpleNamespace(
                **dict(context.config.llm.__dict__, opencode_cli_generation="V2")
            ),
        )
    context.opencode_executable = None
    calls = []
    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process",
        lambda *args, **kwargs: calls.append(args),
    )
    request = _request(
        context,
        variant="high" if invalid == "variant" else "",
        artifacts=(RunnerArtifact("unreachable", "x", tmp_path / "missing"),),
    )
    result = RunnerResult(
        "[]",
        0,
        "",
        "",
        None
        if entry == "finalize-without-session"
        else SessionMetadata("known", "persisted", True),
    )
    with pytest.raises(RunnerContractError, match="generation|variant"):
        if entry == "run":
            OpenCodeRunner().run(request)
        else:
            OpenCodeRunner().finalize(request, result)
    assert calls == []
    assert not (context.run_dir / "opencode-isolation").exists()
    assert not list(context.run_dir.glob("opencode-attachments-*"))


@pytest.mark.parametrize(
    "generation,count,accepted",
    [("v2", 101, False), ("v2", 100, True), ("legacy", 101, True)],
)
def test_opencode_cli_generation_attachment_limit_before_materialization(
    tmp_path, monkeypatch, generation, count, accepted
):
    context = _cli_generation_context(tmp_path, generation)
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        assert argv.count("--file") == count
        assert argv[-2:] == ["--", "Return a JSON array."]
        return ProcessResult(0, '{"type":"text","part":{"text":"[]"}}', "")

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", fake)
    request = _request(
        context,
        variant="",
        artifacts=tuple(RunnerArtifact(f"{i}.txt", "x") for i in range(count)),
    )
    if accepted:
        assert OpenCodeRunner().run(request).final_text == "[]"
        assert len(calls) == 1
    else:
        context.opencode_executable = None
        with pytest.raises(RunnerContractError, match="100"):
            OpenCodeRunner().run(request)
        assert calls == []
        assert not (context.run_dir / "opencode-isolation").exists()
    assert not list(context.run_dir.glob("opencode-attachments-*"))


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
