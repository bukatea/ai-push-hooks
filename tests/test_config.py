from __future__ import annotations

import pathlib
import re

import pytest

from ai_push_hooks import config as config_module
from ai_push_hooks.artifacts import generate_run_id
from ai_push_hooks.config import load_config, resolve_runner_profile
from ai_push_hooks import git_utils
from ai_push_hooks.types import HookError, LlmConfig, StepConfig

from .conftest import init_repo


def _opencode_cli_generation_document(llm: str, profiles: str = "") -> str:
    return f"""
[llm]
{llm}
{profiles}
[workflow]
modules = ["docs"]
[[modules.docs.steps]]
id = "query"
type = "ask"
schema = "string_array"
prompt = "Return []."
output = "result.json"
"""


@pytest.fixture
def opencode_cli_generation_no_launch(monkeypatch):
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(args)
        pytest.fail("config validation must not launch OpenCode")

    monkeypatch.setattr(
        "ai_push_hooks.executors.runners.opencode.run_process", unexpected
    )
    yield calls
    assert calls == []


@pytest.mark.parametrize("generation", [None, "legacy", "v2"])
@pytest.mark.parametrize("selection", ["implicit", "explicit", "step", "codex"])
def test_opencode_cli_generation_load_and_effective_rebuild(
    tmp_path, monkeypatch, generation, selection, opencode_cli_generation_no_launch
):
    llm = "" if generation is None else f'opencode_cli_generation = "{generation}"'
    profiles = ""
    if selection != "implicit":
        kind = "codex" if selection == "codex" else "opencode"
        profiles = f'[runners.selected]\ntype = "{kind}"\nmodel = "profile/model"'
        if selection != "step":
            llm += '\nrunner = "selected"'
    (tmp_path / "ai-push-hooks.toml").write_text(
        _opencode_cli_generation_document(llm, profiles), encoding="utf-8"
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", "opaque/model#native")
    monkeypatch.setenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS", "17")
    # No generation environment override is supported.
    monkeypatch.setenv("AI_PUSH_HOOKS_OPENCODE_CLI_GENERATION", "not-a-generation")
    config, _ = load_config(tmp_path)
    assert config.llm.opencode_cli_generation == (generation or "legacy")
    assert config.llm.timeout_seconds == 17
    step = StepConfig(
        "query", "ask", runner="selected" if selection == "step" else None
    )
    profile = resolve_runner_profile(config, step)
    assert profile.model == "opaque/model#native"
    assert profile.type == ("codex" if selection == "codex" else "opencode")


@pytest.mark.parametrize("value", ["legacy", "v2"])
def test_opencode_cli_generation_direct_construction(value):
    assert LlmConfig(opencode_cli_generation=value).opencode_cli_generation == value


@pytest.mark.parametrize(
    "value", ["", "V2", "v2 ", " legacy", "auto", True, False, 2, 2.0, [], None, {}]
)
def test_opencode_cli_generation_rejects_invalid_direct_values(
    value, opencode_cli_generation_no_launch
):
    with pytest.raises(HookError, match="opencode_cli_generation.*legacy.*v2"):
        LlmConfig(opencode_cli_generation=value)


@pytest.mark.parametrize(
    "literal",
    [
        '""',
        '"V2"',
        '"v2 "',
        '" legacy"',
        '"auto"',
        "true",
        "false",
        "2",
        "2.0",
        "[]",
        "{}",
    ],
)
def test_opencode_cli_generation_rejects_invalid_toml(
    tmp_path, literal, opencode_cli_generation_no_launch
):
    (tmp_path / "ai-push-hooks.toml").write_text(
        _opencode_cli_generation_document(f"opencode_cli_generation = {literal}"),
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="opencode_cli_generation.*legacy.*v2"):
        load_config(tmp_path)


@pytest.mark.parametrize("source", ["llm", "profile", "unselected", "env", "effective"])
def test_opencode_cli_generation_rejects_v2_variant_every_config_path(
    tmp_path, monkeypatch, source, opencode_cli_generation_no_launch
):
    llm = 'opencode_cli_generation = "v2"'
    profiles = ""
    if source == "llm":
        llm += '\nvariant = "high"'
    if source in {"profile", "unselected", "effective"}:
        profiles = '[runners.selected]\ntype = "opencode"\nvariant = ""'
        if source != "unselected":
            llm += '\nrunner = "selected"'
        if source != "effective":
            profiles = profiles.replace('variant = ""', 'variant = "high"')
    (tmp_path / "ai-push-hooks.toml").write_text(
        _opencode_cli_generation_document(llm, profiles), encoding="utf-8"
    )
    if source == "effective":
        config, _ = load_config(tmp_path)
        with pytest.raises(HookError, match="v2.*variant"):
            resolve_runner_profile(
                config, StepConfig("query", "ask"), {"AI_PUSH_HOOKS_VARIANT": "high"}
            )
    else:
        if source == "env":
            monkeypatch.setenv("AI_PUSH_HOOKS_VARIANT", "high")
        with pytest.raises(HookError, match="v2.*variant"):
            load_config(tmp_path)


@pytest.mark.parametrize("variant", ["", "high"])
def test_opencode_cli_generation_direct_variant_validation(variant):
    if variant:
        with pytest.raises(HookError, match="v2.*variant"):
            LlmConfig(opencode_cli_generation="v2", variant=variant)
    else:
        assert LlmConfig(opencode_cli_generation="v2", variant=variant).variant == ""


def test_opencode_cli_generation_empty_variant_and_non_opencode_env(
    tmp_path, monkeypatch
):
    (tmp_path / "ai-push-hooks.toml").write_text(
        _opencode_cli_generation_document(
            'opencode_cli_generation = "v2"', '[runners.selected]\ntype = "codex"'
        ),
        encoding="utf-8",
    )
    config, _ = load_config(tmp_path)
    assert (
        resolve_runner_profile(
            config, StepConfig("q", "ask"), {"AI_PUSH_HOOKS_VARIANT": ""}
        ).variant
        == ""
    )
    assert (
        resolve_runner_profile(
            config,
            StepConfig("q", "ask", runner="selected"),
            {"AI_PUSH_HOOKS_VARIANT": "high"},
        ).variant
        is None
    )


def test_load_config_requires_config_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(
        HookError, match="Missing required config file `ai-push-hooks.toml`"
    ):
        load_config(tmp_path)


def test_load_config_rejects_legacy_shape(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[prompts]
query_file = "query.txt"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="Legacy or unsupported config keys"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_step_type(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "bad"
type = "mystery"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="Unknown step type"):
        load_config(tmp_path)


@pytest.mark.parametrize("legacy_type", ["llm", "agent"])
def test_load_config_rejects_legacy_step_type_in_unselected_module(
    tmp_path: pathlib.Path, legacy_type: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[modules.unselected]
enabled = false

[[modules.unselected.steps]]
id = "legacy"
type = "{legacy_type}"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError,
        match=r"modules\.unselected\.steps\[1\]\.type.*use `ask` instead",
    ):
        load_config(tmp_path)


def test_load_config_supports_standard_toml_inline_tables(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules]
docs = { enabled = true, steps = [{ id = "collect", type = "collect", collector = "docs_context" }] }
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.workflow.modules == ("docs",)
    assert config.modules["docs"].steps[0].id == "collect"


def test_load_config_preserves_existing_valid_configuration(repo: pathlib.Path) -> None:
    config, config_path = load_config(repo)

    assert config_path == repo / "ai-push-hooks.toml"
    assert config.general.allow_push_on_error is False
    assert config.modules["docs"].enabled is True
    assert config.workflow.modules == ("docs",)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("allow_push_on_error", '"false"', "general.allow_push_on_error"),
        ("require_clean_worktree", '"false"', "general.require_clean_worktree"),
        ("skip_on_sync_branch", '"true"', "general.skip_on_sync_branch"),
    ],
)
def test_load_config_rejects_quoted_security_booleans(
    tmp_path: pathlib.Path, field: str, value: str, message: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[general]
{field} = {value}

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match=message):
        load_config(tmp_path)


def test_load_config_rejects_quoted_module_enabled(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = "false"

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="modules.docs.enabled"):
        load_config(tmp_path)


def test_load_config_preserves_string_arrays(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = "Apply the change"
inputs = ["collect/context.txt"]
allow_paths = ["README.md", "docs/**/*.md"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)
    step = config.modules["docs"].steps[0]

    assert step.inputs == ("collect/context.txt",)
    assert step.allow_paths == ("README.md", "docs/**/*.md")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", '"800"'),
        ("max_parallel", "1.5"),
        ("json_max_retries", "true"),
        ("invalid_json_feedback_max_chars", "0"),
        ("max_diff_bytes", "-1"),
    ],
)
def test_load_config_rejects_invalid_numeric_budgets(
    tmp_path: pathlib.Path, field: str, value: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[llm]
{field} = {value}

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match=f"llm\\.{field}"):
        load_config(tmp_path)


def test_load_config_rejects_non_string_list_entries(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs", 3]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="workflow.modules must be an array of strings"):
        load_config(tmp_path)


def test_load_config_rejects_duplicate_workflow_modules(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs", " docs "]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError, match=r"Duplicate module id `docs` in workflow\.modules"
    ):
        load_config(tmp_path)


def test_load_config_rejects_duplicate_step_ids_within_module(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = " collect "
type = "collect"
collector = "other_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError,
        match=r"Duplicate step id `collect` at modules\.docs\.steps\[2\]\.id",
    ):
        load_config(tmp_path)


def test_load_config_rejects_duplicate_step_ids_in_unselected_module(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[modules.unselected]
enabled = false

[[modules.unselected.steps]]
id = "query"
type = "ask"
prompt = "Return JSON"
output = "query.json"

[[modules.unselected.steps]]
id = " query "
type = "ask"
prompt = "Return JSON"
output = "other.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError,
        match=r"Duplicate step id `query` at modules\.unselected\.steps\[2\]\.id",
    ):
        load_config(tmp_path)


def test_load_config_allows_same_step_id_in_different_modules(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs", "quality"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[modules.quality]
enabled = true

[[modules.quality.steps]]
id = "collect"
type = "collect"
collector = "quality_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)

    assert config.modules["docs"].steps[0].id == "collect"
    assert config.modules["quality"].steps[0].id == "collect"


def test_load_config_rejects_unknown_nested_field(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true
unexpected = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="Unknown field.*unexpected"):
        load_config(tmp_path)


def test_load_config_reports_malformed_toml(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text("[workflow\n", encoding="utf-8")

    with pytest.raises(HookError, match="Invalid TOML.*line"):
        load_config(tmp_path)


def test_load_config_reports_invalid_utf8(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_bytes(b"[workflow]\nmodules = [\xff]\n")

    with pytest.raises(HookError, match="not valid UTF-8"):
        load_config(tmp_path)


def test_load_config_rejects_oversized_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").write_bytes(
        b"x" * (config_module.CONFIG_MAX_BYTES + 1)
    )

    with pytest.raises(HookError, match="exceeds maximum size"):
        load_config(tmp_path)


def test_load_config_rejects_symlink(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "real-config.toml"
    target.write_text('[workflow]\nmodules = ["docs"]\n', encoding="utf-8")
    (tmp_path / "ai-push-hooks.toml").symlink_to(target)

    with pytest.raises(HookError, match="symlink"):
        load_config(tmp_path)


def test_load_config_rejects_non_regular_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ai-push-hooks.toml").mkdir()

    with pytest.raises(HookError, match="regular file"):
        load_config(tmp_path)


def test_load_config_rejects_invalid_numeric_environment_override(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(HookError, match="AI_PUSH_HOOKS_TIMEOUT_SECONDS"):
        load_config(tmp_path)


def test_load_config_rejects_invalid_boolean_environment_override(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR", "sometimes")

    with pytest.raises(HookError, match="AI_PUSH_HOOKS_ALLOW_PUSH_ON_ERROR"):
        load_config(tmp_path)


def test_load_config_ignores_legacy_dot_filename(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        HookError, match="Missing required config file `ai-push-hooks.toml`"
    ):
        load_config(tmp_path)


def test_generate_run_id_is_unique_and_high_resolution() -> None:
    first = generate_run_id()
    second = generate_run_id()

    assert first != second
    assert re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", first)


def test_base_branch_can_be_overridden_by_env(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[general]
base_branch = "develop"

[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_BASE_BRANCH", "release")

    config, _ = load_config(tmp_path)

    assert config.general.base_branch == "release"


def test_collect_ranges_uses_configured_base_branch_for_new_remote_branch(
    monkeypatch,
) -> None:
    calls = []
    local_oid = "a" * 40
    base_oid = "b" * 40

    def fake_git(cwd, args, check=True):
        calls.append(args)
        if args == ["remote"]:
            return "origin"
        if args == [
            "rev-parse",
            "--verify",
            "--quiet",
            f"{local_oid}^{{commit}}",
        ]:
            return local_oid
        if args == [
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/origin/develop^{commit}",
        ]:
            return base_oid
        if args == ["merge-base", local_oid, base_oid]:
            return base_oid
        return ""

    monkeypatch.setattr(git_utils, "git", fake_git)

    ranges = git_utils.collect_ranges_from_stdin(
        pathlib.Path("/repo"),
        "origin",
        [f"refs/heads/feature/x {local_oid} refs/heads/feature/x {'0' * 40}"],
        "develop",
    )

    assert ranges == [f"{base_oid}..{local_oid}"]
    assert ["merge-base", local_oid, base_oid] in calls


@pytest.mark.parametrize("storage_path", ["/tmp/logs", "../logs", "C:\\temp\\logs"])
def test_load_config_rejects_storage_paths_outside_repository(
    tmp_path: pathlib.Path, storage_path: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[logging]
dir = {storage_path!r}

[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="logging.dir"):
        load_config(tmp_path)


def test_resolve_storage_path_rejects_symlink_escape(tmp_path: pathlib.Path) -> None:
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    outside = tmp_path / "outside"
    repo_root.mkdir()
    git_dir.mkdir()
    outside.mkdir()
    (repo_root / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HookError, match="symlink"):
        git_utils.resolve_storage_path(repo_root, git_dir, "logs/output")


def test_resolve_storage_path_rejects_git_namespace_symlink(
    tmp_path: pathlib.Path,
) -> None:
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    objects = git_dir / "objects"
    repo_root.mkdir()
    objects.mkdir(parents=True)
    (git_dir / "ai-push-hooks").symlink_to(objects, target_is_directory=True)

    with pytest.raises(HookError, match="symlink"):
        git_utils.resolve_storage_path(repo_root, git_dir, ".git/ai-push-hooks/logs")


@pytest.mark.parametrize("key", ["dir", "transcript_dir", "summary_dir"])
@pytest.mark.parametrize("storage_path", ["logs", ".git/objects", ".git/hooks/output"])
def test_load_config_constrains_all_runtime_storage_to_owned_git_namespace(
    tmp_path: pathlib.Path, key: str, storage_path: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[logging]
{key} = {storage_path!r}

[workflow]
modules = ["docs"]

[modules.docs]
enabled = false

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="must be inside .git/ai-push-hooks"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "protected_allow_path", [".GiT/**", ".ＧＩＴ/**", "aGeNtS.Md", "ＡＧＥＮＴＳ.md"]
)
def test_load_config_rejects_case_variant_protected_apply_paths(
    tmp_path: pathlib.Path, protected_allow_path: str
) -> None:
    repo = init_repo(tmp_path, branch="feature/config")
    config_path = repo / "ai-push-hooks.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'allow_paths = ["README.md", "docs/**/*.md"]',
            f'allow_paths = ["{protected_allow_path}"]',
        ),
        encoding="utf-8",
    )

    with pytest.raises(HookError, match="Git metadata|AGENTS.md"):
        load_config(repo)


def test_runner_profiles_resolve_step_override_and_environment_precedence(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[llm]
runner = "codex-review"
model = "legacy-model"
variant = "legacy-variant"

[runners.codex-review]
type = "codex"
model = "codex-model"
project_access = "project"

[runners.pi-apply]
type = "command"
model = "pi-model"
project_access = "project"
prompt_transport = "stdin"
command = ["pi", "--print", "--model", "{model}"]

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = "Apply the change"
allow_paths = ["README.md"]
runner = "pi-apply"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)
    ask_step = config.modules["docs"].steps[0]
    apply_step = config.modules["docs"].steps[1]

    default = resolve_runner_profile(
        config, ask_step, {"AI_PUSH_HOOKS_MODEL": "env-model"}
    )
    selected = resolve_runner_profile(
        config,
        apply_step,
        {"AI_PUSH_HOOKS_MODEL": "env-model", "AI_PUSH_HOOKS_VARIANT": "ignored"},
    )

    assert (default.name, default.type, default.model, default.project_access) == (
        "codex-review",
        "codex",
        "env-model",
        "project",
    )
    assert (selected.name, selected.type, selected.model, selected.command) == (
        "pi-apply",
        "command",
        "env-model",
        ("pi", "--print", "--model", "{model}"),
    )
    assert selected.variant is None


def test_model_environment_override_rejects_whitespace_at_load_and_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "query"
type = "ask"
prompt = "Return JSON"
output = "query.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", " \t ")

    with pytest.raises(HookError, match="AI_PUSH_HOOKS_MODEL"):
        load_config(tmp_path)

    monkeypatch.delenv("AI_PUSH_HOOKS_MODEL")
    config, _ = load_config(tmp_path)
    with pytest.raises(HookError, match="AI_PUSH_HOOKS_MODEL"):
        resolve_runner_profile(
            config,
            config.modules["docs"].steps[0],
            {"AI_PUSH_HOOKS_MODEL": " \t "},
        )


def test_command_model_placeholder_accepts_effective_environment_model(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[llm]
runner = "env-command"

[runners.env-command]
type = "command"
command = ["agent", "--model", "{model}"]

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "query"
type = "ask"
prompt = "Return JSON"
output = "query.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PUSH_HOOKS_MODEL", "env-model")

    config, _ = load_config(tmp_path)
    profile = resolve_runner_profile(config, config.modules["docs"].steps[0])

    assert profile.model == "env-model"
    assert profile.command == ("agent", "--model", "{model}")


def test_missing_runner_reference_is_checked_in_unselected_modules(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"

[modules.unselected]
enabled = false

[[modules.unselected.steps]]
id = "query"
type = "ask"
prompt = "Return JSON"
output = "query.json"
runner = "missing-profile"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError,
        match=r"modules\.unselected\.steps\[1\]\.runner.*missing-profile",
    ):
        load_config(tmp_path)


def test_legacy_opencode_runner_resolves_implicit_artifact_profile(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[llm]
runner = "opencode"
model = "legacy-model"
variant = ""

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "query"
type = "ask"
prompt = "Return JSON"
output = "query.json"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_config(tmp_path)
    profile = resolve_runner_profile(config, config.modules["docs"].steps[0], {})

    assert profile.name == "opencode"
    assert profile.type == "opencode"
    assert profile.model == "legacy-model"
    assert profile.variant == ""
    assert profile.project_access == "artifacts"


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        ('type = "codex"\nvariant = "sonnet"', "runners.bad.variant"),
        (
            'type = "command"\ncommand = ["agent", "--prompt={prompt}"]',
            "whole argv elements",
        ),
        (
            'type = "command"\nprompt_transport = "argv"\ncommand = ["agent"]',
            "exactly one",
        ),
        ('type = "command"\ncommand = ["agent", "{unknown}"]', "Unknown placeholder"),
    ],
)
def test_runner_profile_validation_is_strict(
    tmp_path: pathlib.Path, profile: str, message: str
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[runners.bad]
{profile}

[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match=message):
        load_config(tmp_path)


def test_runner_selection_is_rejected_on_non_promptable_step(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[modules.docs]
enabled = true

[[modules.docs.steps]]
id = "collect"
type = "collect"
collector = "docs_context"
runner = "opencode"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookError, match=r"modules.docs.steps\[1\].runner"):
        load_config(tmp_path)


def _write_extension_config(repo: pathlib.Path, step: str) -> None:
    checks_path = repo / "checks.py"
    if not checks_path.exists():
        checks_path.write_text(
            "# deliberately not imported by config loading\n", encoding="utf-8"
        )
    (repo / "ai-push-hooks.toml").write_text(
        f"""
[workflow]
modules = ["quality"]

[modules.quality]
enabled = true

[[modules.quality.steps]]
{step}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_extension_steps_normalize_python_and_command_fields(
    tmp_path: pathlib.Path,
) -> None:
    _write_extension_config(
        tmp_path,
        """
id = "collect"
type = "collect"
python = "checks.py:collect_context"
inputs = ["prior/result.json"]
options = { include_generated = false, limits = [1, 2.5] }

[[modules.quality.steps]]
id = "lint"
type = "exec"
command = ["{python}", "script.py", "{input:collect/result.json}"]
inputs = ["collect/result.json"]
stdin = "collect/result.json"
""",
    )

    config, _ = load_config(tmp_path)
    collect, command = config.modules["quality"].steps

    assert collect.python == "checks.py:collect_context"
    assert collect.options == {"include_generated": False, "limits": [1, 2.5]}
    assert command.command == ("{python}", "script.py", "{input:collect/result.json}")
    assert command.timeout_seconds == 60
    assert command.stdin == "collect/result.json"


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (
            'id = "x"\ntype = "collect"\ncollector = "builtin"\npython = "checks.py:hook"',
            "exactly one implementation",
        ),
        (
            'id = "x"\ntype = "exec"\nexecutor = "builtin"\npython = "checks.py:hook"',
            "exactly one implementation",
        ),
        (
            'id = "x"\ntype = "assert"\npython = "checks.py:hook"\ntimeout_seconds = 5',
            "timeout_seconds is only valid with",
        ),
        (
            'id = "x"\ntype = "apply"\nallow_paths = ["README.md"]\npython = "checks.py:hook"',
            "python is not valid",
        ),
        (
            'id = "x"\ntype = "exec"\nexecutor = "builtin"\noptions = { answer = 1 }',
            "options requires",
        ),
    ],
)
def test_extension_implementation_fields_are_strict(
    tmp_path: pathlib.Path, step: str, message: str
) -> None:
    _write_extension_config(tmp_path, step)

    with pytest.raises(HookError, match=message):
        load_config(tmp_path)


def test_python_reference_validation_does_not_import_code(
    tmp_path: pathlib.Path,
) -> None:
    marker = tmp_path / "imported"
    (tmp_path / "checks.py").write_text(
        f"{marker!s}.write_text('bad')\n", encoding="utf-8"
    )
    _write_extension_config(
        tmp_path,
        'id = "collect"\ntype = "collect"\npython = "checks.py:collect_context"',
    )

    load_config(tmp_path)

    assert not marker.exists()


def test_load_config_validates_each_callback_reference_once_across_env_rebuild(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "checks.py").write_text("# callback fixture\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("# callback fixture\n", encoding="utf-8")
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["selected"]

[modules.selected]
enabled = true

[[modules.selected.steps]]
id = "selected"
type = "collect"
python = "checks.py:hook"

[modules.disabled]
enabled = false

[[modules.disabled.steps]]
id = "disabled"
type = "collect"
python = "checks.py:hook"

[modules.unselected]
enabled = true

[[modules.unselected.steps]]
id = "unselected"
type = "collect"
python = "other.py:hook"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []
    validate_reference = config_module._validate_python_reference

    def count_filesystem_validations(
        value: str, label: str, repo_root: pathlib.Path | None = None
    ) -> str:
        if repo_root is not None:
            calls.append(value)
        return validate_reference(value, label, repo_root)

    monkeypatch.setattr(
        config_module, "_validate_python_reference", count_filesystem_validations
    )

    config, _ = load_config(tmp_path)

    assert config.workflow.modules == ("selected",)
    assert calls == ["checks.py:hook", "other.py:hook"]


def test_load_config_rejects_invalid_python_reference_in_disabled_module(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "checks.py").write_text("# callback fixture\n", encoding="utf-8")
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["selected"]

[modules.selected]
enabled = true

[[modules.selected.steps]]
id = "selected"
type = "collect"
python = "checks.py:hook"

[modules.disabled]
enabled = false

[[modules.disabled.steps]]
id = "disabled"
type = "collect"
python = "missing.py:hook"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        HookError,
        match=r"modules\.disabled\.steps\[1\]\.python path must reference an existing regular file",
    ):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("checks.py", "repository-relative .py path"),
        ("../checks.py:hook", "must not contain '..'"),
        ("checks.txt:hook", "must name a .py file"),
        ("checks.py:obj.hook", "one top-level identifier"),
        ("missing.py:hook", "existing regular file"),
    ],
)
def test_python_reference_is_safe_and_strict(
    tmp_path: pathlib.Path, reference: str, message: str
) -> None:
    _write_extension_config(
        tmp_path,
        f'id = "collect"\ntype = "collect"\npython = "{reference}"',
    )

    with pytest.raises(HookError, match=message):
        load_config(tmp_path)


def test_python_steps_reject_duplicate_input_references(tmp_path: pathlib.Path) -> None:
    _write_extension_config(
        tmp_path,
        """
id = "collect"
type = "collect"
python = "checks.py:collect_context"
inputs = ["prior/result.json", "prior/result.json"]
""",
    )

    with pytest.raises(HookError, match=r"modules\.quality\.steps\[1\]\.inputs"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["{repos}"], "Unknown command placeholder"),
        (["--path={repo}"], "must be whole argv elements"),
        (["{input:missing/result.json}"], "undeclared input"),
    ],
)
def test_command_placeholder_grammar_is_strict(
    tmp_path: pathlib.Path, command: list[str], message: str
) -> None:
    command_literal = ", ".join(f'"{item}"' for item in command)
    _write_extension_config(
        tmp_path,
        f"""
id = "exec"
type = "exec"
command = [{command_literal}]
""",
    )

    with pytest.raises(HookError, match=message):
        load_config(tmp_path)


def test_command_literal_braces_and_stdin_reference_are_preserved(
    tmp_path: pathlib.Path,
) -> None:
    _write_extension_config(
        tmp_path,
        """
id = "exec"
type = "exec"
inputs = ["collect/files.json"]
command = ["bash", "-c", "for x in {a,b}; do printf x; done", "awk '{print $1}'"]
stdin = "collect/files.json"
timeout_seconds = 7
""",
    )

    config, _ = load_config(tmp_path)
    step = config.modules["quality"].steps[0]
    assert step.command[2] == "for x in {a,b}; do printf x; done"
    assert step.command[3] == "awk '{print $1}'"
    assert step.timeout_seconds == 7


def test_options_are_json_compatible_and_null_free(tmp_path: pathlib.Path) -> None:
    _write_extension_config(
        tmp_path,
        """
id = "collect"
type = "collect"
python = "checks.py:collect_context"
options = { nested = { value = nan } }
""",
    )

    with pytest.raises(HookError, match=r"options\.nested\.value.*finite"):
        load_config(tmp_path)


def _auto_commit_config(tmp_path: pathlib.Path, apply_extra: str) -> pathlib.Path:
    (tmp_path / "ai-push-hooks.toml").write_text(
        f"""
[workflow]
modules = ["docs"]

[[modules.docs.steps]]
id = "apply"
type = "apply"
prompt = "fix it"
allow_paths = ["README.md"]
{apply_extra}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_apply_auto_commit_defaults_to_false(
    tmp_path: pathlib.Path,
) -> None:
    config, _ = load_config(_auto_commit_config(tmp_path, ""))
    step = config.modules["docs"].steps[0]
    assert step.auto_commit is False
    assert step.auto_push is False
    assert step.commit_message is None


def test_apply_auto_push_requires_auto_commit(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="auto_push requires .*auto_commit"):
        load_config(_auto_commit_config(tmp_path, "auto_push = true"))


def test_apply_commit_message_requires_auto_commit(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="commit_message requires .*auto_commit"):
        load_config(_auto_commit_config(tmp_path, 'commit_message = "docs: fix"'))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('""', "must not be empty"),
        ('"-rf something"', "must not start with"),
        (f'"{"x" * 200}"', "at most"),
        ('"first\\nsecond"', "single line"),
    ],
)
def test_apply_commit_message_is_validated(
    tmp_path: pathlib.Path, value: str, expected: str
) -> None:
    with pytest.raises(HookError, match=expected):
        load_config(
            _auto_commit_config(
                tmp_path, f"auto_commit = true\ncommit_message = {value}"
            )
        )


def test_apply_auto_commit_must_be_a_boolean(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HookError, match="auto_commit must be a boolean"):
        load_config(_auto_commit_config(tmp_path, 'auto_commit = "true"'))


def test_auto_commit_keys_are_rejected_on_non_apply_steps(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "ai-push-hooks.toml").write_text(
        """
[workflow]
modules = ["docs"]

[[modules.docs.steps]]
id = "review"
type = "ask"
prompt = "review"
output = "issues.json"
auto_commit = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(HookError, match="auto_commit is not valid for ask steps"):
        load_config(tmp_path)
