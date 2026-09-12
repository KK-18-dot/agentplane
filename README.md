# agentplane

**Define project policy once. Render it into every agent harness. Route work across providers. Get auditable results.**

agentplane is a small, provider-neutral control plane for teams that use more than one AI coding agent (Claude Code, Codex, Cursor, Gemini CLI, local models, or anything with a headless CLI). It does four things and nothing else:

| | What | Command |
|---|---|---|
| 1 | **One policy, many harnesses.** `PROJECT.md` is the single source; `CLAUDE.md`, `AGENTS.md`, `.cursor/rules/project.mdc` (and more) are generated from it and guarded against hand edits. | `agentplane render` |
| 2 | **Understandable routing.** A *role* is a named row in `agentplane.toml`: provider, model, effort, timeout, read-only, fallback. Nothing is chosen by heuristics at runtime. | `agentplane routes` |
| 3 | **Auditable execution.** Every delegated task ends in a `HANDOFF.md` built from measured facts (git diff, exit code, duration, effective model), a typed status, and a JSONL ledger. | `agentplane run` |
| 4 | **Reproducible evals and diagnostics.** Fixture directories run through the same path and are graded deterministically; `doctor` says what is broken vs. merely optional. | `agentplane eval`, `agentplane doctor` |

It is *not* a skill library, an agent framework, or a swarm runtime. It sits under those and gives them one policy file, one routing table, and one result contract.

## Install

Python 3.11+ and git. No other runtime dependencies.

```bash
pipx install agentplane          # or: uv tool install agentplane
# from a checkout or a release wheel:
pipx install .                   # or: uv tool install .
```

Provider CLIs are optional. Install whichever you use (`claude`, `codex`, `cursor-agent`, `gemini`, `ollama`); agentplane detects them and works offline with a built-in mock provider when none is present.

### Use it with whatever plan you have

agentplane never talks to a model API itself. It launches the provider CLIs you already have, under the login and billing you already use, so pick the setup that matches your contract:

| You have | Use | Notes |
|---|---|---|
| A subscription that includes a CLI (Claude Pro/Max → `claude`, ChatGPT Plus/Pro → `codex`, Cursor → `cursor-agent`, Google AI → `gemini`) | roles on that provider | the CLI's own login is used; no API key is needed or forwarded |
| API keys instead of a subscription | the same CLIs configured for API billing, per each vendor's docs | keys stay in the CLI's own config; agentplane's environment allowlist does not forward them |
| Several of the above | one role per provider, `fallback` between them | `routes` shows which provider each role bills |
| No paid plan, or private data | `ollama` with a local model, `read_only` | offline, nothing leaves the machine |
| Nothing yet | the `mock` provider (`--role dry`) | exercises the whole pipeline without a model |

The model ids written by `agentplane init` are examples. Replace them with the ids your plan actually enables (each CLI can list its models), keep personal choices in `~/.config/agentplane/config.toml`, and check the resolved command with `agentplane run --role X --dry-run` before spending quota.

## Quickstart (5 minutes)

```bash
cd your-project
agentplane init                  # writes agentplane.toml + PROJECT.md, roles for the CLIs it finds
$EDITOR PROJECT.md               # describe the project once: purpose, stack, commands, quality gate, do-nots
agentplane render                # -> CLAUDE.md, AGENTS.md, .cursor/rules/project.mdc
agentplane routes                # what each role resolves to, and whether its provider is installed
agentplane doctor                # OK / WARN / NOTE; exit 1 only on real problems
```

Delegate a task and read the result:

```bash
agentplane run --role dry "Summarize the repo layout in five bullets"      # offline mock, always works
agentplane run --role review --read-only "Review src/ for missing error handling"
agentplane run --role impl --task-file PLAN.md --timeout 1200
cat HANDOFF.md                   # status, changed files, verification facts, provider output tail
agentplane runs                  # ledger of every run
```

Keep generated files honest in CI:

```bash
agentplane render --check        # exit 1 on drift
agentplane doctor                # exit 1 on WARN
```

Run the bundled offline eval suite:

```bash
agentplane eval run evals/suites/smoke --role dry
```

## How it fits together

```
PROJECT.md ──render──▶ CLAUDE.md / AGENTS.md / .cursor/rules/project.mdc / GEMINI.md / …
     ▲                      (generated marker; `guard` and `render --check` protect them)
     │
agentplane.toml ── roles ──▶ run --role X ──▶ provider CLI (env allowlist, timeout, sandbox flags)
     │                                            │
     └── providers (built-in + packs + user)      ▼
                                       HANDOFF.md + runs.jsonl + log   ←── evals grade these
```

- **Policy layer**: `PROJECT.md` (+ optional per-target appendices in `.agentplane/appendix/`). Model aliases `{{model:NAME}}` expand from `[models]`.
- **Routing layer**: `[roles.*]` in `agentplane.toml`; personal provider choices go in `~/.config/agentplane/config.toml` and never into the repo.
- **Execution layer**: `run` launches the provider headless with an allowlisted environment, a timeout, and the provider's own sandbox flags; it never passes flags that disable a harness's approvals.
- **Result contract**: exit code `0 done · 1 failed · 2 usage · 3 safety boundary · 4 empty output · 124 timeout`, plus the provider's typed self-report (`DONE`, `DONE_WITH_CONCERNS`, `BLOCKED`, `NEEDS_CONTEXT`) read only from its last line.
- **Extension**: packs (`pack.toml`) add providers, targets, roles, and appendices. See `packs/`.

Full docs: [docs/quickstart.md](docs/quickstart.md) · [docs/configuration.md](docs/configuration.md) · [docs/routing.md](docs/routing.md) · [docs/handoff.md](docs/handoff.md) · [docs/evals.md](docs/evals.md) · [docs/packs.md](docs/packs.md) · [docs/architecture.md](docs/architecture.md) · [docs/security.md](docs/security.md)

## Status

0.1.0, alpha. The Claude Code, Codex, and Cursor provider definitions mirror flags used in production; Gemini CLI is marked experimental. Provider CLIs change their flags; if one breaks, override the provider table in your user config and open an issue.

## License

MIT — see [LICENSE](LICENSE).
