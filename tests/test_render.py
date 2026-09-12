from pathlib import Path

import pytest

from agentplane.config import load_config
from agentplane.errors import ConfigError, UsageError
from agentplane.render import HEADER, adopt, is_generated, render


def test_render_writes_all_targets_with_marker_and_model_expansion(project: Path) -> None:
    cfg = load_config(project)
    results = render(cfg)
    assert [r.action for r in results] == ["written", "written", "written"]
    claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith(HEADER)
    assert "Use fake-fast-1 for routine work." in claude
    assert "{{model:" not in claude
    cursor = (project / ".cursor/rules/project.mdc").read_text(encoding="utf-8")
    assert cursor.startswith("---\ndescription:")
    assert HEADER in cursor
    assert is_generated(project / "AGENTS.md")


def test_render_is_idempotent_and_check_detects_drift(project: Path) -> None:
    cfg = load_config(project)
    render(cfg)
    assert [r.action for r in render(cfg)] == ["unchanged"] * 3
    assert all(r.action == "unchanged" for r in render(cfg, check=True))
    (project / "PROJECT.md").write_text("# changed\n", encoding="utf-8")
    actions = {r.target: r.action for r in render(cfg, check=True)}
    assert actions == {"claude": "drift", "codex": "drift", "cursor": "drift"}
    # --check never writes
    assert "Use fake-fast-1" in (project / "CLAUDE.md").read_text(encoding="utf-8")


def test_hand_written_file_is_refused_without_force(project: Path) -> None:
    cfg = load_config(project)
    (project / "CLAUDE.md").write_text("# my precious hand-written notes\n", encoding="utf-8")
    results = {r.target: r for r in render(cfg)}
    assert results["claude"].action == "refused"
    assert "hand-written" in results["claude"].message
    assert "precious" in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert results["codex"].action == "written"
    results = {r.target: r for r in render(cfg, force=True)}
    assert results["claude"].action == "written"


def test_adopt_promotes_claude_md(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").unlink()
    (project / "CLAUDE.md").write_text("# legacy policy\n", encoding="utf-8")
    backup = adopt(cfg)
    assert backup.is_file()
    assert (project / "PROJECT.md").read_text(encoding="utf-8") == "# legacy policy\n"
    with pytest.raises(UsageError, match="already exists"):
        adopt(cfg)


def test_appendix_is_appended_per_target(project: Path) -> None:
    cfg = load_config(project)
    appendix = project / ".agentplane" / "appendix"
    appendix.mkdir(parents=True)
    (appendix / "claude.md").write_text("Claude-only note.\n", encoding="utf-8")
    render(cfg)
    assert "Claude-only note." in (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Claude-only note." not in (project / "AGENTS.md").read_text(encoding="utf-8")


def test_unknown_model_token_fails(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").write_text("{{model:ghost}}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no key 'ghost'"):
        render(cfg)


def test_missing_policy_is_usage_error(project: Path) -> None:
    cfg = load_config(project)
    (project / "PROJECT.md").unlink()
    with pytest.raises(UsageError, match="not found"):
        render(cfg)
