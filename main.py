"""
Helix — AI Agent Harness
Main entry point. Wires everything together and starts all services.

Usage:
  python main.py                    # Start Helix
  python main.py setup              # First-time setup wizard
  python main.py secrets set KEY V  # Manage secrets
  python main.py secrets list       # List secret keys
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path

def _get_version() -> str:
    """Derive version from the latest version tag in git log."""
    import re
    import subprocess
    try:
        log = subprocess.check_output(
            ["git", "log", "--oneline", "-50"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for line in log.splitlines():
            match = re.search(r"v(\d+\.\d+\.\d+)", line)
            if match:
                return match.group(1)
    except Exception:
        pass
    return "unknown"

VERSION = _get_version()

# ─── Logging setup ────────────────────────────────────────────────────────────

LOG_PATH = Path.home() / ".helix" / "logs" / "helix.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Explicit utf-8: agent names and responses contain emoji, and the
        # Windows default (cp1252) raises UnicodeEncodeError mid-log.
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
logger = logging.getLogger("helix.main")


# ─── CLI sub-commands ─────────────────────────────────────────────────────────

def _cli_secrets():
    from security.secrets import set_secret, get_secret, delete_secret, list_keys
    args = sys.argv[2:]
    if not args:
        print("Usage: main.py secrets [set KEY VALUE | get KEY | delete KEY | list]")
        return
    cmd = args[0]
    if cmd == "set" and len(args) == 3:
        set_secret(args[1], args[2])
        print(f"✓ Set {args[1]}")
    elif cmd == "get" and len(args) == 2:
        val = get_secret(args[1])
        print(val if val is not None else f"Key '{args[1]}' not found")
    elif cmd == "delete" and len(args) == 2:
        ok = delete_secret(args[1])
        print("Deleted" if ok else "Not found")
    elif cmd == "list":
        keys = list_keys()
        print("\n".join(keys) if keys else "(empty)")
    else:
        print("Unknown secrets command")


def _cli_config():
    """Print current config."""
    from core.config import load_config
    import json
    cfg = load_config()
    print(json.dumps(cfg.model_dump(), indent=2))


# ─── Scheduler helpers ────────────────────────────────────────────────────────

async def _run_retention_job(session_manager, days: int, student_days: int = 0):
    """Daily: delete conversations past the retention window, and prune images
    that no skill file references.

    Pupils have a shorter window than staff; assets are pruned on the longer one,
    since an image is only reachable from a conversation that still exists.
    """
    try:
        removed = await session_manager.purge_older_than(days, student_days)
        if removed:
            logger.info(f"Retention job removed {removed} session(s)")
    except Exception as e:
        logger.warning(f"Retention job error: {e}")

    try:
        _prune_unused_assets(days)
    except Exception as e:
        logger.warning(f"Asset prune error: {e}")

    try:
        _prune_stale_uploads()
    except Exception as e:
        logger.warning(f"Upload prune error: {e}")


def _prune_stale_uploads() -> None:
    """Sweep the temp upload directory, catching files orphaned by a restart."""
    from core.file_handler import UPLOAD_TTL_SECONDS, prune_stale_uploads

    removed = prune_stale_uploads(UPLOAD_TTL_SECONDS)
    if removed:
        logger.info(f"Removed {removed} stale upload(s)")


def _prune_unused_assets(days: int) -> None:
    """Delete images older than `days` that nothing references.

    Every uploaded screenshot is saved so the agent can never write a dead
    reference. Most are one-off chat attachments, so anything not cited by a
    skill file or a workspace document is removed once it has aged out.
    """
    import re as _re
    import time as _time
    from core.config import get_config

    ws = Path(get_config().workspace_path)
    assets = ws / "assets"
    if not assets.is_dir():
        return

    referenced = set()
    for md in ws.rglob("*.md"):
        try:
            referenced.update(_re.findall(r"asset:([A-Za-z0-9._-]+)", md.read_text(encoding="utf-8")))
        except OSError:
            continue

    cutoff = _time.time() - max(days, 1) * 86400
    removed = 0
    for f in assets.iterdir():
        if not f.is_file() or f.name in referenced:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info(f"Removed {removed} unreferenced image(s) older than {days} days")


async def _run_heartbeat_job(agent_loop, audit):
    """APScheduler job: run the agent's heartbeat check."""
    try:
        logger.debug("Running heartbeat check...")
        result = await agent_loop.run_heartbeat(channel="heartbeat", peer="heartbeat")
        if result:
            logger.info(f"Heartbeat response: {result[:200]}")
    except Exception as e:
        logger.warning(f"Heartbeat error: {e}")


# ─── Main service ─────────────────────────────────────────────────────────────

async def main():
    logger.info("Helix starting up...")

    from core.config import load_config
    from core.session_manager import SessionManager
    from core.agent_loop import AgentLoop
    from security.auth import AuthManager
    from security.audit import AuditLogger
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger

    cfg = load_config()

    # ── Core services ───────────────────────────────────────────────────────
    audit = AuditLogger()
    session_manager = SessionManager(agent_id=cfg.agent_id)
    await session_manager.start()
    logger.info(f"Session manager started (agent: {cfg.agent_id})")

    agent_loop = AgentLoop(session_manager=session_manager)
    auth = AuthManager(audit_logger=audit)

    logger.info(f"Agent loop initialized (default model: {cfg.models.default_id})")

    # ── Notification service ────────────────────────────────────────────────
    from core.notification_service import NotificationService
    notification_svc = NotificationService()

    # ── Task queue ──────────────────────────────────────────────────────────
    from core.queue_manager import QueueManager
    from channels.slash_commands import set_queue_manager
    queue_manager = QueueManager()
    queue_manager.set_agent_loop(agent_loop)
    queue_manager.set_notification_service(notification_svc)
    await queue_manager.start()
    set_queue_manager(queue_manager)
    logger.info("Task queue started")

    # ── Web Admin UI ────────────────────────────────────────────────────────
    from web.app import app as fastapi_app, init_web, _execute_cron
    import uvicorn

    # Scheduler created before init_web so the web API has a reference
    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    init_web(session_manager, agent_loop, scheduler, queue_manager=queue_manager,
             audit_logger=audit)

    web_config = uvicorn.Config(
        fastapi_app,
        host=cfg.web.host,
        port=cfg.web.admin_port,
        log_level="warning",
        access_log=False,
    )
    web_server = uvicorn.Server(web_config)

    async def _safe_serve():
        try:
            await web_server.serve()
        except SystemExit:
            logger.error(f"Web UI failed to start — port {cfg.web.admin_port} may be in use")
        except Exception as e:
            logger.error(f"Web UI error: {e}")

    asyncio.create_task(_safe_serve())
    logger.info(f"Web admin UI starting on http://{cfg.web.host}:{cfg.web.admin_port}")

    # ── Channels ────────────────────────────────────────────────────────────
    adapters = []

    if cfg.discord.enabled:
        from channels.discord_adapter import DiscordAdapter
        discord_adapter = DiscordAdapter(
            handler=None,
            auth=auth,
            agent_loop=agent_loop,
            session_manager=session_manager,
            audit_logger=audit,
        )
        await discord_adapter.start()
        adapters.append(discord_adapter)
        notification_svc.register_discord(discord_adapter)
        logger.info("Discord adapter started")
    else:
        logger.info("Discord disabled (set discord.enabled=true in config to enable)")

    if cfg.telegram.enabled:
        from channels.telegram_adapter import TelegramAdapter
        telegram_adapter = TelegramAdapter(
            handler=None,
            auth=auth,
            agent_loop=agent_loop,
            session_manager=session_manager,
            audit_logger=audit,
        )
        await telegram_adapter.start()
        adapters.append(telegram_adapter)
        notification_svc.register_telegram(telegram_adapter)
        logger.info("Telegram adapter started")
    else:
        logger.info("Telegram disabled (set telegram.enabled=true in config to enable)")

    # ── Scheduler: heartbeat + user cron jobs ───────────────────────────────
    first_hb = datetime.now() + timedelta(minutes=5)
    scheduler.add_job(
        _run_heartbeat_job,
        IntervalTrigger(minutes=cfg.heartbeat_interval_minutes),
        id="heartbeat",
        next_run_time=first_hb,
        args=[agent_loop, audit],
    )

    # Surface dead image references at startup. A dangling reference renders as
    # "Bildet er ikke tilgjengelig" to staff, which looks like a broken system.
    try:
        import re as _re
        _ws = Path(cfg.workspace_path)
        _have = {f.name for f in (_ws / "assets").iterdir()} if (_ws / "assets").is_dir() else set()
        _dangling = {}
        # Only knowledge files. AGENTS.md and commands/*.md contain example
        # names that are deliberately not real.
        for _md in (_ws / "skills").rglob("*.md"):
            try:
                for _n in _re.findall(r"asset:([A-Za-z0-9._-]+)", _md.read_text(encoding="utf-8")):
                    if _n not in _have and not _n.startswith("<"):
                        _dangling.setdefault(_md.name, set()).add(_n)
            except OSError:
                continue
        if _dangling:
            for _file, _names in _dangling.items():
                logger.warning(
                    f"BROKEN IMAGE REFS in {_file}: {', '.join(sorted(_names))} "
                    f"— these show as 'Bildet er ikke tilgjengelig' to users"
                )
    except Exception as _e:
        logger.debug(f"Asset reference check skipped: {_e}")

    retention_days = getattr(cfg.session, "retention_days", 0)
    student_days = getattr(cfg.session, "student_retention_days", 0)
    if retention_days and retention_days > 0:
        scheduler.add_job(
            _run_retention_job,
            CronTrigger(hour=3, minute=30, timezone=cfg.timezone),
            id="retention",
            args=[session_manager, retention_days, student_days],
        )
        logger.info(
            f"Retention: conversations purged daily at 03:30 — "
            f"{retention_days} days, pupils {student_days or retention_days}"
        )
        # Also sweep once at startup, so a long downtime doesn't leave expired
        # data sitting around until the next scheduled run.
        asyncio.create_task(
            _run_retention_job(session_manager, retention_days, student_days)
        )
    else:
        logger.warning("Retention disabled — conversations are kept indefinitely")

    for cron in cfg.crons:
        if cron.enabled:
            try:
                scheduler.add_job(
                    _execute_cron,
                    CronTrigger.from_crontab(cron.schedule, timezone=cfg.timezone),
                    id=f"cron_{cron.id}",
                    args=[cron.id],
                )
                logger.info(f"Scheduled cron: {cron.name} ({cron.schedule})")
            except Exception as e:
                logger.warning(f"Failed to schedule cron '{cron.name}': {e}")

    scheduler.start()
    logger.info(f"Scheduler started ({len(cfg.crons)} user cron(s) + heartbeat)")

    logger.info("Helix fully started.")
    logger.info(f"  Admin UI: http://{cfg.web.host}:{cfg.web.admin_port}")

    # ── Wait for shutdown ────────────────────────────────────────────────────
    stop_event = asyncio.Event()

    def _handle_signal(sig):
        logger.info(f"Received signal {sig}, shutting down...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows event loops don't support add_signal_handler. Fall back to
            # signal.signal and hop back onto the loop thread to set the event.
            signal.signal(
                sig,
                lambda s, _frame: loop.call_soon_threadsafe(_handle_signal, s),
            )

    await stop_event.wait()

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down...")
    scheduler.shutdown(wait=False)
    web_server.should_exit = True
    try:
        await queue_manager.stop()
    except Exception as e:
        logger.warning(f"Error stopping queue manager: {e}")
    for adapter in adapters:
        try:
            await adapter.stop()
        except Exception as e:
            logger.warning(f"Error stopping adapter: {e}")
    try:
        await session_manager.stop()
    except Exception:
        pass
    await asyncio.sleep(0.5)
    logger.info("Helix stopped.")


def _venv_python(venv_dir: Path) -> Path:
    """Path to the venv interpreter. Windows uses Scripts/, POSIX uses bin/."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _reexec(venv_python: Path):
    """Re-enter the venv interpreter. os.execv is unreliable on Windows, so
    spawn a child and forward its exit code there instead."""
    import os
    import subprocess
    argv = [str(venv_python), *sys.argv]
    if sys.platform == "win32":
        sys.exit(subprocess.run(argv).returncode)
    os.execv(str(venv_python), argv)


def _ensure_venv():
    """Bootstrap .venv if needed, then re-enter it."""
    import os
    venv_dir = Path(__file__).parent / ".venv"
    venv_python = _venv_python(venv_dir)
    requirements = Path(__file__).parent / "requirements.txt"

    if venv_python.exists():
        # Compare normalized paths — sys.prefix casing/separators vary on Windows
        if os.path.normcase(os.path.normpath(sys.prefix)) == os.path.normcase(
            os.path.normpath(str(venv_dir))
        ):
            return  # Already inside the venv
        print("  Activating virtual environment...")
        _reexec(venv_python)

    # Create venv
    import subprocess
    print("  Creating virtual environment...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Error creating venv: {result.stderr}")
        sys.exit(1)

    # Install requirements. Invoke pip via `python -m pip` — the standalone pip
    # shim is absent in some venvs and lives in Scripts/ on Windows.
    if requirements.exists():
        print("  Installing dependencies (this may take a minute)...")
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-q", "-r", str(requirements)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  Error installing dependencies: {result.stderr[:500]}")
            sys.exit(1)
        print("  Dependencies installed.")

    # Re-enter the venv so all imports work
    _reexec(venv_python)


if __name__ == "__main__":
    # Bootstrap venv before any project imports
    _ensure_venv()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "setup":
            from setup import run_setup
            run_setup()
            sys.exit(0)
        elif cmd == "secrets":
            _cli_secrets()
            sys.exit(0)
        elif cmd == "config":
            _cli_config()
            sys.exit(0)
        elif cmd == "version":
            print(f"Helix {VERSION}")
            sys.exit(0)
        elif cmd == "help":
            print(__doc__)
            sys.exit(0)

    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Helix failed to start")
        # Non-daemon threads (sqlite, task queue) keep the interpreter alive
        # after a fatal error, leaving a process that looks healthy to the PID
        # file while serving nothing. Force the exit so failures are visible.
        import os as _os
        _os._exit(1)
