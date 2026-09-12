# Quickstart

## 1. Install

```bash
pipx install agentplane        # or: uv tool install agentplane
agentplane --version
```

Requirements: Python 3.11+, git. Provider CLIs (`claude`, `codex`, `cursor-agent`, `gemini`, `ollama`) are optional; install the ones you use.

## 2. Initialise a project

```bash
cd your-project
agentplane init
```

This writes two files:

- `PROJECT.md` — the policy. Purpose, stack, commands, quality gate, do-nots, pitfalls. Written for an agent, kept short.
- `agentplane.toml` — configuration. Which targets to render, model aliases, and **roles** for the provider CLIs found on your machine (plus `dry`, an offline mock that always works).

If the project already has a hand-written `CLAUDE.md`, promote it instead of starting from the template:

```bash
agentplane render --adopt      # CLAUDE.md -> PROJECT.md (backup kept), then renders
```

## 3. Render

```bash
agentplane render
```

`PROJECT.md` becomes `CLAUDE.md`, `AGENTS.md`, and `.cursor/rules/project.mdc` (change the list under `[render] targets`; `gemini`, `copilot`, `windsurf`, `cline` are built in). Every generated file starts with a marker comment; agentplane refuses to overwrite files that lack it, and `agentplane guard` lets a harness hook refuse edits to them.

Per-harness extras go in `.agentplane/appendix/<target>.md` and are appended to that target only.

Commit both the source and the generated files. In CI:

```bash
agentplane render --check      # exit 1 on drift
```

## 4. Look at routing

```bash
agentplane routes
agentplane routes review       # explain one role
```

A role is a row: provider, model, effort, timeout, read-only, fallback. `routes` also tells you whether each provider CLI is installed. Nothing else influences what `run` will do.

## 5. Run a task

```bash
agentplane run --role dry "List the top-level directories and what each is for"
agentplane run --role review --read-only "Review src/ for unhandled errors"
agentplane run --role impl --task-file PLAN.md --timeout 1200 --dir ./service
```

What happens:

1. The provider CLI starts headless inside `--dir` with an allowlisted environment (no API keys from your shell) and a timeout.
2. The task is prefixed with a short preamble asking for a final `AGENTPLANE-STATUS: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT` line.
3. agentplane measures the result (git diff before/after, exit code, duration, effective model, that status line) and writes `HANDOFF.md` plus one line in `~/.local/state/agentplane/runs.jsonl`.

Exit codes: `0` done · `1` provider failed · `2` usage/config · `3` safety boundary · `4` empty output · `124` timeout.

```bash
cat HANDOFF.md
agentplane runs
agentplane status
```

## 6. Diagnose

```bash
agentplane doctor
```

`OK` / `WARN` / `NOTE` lines. WARN = broken or drifted (exit 1). NOTE = informational, such as an optional provider you do not have. Use it in CI next to `render --check`.

## 7. Evals

```bash
agentplane eval run evals/suites/smoke --role dry            # offline, deterministic
agentplane eval run evals/suites/smoke --role impl_claude     # same suite, real provider
agentplane eval report ~/.local/state/agentplane/evals/<run>/results.jsonl --baseline <older>/results.jsonl
```

See [evals.md](evals.md) for writing your own cases.

## 8. Personal choices stay out of the repo

Put provider overrides and private model preferences in `~/.config/agentplane/config.toml`; it uses the same format and sits below the project file. The repository's `agentplane.toml` then describes the team's routing, not one machine's.
