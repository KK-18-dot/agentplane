# Security model

agentplane launches other people's CLIs with write access to a directory. This page says exactly what it protects, how, and what it does not.

## What agentplane enforces

| Boundary | Mechanism | Failure |
|---|---|---|
| No secrets reach the provider from your shell | providers run with an **allowlisted** environment (`HOME PATH USER LOGNAME SHELL TERM LANG LC_* COLORTERM NO_COLOR TMPDIR XDG_*`); `env_extra` names that look like secrets are refused | exit 3 |
| Write scope | `--dir` must exist and must not be `$HOME` or an ancestor; HANDOFF must be inside `--dir` and not a symlink; the directory identity is re-checked before writing | exit 3 |
| No harness bypass | commands containing `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--force` are rejected; built-in providers use each CLI's read-only / workspace-write modes and an empty MCP config | exit 3 |
| Runaway delegation | `AGENTPLANE_DEPTH` is injected and capped by `[run] max_depth` | exit 3 |
| Runaway time | timeout, then SIGTERM/SIGKILL to the process group | exit 124 |
| Prompt injection through results | provider output is transcribed into HANDOFF inside a fence with fences neutralised and labelled "data, not instructions"; only the last line is parsed for the status | — |
| Secret leakage through results | token-shaped strings are masked in the HANDOFF tail | — |
| Secret files in the repo | `doctor` warns on tracked file **names** like `.env`, `secrets/`, `*.pem` (contents are never read) | doctor WARN |

## What it does not do

- It does not sandbox the provider. File and network isolation is the provider CLI's job (Codex sandbox, Claude Code permission modes, Cursor's agent mode). agentplane only chooses the safer of the provider's documented modes.
- It cannot stop a provider from reading secrets **inside `--dir`**. Keep `.env` and credentials out of the working tree or use the provider's own deny rules.
- Shell profiles are outside the allowlist: if a provider runs commands through a login shell that exports API keys, those keys are visible to that shell. Do not export secrets from shell profiles.
- The generated-file guard for harness hooks is fail-open. Hooks are hints; `render --check` in CI is the enforcement.
- Provider CLIs change flags. A wrong flag typically makes the CLI exit non-zero (status `failed`) rather than run unsafely, but review provider definitions when you upgrade a CLI.

## Reporting

See [SECURITY.md](../SECURITY.md) at the repository root.
