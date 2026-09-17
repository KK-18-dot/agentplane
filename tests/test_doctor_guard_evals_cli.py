import json
import os
import subprocess
from pathlib import Path

from agentplane.cli import main
from agentplane.config import load_config, state_dir
from agentplane.doctor import run_doctor
from agentplane.evals import load_results, render_report, run_suite
from agentplane.guard import check_paths, claude_hook
from agentplane.render import render

# ---- doctor -----------------------------------------------------------------------------------


def _levels(report):
    return {(f.level, f.message.split(":")[0]) for f in report.findings}


def test_doctor_all_ok_after_render(project: Path) -> None:
    render(load_config(project))
    (project / "agentplane.toml").write_text(
        (project / "agentplane.toml").read_text().replace('provider = "fakecli"', 'provider = "mock"'), encoding="utf-8"
    )
    report = run_doctor(project)
    assert report.warnings == 0, report.render()
    assert "RESULT: ALL OK" in report.render()


def test_doctor_warns_on_missing_provider_used_by_role_and_drift(project: Path) -> None:
    render(load_config(project))
    (project / "PROJECT.md").write_text("# drifted\n", encoding="utf-8")
    report = run_doctor(project)
    text = report.render()
    assert "WARN provider fakecli: 'fakecli' not found in PATH; used by roles: ro, shim" in text
    assert "WARN render target claude: CLAUDE.md drifted" in text
    assert "NOTE provider claude" in text  # optional provider, not used by any role
    assert report.warnings >= 4


def test_doctor_detects_retired_models_and_tracked_secret_names(project: Path) -> None:
    (project / "PROJECT.md").write_text("Never use old-model-9 again.\n", encoding="utf-8")
    render(load_config(project))
    (project / ".env").write_text("X=1\n", encoding="utf-8")
    (project / ".env.example").write_text("X=\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".env", ".env.example"], cwd=project, check=True)
    text = run_doctor(project).render()
    assert "WARN retired model ids in use: CLAUDE.md mentions old-model-9" in text
    assert "WARN tracked files with secret-like names: .env" in text
    assert ".env.example" not in text


def test_doctor_warns_on_forbidden_flags_before_anything_runs(project: Path, capsys) -> None:
    toml = project / "agentplane.toml"
    toml.write_text(
        toml.read_text()
        + '\n[providers.risky]\ncommand = ["risky", "--sandbox", "{permission_mode}"]\n'
        + 'write_mode = "danger-full-access"\nread_only_mode = "read-only"\n'
        + 'read_only_args = ["--approval-mode", "yolo"]\n',
        encoding="utf-8",
    )
    text = run_doctor(project).render()
    assert "WARN provider risky: forbidden flag or value 'danger-full-access' in write_mode" in text
    assert "WARN provider risky: forbidden flag or value 'yolo' in read_only_args" in text
    assert "provider fakecli: forbidden" not in text
    assert main(["run", "--provider", "risky", "--dir", str(project), "--dry-run", "x"]) == 3
    assert "forbidden flag" in capsys.readouterr().err


def test_doctor_warns_when_state_is_readable_by_others(project: Path) -> None:
    render(load_config(project))
    sd = state_dir()
    sd.mkdir(parents=True)
    os.chmod(sd, 0o755)
    ledger = sd / "runs.jsonl"
    ledger.write_text("", encoding="utf-8")
    os.chmod(ledger, 0o644)
    text = run_doctor(project).render()
    assert f"WARN state directory {sd} is accessible by group/other (mode 755)" in text
    assert f"WARN run ledger {ledger} is readable by group/other (mode 644)" in text
    assert "chmod -R go-rwx" in text
    os.chmod(sd, 0o700)
    os.chmod(ledger, 0o600)
    text = run_doctor(project).render()
    assert "state directory" in text and "accessible by group/other" not in text


def test_doctor_reports_broken_config_as_warn(sandbox: Path) -> None:
    proj = sandbox / "work" / "broken"
    proj.mkdir(parents=True)
    (proj / "agentplane.toml").write_text('[roles.x]\nprovider = "ghost"\n', encoding="utf-8")
    report = run_doctor(proj)
    assert report.warnings == 1 and "configuration" in report.findings[-1].message


# ---- guard --------------------------------------------------------------------------------------


def test_guard_flags_generated_files_only(project: Path) -> None:
    render(load_config(project))
    assert check_paths([str(project / "CLAUDE.md"), str(project / "PROJECT.md"), "/nonexistent"]) == [
        project / "CLAUDE.md"
    ]


def test_claude_hook_asks_for_generated_and_is_silent_otherwise(project: Path) -> None:
    render(load_config(project))
    event = {"tool_name": "Edit", "tool_input": {"file_path": str(project / "CLAUDE.md")}}
    out = json.loads(claude_hook(json.dumps(event)))
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert (
        claude_hook(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(project / "PROJECT.md")}})) is None
    )
    assert claude_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})) is None
    assert claude_hook("not json") is None


# ---- evals --------------------------------------------------------------------------------------


def _suite(sandbox: Path) -> Path:
    suite = sandbox / "suite"
    ok = suite / "echo-ok"
    ok.mkdir(parents=True)
    (ok / "task.md").write_text("Say hello\n", encoding="utf-8")
    (ok / "case.toml").write_text(
        'expect_exit = 0\nexpect_status = "done"\nexpect_output_regex = "Say hello"\n', encoding="utf-8"
    )
    (ok / "check.sh").write_text(
        '#!/usr/bin/env bash\ntest -f "$AGENTPLANE_HANDOFF" && grep -q "status: done" "$AGENTPLANE_HANDOFF"\n',
        encoding="utf-8",
    )
    seeded = suite / "seeded"
    (seeded / "seed" / "src").mkdir(parents=True)
    (seeded / "seed" / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (seeded / "task.md").write_text("Inspect src/app.py\n", encoding="utf-8")
    (seeded / "case.toml").write_text('expect_files = ["src/app.py", "src/missing.py"]\n', encoding="utf-8")
    return suite


def test_eval_suite_runs_grades_and_reports(project: Path, sandbox: Path) -> None:
    suite = _suite(sandbox)
    cfg = load_config(project)
    results_path, results = run_suite(cfg, suite, role="dry", provider=None, trials=2, results_dir=sandbox / "out")
    by_case = {(r.case, r.trial): r for r in results}
    assert by_case[("echo-ok", 1)].passed and by_case[("echo-ok", 2)].passed
    assert not by_case[("seeded", 1)].passed
    assert by_case[("seeded", 1)].reasons == ["expected file missing: src/missing.py"]
    rows = load_results(results_path)
    assert len(rows) == 4
    report = render_report(rows, None)
    assert "| echo-ok | mock/-/low | 2/2 | 100% |" in report
    assert "| seeded | mock/-/low | 0/2 | 0% |" in report
    assert (sandbox / "out" / "report.md").is_file()
    # signed diff against a baseline in which seeded passed
    baseline = [dict(r, passed=True) for r in rows]
    diff = render_report(rows, baseline)
    assert "-100pt" in diff and "+0pt" in diff


def test_bundled_smoke_suite_passes_offline(project: Path) -> None:
    suite = Path(__file__).resolve().parents[1] / "evals" / "suites" / "smoke"
    cfg = load_config(project)
    _, results = run_suite(cfg, suite, role="dry", provider=None, trials=1, results_dir=None)
    assert results and all(r.passed for r in results), [(r.case, r.reasons) for r in results]
    assert (state_dir() / "evals").stat().st_mode & 0o777 == 0o700


# ---- cli ----------------------------------------------------------------------------------------


def test_cli_end_to_end(project: Path, capsys) -> None:
    assert main(["render", "--dir", str(project)]) == 0
    assert main(["render", "--check", "--dir", str(project)]) == 0
    assert main(["routes", "--dir", str(project)]) == 0
    out = capsys.readouterr().out
    assert "shim" in out and "unavailable" in out and "dry" in out
    assert main(["routes", "shim", "--dir", str(project)]) == 0
    assert "fallback: dry" in capsys.readouterr().out
    assert main(["run", "--role", "dry", "--dir", str(project), "--quiet", "hello"]) == 0
    assert "HANDOFF:" in capsys.readouterr().out
    assert main(["run", "--role", "shim", "--dir", str(project), "--dry-run", "hello"]) == 0
    out = capsys.readouterr().out
    assert "command: fakecli --mode write --model fake-fast-1 --effort high" in out
    assert main(["runs"]) == 0
    assert main(["status", "--dir", str(project)]) == 0
    assert "## recent runs" in capsys.readouterr().out
    assert main(["run", "--role", "nope", "--dir", str(project), "x"]) == 2
    assert "unknown role" in capsys.readouterr().err
    assert main([]) == 2


def test_cli_init_creates_files_and_doctor_is_clean(sandbox: Path, capsys) -> None:
    proj = sandbox / "work" / "fresh"
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    assert main(["init", "--dir", str(proj)]) == 0
    assert (proj / "agentplane.toml").is_file() and (proj / "PROJECT.md").is_file()
    cfg = load_config(proj)
    assert set(cfg.roles) == {"dry"}  # no real CLI on PATH → only the offline role
    assert main(["render", "--dir", str(proj)]) == 0
    assert main(["doctor", "--dir", str(proj)]) == 0
    assert "RESULT: ALL OK" in capsys.readouterr().out
    assert main(["init", "--dir", str(proj)]) == 2


def test_cli_init_generates_roles_for_detected_clis(sandbox: Path, fake_cli, capsys) -> None:
    fake_cli("claude")
    fake_cli("codex")
    proj = sandbox / "work" / "detected"
    proj.mkdir(parents=True)
    assert main(["init", "--dir", str(proj)]) == 0
    cfg = load_config(proj)
    assert {"impl", "impl_claude", "review", "dry"} <= set(cfg.roles)
    assert cfg.roles["impl"]["fallback"] == "impl_claude"
