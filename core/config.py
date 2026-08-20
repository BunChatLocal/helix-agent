"""
Helix Configuration
Loads from ~/.helix/config.json. All sensitive values come from secrets.py, not here.
"""

import json
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator


CONFIG_PATH = Path.home() / ".helix" / "config.json"
WORKSPACE_PATH = Path.home() / ".helix" / "workspace"

# Permission tiers. See ROLES.md for what each one may do.
TIER_STAFF = "staff"       # FRID employee: every command, no limit on subject
TIER_STUDENT = "student"   # pupil: no commands, IT questions only

# ─── Sub-models ───────────────────────────────────────────────────────────────

class ModelEntry(BaseModel):
    id: str
    alias: str
    tier: str  # fast | balanced | powerful
    description: str = ""


class ModelsConfig(BaseModel):
    roster: list[ModelEntry] = Field(default_factory=lambda: [
        ModelEntry(id="claude-haiku-4-5",  alias="haiku", tier="fast",      description="Heartbeats, compaction, simple tasks"),
        ModelEntry(id="claude-sonnet-4-6",          alias="sonnet", tier="balanced", description="General work, default"),
        ModelEntry(id="claude-opus-4-6",            alias="opus",   tier="powerful", description="Complex reasoning, heavy tasks"),
    ])
    default: str = "sonnet"
    heartbeat: str = "haiku"
    compaction: str = "haiku"

    def resolve(self, alias_or_id: str) -> str:
        """Return model ID for an alias or pass through a full model ID."""
        for m in self.roster:
            if m.alias == alias_or_id or m.id == alias_or_id:
                return m.id
        raise ValueError(f"Unknown model alias or ID: {alias_or_id!r}")

    @property
    def default_id(self) -> str:
        return self.resolve(self.default)

    @property
    def heartbeat_id(self) -> str:
        return self.resolve(self.heartbeat)

    @property
    def compaction_id(self) -> str:
        return self.resolve(self.compaction)


class DiscordConfig(BaseModel):
    enabled: bool = False
    allowed_users: list[str] = Field(default_factory=list)   # Discord snowflake IDs
    guild_channels: list[str] = Field(default_factory=list)  # Channel IDs to listen in
    mention_only: bool = False  # Only respond when mentioned in guild channels


class TelegramConfig(BaseModel):
    enabled: bool = False
    allowed_users: list[int] = Field(default_factory=list)   # Telegram user IDs


class RateLimitConfig(BaseModel):
    per_minute: int = 20
    per_hour: int = 200
    # Pupils get a tighter allowance. There is one Claude subscription behind
    # every conversation on this deployment, so a class of thirty pupils all
    # pressing send is the sharpest operational risk there is — and when the
    # quota goes, it goes for staff too. 0 falls back to the shared limit.
    student_per_minute: int = 8
    student_per_hour: int = 60

    def for_tier(self, tier: str) -> tuple[int, int]:
        """The (per-minute, per-hour) allowance for this tier."""
        if tier == TIER_STUDENT and self.student_per_minute > 0:
            return self.student_per_minute, self.student_per_hour or self.per_hour
        return self.per_minute, self.per_hour


class SecurityConfig(BaseModel):
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    web_fetch_domain_allowlist: list[str] = Field(default_factory=list)  # empty = all allowed
    inject_detection_enabled: bool = True
    audit_log_max_mb: int = 100
    # One Claude Code process per in-flight turn. 50 concurrent was clean on
    # this machine; 100 started failing with CLI exit 1. Overflow waits in
    # line instead of all launching at once. 0 = no cap.
    max_concurrent_claude: int = 50



class SessionConfig(BaseModel):
    routing: str = "per-channel-peer"
    daily_reset_hour: int = 4   # Hour in configured timezone
    idle_reset_minutes: int = 0    # 0 = no idle reset
    compaction_token_budget: int = 20000
    compaction_keep_pct: float = 0.6
    # Delete conversations untouched for this many days. 0 disables purging.
    # Conversations contain staff and pupil details — keep this bounded.
    retention_days: int = 30
    # Pupils get a shorter clock: they are minors, the conversation is usually a
    # single fixed problem, and there is no reason to hold it for a month. A
    # pupil who still needs theirs can keep a summary of the solution instead,
    # which resets the clock. 0 falls back to retention_days. See ROLES.md.
    student_retention_days: int = 5
    # Fraction of the usable context window at which the user must compact
    # before sending another message. Keeps long threads from silently
    # re-sending a huge history on every turn.
    context_limit_pct: float = 0.75


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    admin_port: int = 18791
    jwt_expiry_hours: int = 24
    # TEST ONLY. Lets anyone sign in by typing an email address, with no
    # verification whatsoever. Exists so the chat UI can be trialled before the
    # Entra app registration is available. Never enable on a reachable host.
    dev_login: bool = False


class NotificationsConfig(BaseModel):
    enabled: bool = False
    discord_user_id: str = ""   # Snowflake ID to DM on task completion
    telegram_user_id: int = 0   # Telegram user ID to DM on task completion


class CronJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    schedule: str        # standard 5-field cron expression e.g. "0 8 * * *"
    prompt: str          # message to send to the agent when this fires
    enabled: bool = True
    last_run: Optional[str] = None   # ISO timestamp
    model: Optional[str] = None      # alias/id override; None = use heartbeat model


class EntraConfig(BaseModel):
    """Microsoft Entra ID single sign-on for the staff-facing web chat.

    Secrets (client secret) live in the encrypted secret store, not here.
    """
    enabled: bool = False
    tenant_id: str = ""          # your directory (tenant) ID
    client_id: str = ""          # application (client) ID of the app registration
    redirect_uri: str = ""       # must match the app registration exactly
    # Restrict sign-in to your own tenant. Leaving this on means a personal or
    # foreign-tenant Microsoft account cannot get in even if it reaches the page.
    restrict_to_tenant: bool = True
    # Optional allow-list of Entra group object IDs. Empty = any tenant member.
    allowed_group_ids: list[str] = Field(default_factory=list)


class TiersConfig(BaseModel):
    """Which directory someone signs in from decides what they may do.

    FRID employees and pupils are in separate Entra tenants, so the `tid` claim
    already separates them. It arrives inside the id_token, so the client cannot
    set it, and it needs no group claim or directory lookup. A tenant in neither
    list is refused: there is no default tier and no implicit access.

    To verify a tenant id, fetch
    https://login.microsoftonline.com/<domain>/v2.0/.well-known/openid-configuration
    and read the `issuer` field — it carries the directory id.
    """
    staff_tenant_ids: list[str] = Field(default_factory=lambda: [
        "9c3d6c7b-385f-40cb-bf07-1edf937af70d",   # frid.no
        "fe578cc6-ca56-451f-a297-08c5b8011b0d",   # frid-iks.no
    ])
    student_tenant_ids: list[str] = Field(default_factory=lambda: [
        "4d6d8a90-10fd-4f78-8fc1-5e28844e0292",   # afk.no — Akershus fylkeskommune
    ])
    # Test sign-in has no tenant claim, so it maps the email domain instead.
    # Same tiers, same refusal for anything unrecognised, so the mapping is
    # exercised in testing rather than first meeting reality on the day SSO
    # goes live.
    staff_domains: list[str] = Field(default_factory=lambda: ["frid.no", "frid-iks.no"])
    student_domains: list[str] = Field(default_factory=lambda: ["afk.no"])

    @property
    def tenant_ids(self) -> list[str]:
        """Every tenant admitted, staff first."""
        return [*self.staff_tenant_ids, *self.student_tenant_ids]

    def for_tenant(self, tid: str) -> Optional[str]:
        """Tier for an Entra tenant id, or None if it is not admitted."""
        if not tid:
            return None
        if tid in self.staff_tenant_ids:
            return TIER_STAFF
        if tid in self.student_tenant_ids:
            return TIER_STUDENT
        return None

    def for_email(self, email: str) -> Optional[str]:
        """Tier for an email domain, or None if it is not admitted.

        Only for test sign-in. Real sign-ins go by tenant: the domain half of an
        address is not proof of anything, and staff and pupils can share one.
        """
        domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
        if not domain:
            return None
        if domain in [d.lower() for d in self.staff_domains]:
            return TIER_STAFF
        if domain in [d.lower() for d in self.student_domains]:
            return TIER_STUDENT
        return None


class HelixConfig(BaseModel):
    agent_id: str = "helix"
    agent_name: str = ""   # Human-readable display name set during setup
    timezone: str = "UTC"
    workspace_path: str = str(WORKSPACE_PATH)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    entra: EntraConfig = Field(default_factory=EntraConfig)
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    crons: list[CronJob] = Field(default_factory=list)
    log_level: str = "INFO"
    heartbeat_interval_minutes: int = 30

    @model_validator(mode="after")
    def validate_workspace(self) -> "HelixConfig":
        p = Path(self.workspace_path)
        p.mkdir(parents=True, exist_ok=True)
        return self


# ─── Load / Save ──────────────────────────────────────────────────────────────

_config_cache: Optional[HelixConfig] = None
_config_mtime: float = 0.0


def load_config(path: Path = CONFIG_PATH, force_reload: bool = False) -> HelixConfig:
    global _config_cache, _config_mtime
    current_mtime = path.stat().st_mtime if path.exists() else 0.0
    if _config_cache is not None and not force_reload and current_mtime == _config_mtime:
        return _config_cache

    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        _config_cache = HelixConfig.model_validate(raw)
        _config_mtime = current_mtime
    else:
        _config_cache = HelixConfig()
        save_config(_config_cache, path)
        _config_mtime = path.stat().st_mtime if path.exists() else 0.0

    return _config_cache


def save_config(cfg: HelixConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg.model_dump(), indent=2), encoding="utf-8")
    path.chmod(0o600)


def get_config() -> HelixConfig:
    return load_config()
