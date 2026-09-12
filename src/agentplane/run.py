"""Execute one task on one provider and record the result.

Safety boundaries (all fail closed, exit 3):
- the working directory must exist and must not be $HOME or an ancestor of it
- the HANDOFF output path must be inside the working directory and must not be a symlink
- only an allowlist of environment variables reaches the provider process
- a provider may delegate once more (AGENTPLANE_DEPTH), never deeper than [run] max_depth
- provider commands may not contain flags that disable the harness's own sandbox/approvals
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .config import Config, state_dir
from .errors import EXIT_EMPTY, EXIT_TIMEOUT, AgentplaneError, ConfigError, SafetyError, UsageError
from .handoff import (
    PREAMBLE,
    RunRecord,
    append_record,
    failure_kind,
    new_run_id,
    self_report,
    status_for,
    write_handoff,
)
from .routing import Route, provider_status, resolve_route

DEPTH_VAR = "AGENTPLANE_DEPTH"
FORBIDDEN_FLAGS = {
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "--force",
}


@dataclass
class RunOutcome:
    code: int
    status: str
    record: RunRecord


# ---- boundary checks ---------------------------------------------------------------------------


def check_dir(target: Path) -> Path:
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"--dir does not exist: {target} ({exc})") from exc
    if not resolved.is_dir():
        raise SafetyError(f"--dir is not a directory: {target}")
    home = Path.home().resolve()
    ancestor = home
    while True:
        if resolved == ancestor:
            raise SafetyError(f"--dir may not be $HOME or an ancestor of it (write boundary too wide): {target}")
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    return resolved


def check_out(out: Path | None, target: Path) -> Path:
    if out is None:
        out = target / "HANDOFF.md"
    if out.name in ("", ".", ".."):
        raise SafetyError(f"invalid --out basename: {out}")
    if out.is_symlink():
        raise SafetyError(f"--out must not be an existing symlink: {out}")
    parent = out.parent
    if not parent.is_absolute():
        parent = Path.cwd() / parent
    try:
        parent = parent.resolve(strict=True)
    except OSError as exc:
        raise SafetyError(f"--out parent directory does not exist: {out}") from exc
    final = parent / out.name
    if final != target and target not in final.parents:
        raise SafetyError(f"--out must be inside --dir ({target}): {final}")
    return final


def check_depth(cfg: Config) -> int:
    raw = os.environ.get(DEPTH_VAR, "0")
    depth = int(raw) if raw.isdigit() else 0
    if depth >= int(cfg.run.get("max_depth", 2)):
        raise SafetyError(f"{DEPTH_VAR}={depth}: delegation depth limit reached; split the task and run it directly")
    return depth


# ---- command assembly ----------------------------------------------------------------------------


def empty_mcp_path() -> Path:
    path = state_dir() / "empty-mcp.json"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            resources.files("agentplane").joinpath("templates/empty-mcp.json").read_text(), encoding="utf-8"
        )
    return path


def build_command(cfg: Config, route: Route, task: str, target: Path) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text). ``stdin_text`` is None when the task travels as an argument."""
    spec = cfg.providers[route.provider]
    mode_key = "read_only_mode" if route.read_only else "write_mode"
    values = {
        "model": route.model or "",
        "effort": route.effort or "",
        "task": task,
        "dir": str(target),
        "empty_mcp": str(empty_mcp_path()),
        "permission_mode": spec.get(mode_key, ""),
    }
    argv: list[str] = []

    def add(parts: list[str]) -> None:
        for part in parts:
            if "{permission_mode}" in part and not values["permission_mode"]:
                raise ConfigError(f"provider {route.provider}: '{mode_key}' is required by its command template")
            argv.append(part.format_map(values))

    add(list(spec.get("command", [])))
    if route.model and spec.get("model_args"):
        add(list(spec["model_args"]))
    if route.effort and spec.get("effort_args"):
        add(list(spec["effort_args"]))
    if route.read_only:
        add(list(spec.get("read_only_args", []) or []))
    else:
        add(list(spec.get("write_args", []) or []))

    stdin_text: str | None
    if spec.get("task_via", "stdin") == "stdin":
        marker = spec.get("task_stdin_marker")
        if marker:
            argv.append(marker)
        stdin_text = task
    else:
        if not any("{task}" in part for part in spec.get("command", [])):
            argv.append(task)
        stdin_text = None

    for arg in argv:
        if arg in FORBIDDEN_FLAGS:
            raise SafetyError(f"provider {route.provider} command contains a forbidden flag: {arg}")
    return argv, stdin_text


def build_env(cfg: Config, route: Route, depth: int) -> dict[str, str]:
    names = cfg.env_allowlist() + list(cfg.providers[route.provider].get("env_extra", []) or [])
    env = {name: os.environ[name] for name in names if name in os.environ}
    env[DEPTH_VAR] = str(depth + 1)
    return env


# ---- execution ----------------------------------------------------------------------------------


def git_state(target: Path) -> list[str] | None:
    try:
        probe = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    status = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"], capture_output=True, text=True, timeout=60
    )
    return sorted(line for line in status.stdout.splitlines() if line.strip())


def _run_mock(cfg: Config, route: Route, task: str, target: Path, log_path: Path, echo: bool) -> int:
    spec = cfg.providers[route.provider]
    response = os.environ.get("AGENTPLANE_MOCK_RESPONSE")
    if response is None:
        response = spec.get("response")
    if response is None:
        response = (
            f"[mock provider] model={route.model or '-'} effort={route.effort or '-'} read_only={route.read_only}\n"
            f"Task received ({len(task)} chars):\n{task}\n\nAGENTPLANE-STATUS: DONE\n"
        )
    code_raw = os.environ.get("AGENTPLANE_MOCK_EXIT", str(spec.get("exit_code", 0)))
    code = int(code_raw) if str(code_raw).lstrip("-").isdigit() else 0
    sleep = float(os.environ.get("AGENTPLANE_MOCK_SLEEP", "0") or 0)
    if sleep > route.timeout:
        time.sleep(route.timeout)
        log_path.write_text("[mock] still running\n", encoding="utf-8")
        return EXIT_TIMEOUT
    if sleep:
        time.sleep(sleep)
    if not route.read_only:
        for rel, content in (spec.get("writes") or {}).items():
            path = target / rel
            if target not in path.resolve().parents:
                raise SafetyError(f"mock writes outside --dir: {rel}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
    log_path.write_text(response, encoding="utf-8")
    if echo:
        sys.stdout.write(response)
        sys.stdout.flush()
    return code


def _run_cli(
    argv: list[str], stdin_text: str | None, env: dict[str, str], cwd: Path, timeout: int, log_path: Path, echo: bool
) -> int:
    with log_path.open("wb") as log:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise AgentplaneError(f"cannot start provider: {exc}") from exc

        def pump() -> None:
            assert proc.stdout is not None
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                log.write(chunk)
                log.flush()
                if echo:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(proc)
        reader.join(timeout=30)
        return EXIT_TIMEOUT if timed_out else proc.returncode


def _terminate(proc: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=15)


def run_task(
    cfg: Config,
    task: str,
    *,
    route: Route,
    target_dir: Path,
    out: Path | None = None,
    echo: bool = True,
    allow_fallback: bool = True,
    fallback_from: str | None = None,
) -> RunOutcome:
    if not task.strip():
        raise UsageError("task is empty (pass it as an argument, --task-file, or on stdin)")
    target = check_dir(target_dir)
    out_path = check_out(out, target)
    depth = check_depth(cfg)

    status = provider_status(cfg, route.provider)
    if not status.available:
        raise UsageError(
            f"provider {route.provider} is not available: {status.note}. "
            f"Run `agentplane doctor`, or choose another role/provider."
        )

    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id(route.provider)
    log_path = log_dir / f"{run_id}.log"
    full_task = PREAMBLE + task
    spec = cfg.providers[route.provider]

    before = git_state(target)
    start = time.monotonic()
    if spec.get("kind", "cli") == "mock":
        argv = ["<mock>"]
        code = _run_mock(cfg, route, task, target, log_path, echo)
    else:
        argv, stdin_text = build_command(cfg, route, full_task, target)
        env = build_env(cfg, route, depth)
        code = _run_cli(argv, stdin_text, env, target, route.timeout, log_path, echo)
    seconds = int(time.monotonic() - start)

    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    if code == 0 and len(log_text.encode("utf-8")) < int(cfg.run.get("min_output_bytes", 200)):
        print(f"agentplane: warning: exit 0 but output is shorter than min_output_bytes: {log_path}", file=sys.stderr)
        code = EXIT_EMPTY
    kind = (
        failure_kind(log_text, spec.get("quota_markers", []) or [], spec.get("auth_markers", []) or [])
        if code not in (0, EXIT_EMPTY, EXIT_TIMEOUT)
        else None
    )
    reported = self_report(log_text)
    status_name, next_note = status_for(code if code in (0, EXIT_EMPTY, EXIT_TIMEOUT) else 1, reported, kind)

    after = git_state(target)
    if before is None or after is None:
        changed: list[str] = ["(not a git repository: changes could not be detected)"]
    else:
        changed = sorted(set(after) ^ set(before))

    record = RunRecord(
        id=run_id,
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        provider=route.provider,
        role=route.role,
        model=route.model,
        effort=route.effort,
        dir=str(target),
        out=str(out_path),
        log=str(log_path),
        exit=code if code in (0, EXIT_EMPTY, EXIT_TIMEOUT) else 1,
        status=status_name,
        seconds=seconds,
        self_report=reported,
        changed=changed,
        fallback_from=fallback_from,
        task_head=task.strip().splitlines()[0][:120],
        command=argv,
        read_only=route.read_only,
        depth=depth,
    )

    # One automatic retry on a different route when the failure is not about the task itself.
    if kind in ("quota-exhausted", "auth-required") and allow_fallback and route.fallback and fallback_from is None:
        append_record(record)
        print(
            f"agentplane: {route.provider} reported {kind}; retrying once via fallback role {route.fallback}",
            file=sys.stderr,
        )
        fb_route = resolve_route(
            cfg,
            role=route.fallback,
            timeout=int(route.explicit["timeout"]) if "timeout" in route.explicit else None,
            read_only=route.read_only,
        )
        return run_task(
            cfg,
            task,
            route=fb_route,
            target_dir=target,
            out=out_path,
            echo=echo,
            allow_fallback=False,
            fallback_from=f"{route.role or route.provider}/{kind}/{log_path}",
        )

    try:
        write_handoff(record, task, log_text, next_note)
    except AgentplaneError as exc:
        print(f"agentplane: {exc}", file=sys.stderr)
        record.exit = 1
        record.status = "handoff-write-failed"
    append_record(record)
    return RunOutcome(record.exit, record.status, record)
