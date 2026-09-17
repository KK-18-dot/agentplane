"""Execute one task on one provider and record the result.

Safety boundaries (all fail closed, exit 3):
- the working directory must exist and must not be $HOME or an ancestor of it
- the HANDOFF output path must be inside the working directory and must not be a symlink
- only an allowlist of environment variables reaches the provider process
- a provider may delegate once more (AGENTPLANE_DEPTH), never deeper than [run] max_depth
- provider commands may not contain flags that disable the harness's own sandbox/approvals
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import BinaryIO

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


NOT_GIT_NOTE = "(not a git repository: changes could not be detected)"
AGAIN_SUFFIX = " (modified before the run and again during it)"
CLEAN_NOW_PREFIX = "(clean now; was modified before the run) "
HASH_LIMIT = 5000


@dataclass
class GitSnapshot:
    """HEAD plus every dirty path with a fingerprint of its content.

    Comparing ``git status`` lines alone misses a second edit to a file that was already dirty,
    new files inside a directory that was already untracked, and commits made by the provider,
    so the snapshot keeps the commit and a per-path content fingerprint instead.
    """

    root: Path
    head: str | None  # None while HEAD is unborn
    entries: dict[str, tuple[str, str]] = field(default_factory=dict)  # path -> (XY, fingerprint)
    hashed: bool = True
    error: str | None = None


def _git(root: Path, *args: str, stdin: bytes | None = None, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )


def _display(path: str) -> str:
    """Paths are kept as os.fsdecode() strings; show undecodable bytes as escapes."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _blob_hash(path: Path) -> str:
    """Same value as ``git hash-object --no-filters`` in a SHA-1 repository."""
    try:
        digest = hashlib.sha1(f"blob {path.stat().st_size}\0".encode(), usedforsecurity=False)
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return "missing"
    return digest.hexdigest()


def _fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    prints: dict[str, str] = {}
    batch: list[str] = []
    for rel in paths:
        full = root / rel
        if full.is_symlink():
            prints[rel] = "link:" + os.readlink(full)
        elif full.is_file():
            # --stdin-paths reads one path per line and C-unquotes a leading '"'.
            if "\n" in rel or rel.startswith('"'):
                prints[rel] = _blob_hash(full)
            else:
                batch.append(rel)
        elif full.is_dir():
            prints[rel] = "dir"  # an untracked nested repository or a submodule
        else:
            prints[rel] = "missing"
    if batch:
        stdin = b"".join(os.fsencode(rel) + b"\n" for rel in batch)
        res = _git(root, "hash-object", "--no-filters", "--stdin-paths", stdin=stdin, timeout=300)
        hashes = res.stdout.decode("ascii", "replace").split()
        if res.returncode == 0 and len(hashes) == len(batch):
            prints.update(zip(batch, hashes, strict=True))
        else:  # a file vanished between status and hashing: hash one by one
            prints.update((rel, _blob_hash(root / rel)) for rel in batch)
    return prints


def git_state(target: Path) -> GitSnapshot | None:
    """Snapshot the repository around ``target``; None when it is not inside a work tree."""
    try:
        probe = _git(target, "rev-parse", "--is-inside-work-tree", "--show-toplevel", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = probe.stdout.decode("utf-8", "surrogateescape").splitlines()
    if probe.returncode != 0 or not lines or lines[0] != "true" or len(lines) < 2:
        return None
    root = Path(lines[1])
    try:
        head_res = _git(root, "rev-parse", "--verify", "-q", "HEAD")
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitSnapshot(root, None, error=f"git status failed: {exc}")
    if status.returncode != 0:
        detail = status.stderr.decode("utf-8", "replace").strip().splitlines()
        return GitSnapshot(root, None, error="git status failed" + (f": {detail[-1]}" if detail else ""))
    head = head_res.stdout.decode("ascii", "replace").strip() if head_res.returncode == 0 else None

    states: dict[str, str] = {}
    tokens = iter(status.stdout.split(b"\0"))
    for token in tokens:
        if len(token) < 4:
            continue
        xy = token[:2].decode("ascii", "replace")
        states[os.fsdecode(token[3:])] = xy
        if "R" in xy or "C" in xy:  # not produced with --no-renames; skip the source path anyway
            next(tokens, None)
    if len(states) > HASH_LIMIT:
        return GitSnapshot(root, head, {p: (xy, "") for p, xy in states.items()}, hashed=False)
    prints = _fingerprints(root, list(states))
    return GitSnapshot(root, head, {p: (xy, prints[p]) for p, xy in states.items()})


def _commit_lines(root: Path, old: str | None, new: str | None) -> list[str]:
    old_label = old[:7] if old else "(none)"
    if new is None:
        return [f"commits: {old_label}..(none) (HEAD is unborn now)"]
    try:
        count_res = _git(root, "rev-list", "--count", f"{old}..{new}" if old else new)
        # From an unborn HEAD every commit is new: diff against the empty tree, not just the last commit.
        base = old or _git(root, "hash-object", "-t", "tree", "--stdin", stdin=b"").stdout.decode().strip()
        diff = _git(root, "diff", "--name-status", "--no-renames", "--no-ext-diff", "-z", base, new)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"commits: {old_label}..{new[:7]} (could not be listed: {exc})"]
    count = count_res.stdout.decode().strip() if count_res.returncode == 0 else "?"
    moved = " (HEAD moved without adding commits)" if count == "0" else ""
    lines = [f"commits: {old_label}..{new[:7]} ({count}){moved}"]
    fields = diff.stdout.split(b"\0") if diff.returncode == 0 else []
    for status, path in zip(fields[0::2], fields[1::2], strict=False):
        if status:
            lines.append(f"committed {status.decode('ascii', 'replace')} {_display(os.fsdecode(path))}")
    return lines


def diff_git_state(before: GitSnapshot | None, after: GitSnapshot | None) -> list[str]:
    """What the run changed: new commits, then paths whose status or content moved."""
    if before is None or after is None:
        return [NOT_GIT_NOTE]
    if before.error or after.error:
        return [f"(changes could not be detected: {before.error or after.error})"]
    lines: list[str] = []
    if before.head != after.head:
        lines.extend(_commit_lines(after.root, before.head, after.head))
    compare_content = before.hashed and after.hashed
    if not compare_content:
        lines.append(
            f"(more than {HASH_LIMIT} dirty paths: content was not compared, so further edits to paths "
            "that were already dirty are not listed)"
        )
    for path in sorted(after.entries):
        xy, digest = after.entries[path]
        old = before.entries.get(path)
        if old is None:
            lines.append(f"{xy} {_display(path)}")
        elif old[0] != xy or (compare_content and old[1] != digest):
            lines.append(f"{xy} {_display(path)}{AGAIN_SUFFIX}")
    for path in sorted(set(before.entries) - set(after.entries)):
        lines.append(f"{CLEAN_NOW_PREFIX}{_display(path)}")
    return lines


def _create_log(provider: str) -> tuple[str, Path, int]:
    """Reserve a run id by creating its log file exclusively.

    The id already carries a random suffix; O_EXCL makes a collision a retry instead of two runs
    silently sharing (and overwriting) one log.
    """
    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(5):
        run_id = new_run_id(provider)
        log_path = log_dir / f"{run_id}.log"
        try:
            return run_id, log_path, os.open(log_path, flags, 0o600)
        except FileExistsError:
            continue
    raise AgentplaneError(f"could not allocate a unique run id in {log_dir}")


def _run_mock(cfg: Config, route: Route, task: str, target: Path, log: BinaryIO, echo: bool) -> int:
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
        log.write(b"[mock] still running\n")
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
    log.write(response.encode("utf-8"))
    if echo:
        sys.stdout.write(response)
        sys.stdout.flush()
    return code


def _run_cli(
    argv: list[str], stdin_text: str | None, env: dict[str, str], cwd: Path, timeout: int, log: BinaryIO, echo: bool
) -> int:
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

    full_task = PREAMBLE + task
    spec = cfg.providers[route.provider]
    if spec.get("kind", "cli") == "mock":
        argv, stdin_text = ["<mock>"], None
    else:
        argv, stdin_text = build_command(cfg, route, full_task, target)
    run_id, log_path, log_fd = _create_log(route.provider)

    before = git_state(target)
    start = time.monotonic()
    with os.fdopen(log_fd, "w+b") as log:
        if spec.get("kind", "cli") == "mock":
            code = _run_mock(cfg, route, task, target, log, echo)
        else:
            env = build_env(cfg, route, depth)
            code = _run_cli(argv, stdin_text, env, target, route.timeout, log, echo)
        seconds = int(time.monotonic() - start)
        log.flush()
        log.seek(0)
        log_text = log.read().decode("utf-8", errors="replace")
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

    changed = diff_git_state(before, git_state(target))

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
