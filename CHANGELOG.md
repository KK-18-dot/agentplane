# Changelog

## 0.1.0 — 2026-09-13

First public release.

- `init`, `render` (`--check`, `--adopt`, `--force`, appendices, `{{model:…}}` expansion), built-in targets for Claude Code, Codex/AGENTS.md, Cursor, Gemini CLI, Copilot, Windsurf, Cline.
- `run` with role-based routing, explicit overrides, allowlisted environment, timeout, safety boundaries, typed self-report, failure classification, one-shot fallback, HANDOFF.md, JSONL ledger.
- Built-in providers: claude, codex, cursor, gemini (experimental), ollama, mock.
- `routes`, `doctor` (OK/WARN/NOTE), `status`, `runs`, `packs`, `guard` (Claude Code hook adapter).
- `eval run` / `eval report` with fixture suites, code graders, signed baseline diffs; bundled offline smoke suite.
- Packs: directories of the same TOML tables, with `{pack_dir}` and pack-relative appendices.
