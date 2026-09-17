"""Reproducible evals: fixture directories → agentplane run → deterministic checks → report.

A suite is a directory of cases. Each case directory contains:

    task.md       the task text (required)
    seed/         files copied into a fresh git-initialised workdir before the run (optional)
    check.sh      code grader; exit 0 = PASS (optional). Receives WORKDIR as $1, and the env
                  AGENTPLANE_LOG, AGENTPLANE_HANDOFF, AGENTPLANE_EXIT, AGENTPLANE_STATUS
    case.toml     expectations and per-case overrides (optional):
                    role = "impl"           read_only = true      timeout = 300
                    expect_exit = 0         expect_status = "done"
                    expect_output_regex = "..."   expect_files = ["src/x.py"]

Every run appends one JSON line to results.jsonl; ``eval report`` renders a markdown table and,
with ``--baseline``, signed pass-rate diffs so improvements and regressions surface together.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, ensure_state_dir
from .errors import UsageError
from .routing import resolve_route
from .run import run_task

CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class CaseResult:
    suite: str
    case: str
    trial: int
    role: str | None
    provider: str
    model: str | None
    effort: str | None
    exit: int
    status: str
    seconds: int
    passed: bool
    reasons: list[str] = field(default_factory=list)
    workdir: str = ""
    ts: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def discover_cases(suite_dir: Path) -> list[Path]:
    if not suite_dir.is_dir():
        raise UsageError(f"suite directory not found: {suite_dir}")
    cases = sorted(p for p in suite_dir.iterdir() if p.is_dir() and (p / "task.md").is_file())
    if not cases:
        raise UsageError(f"no cases in {suite_dir} (a case is a directory containing task.md)")
    for case in cases:
        if not CASE_NAME_RE.match(case.name):
            raise UsageError(f"case name {case.name!r} must match {CASE_NAME_RE.pattern}")
    return cases


def _load_case(case_dir: Path) -> dict[str, Any]:
    spec_path = case_dir / "case.toml"
    if spec_path.is_file():
        with spec_path.open("rb") as fh:
            return tomllib.load(fh)
    return {}


def _prepare_workdir(case_dir: Path, workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    seed = case_dir / "seed"
    if seed.is_dir():
        shutil.copytree(seed, workdir, dirs_exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "agentplane-eval",
        "GIT_AUTHOR_EMAIL": "eval@localhost",
        "GIT_COMMITTER_NAME": "agentplane-eval",
        "GIT_COMMITTER_EMAIL": "eval@localhost",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", str(workdir), *args], check=True, capture_output=True, env=env)


def _check(
    case_dir: Path, spec: dict[str, Any], workdir: Path, log_path: Path, handoff: Path, exit_code: int, status: str
) -> list[str]:
    reasons: list[str] = []
    if "expect_exit" in spec and exit_code != int(spec["expect_exit"]):
        reasons.append(f"exit {exit_code} != expected {spec['expect_exit']}")
    if "expect_status" in spec and status != str(spec["expect_status"]):
        reasons.append(f"status {status!r} != expected {spec['expect_status']!r}")
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    if "expect_output_regex" in spec and not re.search(str(spec["expect_output_regex"]), log_text, re.S):
        reasons.append(f"output does not match /{spec['expect_output_regex']}/")
    for rel in spec.get("expect_files", []) or []:
        if not (workdir / rel).exists():
            reasons.append(f"expected file missing: {rel}")
    check = case_dir / "check.sh"
    if check.is_file():
        env = {
            **os.environ,
            "AGENTPLANE_LOG": str(log_path),
            "AGENTPLANE_HANDOFF": str(handoff),
            "AGENTPLANE_EXIT": str(exit_code),
            "AGENTPLANE_STATUS": status,
        }
        try:
            res = subprocess.run(
                ["bash", str(check), str(workdir)],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                cwd=str(workdir),
            )
        except subprocess.TimeoutExpired:
            reasons.append("check.sh timed out")
        else:
            if res.returncode != 0:
                detail = (res.stdout + res.stderr).strip().splitlines()
                reasons.append("check.sh failed" + (f": {detail[-1]}" if detail else ""))
    return reasons


def run_suite(
    cfg: Config,
    suite_dir: Path,
    *,
    role: str | None,
    provider: str | None,
    trials: int,
    results_dir: Path | None,
    only: str | None = None,
) -> tuple[Path, list[CaseResult]]:
    cases = discover_cases(suite_dir)
    if only:
        cases = [c for c in cases if re.search(only, c.name)]
        if not cases:
            raise UsageError(f"no case matches --only {only!r}")
    stamp = time.strftime("%Y%m%d%H%M%S")
    results_dir = results_dir or (ensure_state_dir("evals") / f"{suite_dir.name}-{stamp}")
    results_dir.mkdir(parents=True, exist_ok=True)
    results_path = results_dir / "results.jsonl"
    results: list[CaseResult] = []
    with results_path.open("a", encoding="utf-8") as fh:
        for case_dir in cases:
            spec = _load_case(case_dir)
            case_role = role or spec.get("role")
            route = resolve_route(
                cfg,
                role=case_role,
                provider=provider if not case_role else None,
                timeout=int(spec["timeout"]) if "timeout" in spec else None,
                read_only=bool(spec["read_only"]) if "read_only" in spec else None,
            )
            task = (case_dir / "task.md").read_text(encoding="utf-8")
            for trial in range(1, trials + 1):
                workdir = results_dir / case_dir.name / f"trial-{trial}"
                _prepare_workdir(case_dir, workdir)
                handoff = workdir / "HANDOFF.md"
                outcome = run_task(cfg, task, route=route, target_dir=workdir, out=handoff, echo=False)
                rec = outcome.record
                reasons = _check(case_dir, spec, workdir, Path(rec.log), handoff, rec.exit, rec.status)
                result = CaseResult(
                    suite=suite_dir.name,
                    case=case_dir.name,
                    trial=trial,
                    role=route.role,
                    provider=route.provider,
                    model=route.model,
                    effort=route.effort,
                    exit=rec.exit,
                    status=rec.status,
                    seconds=rec.seconds,
                    passed=not reasons,
                    reasons=reasons,
                    workdir=str(workdir),
                    ts=rec.ts,
                )
                results.append(result)
                fh.write(result.to_json() + "\n")
                fh.flush()
                mark = "PASS" if result.passed else "FAIL"
                print(
                    f"{mark} {case_dir.name} trial {trial} "
                    f"({route.provider} {route.model or '-'} {route.effort or '-'}) "
                    f"exit={rec.exit} status={rec.status} {rec.seconds}s"
                )
                for reason in reasons:
                    print(f"     - {reason}")
    report_path = results_dir / "report.md"
    report_path.write_text(render_report(results, None), encoding="utf-8")
    return results_path, results


def load_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise UsageError(f"results file not found: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _pass_rates(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    rates: dict[str, tuple[int, int]] = {}
    for row in rows:
        passed, total = rates.get(row["case"], (0, 0))
        rates[row["case"]] = (passed + (1 if row["passed"] else 0), total + 1)
    return rates


def render_report(results: list[CaseResult] | list[dict[str, Any]], baseline: list[dict[str, Any]] | None) -> str:
    rows = [asdict(r) if isinstance(r, CaseResult) else r for r in results]
    rates = _pass_rates(rows)
    base_rates = _pass_rates(baseline) if baseline else {}
    lines = ["# agentplane eval report", ""]
    if rows:
        lines.append(f"suite: {rows[0]['suite']}  runs: {len(rows)}  cases: {len(rates)}")
        lines.append("")
    header = "| case | route | pass | rate |" + (" baseline | diff |" if baseline else "")
    lines.append(header)
    lines.append("|---|---|---|---|" + ("---|---|" if baseline else ""))
    total_pass = total_runs = 0
    for case, (passed, total) in sorted(rates.items()):
        total_pass += passed
        total_runs += total
        route = next(
            (f"{r['provider']}/{r['model'] or '-'}/{r['effort'] or '-'}" for r in rows if r["case"] == case), "-"
        )
        rate = passed / total * 100 if total else 0.0
        line = f"| {case} | {route} | {passed}/{total} | {rate:.0f}% |"
        if baseline:
            bp, bt = base_rates.get(case, (0, 0))
            if bt:
                brate = bp / bt * 100
                line += f" {bp}/{bt} ({brate:.0f}%) | {rate - brate:+.0f}pt |"
            else:
                line += " - | new |"
        lines.append(line)
    if total_runs:
        lines.append("")
        lines.append(f"overall: {total_pass}/{total_runs} ({total_pass / total_runs * 100:.0f}%)")
    failures = [r for r in rows if not r["passed"]]
    if failures:
        lines.append("")
        lines.append("## failures")
        lines.append("")
        for r in failures:
            lines.append(f"- {r['case']} trial {r['trial']}: {'; '.join(r['reasons'])} (workdir: {r['workdir']})")
    return "\n".join(lines) + "\n"
