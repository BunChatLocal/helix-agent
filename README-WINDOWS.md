# Helix on native Windows

This fork patches upstream Helix (Linux/macOS-only) to run natively on Windows,
and isolates the agent's Claude Code from your personal one.

## Setup

```powershell
cd C:\Users\jorgenthin_f\helix-agent
python main.py setup
```

No second login needed — Helix reuses your existing Claude Code credentials.

Start and stop:

```powershell
.\start.ps1
.\stop.ps1
```

Admin UI: http://127.0.0.1:18791

## Isolation from your personal Claude Code

The agent runs with `CLAUDE_CONFIG_DIR=~/.helix/claude`, so it does **not** see:

- your `~/.claude/settings.json` and permission rules
- your MCP servers, local or claude.ai connectors (see below)
- your session history, projects, and `~/.claude/CLAUDE.md` memory

It also runs with cwd = `~/.helix/workspace` (not this repo), and its permissions
are scoped to that workspace. `~/.claude/**` and the Helix secret store are in
the `deny` list, which takes precedence over `allow`.

**Auth is shared unless a yearly token is set.** By default only
`.credentials.json` is copied into Helix's config dir, and only when your copy
is newer, so token refreshes propagate. Nothing else crosses over.

For unattended `/chat` that must survive overnight, generate a one-year token
once (`claude setup-token`) and store it in Helix's secret store — not as a
Windows user environment variable, which would also pin your interactive CLI:

```powershell
.\.venv\Scripts\python.exe main.py secrets set CLAUDE_CODE_OAUTH_TOKEN "<the token>"
.\stop.ps1
.\start.ps1
```

Helix then passes that token only into the agent subprocess. `git pull` and
Claude CLI updates do not rotate it. Regenerate before it turns a year old, or
sooner if Anthropic revokes the session. Delete the secret to fall back to the
file login:

```powershell
.\.venv\Scripts\python.exe main.py secrets delete CLAUDE_CODE_OAUTH_TOKEN
```

Claude Code still writes the operator's account email into
`~/.helix/claude/.claude.json` and would inject it into every chat. Helix
blanks those fields before each call, keeps `USER.md` off the `/chat` prompt,
and tells the model the signed-in person is the only identity in the conversation.

**Important caveat:** sharing the login also carries over your claude.ai account
connectors (Exa, Spotify), and `--strict-mcp-config` does *not* block those —
they are account-level, not config-level. Two settings suppress them:

- `disableClaudeAiConnectors: true` in `~/.helix/claude/settings.json`
- `ENABLE_CLAUDEAI_MCP_SERVERS=false` in the agent's environment

Both are applied. Verified: `claude mcp list` under Helix's config reports no
servers while your normal CLI still shows Exa and Spotify. If you ever remove
those two settings, your connectors become visible to the agent again.

## Protecting your git repositories

The agent runs with cwd = `~/.helix/workspace`, which is not a git repo (upstream
ran it inside the Helix source tree — that was a real commit/merge risk).

Note that `Read`/`Edit` path scoping does **not** constrain `Bash`, which is
allowed broadly. So the state-changing git verbs are in the `deny` list:
`commit`, `push`, `pull`, `merge`, `rebase`, `reset`, `revert`, `checkout`,
`switch`, `restore`, `add`, `stash`, `clone`, `remote`, `branch`, `config`.
Read-only use (`git status`, `git log`, `git diff`) still works.

This is a command-pattern filter, not a sandbox — `git -C`, a shell alias, or a
script could route around it. It stops accidents, not a determined agent.

Edit agent permissions at `~/.helix/claude/settings.json` — never `~/.claude`.
Use `Edit(path)` rules, not `Write(path)`: Claude Code rejects `Write` rules in
file permission checks, and `Edit` already covers all file-editing tools.

## What was patched vs upstream

| File | Change |
|---|---|
| `main.py` | venv `Scripts\` paths; `python -m pip`; `subprocess` instead of `os.execv`; signal-handler fallback |
| `security/secrets.py` | registry `MachineGuid` instead of `os.getuid()` |
| `core/cli_backend.py` | `os.pathsep` for PATH; `.exe` lookup; isolated config dir; credential sync; workspace cwd; `--strict-mcp-config`; connectors off |
| `core/file_handler.py` | `tempfile.gettempdir()` instead of `/tmp` |
| `setup.py` | PowerShell launch; writes to Helix's own config dir, not `~/.claude` |
| `start.ps1` / `stop.ps1` | new — Windows equivalents of the `.sh` scripts |
| encoding (8 sites) | explicit `utf-8` on file reads/writes and the log handler; upstream relied on the locale default, which is cp1252 here and crashes on emoji and `æøå` |

`git pull` will conflict with these. The `.sh` scripts are left untouched.

## Known gaps

- Timezone auto-detection only reads `/etc/timezone` and `timedatectl`, so it
  falls back to UTC. Type your zone (e.g. `Europe/Oslo`) at the setup prompt.
- `start.ps1` checks dependency *presence*, not version ranges — comparing
  specifiers would need `packaging`, which is not a declared dependency.
- The Discord/Telegram adapters are still unexercised (both disabled).
- The venv launcher re-spawns the base interpreter, so the PID file records the
  parent while the child holds port 18791. `stop.ps1` kills every process whose
  command line contains this repo's `main.py` (literal match, not a regex) and
  `start.ps1` refuses to launch if that path is already running or the port is
  bound — a stale PID file is not enough to start a second copy.

Verified working end to end: setup wizard, `start.ps1` (admin UI returns HTTP
200), double-start guard, `stop.ps1` (port released), 43/43 tests.
