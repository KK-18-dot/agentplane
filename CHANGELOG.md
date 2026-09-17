# Changelog

## Unreleased

### Fixed

- `changed` no longer misses real changes. The before/after snapshot now records `HEAD` and a content hash per dirty path (untracked directories expanded), so a second edit to a file that was already modified, a new file inside an already-untracked directory, and commits made by the provider (`commits: a..b (N)` plus `committed <S> <path>` lines) all show up. Paths that were dirty before and are clean now are listed too. Entries stay plain strings in the ledger.
- Run ids carry a random suffix and log files are created exclusively (`O_EXCL`), so eval trials started by one process within the same second no longer share, and overwrite, one log.

## 0.1.0 — 2026-09-13

First public release, available on PyPI (`pipx install agentplane`).

- `init`, `render` (`--check`, `--adopt`, `--force`, appendices, `{{model:…}}` expansion), built-in targets for Claude Code, Codex/AGENTS.md, Cursor, Gemini CLI, Copilot, Windsurf, Cline.
- `run` with role-based routing, explicit overrides, allowlisted environment, timeout, safety boundaries, typed self-report, failure classification, one-shot fallback, HANDOFF.md, JSONL ledger.
- Built-in providers: claude, codex, cursor, gemini (experimental), ollama, mock.
- `routes`, `doctor` (OK/WARN/NOTE), `status`, `runs`, `packs`, `guard` (Claude Code hook adapter).
- `eval run` / `eval report` with fixture suites, code graders, signed baseline diffs; bundled offline smoke suite.
- Packs: directories of the same TOML tables, with `{pack_dir}` and pack-relative appendices.
