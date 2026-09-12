# Configuration

agentplane reads TOML from four layers. Later layers override earlier ones table-by-table.

| Layer | Where | Purpose |
|---|---|---|
| built-in | package data | provider definitions, render targets, `[run]` defaults |
| user | `$XDG_CONFIG_HOME/agentplane/config.toml` (default `~/.config/agentplane/config.toml`) | personal provider choices; never committed |
| packs | directories listed under `[packs] paths` | shareable bundles of providers, targets, roles, appendices |
| project | `agentplane.toml` in the project root | the team's policy file name, targets, models, roles |

`agentplane doctor` prints the layers it loaded.

## `[project]`

```toml
[project]
name = "my-service"
policy = "PROJECT.md"    # the single source that gets rendered
```

## `[render]`

```toml
[render]
targets = ["claude", "codex", "cursor"]
```

Built-in targets and their output paths:

| target | path |
|---|---|
| claude | `CLAUDE.md` |
| codex | `AGENTS.md` (also read by many other agents) |
| cursor | `.cursor/rules/project.mdc` (with frontmatter) |
| gemini | `GEMINI.md` |
| copilot | `.github/copilot-instructions.md` |
| windsurf | `.windsurfrules` |
| cline | `.clinerules` |

Each target appends `.agentplane/appendix/<target>.md` if that file exists. Define or override a target:

```toml
[targets.mytool]
path = "docs/MYTOOL.md"
frontmatter = "---\nkind: rules\n---\n"
appendix = ".agentplane/appendix/mytool.md"
```

## `[models]`

Aliases used by roles and by `{{model:ALIAS}}` tokens in `PROJECT.md`. Values are literal ids for the provider that runs them. `retired` lists ids that must never reappear (checked by `doctor`).

```toml
[models]
fast = "claude-sonnet-5"
strong = "claude-opus-5"
local = "gemma4:e2b"
retired = ["claude-opus-4-8"]
```

## `[roles.*]`

```toml
[roles.impl]
provider = "codex"        # required; a defined provider
model = "codex_default"   # alias or literal id; optional if the provider has default_model
effort = "low"            # must be listed in the provider's `efforts`
timeout = 900             # 1..7200 seconds; default [run] timeout
read_only = false         # true = provider is asked not to modify files
fallback = "impl_claude"  # role retried once on quota-exhausted / auth-required
```

Role names match `^[a-z][a-z0-9_-]{0,31}$`.

## `[providers.*]`

Built-in: `claude`, `codex`, `cursor`, `gemini` (experimental), `ollama`, `mock`. Any table with the same name overrides the built-in field by field; new names add providers.

```toml
[providers.opencode]
description = "OpenCode CLI"
binary = "opencode"                              # what `doctor` looks for (default: command[0])
command = ["opencode", "run", "--model", "{model}"]
model_args = []                                  # appended when a model is set
effort_args = []                                 # appended when an effort is set
efforts = []                                     # empty = effort unsupported
read_only_args = []                              # appended when read_only
write_args = []                                  # appended when not read_only
write_mode = ""                                  # value of {permission_mode} when writing
read_only_mode = ""                              # value of {permission_mode} when read-only
task_via = "arg"                                 # "stdin" (default) or "arg"
task_stdin_marker = ""                           # e.g. "-" for CLIs that need it
env_extra = []                                   # extra env names forwarded (never secret-like)
quota_markers = ["rate limit"]                   # lower-cased substrings → status quota-exhausted
auth_markers = ["not logged in"]                 # → status auth-required
default_model = ""                               # used when a role sets none
default_effort = ""
experimental = true
```

Placeholders available in `command` and the `*_args` lists: `{model}`, `{effort}`, `{task}`, `{dir}`, `{permission_mode}`, `{empty_mcp}` (path to an empty MCP config), and, inside packs, `{pack_dir}`.

With `task_via = "arg"`, `{task}` is substituted where it appears; if it appears nowhere the task is appended as the last argument.

agentplane refuses commands containing `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, or `--force`.

### Mock provider

`kind = "mock"` runs nothing. Useful for tests, evals of the pipeline itself, and trying agentplane without any CLI.

```toml
[providers.mock]
response = "custom output\nAGENTPLANE-STATUS: DONE"
exit_code = 0
[providers.mock.writes]
"notes/out.md" = "content written when not read_only"
```

Environment overrides: `AGENTPLANE_MOCK_RESPONSE`, `AGENTPLANE_MOCK_EXIT`, `AGENTPLANE_MOCK_SLEEP`.

## `[run]`

```toml
[run]
timeout = 900
min_output_bytes = 40   # exit 0 with less output → exit 4
max_depth = 2           # AGENTPLANE_DEPTH guard for nested delegation
env_extra = []          # names forwarded to providers in addition to the allowlist
```

Allowlist always forwarded: `HOME PATH USER LOGNAME SHELL TERM LANG LC_ALL LC_CTYPE COLORTERM NO_COLOR TMPDIR XDG_*`. Names containing KEY, TOKEN, SECRET, PASS, CRED, AUTH, COOKIE, SESSION, PRIVATE, BEARER, ACCESS or ending in `_PAT` are refused in `env_extra`.

## `[packs]`

```toml
[packs]
paths = ["packs/example-pack", "~/agentplane-packs/team"]
```

See [packs.md](packs.md).

## Environment variables

| Variable | Effect |
|---|---|
| `AGENTPLANE_STATE_DIR` | where logs, `runs.jsonl`, and eval results go (default `$XDG_STATE_HOME/agentplane`) |
| `XDG_CONFIG_HOME` | location of the user config layer |
| `AGENTPLANE_DEPTH` | set by agentplane for providers it launches; do not set by hand |
