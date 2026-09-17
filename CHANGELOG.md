# Changelog

## Unreleased

### Fixed

- `changed` no longer misses real changes. The before/after snapshot now records `HEAD` and a content hash per dirty path (untracked directories expanded), so a second edit to a file that was already modified, a new file inside an already-untracked directory, and commits made by the provider (`commits: a..b (N)` plus `committed <S> <path>` lines) all show up. Paths that were dirty before and are clean now are listed too. Entries stay plain strings in the ledger.
- Run ids carry a random suffix and log files are created exclusively (`O_EXCL`), so eval trials started by one process within the same second no longer share, and overwrite, one log.
- A fallback no longer runs a `read_only = true` fallback role writable when the original route was writable. Fallback can only tighten read-only.
- Cancelling agentplane no longer leaves the provider running. SIGINT / SIGTERM / SIGHUP stop the provider's process group, and the run is still recorded (HANDOFF + ledger) with the new status `cancelled` and exit code 130 (SIGINT) or 143 (SIGTERM, SIGHUP). Cancelled runs never trigger the fallback. The task is now fed to the provider's stdin from a thread, so a provider that does not read a large task can no longer block the timeout.
- The forbidden-flag check matched exact strings only. It now also refuses `--dangerously-*=…`, `--force=…`, the short forms `-f` / `-y`, and the values `bypassPermissions`, `danger-full-access` and `yolo` as separate arguments or after `=` (covering `--permission-mode bypassPermissions`, `--sandbox=danger-full-access`, `-c sandbox_mode=danger-full-access`, `--approval-mode yolo`). Task text is no longer checked, so a task reading `--force` is not refused. `doctor` warns about such flags in any provider table.
- The ledger no longer stores the full task for `task_via = "arg"` providers (cursor, gemini): `command` records `<task>` in its place, and `task_head` is masked like the HANDOFF. Ledger lines are appended with a single `os.write` on an `O_APPEND` descriptor.
- State files were world-readable. The state directory, `logs/` and `evals/` are now created 0700, log files and `runs.jsonl` 0600, without touching the umask the provider inherits. `doctor` warns when an existing state directory or ledger is open to group/other and prints the `chmod` fix. Token shapes are masked in the log on disk after each run.

### Added

- `max_bytes` on render targets. The built-in `codex` target sets 32768, the size at which Codex CLI stops reading `AGENTS.md`. An oversized target is still written with a warning, fails `render --check` as `too-large`, and is a `doctor` WARN.

## 0.1.0 — 2026-09-13

First public release, available on PyPI (`pipx install agentplane`).

- `init`, `render` (`--check`, `--adopt`, `--force`, appendices, `{{model:…}}` expansion), built-in targets for Claude Code, Codex/AGENTS.md, Cursor, Gemini CLI, Copilot, Windsurf, Cline.
- `run` with role-based routing, explicit overrides, allowlisted environment, timeout, safety boundaries, typed self-report, failure classification, one-shot fallback, HANDOFF.md, JSONL ledger.
- Built-in providers: claude, codex, cursor, gemini (experimental), ollama, mock.
- `routes`, `doctor` (OK/WARN/NOTE), `status`, `runs`, `packs`, `guard` (Claude Code hook adapter).
- `eval run` / `eval report` with fixture suites, code graders, signed baseline diffs; bundled offline smoke suite.
- Packs: directories of the same TOML tables, with `{pack_dir}` and pack-relative appendices.
