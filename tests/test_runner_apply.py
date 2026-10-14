from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from dataclasses import replace

import pytest

import ai_push_hooks.executors.apply as apply_executor
from ai_push_hooks.config import load_config
from ai_push_hooks.executors.apply import run_apply_step
from ai_push_hooks.executors.runners.contracts import RunnerResult
from ai_push_hooks.types import HookError, ModuleRuntimeState, RunnerProfile

from .conftest import build_context, init_repo


def _issues_artifact(context) -> pathlib.Path:
    path = context.run_dir / "issues.json"
    path.write_text('[{"file":"README.md","description":"stale"}]\n', encoding="utf-8")
    return path


def _empty_issues_artifact(context) -> pathlib.Path:
    path = context.run_dir / "issues.json"
    path.write_text("[]\n", encoding="utf-8")
    return path


def _runner_config(config, *, project_access: str):
    profile = RunnerProfile(
        name="apply-runner",
        type="command",
        model="test-model",
        project_access=project_access,
        command=("test-runner",),
    )
    return replace(config, runners={"apply-runner": profile})


def _run(context, step, input_path):
    # Direct executor tests supply one synthetic artifact instead of the full
    # engine-resolved input list.
    step = replace(step, inputs=("issues.json",))
    return run_apply_step(
        context,
        ModuleRuntimeState(module=context.config.modules["docs"]),
        step,
        "apply prompt",
        [input_path],
        "docs.apply",
    )


def _success() -> RunnerResult:
    return RunnerResult(final_text="", returncode=0, stdout="", stderr="")


@pytest.mark.parametrize("failure", ["export", "delete"])
def test_strict_actual_apply_preserves_policy_and_checkout(
    tmp_path, monkeypatch, failure
):
    import json
    from ai_push_hooks.executors.runners import ProcessResult
    from ai_push_hooks.types import RequiredRetentionError

    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = replace(
        config, logging=replace(config.logging, require_opencode_retention=True)
    )
    context = build_context(repo, config)
    context.opencode_executable = "/fixture/opencode"
    context.run_dir = repo / ".git" / "ai-push-hooks" / "runs" / "strict-apply"
    context.run_dir.mkdir(parents=True)
    before = (repo / "README.md").read_bytes()
    completions = []
    calls = []
    monkeypatch.setattr(
        context.logger,
        "llm_complete",
        lambda *args, **kwargs: completions.append(kwargs),
    )

    def process(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "run":
            assert kwargs["cwd"] != repo
            (kwargs["cwd"] / "README.md").write_text("staged change\n")
            payload = {
                "type": "text",
                "sessionID": "s1",
                "part": {
                    "type": "text",
                    "sessionID": "s1",
                    "messageID": "msg_1",
                    "text": "done",
                },
            }
        else:
            payload = {
                "info": {"id": "s1"},
                "messages": [
                    {
                        "id": "msg_1",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    }
                ],
            }
        raw = (
            b"{}"
            if failure == "export" and argv[1] == "export"
            else json.dumps(payload).encode()
            if argv[1] != "session"
            else b""
        )
        return ProcessResult(
            1 if argv[1] == "session" else 0, raw.decode(), "", stdout_bytes=raw
        )

    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    with pytest.raises(RequiredRetentionError) as caught:
        _run(
            context,
            replace(config.modules["docs"].steps[3], auto_commit=False),
            _issues_artifact(context),
        )
    assert isinstance(caught.value.__cause__, RequiredRetentionError)
    assert (repo / "README.md").read_bytes() == before
    assert calls == (
        ["run", "export"] if failure == "export" else ["run", "export", "session"]
    )
    assert completions[-1]["session_state"] == "unknown"
    assert completions[-1]["resumable"] is False


@pytest.mark.parametrize("allow_error", ["true", "false"])
def test_strict_actual_apply_hook_blocks_late_fail_open(
    tmp_path, monkeypatch, allow_error
):
    import json
    from ai_push_hooks.hook import run_hook
    from ai_push_hooks.executors.runners import ProcessResult
    from ai_push_hooks.types import RequiredRetentionError

    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / "ai-push-hooks.toml").write_text("""
[logging]
require_opencode_retention = true
[workflow]
modules = ["check"]
[[modules.check.steps]]
id = "fix"
type = "apply"
prompt = "Update README"
allow_paths = ["README.md"]
""")
    before = (repo / "README.md").read_bytes()
    calls = []

    def process(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "run":
            (kwargs["cwd"] / "README.md").write_text("staged change\n")
            payload = {
                "type": "text",
                "sessionID": "s1",
                "part": {
                    "type": "text",
                    "sessionID": "s1",
                    "messageID": "msg_1",
                    "text": "done",
                },
            }
        else:
            payload = {
                "info": {"id": "s1"},
                "messages": [
                    {
                        "id": "msg_1",
                        "type": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    }
                ],
            }
        if argv[1] == "session":
            # Change only after effective config validation, exercising outer catch.
            monkeypatch.setenv("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR", allow_error)
        raw = json.dumps(payload).encode() if argv[1] != "session" else b""
        return ProcessResult(
            1 if argv[1] == "session" else 0, raw.decode(), "", stdout_bytes=raw
        )

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.resolve_opencode_executable",
        lambda: "/fixture/opencode",
    )
    monkeypatch.setattr("ai_push_hooks.executors.runners.opencode.run_process", process)
    with pytest.raises(RequiredRetentionError):
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        run_hook(
            cwd=repo,
            stdin_lines=[
                f"refs/heads/feature/docs {sha} refs/heads/feature/docs {'0' * 40}"
            ],
        )
    assert calls == ["run", "export", "session"]
    assert (repo / "README.md").read_bytes() == before


def test_project_apply_projection_is_broad_but_propagation_stays_allowlisted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    (repo / ".gitignore").write_text(
        "ignored.txt\nnested/ignored.txt\n", encoding="utf-8"
    )
    (repo / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "ignored.txt").write_text(
        "tracked but ignored\n", encoding="utf-8"
    )
    (repo / "nested" / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    os.mkfifo(repo / "special.fifo")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (repo / "nested-link.txt").symlink_to(outside)
    subprocess.run(
        ["git", "add", ".gitignore", "nested/AGENTS.md", "nested-link.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-f", "ignored.txt", "nested/ignored.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add project exclusions"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(repo)
    config = _runner_config(config, project_access="project")
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], runner="apply-runner")
    input_path = _issues_artifact(context)
    observed: dict[str, pathlib.Path] = {}

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        prompt = args[2]
        assert "eligible readable project projection" in prompt
        assert "beyond the allowlist" in prompt
        assert "Only changes to paths matching the allowlist" in prompt
        assert "Any other staging change fails" in prompt
        assert "Tool availability is controlled by runner and user policy" in prompt
        assert "Do not use commands" not in prompt
        observed["staging"] = staging
        assert (staging / "README.md").exists()
        assert (staging / "docs" / "INDEX.md").exists()
        assert (staging / "src" / "app.py").exists()
        assert (staging / "ai-push-hooks.toml").exists()
        assert not (staging / "ignored.txt").exists()
        assert not (staging / "nested" / "ignored.txt").exists()
        assert not (staging / "nested-link.txt").exists()
        assert not (staging / "nested" / "AGENTS.md").exists()
        assert not (staging / "special.fifo").exists()
        assert not (staging / ".git").exists()
        (staging / "README.md").write_text("# Project-aware update\n", encoding="utf-8")
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    result = _run(context, step, input_path)

    assert result["changed_files"] == ["README.md"]
    assert (repo / "README.md").read_text(
        encoding="utf-8"
    ) == "# Project-aware update\n"
    assert observed["staging"] != repo


def test_project_apply_rejects_nonallowlisted_mutation_before_propagation(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    config = _runner_config(config, project_access="project")
    context = build_context(repo, config)
    step = replace(config.modules["docs"].steps[3], runner="apply-runner")
    input_path = _issues_artifact(context)

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        (staging / "src" / "app.py").write_text(
            "must not propagate\n", encoding="utf-8"
        )
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    with pytest.raises(HookError, match="outside allowlist"):
        _run(context, step, input_path)
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_compatibility_apply_projection_remains_minimal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _issues_artifact(context)

    def fake_runner(*args, **kwargs):
        staging = kwargs["working_directory"]
        prompt = args[2]
        assert "only eligible readable files selected by the allowlist" in prompt
        assert (
            "may include readable repository files beyond the allowlist" not in prompt
        )
        assert "Only changes to paths matching the allowlist" in prompt
        assert "Any other staging change fails" in prompt
        assert "Tool availability is controlled by runner and user policy" in prompt
        assert "Do not use commands" not in prompt
        assert (staging / "README.md").exists()
        assert (staging / "docs" / "INDEX.md").exists()
        assert not (staging / "src" / "app.py").exists()
        assert not (staging / "ai-push-hooks.toml").exists()
        assert not (staging / ".git").exists()
        (staging / "README.md").write_text("# Compatibility update\n", encoding="utf-8")
        return _success()

    monkeypatch.setattr(apply_executor, "run_runner_once", fake_runner)

    assert _run(context, step, input_path)["changed_files"] == ["README.md"]


def test_legacy_empty_issues_shortcut_is_retained_and_marked(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    config, _ = load_config(repo)
    context = build_context(repo, config)
    step = config.modules["docs"].steps[3]
    input_path = _empty_issues_artifact(context)

    def unexpected_runner(*args, **kwargs):
        pytest.fail("legacy empty issues shortcut must not invoke the runner")

    monkeypatch.setattr(apply_executor, "run_runner_once", unexpected_runner)

    assert _run(context, step, input_path) == {
        "changed": False,
        "changed_files": [],
        "skipped": True,
    }


@pytest.mark.parametrize(
    ("hooks_path_kind", "operation", "target"),
    [
        ("relative", "modify", ".githooks/pre-commit"),
        ("relative", "delete", ".githooks/pre-commit"),
        ("relative", "new", ".githooks/new-hook"),
        ("absolute", "modify", ".githooks/pre-commit"),
        ("relative-late", "modify", "zz-hooks/pre-commit"),
        ("nonexistent", "new", ".newhooks/new-hook"),
    ],
)
def test_command_runner_rejects_staging_writes_inside_effective_hooks_path(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    hooks_path_kind: str,
    operation: str,
    target: str,
) -> None:
    repo = init_repo(tmp_path, branch="feature/docs")
    hooks_directory = repo / (
        "zz-hooks" if hooks_path_kind == "relative-late" else ".githooks"
    )
    hooks_directory.mkdir()
    hook = hooks_directory / "pre-commit"
    hook_bytes = b"#!/bin/sh\nexit 0\n"
    hook.write_bytes(hook_bytes)
    hook.chmod(0o755)
    subprocess.run(
        ["git", "add", f"{hooks_directory.name}/pre-commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add clean test hook"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    configured_hooks_path = (
        str(hooks_directory)
        if hooks_path_kind == "absolute"
        else "zz-hooks"
        if hooks_path_kind == "relative-late"
        else ".githooks"
    )
    if hooks_path_kind == "nonexistent":
        configured_hooks_path = ".newhooks"
    subprocess.run(
        ["git", "config", "core.hooksPath", configured_hooks_path],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    config, _ = load_config(repo)
    profile = RunnerProfile(
        name="apply-command-runner",
        type="command",
        model="test-model",
        command=(
            sys.executable,
            "-c",
            "import pathlib, sys; "
            "target = pathlib.Path(sys.argv[1]); "
            "operation = sys.argv[2]; "
            "target.unlink() if operation == 'delete' else "
            "(target.parent.mkdir(parents=True, exist_ok=True) if operation == 'new' else None); "
            "target.write_text('changed hook\\n', encoding='utf-8') if operation == 'modify' else "
            "(target.write_text('new hook\\n', encoding='utf-8') if operation == 'new' else None); "
            "pathlib.Path('README.md').write_text('runner README\\n', encoding='utf-8')",
            target,
            operation,
        ),
    )
    config = replace(config, runners={profile.name: profile})
    context = build_context(repo, config)
    step = replace(
        config.modules["docs"].steps[3],
        runner=profile.name,
        allow_paths=("**",),
    )
    input_path = _issues_artifact(context)
    original_hook_mode = hook.stat().st_mode
    checked_destinations: list[str] = []
    real_safe_destination = apply_executor._safe_destination

    def tracking_safe_destination(context, relative_path):
        checked_destinations.append(relative_path)
        return real_safe_destination(context, relative_path)

    monkeypatch.setattr(apply_executor, "_safe_destination", tracking_safe_destination)

    with pytest.raises(HookError, match="configured Git hooks path"):
        _run(context, step, input_path)

    assert hook.read_bytes() == hook_bytes
    assert hook.stat().st_mode == original_hook_mode
    assert (repo / "README.md").read_text(encoding="utf-8") == "# Example\n"
    if operation == "new":
        assert not (repo / target).exists()
    if hooks_path_kind == "relative-late":
        assert checked_destinations == ["README.md", target]
        assert checked_destinations == sorted(checked_destinations)
