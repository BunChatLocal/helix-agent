"""
Helix CLI Backend
Routes all AI calls through `claude -p` (Claude Code CLI) so they use
the Max subscription natively — no "extra usage" / API key required.

Session continuity via --resume: each (channel, peer) pair gets a persistent
Claude Code session that resumes across messages. New sessions use --session-id
to assign a known UUID. Falls back to a fresh session if resume fails.
"""

import asyncio
import time
import base64
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

logger = logging.getLogger("helix.cli_backend")


class AuthExpiredError(RuntimeError):
    """The shared Claude Code login is no longer valid.

    Distinct from a transient failure: no amount of retrying helps, a human
    must log in again on the Helix machine.
    """


class QuotaExhaustedError(RuntimeError):
    """The Max five-hour window is empty. Lining people up cannot create answers."""

    def __init__(self, resets_at: int = 0) -> None:
        self.resets_at = int(resets_at or 0)
        super().__init__("Claude Max quota exhausted")


def quota_when(resets_at: int) -> str:
    """Human time at which Max will accept calls again, in the org timezone."""
    if not resets_at:
        return "kvoten fylles opp igjen"
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        from core.config import get_config
        tz = ZoneInfo(get_config().timezone)
    except Exception:
        tz = ZoneInfo("Europe/Oslo")
    when = datetime.fromtimestamp(resets_at, tz)
    now = datetime.now(tz)
    clock = when.strftime("%H:%M")
    if when.date() == now.date():
        return f"kl. {clock}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"i morgen kl. {clock}"
    return when.strftime("%d.%m kl. %H:%M")


def quota_user_message(resets_at: int = 0) -> str:
    return (
        f"Jeg er ikke i stand til å svare på flere spørsmål før {quota_when(resets_at)}, "
        "hvis du trenger svar før det kan du snakke med IT-avdelingen"
    )


def _parse_reset_phrase(text: str) -> int:
    """Turn 'resets 6pm (Europe/Oslo)' into unix seconds of the next such clock."""
    if not text:
        return 0
    m = re.search(
        r"resets(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
        text,
        re.I,
    )
    if not m:
        return 0
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    tz_name = (m.group(4) or "").strip()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name or "Europe/Oslo")
    except Exception:
        tz = ZoneInfo("Europe/Oslo")
    now = datetime.now(tz)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return int(when.timestamp())


def _iter_json_blobs(blob: str):
    """Yield JSON objects from a CLI dump (one object, or stream-json lines)."""
    text = (blob or "").strip()
    if not text:
        return
    try:
        obj = json.loads(text)
        if isinstance(obj, (dict, list)):
            yield obj
            return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, (dict, list)):
            yield obj


def _quota_from_cli_output(blob: str) -> Optional[int]:
    """Return resetsAt unix seconds if this is a quota rejection, else None.

    Staff JSON is `{is_error: true, tokens: 0}` with no rate_limit_event.
    Pupil stream-json emits `rate_limit_event`. Both mean Max will not answer.
    """
    found = False
    resets = 0

    def walk(x) -> None:
        nonlocal found, resets
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower().replace("_", "")
                if lk == "resetsat":
                    try:
                        resets = max(resets, int(v))
                        found = True
                    except (TypeError, ValueError):
                        pass
                elif lk in ("ratelimittype", "ratelimitinfo") or (
                    lk == "type" and str(v).lower() in ("rate_limit_event", "rate_limit")
                ):
                    found = True
                elif lk == "overagedisabledreason" and "credit" in str(v).lower():
                    found = True
                walk(v)
            if x.get("is_error"):
                result = str(x.get("result") or "")
                parsed = _parse_reset_phrase(result)
                if parsed:
                    found = True
                    resets = max(resets, parsed)
                usage = x.get("usage") or {}
                tokens = usage.get("output_tokens", 1)
                if tokens == 0 and (
                    not result
                    or "session limit" in result.lower()
                    or "resets" in result.lower()
                    or x.get("api_error_status") == 429
                ):
                    found = True
        elif isinstance(x, list):
            for item in x:
                walk(item)
        elif isinstance(x, str):
            parsed = _parse_reset_phrase(x)
            if parsed:
                found = True
                resets = max(resets, parsed)
            low = x.lower()
            if "out_of_credits" in low or "rate_limit" in low or "session limit" in low:
                found = True

    for obj in _iter_json_blobs(blob):
        walk(obj)
    parsed = _parse_reset_phrase(blob or "")
    if parsed:
        found = True
        resets = max(resets, parsed)
    if found:
        return resets
    compact = (blob or "").replace(" ", "").lower()
    if '"is_error":true' in compact and '"output_tokens":0' in compact:
        m = re.search(r'"resetsAt"\s*:\s*(\d+)', blob or "")
        return int(m.group(1)) if m else parsed
    low = (blob or "").lower()
    if "out_of_credits" in low or "rate_limit_event" in low:
        m = re.search(r'"resetsAt"\s*:\s*(\d+)', blob)
        return int(m.group(1)) if m else 0
    return None


def _login_expired(blob: str) -> bool:
    """True only for an actual Claude login failure, not oauthAccount in a dump."""
    low = (blob or "").lower()
    return any(s in low for s in (
        "please run /login",
        "not logged in",
        "invalid api key",
        "authentication_error",
        "unauthenticated",
    ))


def _raise_cli_failure(err: str, out: str) -> None:
    """Map a Claude CLI dump to quota, login, or a generic crash."""
    blob = (err or "") + (out or "")
    resets_at = _quota_from_cli_output(blob)
    if resets_at is not None:
        _claude_queue().note_quota(resets_at)
        raise QuotaExhaustedError(resets_at)
    if _login_expired(blob):
        logger.critical(
            "CLAUDE LOGIN EXPIRED — every chat reply will fail until this is "
            "fixed. If Helix uses CLAUDE_CODE_OAUTH_TOKEN in the secret store, "
            "run: claude setup-token   then   python main.py secrets set "
            "CLAUDE_CODE_OAUTH_TOKEN <token>   and restart. Otherwise run: "
            "claude  (log in, then /exit). Helix picks a refreshed file login "
            "up automatically."
        )
        raise AuthExpiredError("Claude Code login has expired")
    raise RuntimeError(f"claude CLI exit: {err[:500] or out[:500]}")

def _find_claude_bin() -> str:
    """Locate the claude binary — checks PATH first, then common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    fallbacks = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude.cmd",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
    ]
    for path in fallbacks:
        if path.exists():
            return str(path)
    raise RuntimeError(
        "claude binary not found. Install Claude Code: https://docs.anthropic.com/claude-code"
    )

CLAUDE_BIN = _find_claude_bin()
HELIX_DIR = Path(__file__).parent.parent
DEFAULT_TIMEOUT = 3600  # seconds — long enough for nmap scans, brute force, etc.

# Which tools the model is offered, per tier. Leaving a tool out here is the real
# enforcement point: it is never presented to the model, so there is nothing to
# jailbreak into. Bash is absent for everyone — this agent advises, it never
# executes.
#
# A pupil gets NOTHING. Not even Read.
#
# Read was in this list, on the assumption that the subprocess cwd confined it to
# the workspace. It does not: cwd only decides where the agent starts, and
# `--add-dir` grants access to more directories rather than restricting anything.
# Measured, as a pupil: `~/.helix/config.json`, the operator's `USER.md`, the
# staff-only Azure procedures and files in the source tree outside the workspace
# were all readable — and by extension every other pupil's saved conversation.
#
# Permission rules can block paths, but `deny` beats `allow`, so "deny everything,
# permit one folder" cannot be expressed and any deny-list is one forgotten path
# away from a hole. So the pupil tier keeps no file tools at all, and the two
# things it actually needed them for are supplied directly instead: their guides
# are inlined into the system prompt (see context_engine.PUPIL_KNOWLEDGE) and
# pasted screenshots travel inside the message (see `images` below). Nothing left
# to escape from. See ROLES.md.
TIER_TOOLS = {
    "staff":   "Edit Read Write Glob Grep WebFetch WebSearch",
    "student": "",
}

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Helix gets its own Claude Code config directory, fully separate from the
# user's personal ~/.claude. This keeps the agent from inheriting their
# settings, MCP servers, session history, projects, and global CLAUDE.md.
# See README-WINDOWS.md.
HELIX_CLAUDE_CONFIG = Path.home() / ".helix" / "claude"
USER_CLAUDE_CONFIG = Path.home() / ".claude"


def sync_credentials() -> bool:
    """Share the user's existing Claude Code login instead of a second one.

    Copies ONLY the credential file into Helix's isolated config dir, and only
    when the user's copy is newer — so token refreshes in their normal CLI
    propagate here. Settings, MCP servers, history and memory stay isolated.

    Returns True if Helix has usable credentials afterwards.
    """
    src = USER_CLAUDE_CONFIG / ".credentials.json"
    dst = HELIX_CLAUDE_CONFIG / ".credentials.json"
    try:
        if not src.exists():
            return dst.exists()
        # Never clobber a newer Helix token with a stale one — either side may
        # have refreshed most recently.
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            return True
        HELIX_CLAUDE_CONFIG.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if os.name != "nt":
            dst.chmod(0o600)
        return True
    except OSError as e:
        logger.warning(f"Could not sync Claude credentials: {e}")
        return dst.exists()


_OAUTH_PII_FIELDS = ("emailAddress", "displayName", "organizationName")


def sanitize_helix_oauth_profile() -> bool:
    """Strip the operator's Claude account identity from Helix's config dir.

    Claude Code injects ``oauthAccount.emailAddress`` into every session as a
    ``# userEmail`` block. Helix talks to staff and pupils; that address is the
    operator's personal account and must never reach them.

    Only ``~/.helix/claude/.claude.json`` is touched — never ``~/.claude``.
    Returns True if personal fields were present and removed.
    """
    path = HELIX_CLAUDE_CONFIG / ".claude.json"
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        acct = data.get("oauthAccount")
        if not isinstance(acct, dict):
            return False
        changed = False
        for key in _OAUTH_PII_FIELDS:
            val = acct.get(key)
            if val:
                acct[key] = ""
                changed = True
        if not changed:
            return False
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.warning(
            "Stripped operator identity from Helix Claude profile — it must "
            "not reach staff or pupil chats"
        )
        return True
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not sanitise Helix Claude profile: {e}")
        return False


def _agent_cwd() -> str:
    """Working directory for the claude subprocess.

    The agent's own workspace, NOT the Helix source tree — otherwise it reads
    this repo instead of its memory files. Must stay consistent for --resume.
    """
    try:
        from core.config import load_config
        workspace = Path(load_config().workspace_path)
    except Exception:
        workspace = Path.home() / ".helix" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _build_env() -> dict:
    env = {**os.environ}
    env.setdefault("HOME", str(Path.home()))
    # Point Claude Code at Helix's own config dir. Set unconditionally — an
    # inherited CLAUDE_CONFIG_DIR would otherwise leak the user's config in.
    HELIX_CLAUDE_CONFIG.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(HELIX_CLAUDE_CONFIG)
    # Sharing the login also carries over claude.ai account connectors, which
    # --strict-mcp-config does not block. Disable them for the agent.
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    # Pick up any token refresh from the user's normal CLI before each call
    sync_credentials()
    sanitize_helix_oauth_profile()
    # A long-lived token (claude setup-token) belongs in the Helix secret store,
    # not a Windows user environment variable. If it is set, it is what the
    # subprocess uses; if it is not, a leftover user-wide value must not pin us.
    from security.secrets import get_secret
    token = (get_secret("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    else:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    local_bin = str(Path.home() / ".local" / "bin")
    # PATH is ';'-delimited on Windows, ':' elsewhere — joining with the wrong
    # separator corrupts PATH for the spawned claude process.
    default_path = "" if os.name == "nt" else "/usr/local/bin:/usr/bin:/bin"
    path = env.get("PATH", default_path)
    if local_bin not in path:
        env["PATH"] = f"{local_bin}{os.pathsep}{path}" if path else local_bin
    return env


def _image_block(path: Path) -> Optional[dict]:
    """An image as a base64 content block, or None if it isn't usable.

    A screenshot that cannot be read is not worth failing the whole turn over —
    the pupil still asked a question in words.
    """
    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        logger.warning(f"Not an image type we send inline, skipping: {path.name}")
        return None
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as e:
        logger.warning(f"Could not read attached image {path.name}: {e}")
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _stream_payload(user_message: str, images: list[Path]) -> bytes:
    """One stream-json user message, with any images ahead of the text.

    Images first because the text usually refers to them ("here's the error").
    """
    content: list[dict] = []
    for image in images:
        block = _image_block(image)
        if block:
            content.append(block)
    content.append({"type": "text", "text": user_message})
    payload = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(payload) + "\n").encode("utf-8")


def _usage_from(data: dict, model: str) -> dict:
    """Pull token counts out of a CLI result object.

    `modelUsage` is keyed by the *resolved* model name, which includes a date
    suffix and can differ from the alias we asked for, so an exact lookup often
    misses. Fall back to summing every entry rather than silently reporting zero.
    """
    usage_by_model = data.get("modelUsage", {}) or {}
    entry = usage_by_model.get(model)
    if entry is None:
        entry = {}
        for value in usage_by_model.values():
            for key in ("inputTokens", "outputTokens", "cacheReadInputTokens"):
                entry[key] = entry.get(key, 0) + value.get(key, 0)
            # Window size is a property of the model, not something to add up.
            for key in ("contextWindow", "maxOutputTokens"):
                entry[key] = max(entry.get(key, 0), value.get(key, 0))
    return {
        "input_tokens": entry.get("inputTokens", 0),
        "output_tokens": entry.get("outputTokens", 0),
        "cache_read_input_tokens": entry.get("cacheReadInputTokens", 0),
        "context_window": entry.get("contextWindow", 0),
        "max_output_tokens": entry.get("maxOutputTokens", 0),
    }


def _result_object(raw: str, streaming: bool) -> dict:
    """The CLI's final result object, from either output format."""
    if not streaming:
        return json.loads(raw)
    # stream-json emits one object per line; the last `result` is the outcome.
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result" or "result" in obj:
            return obj
    raise RuntimeError("no result object in claude CLI stream output")


async def _invoke(
    model: str,
    system: str,
    user_message: str,
    session_args: list[str],
    timeout: int,
    tier: str = "staff",
    images: Optional[list[Path]] = None,
) -> Tuple[str, str, dict]:
    """
    Run claude -p with the given session flags.
    Returns (response_text, session_id_used, usage).
    """
    tools = TIER_TOOLS.get(tier, TIER_TOOLS["staff"])
    images = images or []

    # No tools means no way to open an attached file, so the pupil path carries
    # images in the message itself. That needs stream-json input, which the CLI
    # only accepts alongside stream-json output, which in turn requires
    # --verbose. Staff keep the plain text/json path.
    streaming = not tools

    cmd = [CLAUDE_BIN, "-p"]
    if streaming:
        cmd += [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            # Empty string disables every built-in tool.
            "--tools", "",
        ]
    else:
        cmd += [
            "--output-format", "json",
            # NO Bash, for anyone. This agent advises; it never executes. It tells
            # the user which command to run and they run it themselves. Removing a
            # tool here is the real enforcement point — it is never offered to the
            # model, so there is nothing to jailbreak into. Do not re-add it.
            "--allowedTools", tools,
        ]
    # The prompt goes in a file, never on the command line. Windows caps a command
    # line at 32767 characters, and a pupil's prompt is already past that now that
    # their guides are inlined into it — passed as an argument it fails with
    # WinError 206 and every pupil message dies. A file also keeps staff safe as
    # their workspace grows: BOOTSTRAP_CHAR_LIMIT allows 150k.
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".md", delete=False
    )
    try:
        prompt_file.write(system)
        prompt_file.close()

        cmd += [
            "--model", model,
            "--system-prompt-file", prompt_file.name,
            # Ignore every MCP config source — the agent gets no MCP servers unless
            # they're added to Helix's own config dir explicitly.
            "--strict-mcp-config",
        ] + session_args

        logger.debug(
            f"CLI call: model={model}, tier={tier}, streaming={streaming}, "
            f"images={len(images)}, session_args={session_args}, "
            f"sys_len={len(system)}, msg_len={len(user_message)}"
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_agent_cwd(),
            env=_build_env(),
        )

        stdin_bytes = (
            _stream_payload(user_message, images)
            if streaming
            else user_message.encode("utf-8")
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(prompt_file.name)
        except OSError:
            pass

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        out = stdout.decode("utf-8", errors="replace")
        logger.error(f"claude CLI failed (exit {proc.returncode})\nSTDERR: {err[:1000]}\nSTDOUT: {out[:1500]}")
        _raise_cli_failure(err, out)

    raw = stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        raise RuntimeError("claude CLI returned empty output")

    data = _result_object(raw, streaming)

    if data.get("is_error"):
        _raise_cli_failure("", json.dumps(data))

    # A pupil's agent has no tools, so a denial here means something asked for one
    # anyway — worth seeing rather than swallowing.
    denials = data.get("permission_denials") or []
    if denials:
        logger.warning(f"Tool permission denied ({tier}): {str(denials)[:200]}")

    return data.get("result", ""), data.get("session_id", ""), _usage_from(data, model)


OnWait = Callable[[int, int], Awaitable[None]]


class _WaitTicket:
    """One place in the Claude process line."""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.update = asyncio.Event()
        self.position = 0
        self.held = False


class ClaudeQueue:
    """FIFO cap on in-flight `claude` processes, with a live place in line.

    `position` is 1-based among people still waiting: 1 means next to run,
    0 means this ticket already holds a slot.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_flight = 0
        self.waiters: list[_WaitTicket] = []
        self._mu = asyncio.Lock()
        self._last_grant = 0.0
        self._cool_until = 0.0
        self._fails: list[float] = []
        self._quota = False
        self._quota_resets_at = 0

    def busy(self) -> bool:
        return self.in_flight >= self.limit or bool(self.waiters)

    def note_ok(self) -> None:
        self._fails.clear()

    def note_failure(self) -> None:
        now = time.monotonic()
        self._fails = [t for t in self._fails if now - t < 20]
        self._fails.append(now)
        if len(self._fails) >= 5:
            self._cool_until = max(self._cool_until, now + 25)
            logger.warning(
                "Claude cooldown 25s — Max is rejecting calls; "
                "waiters stay in line instead of retrying into the same wall"
            )

    def note_quota(self, resets_at: int) -> None:
        now_wall = time.time()
        now_mono = time.monotonic()
        if resets_at and resets_at > now_wall:
            wait = min(resets_at - now_wall, 6 * 3600)
        else:
            wait = 60
        self._quota = True
        self._quota_resets_at = max(self._quota_resets_at, int(resets_at or 0))
        self._cool_until = max(self._cool_until, now_mono + wait)
        logger.warning(
            "Claude quota exhausted — failing the line instead of draining "
            "into a window that cannot answer"
        )
        self._wake_waiters()

    def _quota_blocked(self) -> bool:
        if not self._quota:
            return False
        if time.monotonic() >= self._cool_until:
            self._quota = False
            self._quota_resets_at = 0
            return False
        return True

    def quota_error(self) -> QuotaExhaustedError:
        return QuotaExhaustedError(self._quota_resets_at)

    def _assign_positions(self) -> None:
        for i, ticket in enumerate(self.waiters):
            ticket.position = i + 1

    def _wake_waiters(self) -> None:
        self._assign_positions()
        for ticket in self.waiters:
            ticket.update.set()

    def _grant_next(self) -> None:
        if self._quota_blocked():
            self._wake_waiters()
            return
        while self.waiters and self.in_flight < self.limit:
            nxt = self.waiters.pop(0)
            self.in_flight += 1
            nxt.held = True
            nxt.position = 0
            nxt.ready.set()
            nxt.update.set()
        self._wake_waiters()

    async def _pace(self) -> None:
        """Hold the slot until starting another claude.exe is safe."""
        while True:
            async with self._mu:
                if self._quota_blocked():
                    raise self.quota_error()
                now = time.monotonic()
                gap = _claude_gap()
                wait = max(0.0, self._last_grant + gap - now)
                cool = max(0.0, self._cool_until - now)
                delay = max(wait, cool)
                if delay <= 0:
                    self._last_grant = now
                    return
            await asyncio.sleep(min(delay, 1.0))

    async def acquire(self, on_wait: Optional[OnWait] = None) -> _WaitTicket:
        ticket = _WaitTicket()
        async with self._mu:
            if self._quota_blocked():
                raise self.quota_error()
            if self.in_flight < self.limit and not self.waiters:
                self.in_flight += 1
                ticket.held = True
                ticket.ready.set()
            else:
                self.waiters.append(ticket)
                self._grant_next()

        last_sent: Optional[int] = None
        try:
            while not ticket.ready.is_set():
                async with self._mu:
                    if self._quota_blocked():
                        raise self.quota_error()
                    pos = ticket.position
                    waiting = len(self.waiters)
                if on_wait and pos and pos != last_sent:
                    await on_wait(pos, waiting)
                    last_sent = pos
                ticket.update.clear()
                if ticket.ready.is_set():
                    break
                await ticket.update.wait()
            if on_wait and last_sent is not None:
                await on_wait(0, 0)
            await self._pace()
            return ticket
        except BaseException:
            await self._drop(ticket)
            raise

    async def _drop(self, ticket: _WaitTicket) -> None:
        async with self._mu:
            if ticket in self.waiters:
                self.waiters.remove(ticket)
                self._wake_waiters()
            elif ticket.held:
                ticket.held = False
                self.in_flight = max(0, self.in_flight - 1)
                self._grant_next()

    async def release(self, ticket: _WaitTicket) -> None:
        async with self._mu:
            if not ticket.held:
                return
            ticket.held = False
            self.in_flight = max(0, self.in_flight - 1)
            self._grant_next()


_QUEUE: Optional[ClaudeQueue] = None
_QUEUE_N = -1


def _claude_limit() -> int:
    try:
        from core.config import get_config
        n = int(get_config().security.max_concurrent_claude)
    except Exception:
        n = 20
    if n <= 0:
        return 10_000
    return n


def _claude_queue() -> ClaudeQueue:
    """Shared cap on in-flight `claude` processes. Lazy so tests can reset it."""
    global _QUEUE, _QUEUE_N
    n = _claude_limit()
    if _QUEUE is None or _QUEUE_N != n:
        _QUEUE = ClaudeQueue(n)
        _QUEUE_N = n
    return _QUEUE


def _claude_gap() -> float:
    """Seconds to wait between starting two claude.exe processes."""
    try:
        from core.config import get_config
        ms = int(get_config().security.claude_start_gap_ms)
    except Exception:
        ms = 400
    return max(0, ms) / 1000.0


def _skip_cli_retry(queue: ClaudeQueue) -> bool:
    """A class blast must drain. Retrying into the same wall doubles the load."""
    return (
        bool(queue.waiters)
        or queue.in_flight > 1
        or time.monotonic() < queue._cool_until
    )


def claude_slots_busy() -> bool:
    """True when a new turn would have to wait for a slot."""
    return _claude_queue().busy()


def reset_claude_gate() -> None:
    """Test helper: drop the cached queue."""
    global _QUEUE, _QUEUE_N
    _QUEUE = None
    _QUEUE_N = -1


async def call_claude(
    model: str,
    system: str,
    user_message: str,
    claude_session_id: Optional[str] = None,
    is_new_session: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
    tier: str = "staff",
    images: Optional[list[Path]] = None,
    on_wait: Optional[OnWait] = None,
) -> Tuple[str, str, dict]:
    """
    Call claude CLI. Returns (response_text, claude_session_id_used, usage_dict).

    usage_dict contains: input_tokens, output_tokens, cache_read_input_tokens,
    context_window, max_output_tokens — extracted from modelUsage in the CLI response.

    - New session (is_new_session=True or no claude_session_id): --session-id <new_uuid>
    - Resume: --resume <claude_session_id>
    - If resume fails for any reason, falls back to a fresh --session-id
    - on_wait(position, waiting) fires while this turn is queued; position 0
      means a slot was granted. Not called at all if we never waited.
    """
    queue = _claude_queue()
    ticket = await queue.acquire(on_wait=on_wait)
    try:
        try:
            result = await _call_claude_once(
                model, system, user_message, claude_session_id,
                is_new_session, timeout, tier, images,
            )
            queue.note_ok()
            return result
        except AuthExpiredError:
            raise
        except QuotaExhaustedError:
            raise
        except Exception as e:
            if _skip_cli_retry(queue):
                queue.note_failure()
                raise
            logger.warning(f"claude CLI failed, retrying once: {e}")
            await asyncio.sleep(2)
            try:
                result = await _call_claude_once(
                    model, system, user_message, None, True, timeout, tier, images,
                )
                queue.note_ok()
                return result
            except AuthExpiredError:
                raise
            except QuotaExhaustedError:
                raise
            except Exception:
                queue.note_failure()
                raise
    finally:
        await queue.release(ticket)


async def _call_claude_once(
    model, system, user_message, claude_session_id, is_new_session,
    timeout, tier, images,
) -> Tuple[str, str, dict]:
    if is_new_session or not claude_session_id:
        new_id = str(uuid.uuid4())
        logger.info(f"Starting new Claude session: {new_id} ({tier})")
        return await _invoke(model, system, user_message, ["--session-id", new_id], timeout, tier, images)

    try:
        logger.debug(f"Resuming Claude session: {claude_session_id}")
        return await _invoke(model, system, user_message, ["--resume", claude_session_id], timeout, tier, images)
    except AuthExpiredError:
        raise
    except QuotaExhaustedError:
        raise
    except Exception as e:
        logger.warning(f"Resume failed for session {claude_session_id}, falling back to new session: {e}")
        new_id = str(uuid.uuid4())
        return await _invoke(model, system, user_message, ["--session-id", new_id], timeout, tier, images)
