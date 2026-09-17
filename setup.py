"""
Helix Setup Wizard
Interactive first-run setup. Creates ~/.helix/, configures the agent,
sets Claude Code permissions, and optionally pairs a messaging platform.

Usage: python main.py setup
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from getpass import getpass
from pathlib import Path

HELIX_ROOT = Path(__file__).parent
HELIX_DIR = Path.home() / ".helix"
CONFIG_PATH = HELIX_DIR / "config.json"
WORKSPACE_PATH = HELIX_DIR / "workspace"
# Helix's own Claude Code config dir — deliberately NOT ~/.claude, so the agent
# never inherits the user's settings, MCP servers, history, or memory.
HELIX_CLAUDE_DIR = HELIX_DIR / "claude"
HELIX_CLAUDE_SETTINGS = HELIX_CLAUDE_DIR / "settings.json"
TEMPLATE_DIR = HELIX_ROOT / "workspace-template"


def _ask(prompt: str, default: str = "") -> str:
    """Prompt with optional default."""
    if default:
        result = input(f"{prompt} (press Enter for default '{default}'): ").strip()
        return result or default
    while True:
        result = input(f"{prompt}: ").strip()
        if result:
            return result
        print("  This field is required.")


def _ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    result = input(f"{prompt} ({hint}): ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def _ask_password(prompt: str) -> str:
    while True:
        pw = getpass(f"{prompt}: ")
        if len(pw) < 4:
            print("  Password must be at least 4 characters.")
            continue
        confirm = getpass("  Confirm password: ")
        if pw != confirm:
            print("  Passwords don't match. Try again.")
            continue
        return pw


def _valid_timezone(tz: str) -> bool:
    """Is this a resolvable IANA timezone name?"""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def _detect_timezone() -> str:
    """Auto-detect the system timezone."""
    # Try /etc/timezone (Debian/Ubuntu)
    tz_file = Path("/etc/timezone")
    if tz_file.exists():
        tz = tz_file.read_text().strip()
        if tz:
            return tz
    # Try timedatectl (systemd)
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Try TZ env var
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    # Try Python's own detection
    try:
        time.tzname[0]
        # tzname gives abbreviations like "CST" — not usable as IANA names
        # Fall back to reading the symlink
        tz_link = Path("/etc/localtime")
        if tz_link.is_symlink():
            target = str(tz_link.resolve())
            if "/zoneinfo/" in target:
                return target.split("/zoneinfo/")[1]
    except Exception:
        pass
    return "UTC"



def _check_claude_installed() -> bool:
    """Locate the claude binary.

    Not just shutil.which() — on Windows ~/.local/bin is commonly absent from
    PATH, so reuse the same fallback list the runtime backend uses.
    """
    if shutil.which("claude"):
        return True
    try:
        from core.cli_backend import _find_claude_bin
        return bool(_find_claude_bin())
    except Exception:
        return False


def _helix_claude_authenticated() -> bool:
    """Does Helix's isolated Claude Code config have usable credentials?

    Shares the user's existing login by copying just the credential file, so
    there's no second login to perform.
    """
    try:
        from core.cli_backend import sync_credentials
        return sync_credentials()
    except Exception:
        return (HELIX_CLAUDE_DIR / ".credentials.json").exists()


def _login_command() -> str:
    """Shell snippet to log in to Helix's isolated Claude Code config."""
    if os.name == "nt":
        return (
            f'    $env:CLAUDE_CONFIG_DIR = "{HELIX_CLAUDE_DIR}"\n'
            f"    claude   # complete the login, then exit with /exit"
        )
    return (
        f'    CLAUDE_CONFIG_DIR="{HELIX_CLAUDE_DIR}" claude\n'
        f"    # complete the login, then exit with /exit"
    )


def _setup_claude_permissions():
    """Write permissions into Helix's OWN Claude Code config directory.

    Deliberately does not touch the user's personal ~/.claude/settings.json.
    Access is scoped to the agent workspace rather than the whole home
    directory, and the user's own Claude config is explicitly denied.
    """
    # Claude Code permission globs use forward slashes on every platform
    workspace = WORKSPACE_PATH.as_posix()
    # resolve() expands Windows 8.3 short names (JORGEN~1 -> jorgenthin_f).
    # Without it the glob never matches the path the agent actually sees, and
    # every pasted screenshot is denied.
    _uploads_dir = Path(tempfile.gettempdir()) / "helix_uploads"
    uploads = _uploads_dir.resolve().as_posix()
    uploads_raw = _uploads_dir.as_posix()
    user_claude = (Path.home() / ".claude").as_posix()
    helix_home = HELIX_DIR.as_posix()

    HELIX_CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    # Load existing Helix settings or start fresh
    if HELIX_CLAUDE_SETTINGS.exists():
        settings = json.loads(HELIX_CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    else:
        settings = {}

    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    deny = permissions.setdefault("deny", [])

    # Edit(path) covers all file-editing tools including Write; a Write(path)
    # rule is not matched by file permission checks and Claude Code rejects it.
    needed = [
        f"Read({workspace}/**)",
        f"Edit({workspace}/**)",
        f"Read({uploads}/**)",
        f"Edit({uploads}/**)",
        # Both spellings, since which one the agent resolves can vary.
        f"Read({uploads_raw}/**)",
        f"Edit({uploads_raw}/**)",
        # Deliberately NO "Bash". The agent advises and never executes. The
        # primary enforcement is `--tools` in core/cli_backend.py, which
        # never offers the tool at all; this omission is the second layer.
    ]
    for entry in needed:
        if entry not in allow:
            allow.append(entry)

    # Keep the agent out of the user's personal Claude Code state and secrets.
    # deny takes precedence over allow.
    denied = [
        f"Read({user_claude}/**)",
        f"Edit({user_claude}/**)",
        f"Read({helix_home}/secrets.enc)",
        f"Read({helix_home}/.salt)",
        # Claude Code stores the operator's account email here and injects it
        # into every session. The agent must not be able to open the file.
        f"Read({helix_home}/claude/.claude.json)",
        # The Read/Edit scoping above does NOT constrain Bash — a blanket
        # "Bash" allow would otherwise let the agent mutate any git repo on
        # the machine. Block the state-changing git verbs. Best-effort: this
        # is a command-pattern filter, not a sandbox.
        "Bash(git commit:*)",
        "Bash(git push:*)",
        "Bash(git pull:*)",
        "Bash(git merge:*)",
        "Bash(git rebase:*)",
        "Bash(git reset:*)",
        "Bash(git revert:*)",
        "Bash(git checkout:*)",
        "Bash(git switch:*)",
        "Bash(git restore:*)",
        "Bash(git add:*)",
        "Bash(git stash:*)",
        "Bash(git clone:*)",
        "Bash(git remote:*)",
        "Bash(git branch:*)",
        "Bash(git config:*)",
        # IT-support profile: the agent diagnoses read-only. These block the
        # obvious state-changing commands. Defence in depth, NOT a sandbox —
        # anything that can run a shell can ultimately route around a prefix
        # filter. The real controls are the rules in AGENTS.md, the absence of
        # admin tooling, and the permissions of the account Helix runs as.
        "Bash(rm:*)",
        "Bash(rmdir:*)",
        "Bash(Remove-Item:*)",
        "Bash(shutdown:*)",
        "Bash(Restart-Computer:*)",
        "Bash(Stop-Computer:*)",
        "Bash(Stop-Service:*)",
        "Bash(Set-Service:*)",
        "Bash(Set-ExecutionPolicy:*)",
        "Bash(reg:*)",
        "Bash(regedit:*)",
        "Bash(diskpart:*)",
        "Bash(format:*)",
        "Bash(net user:*)",
        "Bash(net localgroup:*)",
        "Bash(Install-Module:*)",
        "Bash(winget install:*)",
        "Bash(winget uninstall:*)",
        # Tenant access: not installed today, denied so it stays a decision
        # rather than something the agent quietly starts doing.
        "Bash(az:*)",
        "Bash(Connect-AzAccount:*)",
        "Bash(Connect-MgGraph:*)",
        "Bash(Connect-ExchangeOnline:*)",
        # Belt and braces: even if Bash were somehow re-enabled, deny it wholesale.
        "Bash",
    ]
    for entry in denied:
        if entry not in deny:
            deny.append(entry)

    permissions.setdefault("defaultMode", "acceptEdits")
    settings["permissions"] = permissions

    # Sharing the login also exposes claude.ai account connectors (Exa, Spotify,
    # etc.), which --strict-mcp-config does NOT block. Turn them off explicitly.
    settings["disableClaudeAiConnectors"] = True
    # Claude Code auto-memory is a separate store from Helix MEMORY.md and is
    # not gated by --allowedTools. A /chat conversation must not persist the
    # person into ~/.helix/claude/projects/.../memory.
    settings["autoMemoryEnabled"] = False

    HELIX_CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print(f"  Helix Claude config written to {HELIX_CLAUDE_SETTINGS}")
    print(f"  Agent scoped to {workspace}/ (your ~/.claude is untouched)")


def _copy_templates(workspace: Path):
    """Copy workspace templates, skip files that already exist."""
    if not TEMPLATE_DIR.exists():
        print("  Warning: workspace-template/ not found, skipping.")
        return

    workspace.mkdir(parents=True, exist_ok=True)
    for item in TEMPLATE_DIR.iterdir():
        dest = workspace / item.name
        if item.is_dir():
            _copy_missing(item, dest)
        elif not dest.exists():
            shutil.copy2(item, dest)


def _copy_missing(src: Path, dest: Path) -> None:
    """Copy template files that are not already in the live workspace.

    Never overwrites. A machine that already ran setup still picks up new
    screenshots added to workspace-template/assets/ later.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            _copy_missing(child, target)
        elif not target.exists():
            shutil.copy2(child, target)


def _write_identity(workspace: Path, name: str, emoji: str):
    path = workspace / "IDENTITY.md"
    path.write_text(
        f"# IDENTITY.md - Who Am I?\n\n"
        f"- **Name:** {name}\n"
        f"- **Emoji:** {emoji}\n"
        f"- **Vibe:** [Edit this to describe your agent's tone]\n"
        f"- **Avatar:** _(optional)_\n",
        encoding="utf-8",  # agent emoji is non-ASCII; Windows defaults to cp1252
    )


def _write_user(workspace: Path, name: str, nickname: str, tz: str):
    path = workspace / "USER.md"
    path.write_text(
        f"# USER.md - About Your Human\n\n"
        f"- **Name:** {name}\n"
        f"- **What to call you:** {nickname}\n"
        f"- **Timezone:** {tz}\n\n"
        f"## Preferences\n\n"
        f"_(The agent will fill this in as it learns how you like to work)_\n\n"
        f"## Context\n\n"
        f"_(Background context about you — added over time through conversation)_\n",
        encoding="utf-8",  # names/timezones may contain non-ASCII (e.g. æøå)
    )


def run_setup():
    print()
    print("=" * 50)
    print("  Helix Setup")
    print("=" * 50)
    print()

    # ── Pre-flight checks ──────────────────────────────────────────────
    if not _check_claude_installed():
        print("Error: Claude Code CLI not found.")
        print("Install it first: https://docs.anthropic.com/claude-code")
        print("Then run setup again.")
        sys.exit(1)

    if CONFIG_PATH.exists():
        if not _ask_yn("Helix is already configured. Re-run setup?", default=False):
            print("Setup cancelled.")
            return

    # ── Step 1: Agent identity ─────────────────────────────────────────
    print("— Step 1: Name your agent\n")
    agent_name = _ask("  Agent name", default="Helix")
    agent_emoji = _ask("  Agent emoji", default="🧬")
    agent_id = agent_name.lower().replace(" ", "-")
    print()

    # ── Step 2: About you ──────────────────────────────────────────────
    print("— Step 2: About you\n")
    user_name = _ask("  Your name")
    nickname = _ask("  What should the agent call you?", default=user_name)
    detected_tz = _detect_timezone()
    print("  Your timezone:")
    print(f"  1) System default ({detected_tz})")
    print("  2) UTC")
    print("  Or type a custom timezone (e.g. America/New_York, Europe/London)")
    while True:
        tz_choice = input("  Choose (press Enter for 1): ").strip()
        if tz_choice == "2":
            user_tz = "UTC"
        elif tz_choice in ("", "1"):
            user_tz = detected_tz
        else:
            user_tz = tz_choice
        # Validate before writing it: an unrecognised zone is only caught at
        # startup, where it crashes the scheduler with an opaque error.
        if _valid_timezone(user_tz):
            break
        print(f"  '{user_tz}' is not a valid IANA timezone name.")
        print("  Use the Region/City form, e.g. Europe/Oslo — not a country name.")
    print(f"  Timezone set to: {user_tz}")
    print()

    # ── Step 3: Web admin password ─────────────────────────────────────
    print("— Step 3: Web admin password\n")
    print("  This protects the Helix admin UI.\n")
    web_password = _ask_password("  Set a password")
    print()

    # ── Step 4: Optional messaging platform ────────────────────────────
    print("— Step 4: Messaging platform (optional)\n")
    print("  Your agent is always available through the Helix web UI.")
    print("  You can also connect Discord or Telegram for mobile access.\n")

    discord_config = None
    telegram_config = None

    if _ask_yn("  Pair with a messaging platform?", default=False):
        print()
        print("  1) Discord")
        print("  2) Telegram")
        choice = ""
        while choice not in ("1", "2"):
            choice = input("  Choose (1 or 2): ").strip()

        if choice == "1":
            print()
            print("  Discord setup:")
            print("  1. Create a bot at https://discord.com/developers/applications")
            print("  2. Enable Message Content Intent + Server Members Intent")
            print("  3. Add the bot to your server")
            print()
            discord_token = _ask("  Discord bot token")
            discord_user_id = _ask("  Your Discord user ID (enable Developer Mode to find it)")
            discord_config = {
                "token": discord_token,
                "user_id": discord_user_id,
            }
        else:
            print()
            print("  Telegram setup:")
            print("  1. Message @BotFather on Telegram → /newbot")
            print("  2. Get your user ID from @userinfobot")
            print()
            telegram_token = _ask("  Telegram bot token")
            telegram_user_id = _ask("  Your Telegram user ID")
            telegram_config = {
                "token": telegram_token,
                "user_id": telegram_user_id,
            }
    print()

    # ── Apply everything ───────────────────────────────────────────────
    print("— Setting up...\n")

    # Create directory structure
    HELIX_DIR.mkdir(parents=True, exist_ok=True)
    (HELIX_DIR / "logs").mkdir(exist_ok=True)
    (HELIX_DIR / "sessions").mkdir(exist_ok=True)

    # Copy workspace templates
    _copy_templates(WORKSPACE_PATH)
    print("  Workspace templates installed")

    # Write personalized identity and user files
    _write_identity(WORKSPACE_PATH, agent_name, agent_emoji)
    _write_user(WORKSPACE_PATH, user_name, nickname, user_tz)
    print("  Agent identity and user profile written")

    # Build config
    config = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "timezone": user_tz,
        "workspace_path": str(WORKSPACE_PATH),
        "discord": {"enabled": discord_config is not None},
        "telegram": {"enabled": telegram_config is not None},
    }

    if discord_config:
        config["discord"]["allowed_users"] = [discord_config["user_id"]]
    if telegram_config:
        try:
            config["telegram"]["allowed_users"] = [int(telegram_config["user_id"])]
        except ValueError:
            config["telegram"]["allowed_users"] = [telegram_config["user_id"]]

    # Write config
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)
    print("  Config written to ~/.helix/config.json")

    # Store secrets
    from security.secrets import set_secret
    set_secret("WEB_PASSWORD", web_password)
    if discord_config:
        set_secret("DISCORD_TOKEN", discord_config["token"])
    if telegram_config:
        set_secret("TELEGRAM_TOKEN", telegram_config["token"])
    print("  Secrets encrypted and stored")

    # Set Claude Code permissions
    _setup_claude_permissions()

    # ── Done ───────────────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print()
    print(f"  Agent:    {agent_name} {agent_emoji}")
    print("  Admin UI: http://127.0.0.1:18791")
    print(f"  Config:   {CONFIG_PATH}")
    print(f"  Workspace: {WORKSPACE_PATH}")
    print(f"  Claude config: {HELIX_CLAUDE_DIR}  (isolated from your ~/.claude)")
    print()
    print("  You can add more channels anytime from the admin UI")
    print("  or just ask your agent to set them up for you.")
    print()
    print("  To manage permissions for additional directories, edit:")
    print(f"  {HELIX_CLAUDE_SETTINGS}")
    print()

    # Normally this shares the existing login from ~/.claude. It only fails if
    # the user's own Claude Code isn't logged in either.
    if not _helix_claude_authenticated():
        print("  ─" * 24)
        print("  ACTION REQUIRED: no Claude Code credentials found to share.")
        print("  Log in to Claude Code, then re-run setup — or log Helix in")
        print("  directly with:")
        print()
        print(_login_command())
        print("  ─" * 24)
        print()
        print("  To start after logging in: ./start.ps1" if os.name == "nt"
              else "  To start after logging in: ./start.sh")
        print()
        return False

    if _ask_yn("  Launch Helix now?"):
        if os.name == "nt":
            start_script = HELIX_ROOT / "start.ps1"
            cmd = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(start_script),
            ]
        else:
            start_script = HELIX_ROOT / "start.sh"
            cmd = ["bash", str(start_script)]
        # Do NOT capture output here. The launcher detaches a long-running
        # background process which inherits the stdout/stderr pipe handles;
        # subprocess.run would then block forever waiting for EOF that only
        # arrives when Helix itself exits. Let it write to the console.
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print()
            print("  Helix launched successfully!")
            print("  You may close this terminal.")
            print()
            print("  Admin UI: http://127.0.0.1:18791")
            print()
            print("  Finish customizing your agent and configurations from the web UI.")
            print()
        else:
            print("  Warning: could not start Helix automatically.")
            print("  Run manually: ./start.ps1" if os.name == "nt"
                  else "  Run manually: ./start.sh")
            print("  (the launcher's own error output is above)")
            print()
        return False

    print()
    print("  To start later: ./start.ps1" if os.name == "nt"
          else "  To start later: ./start.sh")
    print("  Or: python main.py")
    print()
    return False
