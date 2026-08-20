"""
Helix Web Admin UI
FastAPI backend on port 18791. WebSocket hub for real-time chat.
JWT auth. REST endpoints for sessions, memory, config, logs, crons.
"""

import asyncio
import collections
import json
import logging
import re
import time
import uuid
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import base64
import hashlib
import hmac
import secrets as secrets_mod
from urllib.parse import urlencode

import aiohttp
import jwt
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends,
                     Request, status, UploadFile, File)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core.config import get_config, save_config, TIER_STAFF, TIER_STUDENT
from core.skill_manager import create_skill, list_skills, read_skill, write_skill
from security.auth import RateLimiter
from security.secrets import get_secret, set_secret
from security.input_validator import sanitize_for_context
from channels.slash_commands import handle_slash
from main import VERSION

logger = logging.getLogger("helix.web")

app = FastAPI(title="Helix Admin", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:18791", "http://127.0.0.1:18791"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer(auto_error=False)

# Global references set by main.py
_session_manager = None
_agent_loop = None
_scheduler = None
_queue_manager = None
_audit = None

# ─── Login brute-force protection ─────────────────────────────────────────────
# Maps client IP -> deque of failed-attempt timestamps.
_LOGIN_FAILURES: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)
_LOGIN_MAX_FAILURES = 5          # attempts
_LOGIN_WINDOW_SECONDS = 60       # rolling window
_LOGIN_LOCKOUT_SECONDS = 300     # 5-minute lockout after max failures

def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login, False if locked out."""
    now = time.time()
    dq = _LOGIN_FAILURES[ip]
    # Purge entries outside the window
    while dq and now - dq[0] > _LOGIN_WINDOW_SECONDS:
        dq.popleft()
    return len(dq) < _LOGIN_MAX_FAILURES

def _record_login_failure(ip: str) -> None:
    _LOGIN_FAILURES[ip].append(time.time())

# ─── Chat rate limiting ───────────────────────────────────────────────────────
#
# One limiter for the whole process, keyed on the signed-in person rather than on
# the socket. The previous limiter was constructed inside the websocket handler,
# so a fresh allowance arrived with every reconnect and several tabs multiplied
# it — which is no limit at all. The allowance is tighter for pupils: one Claude
# subscription sits behind every conversation here. See ROLES.md.
_user_rate = RateLimiter()

# Someone who has hit the limit and keeps pressing send would otherwise write an
# audit line per attempt, which is a log-flooding vector as well as noise. Record
# that they hit it, once per window, not how many times they retried.
_RATE_AUDIT_EVERY = 300
_rate_audit_last: dict[str, float] = {}


def _first_rate_hit(key: str) -> bool:
    now = time.time()
    if now - _rate_audit_last.get(key, 0.0) < _RATE_AUDIT_EVERY:
        return False
    if len(_rate_audit_last) > 5000:
        for k in [k for k, t in _rate_audit_last.items()
                  if now - t > _RATE_AUDIT_EVERY]:
            del _rate_audit_last[k]
    _rate_audit_last[key] = now
    return True

# ─── Model ID allowlist pattern ───────────────────────────────────────────────
_MODEL_ID_RE = re.compile(r'^[a-zA-Z0-9._:-]{1,100}$')


def init_web(session_manager, agent_loop, scheduler=None, queue_manager=None,
             audit_logger=None):
    global _session_manager, _agent_loop, _scheduler, _queue_manager, _audit
    _session_manager = session_manager
    _agent_loop = agent_loop
    _scheduler = scheduler
    _queue_manager = queue_manager
    _audit = audit_logger


# ─── Auth ─────────────────────────────────────────────────────────────────────

JWT_SECRET_KEY = None

def _get_jwt_secret() -> str:
    global JWT_SECRET_KEY
    if JWT_SECRET_KEY is None:
        JWT_SECRET_KEY = get_secret("WEB_JWT_SECRET")
        if not JWT_SECRET_KEY:
            import secrets as _sec
            JWT_SECRET_KEY = _sec.token_hex(32)
            set_secret("WEB_JWT_SECRET", JWT_SECRET_KEY)
    return JWT_SECRET_KEY


def _create_token(
    sub: str = "admin",
    role: str = "admin",
    name: str = "",
    tier: str = TIER_STAFF,
    email: str = "",
) -> str:
    """Mint a session JWT.

    role="admin" — the operator, via the web password. Full management API.
    role="user"  — signed in through Entra. Chat only, own session only.

    `role` decides whether the management API opens; `tier` decides what may be
    asked and which commands exist. They are independent: the operator is
    role=admin and tier=staff, a FRID employee is role=user and tier=staff, a
    pupil is role=user and tier=student. See ROLES.md.

    `sub` is the session key and stays opaque — an Entra object id, so renaming a
    person does not detach them from their conversations. `email` is carried
    alongside for the audit records, which have to be readable by a human.
    """
    cfg = get_config()
    payload = {
        "sub": sub,
        "role": role,
        "name": name,
        "tier": tier,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=cfg.web.jwt_expiry_hours),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm="HS256")


def _tier(claims: dict) -> str:
    """The permission tier carried by a verified token.

    Tokens minted before tiers existed have no claim, and anything unrecognised
    is treated the same way: as a student. Failing to the restricted tier means a
    stale token loses commands rather than keeping them, and signing in again is
    the whole fix.
    """
    tier = claims.get("tier")
    return tier if tier in (TIER_STAFF, TIER_STUDENT) else TIER_STUDENT


def _decode_token(token: str) -> Optional[dict]:
    """Return the token claims, or None if invalid/expired."""
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
    except Exception:
        return None


def _verify_token(token: str) -> bool:
    return _decode_token(token) is not None


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Any valid session — admin or staff user. Returns the claims."""
    claims = _decode_token(credentials.credentials) if credentials else None
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return claims


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Operator only. Guards every management endpoint.

    Staff signed in via Entra hold role="user" and are rejected here — they must
    never reach memory, config, skills, sessions, logs or the queue.
    """
    claims = _decode_token(credentials.credentials) if credentials else None
    if not claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    if claims.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return claims


# ─── Auth endpoints ───────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(request: Request, body: dict):
    """Login with web password to get JWT."""
    client_ip = request.client.host if request.client else "unknown"

    # Brute-force protection: reject IPs that have failed too many times recently
    if not _check_login_rate(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {_LOGIN_LOCKOUT_SECONDS // 60} minutes.",
        )

    password = get_secret("WEB_PASSWORD")
    if not password:
        raise HTTPException(400, "Web password not set. Run: helix secrets set WEB_PASSWORD <pass>")

    provided = body.get("password", "")
    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(provided.encode(), password.encode()):
        _record_login_failure(client_ip)
        raise HTTPException(401, "Invalid password")

    return {
        "token": _create_token(
            sub="admin", role="admin", name="Administrator", tier=TIER_STAFF,
        ),
        "expires_in": get_config().web.jwt_expiry_hours * 3600,
    }


# ─── Microsoft Entra ID single sign-on (staff) ────────────────────────────────
#
# Authorization-code flow with PKCE. Staff sign in with their work account; we
# use the Entra object ID (oid) as the session key, so each person automatically
# gets their own private conversation with no code or password to remember.

_ENTRA_STATES: dict[str, dict] = {}          # state -> {verifier, created}
_ENTRA_STATE_TTL = 600                       # seconds


def _entra_cleanup_states() -> None:
    now = time.time()
    for k in [k for k, v in _ENTRA_STATES.items() if now - v["created"] > _ENTRA_STATE_TTL]:
        _ENTRA_STATES.pop(k, None)


def _entra_authority(cfg) -> str:
    """Which Microsoft endpoint sign-in goes through.

    FRID employees and pupils are in different tenants, and no single-tenant
    authority can serve two directories — so once more than one tenant is
    admitted, sign-in has to use the multi-tenant endpoint. That endpoint accepts
    every work and school account in the world, which makes the tenant allow-list
    in the callback the only thing standing between us and all of them. It is
    therefore unconditional and fail-closed; do not weaken it.
    """
    allowed = cfg.tiers.tenant_ids
    if allowed:
        target = allowed[0] if len(allowed) == 1 else "organizations"
        return f"https://login.microsoftonline.com/{target}"
    tenant = cfg.entra.tenant_id if cfg.entra.restrict_to_tenant else "organizations"
    return f"https://login.microsoftonline.com/{tenant}"


def _entra_error(code: int) -> RedirectResponse:
    """SSO is the /chat page. The admin UI at / is a password, not Entra."""
    return RedirectResponse(f"/chat?login_error={code}", status_code=302)


def _entra_success(token: str) -> RedirectResponse:
    return RedirectResponse(f"/chat#token={token}", status_code=302)


@app.get("/api/auth/microsoft/enabled")
async def entra_enabled():
    """Unauthenticated: lets the login page decide whether to show the SSO button."""
    cfg = get_config()
    return {"enabled": bool(cfg.entra.enabled and cfg.entra.client_id and cfg.entra.tenant_id)}


@app.get("/api/auth/microsoft/login")
async def entra_login():
    """Start sign-in: redirect the browser to Microsoft."""
    cfg = get_config()
    if not (cfg.entra.enabled and cfg.entra.client_id and cfg.entra.tenant_id):
        raise HTTPException(400, "Microsoft sign-in is not configured")

    _entra_cleanup_states()
    verifier = secrets_mod.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets_mod.token_urlsafe(32)
    _ENTRA_STATES[state] = {"verifier": verifier, "created": time.time()}

    params = {
        "client_id": cfg.entra.client_id,
        "response_type": "code",
        "redirect_uri": cfg.entra.redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{_entra_authority(cfg)}/oauth2/v2.0/authorize?{urlencode(params)}"
    return RedirectResponse(url, status_code=302)


@app.get("/api/auth/microsoft/callback")
async def entra_callback(request: Request):
    """Microsoft redirects here with a code. Exchange it and mint our own JWT."""
    cfg = get_config()
    params = request.query_params

    if params.get("error"):
        desc = params.get("error_description", params.get("error", "unknown"))
        logger.warning(f"Entra sign-in error: {desc}")
        return _entra_error(1)

    state = params.get("state", "")
    code = params.get("code", "")
    entry = _ENTRA_STATES.pop(state, None)
    if not entry or not code:
        # Unknown/replayed state — treat as CSRF and refuse.
        return _entra_error(1)
    if time.time() - entry["created"] > _ENTRA_STATE_TTL:
        return _entra_error(1)

    client_secret = get_secret("ENTRA_CLIENT_SECRET") or ""
    data = {
        "client_id": cfg.entra.client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.entra.redirect_uri,
        "code_verifier": entry["verifier"],
        "scope": "openid profile email User.Read",
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{_entra_authority(cfg)}/oauth2/v2.0/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    logger.warning(f"Entra token exchange failed: {payload.get('error_description', payload)}")
                    return _entra_error(1)
    except Exception as e:
        logger.error(f"Entra token exchange error: {e}")
        return _entra_error(1)

    id_token = payload.get("id_token")
    if not id_token:
        return _entra_error(1)

    # The id_token comes straight from Microsoft over TLS in response to our own
    # PKCE-bound request, so we read the claims without re-verifying the
    # signature. We still enforce tenant and audience ourselves below.
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except Exception:
        return _entra_error(1)

    oid = claims.get("oid") or claims.get("sub")
    tid = claims.get("tid", "")
    upn = claims.get("preferred_username") or claims.get("email") or ""
    name = claims.get("name") or upn or "Ukjent bruker"

    if not oid:
        return _entra_error(1)
    if claims.get("aud") and claims["aud"] != cfg.entra.client_id:
        logger.warning("Entra sign-in rejected: audience mismatch")
        return _entra_error(1)
    # The tenant decides the tier, and an unlisted tenant gets no tier and no
    # entry. This check is what makes the multi-tenant authority safe, so it runs
    # whenever tiers are configured, whatever restrict_to_tenant says.
    tier = cfg.tiers.for_tenant(tid)
    if cfg.tiers.tenant_ids:
        if not tier:
            logger.warning(f"Entra sign-in rejected: tenant {tid} is not admitted")
            return _entra_error(2)
    elif cfg.entra.restrict_to_tenant and tid != cfg.entra.tenant_id:
        logger.warning(f"Entra sign-in rejected: foreign tenant {tid}")
        return _entra_error(1)

    if cfg.entra.allowed_group_ids:
        groups = claims.get("groups") or []
        if not set(groups) & set(cfg.entra.allowed_group_ids):
            logger.warning(f"Entra sign-in rejected: {upn} not in an allowed group")
            return _entra_error(2)

    token = _create_token(
        sub=f"entra:{oid}", role="user", name=name, tier=tier or TIER_STAFF,
        email=upn,
    )
    logger.info(f"Entra sign-in: {name} <{upn}> as {tier or TIER_STAFF}")
    # Hand the token to the SPA via the fragment so it never lands in server
    # logs or the Referer header.
    return _entra_success(token)


# ─── Test sign-in (no verification) ───────────────────────────────────────────
#
# Lets you use the staff chat as an ordinary user before the Entra app
# registration exists. It verifies NOTHING — anyone who can reach the endpoint
# can become any identity. Guards, in order:
#   * off unless web.dev_login is explicitly true
#   * refused outright whenever Entra SSO is enabled, so the two can never be
#     live at once and this cannot linger after the real thing is wired up
#   * every use is logged, and the page shows a permanent TESTMODUS badge

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _dev_login_available(cfg) -> bool:
    return bool(cfg.web.dev_login) and not cfg.entra.enabled


@app.get("/api/auth/dev-login/enabled")
async def dev_login_enabled():
    return {"enabled": _dev_login_available(get_config())}


@app.post("/api/auth/dev-login")
async def dev_login(request: Request, body: dict):
    cfg = get_config()
    if not cfg.web.dev_login:
        raise HTTPException(404, "Not found")
    if cfg.entra.enabled:
        raise HTTPException(
            403,
            "Test sign-in is disabled because Entra SSO is enabled. "
            "Set web.dev_login to false.",
        )

    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(client_ip):
        raise HTTPException(429, "Too many attempts. Try again later.")

    email = (body.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        _record_login_failure(client_ip)
        raise HTTPException(400, "Enter a valid email address")

    # The domain decides the tier, exactly as the tenant does in production, and
    # an unknown domain is refused exactly as an unknown tenant is. Testing then
    # exercises the real decision path, refusal included, instead of meeting it
    # for the first time on the day SSO goes live.
    tier = cfg.tiers.for_email(email)
    if not tier:
        _record_login_failure(client_ip)
        staff = ", ".join(f"@{d}" for d in cfg.tiers.staff_domains)
        student = ", ".join(f"@{d}" for d in cfg.tiers.student_domains)
        raise HTTPException(
            403,
            f"Ukjent domene. Bruk {staff} for å teste som ansatt, "
            f"eller {student} for å teste som elev.",
        )

    name = body.get("name") or email.split("@")[0].replace(".", " ").title()
    logger.warning(
        f"TEST SIGN-IN (unverified) as {email} ({tier}) from {client_ip} — "
        f"web.dev_login is enabled"
    )
    return {
        "token": _create_token(
            sub=f"dev:{email}", role="user", name=name, tier=tier, email=email,
        ),
        "expires_in": cfg.web.jwt_expiry_hours * 3600,
    }


# ─── Staff conversations ──────────────────────────────────────────────────────
#
# Each signed-in person can hold several conversations, like the Claude desktop
# app. The session key is "<sub>|<conversation-id>". The <sub> half always comes
# from the verified JWT and the conversation id is validated as a UUID, so a
# client can only ever address its own namespace — there is no way to name
# another person's session, even by tampering with the request.

_CONV_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")

# A conversation is worth keeping once the agent has actually answered
# something — not merely once it has exchanged N messages. The agent is
# designed to ask one clarifying question first, so a two-message thread is
# usually "vague problem -> clarifying question", which has nothing to keep.
# Conversely a single good answer with steps is worth keeping even if short.
_CODE_FENCE = re.compile(r"```")
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+", re.M)

# An answer this long is substantive regardless of shape.
_SUBSTANTIVE_CHARS = 240
# Below this, a message ending in "?" is treated as a clarifying question.
_QUESTION_CHARS = 200


def _is_substantive_answer(text: str) -> bool:
    """Did this assistant message actually answer something?"""
    text = (text or "").strip()
    if not text:
        return False
    # A short message ending in a question mark is the agent asking, not answering.
    if len(text) < _QUESTION_CHARS and text.endswith("?"):
        return False
    if _CODE_FENCE.search(text):            # gave a command to run
        return True
    if len(_LIST_ITEM.findall(text)) >= 2:  # gave steps
        return True
    return len(text) >= _SUBSTANTIVE_CHARS


def _conversation_keepable(messages: list) -> bool:
    """True once the conversation contains at least one real answer."""
    return any(
        m.get("role") == "assistant" and _is_substantive_answer(m.get("content") or "")
        for m in messages
    )


def _retention_days(tier: str) -> int:
    """How long this tier's conversations are kept. 0 means indefinitely.

    Pupils have the shorter clock (ROLES.md). Read from config on every call
    rather than cached, so editing it in the admin UI takes effect without a
    restart, and quoted to the user from here so the countdown they see and the
    sweep that deletes them can never disagree.
    """
    cfg = get_config().session
    if tier == TIER_STUDENT and cfg.student_retention_days > 0:
        return cfg.student_retention_days
    return cfg.retention_days


def _peer_prefix(claims: dict) -> str:
    return f"{claims.get('sub') or 'admin'}|"


def _staff_peer(claims: dict, conversation_id: str) -> str:
    if not _CONV_ID_RE.match(conversation_id or ""):
        raise HTTPException(400, "Invalid conversation id")
    return f"{_peer_prefix(claims)}{conversation_id}"


_ATTACH_LINE = re.compile(r"^\[Attached image:[^\]]*\]\s*", re.M)
# Slash commands reach the agent wrapped in this envelope; it's plumbing, and
# using it as a conversation title shows the user their own machinery.
_CMD_ENVELOPE = re.compile(
    r"^\[Custom command /(\w+) triggered\].*?(?:User args:\s*)(.*)$", re.S
)


# Plumbing we add to the user's message before it reaches the agent. It must
# never be shown back to the user — it exposes local filesystem paths and reads
# as gibberish in their own chat bubble.
_ASSET_INSTRUCTION = re.compile(r"\[The images above are saved as permanent.*?\]", re.S)
_ASSET_INSTRUCTION_OLD = re.compile(r"\[These images are stored as reusable.*?\]", re.S)


def _clean_for_display(msg: dict) -> dict:
    """Strip internal markers from a message before sending it to the browser."""
    if msg.get("role") != "user":
        return msg
    text = msg.get("content") or ""
    text = _ASSET_INSTRUCTION.sub("", text)
    text = _ASSET_INSTRUCTION_OLD.sub("", text)
    text = _ATTACH_LINE.sub("", text).strip()
    return {**msg, "content": text}


def _audit_chat(event: str, claims: dict, **fields) -> None:
    """Record a chat event for operational insight.

    Deliberately separate from the conversations themselves: a pupil's chat is
    deleted on its own retention clock, but "what were people stuck on" has to
    outlive it, and an email plus a subject line is a far smaller thing to keep
    than a full transcript.

    Never raises — insight is worth less than the reply the person is waiting for.
    """
    if not _audit:
        return
    # Dev tokens carry a "dev:" prefix on the subject; the email claim is the
    # real address when it is there.
    email = (claims.get("email") or claims.get("sub") or "").removeprefix("dev:")
    try:
        _audit.log(
            event,
            channel="web",
            sender_id=email,
            name=claims.get("name") or "",
            domain=email.rsplit("@", 1)[-1].lower() if "@" in email else "",
            tier=_tier(claims),
            **fields,
        )
    except Exception as exc:
        logger.warning(f"Could not write audit event {event}: {exc}")


def _classify_topic(text: str, tier: str) -> str:
    """Which skill a question belongs to, for the coverage view. Never raises."""
    try:
        from core.knowledge import classify_topic
        return classify_topic(text, tier) or ""
    except Exception as exc:
        logger.debug(f"Could not classify topic: {exc}")
        return ""


def _remember_subject(text: str, skill: str) -> str:
    """Readable Insights bracket for this opening message. Never raises."""
    try:
        from core.subjects import remember_subject
        return remember_subject(text, skill) or ""
    except Exception as exc:
        logger.debug(f"Could not assign subject: {exc}")
        return ""


def _title_from_transcript(messages: list) -> str:
    """Derive a conversation title from its first user message.

    Strips the [Attached image: <path>] markers we prepend for the agent —
    they are plumbing, and showing a temp-file path in the sidebar is both
    ugly and leaks local paths into the UI.
    """
    for m in messages:
        if m.get("role") != "user":
            continue
        text = m.get("content") or ""
        env = _CMD_ENVELOPE.match(text)
        if env:
            # Show "/dokumenter <what the user actually typed>"
            text = f"/{env.group(1)} {env.group(2)}"
        text = _ATTACH_LINE.sub("", text).strip().replace("\n", " ")
        if text:
            return text[:60] + ("…" if len(text) > 60 else "")
    return ""


@app.get("/api/me/conversations")
async def my_conversations(claims: dict = Depends(require_auth)):
    """List only the caller's own conversations, newest first."""
    if not _session_manager:
        return {"conversations": []}
    prefix = _peer_prefix(claims)
    retention = _retention_days(_tier(claims))
    now = time.time()
    out = []
    for s in await _session_manager.list_sessions():
        if s.get("channel") != "web" or not str(s.get("peer", "")).startswith(prefix):
            continue
        try:
            msgs = await _session_manager.async_read_transcript(s["session_id"])
        except Exception:
            msgs = []
        # Connecting to a conversation creates its row, so every page load or
        # unused "Ny samtale" would otherwise leave an empty entry in the
        # sidebar forever. A conversation earns its place by having content.
        if not msgs:
            continue
        # Days until the retention job removes this conversation. None when
        # retention is off, or when the conversation has nothing in it yet.
        expires_in = None
        if retention and retention > 0 and msgs:
            age_days = (now - (s.get("last_active") or now)) / 86400
            expires_in = max(0, int(round(retention - age_days)))
        out.append({
            "id": str(s["peer"]).split("|", 1)[1],
            "title": _title_from_transcript(msgs),
            "last_active": s.get("last_active"),
            "message_count": len(msgs),
            "expires_in_days": expires_in,
            "can_keep": _conversation_keepable(msgs),
        })
    out.sort(key=lambda c: c.get("last_active") or "", reverse=True)
    return {"conversations": out}


@app.post("/api/me/conversations")
async def new_conversation(claims: dict = Depends(require_auth)):
    """Mint a new conversation id. The session row is created on first message."""
    return {"id": str(uuid.uuid4())}


@app.get("/api/me/conversations/{conversation_id}/messages")
async def my_conversation_messages(conversation_id: str, claims: dict = Depends(require_auth)):
    peer = _staff_peer(claims, conversation_id)
    if not _session_manager:
        return {"messages": []}
    # Look up without creating — reading a conversation that doesn't exist yet
    # must not leave a row behind, or simply browsing would spawn empty sessions.
    match = next(
        (s for s in await _session_manager.list_sessions()
         if s.get("channel") == "web" and s.get("peer") == peer),
        None,
    )
    if not match:
        return {"messages": []}
    msgs = await _session_manager.async_read_transcript(match["session_id"])
    return {"messages": [_clean_for_display(m) for m in msgs[-200:]]}


@app.delete("/api/me/conversations/{conversation_id}")
async def delete_my_conversation(conversation_id: str, claims: dict = Depends(require_auth)):
    peer = _staff_peer(claims, conversation_id)
    if _session_manager:
        await _session_manager.reset_session("web", peer)
    return {"ok": True}


IMAGE_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# ─── Documentation images ─────────────────────────────────────────────────────
#
# Screenshots the agent may show back to staff. These live in the workspace
# alongside the skill files, NOT in the temp upload dir, because they are part
# of the knowledge base and must survive the 6-hour upload cleanup.
#
# Only images attached to a documentation command are kept. Ordinary chat
# screenshots stay temporary — a user's screenshot of their own mailbox must
# not quietly become a permanent shared asset.

_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_DOC_COMMANDS = ("/dokumenter",)

# ─── Completion-claim guard ───────────────────────────────────────────────────
#
# The agent has no ability to change anything. If it ever states that a change
# has happened, a colleague may believe a user's account was fixed when nothing
# was done. Prompt rules reduce this but cannot guarantee it, so replies are
# checked before they are stored and shown.
#
# Deliberately narrow. "Kontoen er nå aktivert" is legitimate when it describes
# the result of a step the USER just performed, and appears in real procedures,
# so bare outcome statements are not matched. Only claims where the agent is
# the actor are caught — those are never correct.
_CLAIM_PATTERNS = [
    re.compile(p, re.I) for p in (
        r"\bjeg har (nå )?(resatt|tilbakestilt|nullstilt|endret|opprettet|slettet|"
        r"aktivert|deaktivert|tildelt|fjernet|lagt til|satt opp|kjørt|utført)\b",
        r"\bjeg (resatte|tilbakestilte|endret|opprettet|slettet|aktiverte|"
        r"deaktiverte|tildelte|fjernet|kjørte|utførte)\b",
        r"\bI (have |'ve )?(reset|changed|created|deleted|enabled|disabled|"
        r"assigned|removed|revoked|executed|ran)\b(?!\s+(a|an|the)?\s*(guide|"
        r"procedure|document|note|entry))",
        r"\bdette er (nå )?(gjort|utført|fullført)\b",
        r"\b(er|har blitt) (nå )?(utført|gjennomført) av meg\b",
    )
]

_CLAIM_NOTICE_NB = (
    "\n\n---\n⚠️ **Merk:** Jeg kan ikke utføre endringer — jeg kan bare forklare "
    "hva du skal gjøre. Hvis svaret over ga inntrykk av at noe allerede er gjort, "
    "stemmer ikke det. Ingenting er endret. Spør meg gjerne om stegene på nytt."
)


# A "how do I…" question must be answered with steps. If the reply *opens* by
# asserting the outcome, the agent has skipped to the end of the procedure and
# is telling the user it is already done. This is the exact shape of the real
# incident: "Kan du fortelle meg hvordan jeg resetter MFA?" -> "MFA er nå
# resatt for brukeren."
#
# Restricted to the opening of the reply so that the same wording remains legal
# later on, where it correctly describes the result of a step the user took.
_HOWTO_QUESTION = re.compile(
    r"\b(hvordan|hvorda|korleis|how (do|can) i|how to|kan du (fortelle|vise|forklare))\b",
    re.I,
)
_OUTCOME_ASSERTION = re.compile(
    r"\b\w+ (er|har blitt) (nå |no )?"
    r"(resatt|resett|tilbakestilt|nullstilt|aktivert|deaktivert|opprettet|"
    r"slettet|endret|tildelt|fjernet|godkjent|utført|fullført)\b",
    re.I,
)
# First sentence only. Inside a step list the same wording is correct — it is
# the result of a step the user just performed — so anything after the opening
# sentence is left alone.
_OPENING_CHARS = 160


def _completion_claim(text: str, user_message: str = "") -> Optional[str]:
    """Return the offending phrase if the reply claims something was done."""
    text = text or ""
    for pat in _CLAIM_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)

    if _HOWTO_QUESTION.search(user_message or ""):
        # Just the opening sentence/line — where a skipped-to-the-end answer
        # announces itself.
        head = text.lstrip()[:_OPENING_CHARS]
        head = re.split(r"(?<=[.!?])\s|\n", head, maxsplit=1)[0]
        m = _OUTCOME_ASSERTION.search(head)
        if m:
            return m.group(0)
    return None


def _assets_dir() -> Path:
    d = Path(get_config().workspace_path) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Filler words that commonly start a description and carry no meaning in a
# filename ("dette bildet hører til steg 6 i ..." -> "steg-6-i-...").
_SLUG_STOPWORDS = {
    "dette", "denne", "det", "den", "her", "er", "bildet", "bilde", "bilder",
    "skjermbildet", "skjermbilde", "skjermbilder", "hoerer", "hører", "til",
    "som", "og", "for", "en", "et", "av", "paa", "på", "i", "the", "this",
    "these", "image", "images", "screenshot", "screenshots", "of", "a", "to",
}


def _slugify(text: str, fallback: str = "bilde") -> str:
    text = (text or "").strip().lower()
    text = text.replace("å", "a").replace("ø", "o").replace("æ", "ae")
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    # Drop leading filler only — a stopword in the middle may be meaningful.
    while words and words[0] in _SLUG_STOPWORDS:
        words.pop(0)
    slug = "-".join(words)[:40].strip("-")
    return slug or fallback


def _persist_assets(paths: list, hint: str) -> list:
    """Copy uploaded screenshots into the workspace assets folder.

    Returns the stored asset names. Names are derived from the user's own
    description so the agent can refer to them meaningfully later.
    """
    import shutil as _shutil

    out = []
    base = _slugify(hint)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    for idx, src in enumerate(paths, start=1):
        try:
            suffix = Path(src).suffix.lower() or ".png"
            name = f"{base}-{stamp}-{idx}{suffix}"
            dest = _assets_dir() / name
            n = 1
            while dest.exists():
                n += 1
                name = f"{base}-{stamp}-{idx}-{n}{suffix}"
                dest = _assets_dir() / name
            _shutil.copy2(src, dest)
            out.append(name)
        except OSError as exc:
            logger.warning(f"Could not store documentation image: {exc}")
    return out


# Asset names a pupil is allowed to fetch: the ones cited by the guides they are
# actually given. Recomputed when those files change.
_ASSET_REF_RE = re.compile(r"asset:([A-Za-z0-9._-]{1,80})")
_pupil_assets_cache: tuple[float, frozenset] = (0.0, frozenset())


def _pupil_visible_assets() -> frozenset:
    """Images cited by the pupil-facing guides, and nothing else.

    The assets folder holds the whole knowledge base, including screenshots of
    Azure PIM and the MFA reset procedure. Pupils are given two guides and those
    guides cite images they genuinely need, so the boundary is the citation: an
    image referenced by a guide a pupil was handed is theirs to load, and
    everything else is not.
    """
    global _pupil_assets_cache
    from core.context_engine import PUPIL_KNOWLEDGE

    workspace = Path(get_config().workspace_path)
    files = [workspace / f for f in PUPIL_KNOWLEDGE]
    stamp = 0.0
    for f in files:
        try:
            stamp = max(stamp, f.stat().st_mtime)
        except OSError:
            continue
    cached_stamp, cached = _pupil_assets_cache
    if cached and stamp == cached_stamp:
        return cached

    names = set()
    for f in files:
        try:
            names.update(_ASSET_REF_RE.findall(f.read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            logger.warning(f"Could not scan {f} for pupil-visible images: {exc}")
    result = frozenset(names)
    _pupil_assets_cache = (stamp, result)
    return result


@app.get("/api/assets/{name}")
async def get_asset(name: str, claims: dict = Depends(require_auth)):
    """Serve a documentation image.

    The name is validated against a strict charset and the resolved path is
    checked to be inside the assets folder, so a crafted name cannot escape it.
    Staff may load any of them; a pupil only those their own guides cite.
    """
    if not _ASSET_NAME_RE.match(name):
        raise HTTPException(404, "Not found")
    if _tier(claims) == TIER_STUDENT and name not in _pupil_visible_assets():
        logger.info(f"Asset not available to pupil {claims.get('sub')}: {name}")
        raise HTTPException(404, "Not found")
    root = _assets_dir().resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "Not found")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(path.suffix.lower())
    if not media:
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type=media)


@app.post("/api/me/upload")
async def upload_image(file: UploadFile = File(...), claims: dict = Depends(require_auth)):
    """Accept a pasted or picked screenshot.

    Returns an opaque id, never a path. The client cannot influence where the
    file lands or later name an arbitrary file — the server keeps the mapping
    and rebuilds the path itself when the message is sent.
    """
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype not in IMAGE_TYPES:
        raise HTTPException(400, "Only PNG, JPEG, GIF or WebP images are accepted")

    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is larger than 5 MB")
    if not data:
        raise HTTPException(400, "Empty file")

    # Trust the bytes, not the declared type — check the magic number.
    sigs = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not (any(data.startswith(s) for s in sigs) or is_webp):
        raise HTTPException(400, "That file doesn't look like an image")

    from core.file_handler import save_file
    name = f"{uuid.uuid4().hex}{IMAGE_TYPES[ctype]}"
    path = save_file(name, data)
    if not path:
        raise HTTPException(500, "Could not save the image")

    _UPLOADS[name] = {"path": str(path), "owner": claims.get("sub"), "at": time.time()}
    _prune_uploads()
    logger.info(f"Image upload {name} ({len(data)} bytes) by {claims.get('sub')}")
    return {"id": name, "size": len(data)}


# id -> {path, owner, at}. Server-side so a client can never name a path.
_UPLOADS: dict[str, dict] = {}


def _prune_uploads() -> None:
    """Forget expired uploads, and sweep the directory they live in.

    The map is dropped on restart, so files uploaded before one are no longer
    named by anything here — the mtime sweep is what removes those. Both matter:
    a pupil's screenshot is never copied into the knowledge base, so this
    directory is the only copy of it.
    """
    from core.file_handler import (UPLOAD_TTL_SECONDS, cleanup_file,
                                   prune_stale_uploads)
    now = time.time()
    for k in [k for k, v in _UPLOADS.items() if now - v["at"] > UPLOAD_TTL_SECONDS]:
        entry = _UPLOADS.pop(k, None)
        if entry:
            cleanup_file(Path(entry["path"]))
    prune_stale_uploads(UPLOAD_TTL_SECONDS)


def _resolve_uploads(ids: list, owner: str) -> list:
    """Map upload ids back to paths, honouring ownership. Unknown ids are dropped."""
    out = []
    for i in ids[:4]:                       # cap attachments per message
        entry = _UPLOADS.get(str(i))
        if entry and entry.get("owner") == owner and Path(entry["path"]).exists():
            out.append(Path(entry["path"]))
    return out


@app.get("/api/me")
async def whoami(claims: dict = Depends(require_auth)):
    cfg = get_config()
    return {
        "name": claims.get("name") or "",
        "role": claims.get("role") or "user",
        "tier": _tier(claims),
        "test_mode": _dev_login_available(cfg),
        "retention_days": _retention_days(_tier(claims)),
    }


@app.get("/api/me/conversations/{conversation_id}/context")
async def my_context(conversation_id: str, claims: dict = Depends(require_auth)):
    peer = _staff_peer(claims, conversation_id)
    if not _session_manager:
        return {"used": 0, "window": 0, "percent": 0.0, "blocked": False}
    cfg = get_config()
    return await _session_manager.context_status("web", peer, cfg.session.context_limit_pct)


# A pupil asking to keep a short exchange gets the clock reset and nothing else.
# Summarising three turns costs a model call to produce something no shorter than
# what is already there; the saving only appears once the thread is long enough
# that carrying it whole is the expensive option. "Not just 2-4 back and forths",
# per the decision in ROLES.md.
_PUPIL_SUMMARY_MIN_TURNS = 5
_PUPIL_SUMMARY_MIN_CHARS = 4000


def _worth_summarising(messages: list) -> bool:
    turns = sum(1 for m in messages if m.get("role") == "user")
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return turns >= _PUPIL_SUMMARY_MIN_TURNS or chars >= _PUPIL_SUMMARY_MIN_CHARS


_STAFF_SUMMARY_PROMPT = (
    "Summarise this support conversation into a briefing for whoever picks "
    "it up next. Keep the reported symptom, everything already ruled out, "
    "commands or checks already run and their results, and the current next "
    "step. Keep it factual and short."
)

# A pupil is not handing this to a technician — they are keeping the answer for
# themselves, so the summary is the fix and nothing else. No symptom history, no
# what-we-ruled-out, and nothing about how the support side works.
_PUPIL_SUMMARY_PROMPT = (
    "En elev vil ta vare på løsningen fra denne samtalen. Skriv KUN løsningen: "
    "de konkrete stegene som virket, i rekkefølge, slik eleven kan gjøre det "
    "igjen selv. Skriv på elevens eget språk, kort og enkelt, uten "
    "feilsøkingshistorikk og uten hva som ble utelukket underveis. Hvis "
    "problemet ikke ble løst, skriv i stedet én setning om hva eleven skal gjøre "
    "videre."
)


@app.post("/api/me/conversations/{conversation_id}/compact")
async def compact_my_conversation(conversation_id: str, claims: dict = Depends(require_auth)):
    """Keep this conversation: reset its retention clock, summarising if that helps.

    Staff get a handover briefing and a fresh context window — the same machinery
    as the /compact slash command, exposed so non-technical users never have to
    type a command. A pupil gets a short note of the solution in their own thread
    instead; nothing they wrote goes anywhere else. See ROLES.md.
    """
    peer = _staff_peer(claims, conversation_id)
    if not (_session_manager and _agent_loop):
        raise HTTPException(503, "Not ready")

    tier = _tier(claims)
    session = await _session_manager.get_or_create("web", peer, tier=tier)
    sid = session["session_id"]
    messages = await _session_manager.async_read_transcript(sid)
    if not _conversation_keepable(messages):
        # Nothing has actually been answered yet, so there's nothing to keep.
        # The UI surfaces this as a notice rather than an error.
        raise HTTPException(
            status_code=409,
            detail={"code": "nothing_to_keep", "messages": len(messages)},
        )

    if tier == TIER_STUDENT and not _worth_summarising(messages):
        await _session_manager.touch(sid)
        return {"ok": True, "summary": None, "kept": "as_is"}

    transcript = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
        for m in messages
    )
    cfg = get_config()
    try:
        model = cfg.models.resolve("haiku")
    except ValueError:
        model = cfg.models.default_id

    pupil = tier == TIER_STUDENT
    from core.cli_backend import call_claude
    try:
        summary, _, _usage = await call_claude(
            model=model,
            system=(
                "Du oppsummerer IT-hjelp for elever. Du skriver bare det som ble "
                "gjort for å løse problemet."
                if pupil else
                "You summarise IT-support conversations accurately and concisely."
            ),
            user_message=(
                f"{_PUPIL_SUMMARY_PROMPT if pupil else _STAFF_SUMMARY_PROMPT}\n\n"
                f"CONVERSATION:\n{transcript}"
            ),
            is_new_session=True,
        )
    except Exception as exc:
        logger.error(f"Compaction failed: {exc}")
        raise HTTPException(502, "Could not compact the conversation")

    await _session_manager.compact_session(sid, summary)
    if pupil:
        # Written into the pupil's own thread so the note is something they can
        # actually read, not just context the model sees on resume.
        await _session_manager.append_message(sid, "assistant", summary)
    return {"ok": True, "summary": summary, "kept": "summary", "in_thread": pupil}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Lightweight health check — returns minimal info to unauthenticated callers."""
    return {"status": "ok"}


@app.get("/api/status", dependencies=[Depends(require_admin)])
async def api_status():
    """Authenticated operational status — CLI, auth, prompt size, drift."""
    cfg = get_config()
    sessions = []
    if _session_manager:
        try:
            sessions = await _session_manager.list_sessions()
        except Exception:
            pass
    from core.ops_health import report as ops_report
    from core.workspace_sync import drift_report
    try:
        ops = ops_report(_read_audit_events(7))
    except Exception as exc:
        logger.warning(f"ops health failed: {exc}")
        ops = {
            "agent": cfg.agent_name or cfg.agent_id,
            "claude_cli": {"found": False, "path": ""},
            "auth": {"oauth_token": False, "credentials_file": False, "ready": False},
            "prompts": {"staff": None, "student": None},
            "last_chat_ts": None,
        }
    try:
        drift = drift_report()
    except Exception as exc:
        logger.warning(f"workspace drift failed: {exc}")
        drift = {"ok": True, "diverged": [], "missing_in_live": [], "only_in_live": []}
    ready = ops["claude_cli"]["found"] and ops["auth"]["ready"]
    return {
        "status": "ok" if ready else "degraded",
        "uptime": int(time.time() - _START_TIME),
        "sessions": len(sessions),
        "channels": [
            c for c, enabled in [
                ("discord", cfg.discord.enabled),
                ("telegram", cfg.telegram.enabled),
            ] if enabled
        ],
        **ops,
        "drift": {
            "ok": drift["ok"],
            "diverged": drift["diverged"],
            "missing_in_live": drift["missing_in_live"],
            "only_in_live": drift["only_in_live"],
        },
    }

_START_TIME = time.time()


# ─── Sessions API ─────────────────────────────────────────────────────────────

@app.get("/api/sessions", dependencies=[Depends(require_admin)])
async def list_sessions():
    if not _session_manager:
        return []
    return await _session_manager.list_sessions()


@app.delete("/api/sessions/{channel}/{peer}", dependencies=[Depends(require_admin)])
async def reset_session(channel: str, peer: str):
    if not _session_manager:
        raise HTTPException(404, "Session manager not running")
    await _session_manager.reset_session(channel, peer)
    return {"status": "reset"}


@app.post("/api/sessions/{channel}/{peer}/model", dependencies=[Depends(require_admin)])
async def set_session_model(channel: str, peer: str, body: dict):
    """Switch the model for a session. Clears claude_session_id so next turn starts fresh."""
    if not _session_manager:
        raise HTTPException(404, "Session manager not running")
    model_alias = body.get("model", "")
    cfg = get_config()
    try:
        model_id = cfg.models.resolve(model_alias)
    except ValueError:
        raise HTTPException(400, f"Unknown model: {model_alias}")
    session = await _session_manager.get_or_create(channel, peer)
    await _session_manager.set_model(session["session_id"], model_id)
    return {"status": "switched", "model": model_id}


@app.get("/api/sessions/{session_id}/transcript", dependencies=[Depends(require_admin)])
async def get_transcript(session_id: str):
    if not _session_manager:
        raise HTTPException(404, "Session manager not running")
    # Validate session_id is a well-formed UUID4 to prevent path traversal
    import uuid as _uuid
    try:
        _uuid.UUID(session_id, version=4)
    except ValueError:
        raise HTTPException(400, "Invalid session ID format")
    messages = await _session_manager.async_read_transcript(session_id)
    return {"session_id": session_id, "messages": messages}


# ─── Memory API ───────────────────────────────────────────────────────────────

@app.get("/api/memory/today", dependencies=[Depends(require_admin)])
async def get_today_memory():
    cfg = get_config()
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    cfg = get_config()
    path = Path(cfg.workspace_path) / "memory" / f"{today}.md"
    if not path.exists():
        return {"date": today, "content": "", "exists": False}
    return {"date": today, "content": path.read_text(encoding="utf-8"), "exists": True}


@app.get("/api/memory/files", dependencies=[Depends(require_admin)])
async def list_memory_files():
    cfg = get_config()
    memory_dir = Path(cfg.workspace_path) / "memory"
    if not memory_dir.exists():
        return []
    files = sorted(memory_dir.glob("*.md"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime} for f in files[:30]]



@app.get("/api/memory/long-term", dependencies=[Depends(require_admin)])
async def get_long_term_memory():
    cfg = get_config()
    path = Path(cfg.workspace_path) / "MEMORY.md"
    if not path.exists():
        return {"content": "", "exists": False}
    return {"content": path.read_text(encoding="utf-8"), "exists": True}


_MEMORY_MAX_BYTES = 64 * 1024  # 64 KB hard cap

@app.put("/api/memory/long-term", dependencies=[Depends(require_admin)])
async def update_long_term_memory(body: dict):
    cfg = get_config()
    path = Path(cfg.workspace_path) / "MEMORY.md"
    content = body.get("content", "")
    if len(content.encode("utf-8")) > _MEMORY_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Content exceeds {_MEMORY_MAX_BYTES // 1024} KB limit.",
        )
    path.write_text(content, encoding="utf-8")
    return {"status": "saved"}


# ─── Config API ───────────────────────────────────────────────────────────────

@app.get("/api/config", dependencies=[Depends(require_admin)])
async def get_cfg():
    cfg = get_config()
    data = cfg.model_dump()
    return data


@app.put("/api/config/discord", dependencies=[Depends(require_admin)])
async def update_discord_config(body: dict):
    cfg = get_config()
    if "enabled" in body:
        cfg.discord.enabled = bool(body["enabled"])
    if "allowed_users" in body:
        cfg.discord.allowed_users = body["allowed_users"]
    if "mention_only" in body:
        cfg.discord.mention_only = bool(body["mention_only"])
    save_config(cfg)
    return {"status": "saved"}


@app.put("/api/config/telegram", dependencies=[Depends(require_admin)])
async def update_telegram_config(body: dict):
    cfg = get_config()
    if "enabled" in body:
        cfg.telegram.enabled = bool(body["enabled"])
    if "allowed_users" in body:
        cfg.telegram.allowed_users = body["allowed_users"]
    save_config(cfg)
    return {"status": "saved"}


@app.post("/api/config/models/add", dependencies=[Depends(require_admin)])
async def add_model(body: dict):
    from core.config import ModelEntry
    cfg = get_config()
    model_id = body.get("id", "")
    model_alias = body.get("alias", "")
    if not _MODEL_ID_RE.match(model_id):
        raise HTTPException(422, "Invalid model id: must match [a-zA-Z0-9._:-]{1,100}")
    if not _MODEL_ID_RE.match(model_alias):
        raise HTTPException(422, "Invalid model alias: must match [a-zA-Z0-9._:-]{1,100}")
    entry = ModelEntry(
        id=model_id,
        alias=model_alias,
        tier=body.get("tier", "balanced"),
        description=body.get("description", ""),
    )
    # Remove existing with same alias
    cfg.models.roster = [m for m in cfg.models.roster if m.alias != entry.alias and m.id != entry.id]
    cfg.models.roster.append(entry)
    save_config(cfg)
    return {"status": "added", "model": entry.model_dump()}


# ─── Task Queue API ───────────────────────────────────────────────────────────

@app.get("/api/queue", dependencies=[Depends(require_admin)])
async def list_queue(limit: int = 30):
    if not _queue_manager:
        raise HTTPException(503, "Task queue not running")
    limit = min(max(limit, 1), 100)
    tasks = await _queue_manager.list_tasks(limit=limit)
    depth = await _queue_manager.queue_depth()
    return {"tasks": tasks, "depth": depth}


@app.post("/api/queue", dependencies=[Depends(require_admin)])
async def enqueue_task(body: dict):
    if not _queue_manager:
        raise HTTPException(503, "Task queue not running")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    model = body.get("model") or None
    task = await _queue_manager.add_task(prompt, "web", "admin", model)
    if task is None:
        raise HTTPException(429, "Queue is full (max 50 pending tasks)")
    return task


@app.delete("/api/queue/done", dependencies=[Depends(require_admin)])
async def clear_queue_done():
    if not _queue_manager:
        raise HTTPException(503, "Task queue not running")
    n = await _queue_manager.clear_done()
    return {"cleared": n}


@app.delete("/api/queue/{task_id}", dependencies=[Depends(require_admin)])
async def cancel_queue_task(task_id: str):
    if not _queue_manager:
        raise HTTPException(503, "Task queue not running")
    ok = await _queue_manager.cancel_task(task_id)
    if not ok:
        raise HTTPException(404, "Task not found or not cancellable (may be running/done)")
    return {"status": "cancelled"}


@app.get("/api/queue/{task_id}", dependencies=[Depends(require_admin)])
async def get_queue_task(task_id: str):
    if not _queue_manager:
        raise HTTPException(503, "Task queue not running")
    task = await _queue_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


# ─── Notifications Config API ─────────────────────────────────────────────────

@app.get("/api/config/notifications", dependencies=[Depends(require_admin)])
async def get_notifications_config():
    cfg = get_config()
    return cfg.notifications.model_dump()


@app.put("/api/config/notifications", dependencies=[Depends(require_admin)])
async def update_notifications_config(body: dict):
    cfg = get_config()
    if "enabled" in body:
        cfg.notifications.enabled = bool(body["enabled"])
    if "discord_user_id" in body:
        cfg.notifications.discord_user_id = str(body["discord_user_id"]).strip()
    if "telegram_user_id" in body:
        try:
            cfg.notifications.telegram_user_id = int(body["telegram_user_id"])
        except (ValueError, TypeError):
            raise HTTPException(400, "telegram_user_id must be an integer")
    save_config(cfg)
    return {"status": "saved"}


# ─── Crons API ────────────────────────────────────────────────────────────────

def _cron_next_run(cron_id: str) -> Optional[str]:
    """Get next scheduled run time for a cron job from APScheduler."""
    if not _scheduler:
        return None
    job = _scheduler.get_job(f"cron_{cron_id}")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


@app.get("/api/crons", dependencies=[Depends(require_admin)])
async def list_crons():
    cfg = get_config()
    return [
        {**c.model_dump(), "next_run": _cron_next_run(c.id)}
        for c in cfg.crons
    ]


@app.post("/api/crons", dependencies=[Depends(require_admin)])
async def create_cron(body: dict):
    from core.config import CronJob
    from apscheduler.triggers.cron import CronTrigger

    name = (body.get("name") or "").strip()
    schedule = (body.get("schedule") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    if not name or not schedule or not prompt:
        raise HTTPException(400, "name, schedule, and prompt are required")

    # Validate the cron expression before saving
    cfg = get_config()
    try:
        trigger = CronTrigger.from_crontab(schedule, timezone=cfg.timezone)
    except Exception as e:
        raise HTTPException(400, f"Invalid cron expression: {e}")

    cfg = get_config()
    cron = CronJob(
        name=name,
        schedule=schedule,
        prompt=prompt,
        enabled=bool(body.get("enabled", True)),
        model=body.get("model") or None,
    )
    cfg.crons.append(cron)
    save_config(cfg)

    if _scheduler and cron.enabled:
        _scheduler.add_job(
            _execute_cron,
            trigger,
            id=f"cron_{cron.id}",
            args=[cron.id],
        )

    return {**cron.model_dump(), "next_run": _cron_next_run(cron.id)}


@app.delete("/api/crons/{cron_id}", dependencies=[Depends(require_admin)])
async def delete_cron(cron_id: str):
    cfg = get_config()
    if not any(c.id == cron_id for c in cfg.crons):
        raise HTTPException(404, "Cron not found")
    cfg.crons = [c for c in cfg.crons if c.id != cron_id]
    save_config(cfg)
    if _scheduler:
        job = _scheduler.get_job(f"cron_{cron_id}")
        if job:
            job.remove()
    return {"status": "deleted"}


@app.patch("/api/crons/{cron_id}/toggle", dependencies=[Depends(require_admin)])
async def toggle_cron(cron_id: str):
    from apscheduler.triggers.cron import CronTrigger
    cfg = get_config()
    cron = next((c for c in cfg.crons if c.id == cron_id), None)
    if not cron:
        raise HTTPException(404, "Cron not found")

    cron.enabled = not cron.enabled
    save_config(cfg)

    if _scheduler:
        job = _scheduler.get_job(f"cron_{cron_id}")
        if cron.enabled and not job:
            try:
                trigger = CronTrigger.from_crontab(cron.schedule, timezone=get_config().timezone)
                _scheduler.add_job(
                    _execute_cron,
                    trigger,
                    id=f"cron_{cron_id}",
                    args=[cron_id],
                )
            except Exception as e:
                logger.warning(f"Failed to re-schedule cron {cron.name}: {e}")
        elif not cron.enabled and job:
            job.remove()

    return {"status": "ok", "enabled": cron.enabled, "next_run": _cron_next_run(cron_id)}


@app.post("/api/crons/{cron_id}/run", dependencies=[Depends(require_admin)])
async def run_cron_now(cron_id: str):
    cfg = get_config()
    cron = next((c for c in cfg.crons if c.id == cron_id), None)
    if not cron:
        raise HTTPException(404, "Cron not found")
    if not _agent_loop:
        raise HTTPException(503, "Agent loop not running")
    asyncio.create_task(_execute_cron(cron_id))
    return {"status": "triggered"}


async def _execute_cron(cron_id: str):
    """Execute a cron job by ID. Used by APScheduler and the 'run now' endpoint."""
    from core.config import load_config, save_config as _save

    cfg = load_config(force_reload=True)
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    cron = next((c for c in cfg.crons if c.id == cron_id), None)
    if not cron or not cron.enabled:
        return

    logger.info(f"Running cron job: {cron.name} ({cron_id})")
    if not _agent_loop:
        logger.warning("Agent loop not available for cron execution")
        return

    result = await _agent_loop.run_cron(cron.id, cron.name, cron.prompt, cron.model)
    if result:
        logger.info(f"Cron [{cron.name}]: {result[:200]}")

    # Update last_run (re-load to avoid overwriting concurrent writes)
    cfg2 = load_config(force_reload=True)
    cron2 = next((c for c in cfg2.crons if c.id == cron_id), None)
    if cron2:
        cron2.last_run = datetime.now(tz).isoformat()
        _save(cfg2)


# ─── Snapshot API ────────────────────────────────────────────────────────────

@app.post("/api/sessions/{channel}/{peer}/snapshot", dependencies=[Depends(require_admin)])
async def take_snapshot(channel: str, peer: str):
    """Write a detailed memory snapshot of the current session to today's daily log.
    Session is left completely untouched — no compaction, no context loss."""
    if not _session_manager:
        raise HTTPException(404, "Session manager not running")
    if not _agent_loop:
        raise HTTPException(503, "Agent loop not running")

    cfg = get_config()
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    ts = datetime.now(tz).strftime("%H:%M")

    session = await _session_manager.get_or_create(channel, peer)
    session_id = session["session_id"]
    messages = await _session_manager.async_read_transcript(session_id)

    if not messages:
        return {"status": "empty", "message": "No conversation to snapshot."}

    # Build a plain-text transcript for the summarization prompt
    transcript_text = "\n\n".join(
        f"[{m['role'].upper()}]: {m['content']}" for m in messages
    )

    snapshot_prompt = (
        "You are writing a detailed memory entry for a personal AI agent's daily log. "
        "Below is a conversation transcript. Your job is to produce a thorough, structured "
        "summary that captures: key topics discussed, decisions made, tasks completed, "
        "important facts or preferences shared by the user, any follow-up items, and "
        "anything the agent should remember for future conversations. "
        "Write in past tense, third-person style (e.g. 'User asked about...', 'Agent explained...'). "
        "Be detailed — this entry replaces the agent's in-context memory before a compaction. "
        "Do NOT include any preamble like 'Here is the summary'. Just the memory entry.\n\n"
        f"TRANSCRIPT:\n{transcript_text}"
    )

    # Use the compaction model (haiku) to write the snapshot
    from core.cli_backend import call_claude
    compaction_model = cfg.models.compaction_id
    summary, _, _ = await call_claude(
        model=compaction_model,
        system="You are an expert at writing detailed memory entries for AI agent logs.",
        user_message=snapshot_prompt,
        is_new_session=False,
    )

    if not summary:
        raise HTTPException(500, "Snapshot generation returned empty response")

    # Append to today's daily memory file
    memory_dir = Path(cfg.workspace_path) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_path = memory_dir / f"{today}.md"

    entry = f"\n\n---\n\n## 📸 Session Snapshot [{ts}]\n\n{summary.strip()}\n"
    with open(memory_path, "a", encoding="utf-8") as f:
        f.write(entry)

    logger.info(f"Snapshot written to {memory_path}")
    return {"status": "ok", "message": f"Snapshot saved to memory/{today}.md", "date": today}


# ─── Logs API ─────────────────────────────────────────────────────────────────

def _audit_log_path() -> Path:
    return Path.home() / ".helix" / "logs" / "audit.log"


@app.get("/api/logs/audit", dependencies=[Depends(require_admin)])
async def get_audit_logs(lines: int = 100):
    lines = min(max(lines, 1), 1000)  # Cap to prevent memory abuse
    audit_path = _audit_log_path()
    if not audit_path.exists():
        return {"lines": []}
    with open(audit_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    recent = all_lines[-lines:]
    parsed = []
    for line in recent:
        try:
            parsed.append(json.loads(line.strip()))
        except Exception:
            parsed.append({"raw": line.strip()})
    return {"lines": parsed}


# Words that say nothing about what someone was stuck on. Norwegian first, since
# that is what pupils write in, plus the English a staff member might use.
_INSIGHT_STOPWORDS = {
    "jeg", "meg", "min", "mitt", "mine", "du", "deg", "din", "ditt", "dine",
    "det", "den", "de", "dem", "vi", "oss", "han", "hun", "seg",
    "er", "var", "har", "hadde", "kan", "kunne", "skal", "skulle", "vil",
    "ville", "må", "får", "får", "gjør", "gjøre", "blir", "ble", "være",
    "ikke", "ingen", "noe", "noen", "alt", "alle", "hva", "hvorfor", "hvordan",
    "når", "hvor", "hvem", "som", "at", "og", "eller", "men", "for", "til",
    "fra", "med", "uten", "på", "i", "av", "om", "over", "under", "etter",
    "en", "et", "ei", "the", "a", "an", "my", "me", "i", "is", "are", "was",
    "have", "has", "can", "cannot", "cant", "wont", "will", "would", "do",
    "does", "did", "not", "no", "what", "why", "how", "when", "where", "who",
    "and", "or", "but", "to", "from", "with", "on", "in", "of", "about",
    "hei", "hallo", "takk", "hjelp", "help", "please", "vaer", "være",
    "problem", "problemer", "funker", "fungerer", "virker", "får",
}
_WORD_RE = re.compile(r"[a-zA-Zæøåäöü0-9\-]{3,}")


def _read_audit_events(days: int) -> list[dict]:
    """Chat audit records from the last `days` days, oldest first."""
    path = _audit_log_path()
    if not path.exists():
        return []
    cutoff = time.time() - days * 86400
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or "chat_" not in line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not str(entry.get("event", "")).startswith("chat_"):
                continue
            if (entry.get("ts") or 0) < cutoff:
                continue
            events.append(entry)
    return events


@app.get("/api/insights", dependencies=[Depends(require_admin)])
async def chat_insights(days: int = 30, recent: int = 50):
    """What people have been asking, counted.

    Built from the audit log rather than from the conversations, so the picture
    survives retention deleting the conversations themselves.
    """
    days = min(max(days, 1), 365)
    recent = min(max(recent, 1), 500)
    events = _read_audit_events(days)

    started = [e for e in events if e["event"] == "chat_started"]
    refusals = [e for e in events if e["event"] == "chat_scope_refusal"]
    blocked = [e for e in events if e["event"] == "chat_command_blocked"]
    throttled = [e for e in events if e["event"] == "chat_rate_limited"]

    by_domain: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_day: dict[str, int] = {}
    by_person: dict[str, int] = {}
    words: dict[str, int] = {}
    # Subject area, not email domain — by_domain above is who they are, this is
    # what they asked about.
    by_topic: dict[str, int] = {}

    tz = None
    try:
        tz = zoneinfo.ZoneInfo(get_config().timezone)
    except Exception:
        pass

    for e in started:
        by_domain[e.get("domain") or "—"] = by_domain.get(e.get("domain") or "—", 0) + 1
        by_tier[e.get("tier") or "—"] = by_tier.get(e.get("tier") or "—", 0) + 1
        if e.get("sender_id"):
            by_person[e["sender_id"]] = by_person.get(e["sender_id"], 0) + 1
        day = datetime.fromtimestamp(e.get("ts") or 0, tz).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + 1
        # Records written before topics were stored still classify, from the
        # title — less to go on, but it keeps the history from reading as empty.
        topic = e.get("topic") or _classify_topic(
            e.get("content_preview") or "", e.get("tier") or ""
        )
        if topic:
            by_topic[topic] = by_topic.get(topic, 0) + 1
        for word in _WORD_RE.findall((e.get("content_preview") or "").lower()):
            if word in _INSIGHT_STOPWORDS or word.isdigit():
                continue
            words[word] = words.get(word, 0) + 1

    def top(counts: dict, limit: int) -> list[dict]:
        return [
            {"key": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        ]

    # Where questions are arriving in a domain with nothing written down. This is
    # the number to act on: it says which skill file to fill in next, in the order
    # people are actually asking.
    try:
        from core.knowledge import coverage_report
        coverage = coverage_report(by_topic)
    except Exception as exc:
        logger.warning(f"Could not build coverage report: {exc}")
        coverage = []
    unclassified = len(started) - sum(by_topic.values())

    try:
        from core.subjects import count_subjects
        by_subject = count_subjects(started)
    except Exception as exc:
        logger.warning(f"Could not cluster subjects: {exc}")
        by_subject = {}

    return {
        "days": days,
        "totals": {
            "conversations": len(started),
            "refusals": len(refusals),
            "blocked_commands": len(blocked),
            "rate_limited": len(throttled),
            "people": len(by_person),
            "undocumented": sum(r["undocumented_demand"] for r in coverage),
        },
        "coverage": coverage,
        "by_topic": top(by_topic, 20),
        "by_subject": top(by_subject, 20),
        "unclassified": unclassified,
        "by_domain": top(by_domain, 20),
        "by_tier": top(by_tier, 10),
        "by_day": [{"key": k, "count": by_day[k]} for k in sorted(by_day)],
        "top_words": top(words, 25),
        "busiest_people": top(by_person, 10),
        "recent": [
            {
                "ts": e.get("ts"),
                "email": e.get("sender_id"),
                "name": e.get("name"),
                "tier": e.get("tier"),
                "title": e.get("content_preview"),
            }
            for e in sorted(started, key=lambda e: e.get("ts") or 0, reverse=True)[:recent]
        ],
        "recent_refusals": [
            {
                "ts": e.get("ts"),
                "email": e.get("sender_id"),
                "asked": e.get("content_preview"),
            }
            for e in sorted(refusals, key=lambda e: e.get("ts") or 0, reverse=True)[:recent]
        ],
    }


@app.get("/api/insights/subject", dependencies=[Depends(require_admin)])
async def insight_subject(key: str, days: int = 30):
    """Opening lines that clustered into one Insights subject."""
    key = (key or "").strip()
    if not key:
        raise HTTPException(400, "key is required")
    days = min(max(days, 1), 365)
    from core.subjects import CATALOG_PATH, resolve_subject, _load
    catalog = _load(CATALOG_PATH)
    rows = []
    for e in _read_audit_events(days):
        if e.get("event") != "chat_started":
            continue
        label = resolve_subject(
            e.get("content_preview") or "", e.get("subject") or "", catalog
        )
        if label != key:
            continue
        rows.append({
            "ts": e.get("ts"),
            "title": e.get("content_preview"),
            "tier": e.get("tier"),
            "topic": e.get("topic"),
            "name": e.get("name"),
            "email": e.get("sender_id"),
        })
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return {"key": key, "conversations": rows[:200]}


@app.post("/api/insights/promote", dependencies=[Depends(require_admin)])
async def promote_insight(body: dict):
    """Append a draft verified-procedure stub to a skill file."""
    from core.skill_promote import promote
    skill = (body.get("skill") or "").strip()
    title = (body.get("title") or "").strip()
    preview = (body.get("preview") or "").strip()
    if not skill or not title:
        raise HTTPException(400, "skill and title are required")
    try:
        return promote(get_config().workspace_path, skill, title, preview)
    except FileNotFoundError:
        raise HTTPException(404, "Skill not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/workspace/drift", dependencies=[Depends(require_admin)])
async def workspace_drift():
    from core.workspace_sync import drift_report
    return drift_report()


@app.get("/api/logs/operational", dependencies=[Depends(require_admin)])
async def get_op_logs(lines: int = 100):
    lines = min(max(lines, 1), 1000)  # Cap to prevent memory abuse
    op_path = Path.home() / ".helix" / "logs" / "helix.log"
    if not op_path.exists():
        return {"lines": []}
    with open(op_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": all_lines[-lines:]}


# ─── Skills API ───────────────────────────────────────────────────────────────

@app.get("/api/skills", dependencies=[Depends(require_admin)])
async def skills_list_api():
    cfg = get_config()
    return {"skills": list_skills(cfg.workspace_path)}


@app.get("/api/skills/{skill_id}", dependencies=[Depends(require_admin)])
async def skills_get_api(skill_id: str):
    cfg = get_config()
    try:
        return read_skill(cfg.workspace_path, skill_id)
    except FileNotFoundError:
        raise HTTPException(404, "Skill not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/skills", dependencies=[Depends(require_admin)])
async def skills_create_api(body: dict):
    cfg = get_config()
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    content = body.get("content", "")
    if not name:
        raise HTTPException(400, "name is required")
    try:
        return create_skill(cfg.workspace_path, name=name, description=description, content=content)
    except FileExistsError:
        raise HTTPException(409, "Skill already exists")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.put("/api/skills/{skill_id}", dependencies=[Depends(require_admin)])
async def skills_save_api(skill_id: str, body: dict):
    cfg = get_config()
    try:
        return write_skill(cfg.workspace_path, skill_id, body.get("content", ""))
    except FileNotFoundError:
        raise HTTPException(404, "Skill not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/skills/{skill_id}/to-template", dependencies=[Depends(require_admin)])
async def skill_to_template_api(skill_id: str):
    """Copy this live skill (and its screenshots) into workspace-template/."""
    from core.workspace_sync import copy_skill_to_template
    cfg = get_config()
    try:
        return copy_skill_to_template(skill_id, workspace=Path(cfg.workspace_path))
    except FileNotFoundError:
        raise HTTPException(404, "Skill not found")
    except ValueError as e:
        raise HTTPException(422, str(e))


# ─── File Browser API ─────────────────────────────────────────────────────────

_FILE_MAX_BYTES = 512 * 1024  # 512 KB cap for reads/writes
_TREE_MAX_NODES = 500         # cap total nodes in tree response


def _resolve_workspace_path(cfg, rel_path: str) -> Path:
    """Resolve *rel_path* relative to workspace_path. Raises 400 on traversal."""
    workspace = Path(cfg.workspace_path).resolve()
    if not rel_path or not rel_path.strip("/"):
        return workspace
    if "\x00" in rel_path:
        raise HTTPException(400, "Invalid path")
    resolved = (workspace / rel_path).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise HTTPException(400, "Path outside workspace")
    return resolved


def _build_tree(workspace: Path) -> dict:
    """Return a nested dict representing the workspace file tree."""
    counter = [0]

    def _walk(path: Path) -> Optional[dict]:
        if counter[0] >= _TREE_MAX_NODES:
            return None
        counter[0] += 1
        rel = path.relative_to(workspace)
        rel_str = "" if str(rel) == "." else str(rel)

        if path.is_symlink():
            return None  # skip symlinks to prevent potential workspace escape

        if path.is_dir():
            children = []
            try:
                entries = sorted(
                    path.iterdir(),
                    key=lambda p: (p.is_file(), p.name.lower()),
                )
                for entry in entries:
                    if counter[0] >= _TREE_MAX_NODES:
                        break
                    child = _walk(entry)
                    if child:
                        children.append(child)
            except PermissionError:
                pass
            return {"name": path.name, "type": "dir", "path": rel_str, "children": children}
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return {"name": path.name, "type": "file", "path": rel_str, "size": size}

    workspace.mkdir(parents=True, exist_ok=True)
    return _walk(workspace) or {"name": workspace.name, "type": "dir", "path": "", "children": []}


@app.get("/api/files/tree", dependencies=[Depends(require_admin)])
async def files_tree():
    """Return the workspace file tree."""
    cfg = get_config()
    return _build_tree(Path(cfg.workspace_path).resolve())


@app.get("/api/files/read", dependencies=[Depends(require_admin)])
async def files_read(path: str = ""):
    """Read a text file from the workspace."""
    if not path:
        raise HTTPException(400, "path is required")
    cfg = get_config()
    resolved = _resolve_workspace_path(cfg, path)
    if not resolved.exists():
        raise HTTPException(404, "File not found")
    if not resolved.is_file():
        raise HTTPException(400, "Path is not a file")
    size = resolved.stat().st_size
    if size > _FILE_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File too large ({size // 1024} KB, max {_FILE_MAX_BYTES // 1024} KB)",
        )
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "File appears to be binary and cannot be displayed")
    return {"path": path, "content": content, "size": size}


@app.put("/api/files/write", dependencies=[Depends(require_admin)])
async def files_write(body: dict):
    """Overwrite an existing workspace file."""
    path = (body.get("path") or "").strip()
    content = body.get("content", "")
    if not path:
        raise HTTPException(400, "path is required")
    if len(content.encode("utf-8")) > _FILE_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Content too large (max {_FILE_MAX_BYTES // 1024} KB)",
        )
    cfg = get_config()
    resolved = _resolve_workspace_path(cfg, path)
    if not resolved.exists():
        raise HTTPException(404, "File not found — use POST /api/files/create to create new files")
    if not resolved.is_file():
        raise HTTPException(400, "Path is not a file")
    resolved.write_text(content, encoding="utf-8")
    return {"status": "saved", "path": path}


@app.post("/api/files/create", dependencies=[Depends(require_admin)])
async def files_create(body: dict):
    """Create a new file in the workspace."""
    import os as _os
    path = (body.get("path") or "").strip()
    content = body.get("content", "")
    if not path:
        raise HTTPException(400, "path is required")
    cfg = get_config()
    resolved = _resolve_workspace_path(cfg, path)
    if resolved.exists():
        raise HTTPException(409, "File already exists")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    _os.chmod(resolved, 0o600)
    return {"status": "created", "path": path}


@app.delete("/api/files/delete", dependencies=[Depends(require_admin)])
async def files_delete(path: str = ""):
    """Delete a file or directory from the workspace."""
    if not path:
        raise HTTPException(400, "path is required")
    cfg = get_config()
    resolved = _resolve_workspace_path(cfg, path)
    if not resolved.exists():
        raise HTTPException(404, "Path not found")
    workspace = Path(cfg.workspace_path).resolve()
    if resolved == workspace:
        raise HTTPException(400, "Cannot delete the workspace root")
    if resolved.is_dir():
        import shutil as _shutil
        _shutil.rmtree(resolved)
    else:
        resolved.unlink()
    return {"status": "deleted", "path": path}


@app.post("/api/files/mkdir", dependencies=[Depends(require_admin)])
async def files_mkdir(body: dict):
    """Create a new directory in the workspace."""
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    cfg = get_config()
    resolved = _resolve_workspace_path(cfg, path)
    if resolved.exists():
        raise HTTPException(409, "Path already exists")
    resolved.mkdir(parents=True, exist_ok=True)
    return {"status": "created", "path": path}


# ─── Scope refusals ───────────────────────────────────────────────────────────
#
# Pupils may ask about IT and nothing else, and every refusal is logged so a
# pupil working steadily through ways around it is visible (ROLES.md).
#
# STUDENT.md mandates the exact opening sentence of a refusal, which is what lets
# this be counted without a second model call. It is a heuristic keyed to that
# wording: reword the sentence in STUDENT.md and this stops matching.
_SCOPE_REFUSAL_NB = re.compile(r"jeg kan bare hjelpe med it", re.I)


# ─── WebSocket chat ───────────────────────────────────────────────────────────

async def _relay_agent_chunks(websocket: WebSocket, agen) -> str:
    """Forward agent output. Status dicts (queue place) go as themselves;
    text is a `chunk` frame and is what gets saved as the reply."""
    parts = []
    async for chunk in agen:
        if isinstance(chunk, dict):
            await websocket.send_json(chunk)
            continue
        parts.append(chunk)
        await websocket.send_json({"chunk": chunk})
    return "".join(parts)


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """Real-time chat via WebSocket."""
    await websocket.accept()

    # Auth via first message
    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        claims = _decode_token(auth_msg.get("token", ""))
        if not claims:
            await websocket.send_json({"error": "Unauthorized"})
            await websocket.close()
            return
    except Exception:
        await websocket.close()
        return

    # Session routing. `peer` is the session key: sessions are unique per
    # (agent_id, channel, peer), so deriving it from the verified token claim
    # gives every signed-in person their own private conversation. Entra users
    # get "entra:<oid>" — stable, unique, and revoked when the account is.
    # It comes from the JWT, never from client-supplied data.
    # The client picks which of its own conversations to open. The user half of
    # the key comes from the token, so a tampered id can only ever land inside
    # the caller's own namespace.
    conversation_id = auth_msg.get("conversation_id") or ""
    if _CONV_ID_RE.match(conversation_id):
        peer = f"{_peer_prefix(claims)}{conversation_id}"
    else:
        peer = claims.get("sub") or "admin"
    display_name = claims.get("name") or peer
    tier = _tier(claims)
    # Commands are staff-only — see ROLES.md. This websocket is the only route
    # to handle_slash for a signed-in person; every other command-shaped endpoint
    # is admin-only or confined to the caller's own conversation.
    commands_allowed = tier == TIER_STAFF

    # Replay the last 30 messages from the web/admin session transcript so the
    # client can repopulate the chat history after a reconnect.
    # Whether the conversation already had content also decides if the next
    # message is the one that names it, for the audit record below.
    new_conversation = True
    if _session_manager:
        try:
            session = await _session_manager.get_or_create("web", peer, tier=tier)
            history = await _session_manager.async_read_transcript(session["session_id"])
            if history:
                new_conversation = False
                tail = history[-30:]
                await websocket.send_json({"type": "history", "messages": tail})
        except Exception as e:
            logger.error(f"WebSocket history replay error: {e}", exc_info=True)

    await websocket.send_json({
        "status": "connected",
        "agent": get_config().agent_name or "helix",
        "user": display_name,
    })

    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message", "")
            attachment_ids = data.get("attachments") or []
            if not message and not attachment_ids:
                continue

            # Rate limit, per person across every connection they hold.
            rate_key = f"web:{claims.get('sub')}"
            rate_err = _user_rate.check(rate_key, tier)
            if rate_err:
                # The code lets the client say this in the user's own language;
                # the text is the operator-facing detail behind it.
                await websocket.send_json({"error": rate_err, "code": "rate_limited"})
                if _first_rate_hit(rate_key):
                    logger.info(f"Rate limit hit by {tier} {rate_key}: {rate_err}")
                    _audit_chat("chat_rate_limited", claims, reason=rate_err)
                continue

            # Resolve attachments up front: slash commands are handled before
            # the normal message path, and /dokumenter is image-driven — without
            # this, screenshots sent with a command would be silently dropped.
            attachment_note = ""
            if attachment_ids:
                paths = _resolve_uploads(attachment_ids, claims.get("sub"))
                if paths:
                    # A staff upload is persisted into assets/ before the agent
                    # sees it, and the agent is shown ONLY the asset path.
                    # Previously images were kept only for /dokumenter, so if a
                    # user pasted screenshots and asked for documentation in
                    # plain words, the agent referenced temp files that were
                    # deleted hours later, leaving dead images in a skill file.
                    # Unreferenced assets are purged on the retention schedule.
                    #
                    # A pupil's upload is NOT persisted. Nothing a pupil sends
                    # becomes part of the knowledge base: their screenshot stays
                    # in the temp upload directory, is passed to the model inside
                    # the message, and is deleted with the rest of the uploads a
                    # few hours later. Only a command can add an image to the
                    # knowledge base, and pupils have no commands (ROLES.md).
                    names = []
                    if tier == TIER_STAFF:
                        hint = message.split(None, 1)[-1] if message.strip() else ""
                        names = _persist_assets(paths, hint)
                        if names:
                            paths = [_assets_dir() / n for n in names]
                    lines = [f"[Attached image: {p}]" for p in paths]
                    if names:
                        # Spell out the mapping and forbid the path form. Given
                        # both a path and a name, the model otherwise tends to
                        # paste the path (raw text, leaks a local path) or the
                        # path's basename (a dead reference).
                        mapping = "\n".join(
                            f"  image {i} -> asset name: {n}"
                            for i, n in enumerate(names, start=1)
                        )
                        lines.append(
                            "[The images above are saved as documentation assets:\n"
                            + mapping + "\n"
                            "To display one — in a reply OR in a skill file — write "
                            "exactly: ![kort beskrivelse](asset:NAME) using a name "
                            "from the list above. Never write a file path or any "
                            "other name; it will not display.]"
                        )
                    attachment_note = "\n".join(lines)

            # A student's leading slash carries no meaning. The message goes on
            # to the agent as ordinary text rather than being refused, so a pupil
            # typing "/hjelp" gets help instead of being told about a command
            # surface they cannot use. Only the command word is logged: the rest
            # of the line is the pupil's own words.
            if not commands_allowed and message.lstrip().startswith("/"):
                attempted = message.lstrip().split(None, 1)[0][:32]
                logger.info(
                    f"Command not available to {tier} {claims.get('sub')}: {attempted}"
                )
                _audit_chat("chat_command_blocked", claims, command=attempted)

            # /nettside <url> — render a JavaScript page server-side and hand
            # the text to the agent. Done here rather than giving the agent a
            # browser: the user chooses the URL, the fetch is anonymous, and
            # the result is fenced as untrusted content before it reaches the
            # model.
            if commands_allowed and message.strip().lower().startswith("/nettside"):
                parts = message.split(None, 2)
                url = parts[1] if len(parts) > 1 else ""
                extra = parts[2] if len(parts) > 2 else ""
                if not url:
                    await websocket.send_json({
                        "chunk": "Bruk: /nettside <adresse> [hva jeg skal se etter]",
                        "done": True})
                    continue
                await websocket.send_json({"chunk": f"Åpner {url} …\n\n"})
                try:
                    from core.web_render import render_page, RenderError
                    page = await render_page(url)
                except RenderError as exc:
                    await websocket.send_json({"chunk": f"⚠️ {exc}", "done": True})
                    continue
                except Exception as exc:
                    logger.error(f"Render failed for {url}: {exc}")
                    await websocket.send_json({
                        "chunk": "⚠️ Klarte ikke å åpne siden.", "done": True})
                    continue

                from security.input_validator import wrap_external_content
                message = (
                    "[Custom command /nettside triggered]\n\n"
                    "The user asked you to read a web page. Its rendered text is "
                    "below. Read it, then follow the instructions in "
                    "commands/nettside.md.\n\n"
                    f"URL: {page['url']}\nTitle: {page['title']}\n\n"
                    + wrap_external_content(page["text"], page["url"])
                    + (f"\n\nUser's note: {extra}" if extra else "")
                )
                full = await _relay_agent_chunks(
                    websocket,
                    _agent_loop.run(
                        "web", peer, message,
                        caller_name=display_name,
                        caller_email=claims.get("email") or "",
                    ),
                )
                await websocket.send_json({
                    "done": True, "full_response": full})
                continue

            # Handle slash commands before passing to agent loop
            if commands_allowed and message.startswith("/"):
                responses = []
                async def send_fn(text):
                    responses.append(text)
                    await websocket.send_json({"chunk": text})
                # Append after the command token so command parsing still works
                # and the paths arrive as arguments.
                slash_message = f"{message}\n\n{attachment_note}" if attachment_note else message
                handled = await handle_slash(slash_message, "web", peer, _session_manager, _agent_loop, send_fn)
                if handled:
                    full = "".join(responses)
                    await websocket.send_json({"done": True, "full_response": full})
                    continue

            # Injection check — mirrors Discord/Telegram adapter behaviour
            clean_message, warnings = sanitize_for_context(message)
            if warnings:
                for w in warnings:
                    logger.warning(f"[INJECTION] WebSocket web/{peer}: {w}")
                await websocket.send_json({
                    "error": "⚠️ Message blocked: potential prompt injection detected.",
                    "done": True,
                })
                continue

            # Context gate: once the window is nearly full, refuse further turns
            # until the user compacts. Without this every message re-sends the
            # whole history, so cost per turn keeps climbing.
            if _session_manager:
                try:
                    st = await _session_manager.context_status(
                        "web", peer, get_config().session.context_limit_pct
                    )
                    if st.get("blocked"):
                        await websocket.send_json({
                            "type": "context_full",
                            "context": st,
                            "done": True,
                        })
                        continue
                except Exception as exc:
                    logger.warning(f"Context check failed: {exc}")

            # Attach pasted screenshots. Paths are rebuilt server-side from ids it
            # owns, so a client can't point the agent at an arbitrary file. A staff
            # agent opens them off disk with Read; a pupil's has no file tools, so
            # agent_loop strips these markers and sends the image bytes inside the
            # message instead.
            if attachment_note:
                body = clean_message.strip() or (
                    "The user attached the image(s) above with no text. "
                    "Read them and say what you see and what it means."
                )
                clean_message = f"{attachment_note}\n\n{body}"

            full = await _relay_agent_chunks(
                websocket,
                _agent_loop.run(
                    "web", peer, clean_message,
                    caller_name=display_name,
                    caller_email=claims.get("email") or "",
                ),
            )

            # One record per conversation, titled by the message that opened it.
            # Recorded after the turn so a conversation that failed outright is not
            # counted as a question anyone asked.
            if new_conversation:
                # The topic is resolved here, from the whole opening message,
                # rather than at report time from the 60-character title — more
                # to go on, and a single label stores less than the title does.
                topic = _classify_topic(clean_message, tier) or ""
                _audit_chat(
                    "chat_started", claims,
                    content_preview=_title_from_transcript(
                        [{"role": "user", "content": clean_message}]
                    ),
                    topic=topic,
                    subject=_remember_subject(clean_message, topic),
                )
                new_conversation = False

            if tier == TIER_STUDENT and _SCOPE_REFUSAL_NB.search(full):
                # The message is logged, truncated: seeing what was asked is the
                # point, and a pupil pressing the same request five different ways
                # is only visible if the requests are there to compare.
                logger.info(
                    f"[SCOPE] refused for {claims.get('sub')}: "
                    f"{clean_message.strip()[:120]!r}"
                )
                _audit_chat(
                    "chat_scope_refusal", claims,
                    content_preview=clean_message.strip(),
                )

            claim = _completion_claim(full, clean_message)
            if claim:
                logger.error(
                    f"[COMPLETION CLAIM] agent claimed an action for {peer}: "
                    f"{claim!r} — correction appended"
                )
                await websocket.send_json({"chunk": _CLAIM_NOTICE_NB})
                full += _CLAIM_NOTICE_NB

            ctx = None
            if _session_manager:
                try:
                    ctx = await _session_manager.context_status(
                        "web", peer, get_config().session.context_limit_pct
                    )
                except Exception:
                    ctx = None
            await websocket.send_json({"done": True, "full_response": full, "context": ctx})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        import traceback
        logger.error(f"WebSocket error: {e}\n{traceback.format_exc()}")


# ─── Static files ─────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Helix Admin UI</h1><p>Static files not found.</p>")


@app.get("/chat", response_class=HTMLResponse)
async def staff_chat():
    """Staff-facing chat. Deliberately separate from the admin dashboard —
    staff must never be shown memory, skills, config or logs."""
    page = _STATIC_DIR / "chat.html"
    if page.exists():
        html = page.read_text(encoding="utf-8")
        # Inject flags server-side so the page never has to ask whether test
        # sign-in is on — one less thing that can fail before login is usable.
        flags = json.dumps({"dev_login": _dev_login_available(get_config())})
        html = html.replace('window.HELIX_FLAGS = {"dev_login": false};',
                            f"window.HELIX_FLAGS = {flags};")
        # No-store: after an update, a cached page would keep talking to a
        # changed API. Staff never see a stale build.
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    return HTMLResponse("<h1>Chat</h1><p>chat.html not found.</p>")
