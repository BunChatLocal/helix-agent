"""
Helix test suite.

Covers:
- Session create, switch, and compact flows
- Slash command routing (/status, /model, /new)
- Snapshot route (regression for Fix 2 — correct kwargs to call_claude)
- File upload sanitization (regression for Fix 3)
- Context engine new vs resumed session behaviour (regression for Fix 4)
"""

import json
import logging
import sqlite3
import sys
import time
import types
import uuid
import base64
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import real FastAPI TestClient BEFORE any stub sections run.
# The stub guard below only fires if fastapi is not yet in sys.modules.
# By importing TestClient here first, the real package is registered and
# the stub guard will be skipped, so integration tests get the real FastAPI.
# ---------------------------------------------------------------------------
try:
    from fastapi.testclient import TestClient as _RealTestClient
    _HAS_REAL_FASTAPI = True
except ImportError:
    _RealTestClient = None  # type: ignore
    _HAS_REAL_FASTAPI = False

# Real PyJWT if installed. The stub below only exists so the suite can import
# without it; console login tests mint a token and then call /api/me, which
# needs the claims to round-trip.
try:
    import jwt as _real_jwt  # noqa: F401
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Minimal stubs so we can import project modules without real external deps
# ---------------------------------------------------------------------------

def _stub_module(name: str, **attrs):
    """Return a freshly-created stub module registered in sys.modules."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# aiosqlite in-memory stub
# The real aiosqlite may not be installed, so we provide a complete fake that
# is backed by an in-process sqlite3 database.
# ---------------------------------------------------------------------------

class _AsyncCursor:
    """Async cursor: awaitable itself AND usable as an async context manager.

    The session_manager code does both:
      - ``await db.execute(sql)``        → direct await, result is the cursor
      - ``async with db.execute(sql)``   → context manager, __aenter__ gives cursor
    We satisfy both by making execute() return a coroutine-like that also
    implements __aenter__/__aexit__.
    """

    def __init__(self, cur):
        self._cur = cur

    # Awaitable support: ``result = await db.execute(sql)``
    def __await__(self):
        return self._identity().__await__()

    async def _identity(self):
        return self

    # Async context manager: ``async with db.execute(sql) as cur``
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def _row_to_dict(self, row):
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return dict(zip(cols, row))

    async def fetchone(self):
        return self._row_to_dict(self._cur.fetchone())

    async def fetchall(self):
        cols = [d[0] for d in self._cur.description] if self._cur.description else []
        return [dict(zip(cols, r)) for r in self._cur.fetchall()]


class _AsyncConnection:
    """Async-compatible wrapper around a real in-memory sqlite3.Connection."""

    def __init__(self, _path):
        self._conn = sqlite3.connect(":memory:")
        # row_factory assigned by SessionManager — we ignore it, handle dicts ourselves
        self.row_factory = None

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        return _AsyncCursor(cur)

    async def commit(self):
        self._conn.commit()

    async def close(self):
        self._conn.close()


async def _aiosqlite_connect(_path):
    return _AsyncConnection(_path)


if "aiosqlite" not in sys.modules:
    aiosqlite_mod = _stub_module("aiosqlite")
    aiosqlite_mod.connect = _aiosqlite_connect
    aiosqlite_mod.Row = dict  # SessionManager sets row_factory to aiosqlite.Row


# ---------------------------------------------------------------------------
# Security stubs (argon2 / cryptography may not be installed)
# ---------------------------------------------------------------------------

if "argon2" not in sys.modules:
    argon2_mod = _stub_module("argon2")
    argon2_mod.low_level = _stub_module("argon2.low_level")
    argon2_mod.low_level.hash_secret_raw = lambda *a, **kw: b"\x00" * 32
    argon2_mod.low_level.Type = MagicMock()

if "cryptography" not in sys.modules:
    crypto_mod = _stub_module("cryptography")
    fernet_mod = _stub_module("cryptography.fernet")

    class _FakeFernet:
        def __init__(self, key):
            self._key = key

        def encrypt(self, data):
            return data

        def decrypt(self, data):
            return data

    class _InvalidToken(Exception):
        pass

    fernet_mod.Fernet = _FakeFernet
    fernet_mod.Fernet.generate_key = staticmethod(lambda: b"k" * 44)
    fernet_mod.InvalidToken = _InvalidToken

if "security" not in sys.modules:
    sec_mod = _stub_module("security")
    # A __path__ makes the stub a package, so modules we have not stubbed below
    # are imported for real from the repo. security.auth is one of them: it
    # depends on nothing but the standard library and the config, and the rate
    # limiter in it is a thing under test rather than a thing to fake.
    sec_mod.__path__ = [str(Path(__file__).parent.parent / "security")]

if "security.secrets" not in sys.modules:
    sec_secrets = _stub_module("security.secrets")
    sec_secrets.get_secret = MagicMock(return_value=None)
    sec_secrets.set_secret = MagicMock()

if "core.file_handler" not in sys.modules:
    file_handler = _stub_module("core.file_handler")
    file_handler.validate_file = MagicMock(return_value=None)
    file_handler.save_file = MagicMock(return_value=None)
    file_handler.cleanup_file = MagicMock()
    file_handler.build_file_context = MagicMock(side_effect=lambda path, msg: msg)
    file_handler.UPLOAD_TTL_SECONDS = 6 * 3600
    file_handler.prune_stale_uploads = MagicMock(return_value=0)

if "security.input_validator" not in sys.modules:
    sec_iv = _stub_module("security.input_validator")
    # Stub: all messages pass through clean (no warnings) in the test environment.
    sec_iv.sanitize_for_context = lambda msg: (msg, [])
    sec_iv.check_injection = lambda msg: None
    sec_iv.wrap_external_content = lambda content, source: content


# ---------------------------------------------------------------------------
# FastAPI stub (for the snapshot route test — fastapi may not be installed)
# ---------------------------------------------------------------------------

if "fastapi" not in sys.modules:
    fastapi_mod = _stub_module("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code = status_code
            self.detail = detail

    def _passthrough_decorator(*args, **kwargs):
        """Decorator that preserves the decorated function unchanged."""
        def _inner(fn):
            return fn
        return _inner

    class _FakeApp:
        """Minimal FastAPI stub: route decorators preserve the function."""
        def add_middleware(self, *a, **kw):
            pass

        def get(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def post(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def put(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def delete(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def patch(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def websocket(self, *a, **kw):
            return _passthrough_decorator(*a, **kw)

        def mount(self, *a, **kw):
            pass

    fastapi_mod.HTTPException = _HTTPException
    fastapi_mod.FastAPI = lambda **kw: _FakeApp()
    fastapi_mod.WebSocket = MagicMock
    fastapi_mod.WebSocketDisconnect = Exception
    fastapi_mod.Depends = lambda x: x
    fastapi_mod.status = MagicMock()

    for _sub in ["fastapi.middleware", "fastapi.middleware.cors",
                  "fastapi.responses", "fastapi.staticfiles", "fastapi.security"]:
        if _sub not in sys.modules:
            _m = _stub_module(_sub)
            _m.CORSMiddleware = MagicMock
            _m.HTMLResponse = MagicMock
            _m.StaticFiles = MagicMock
            _m.HTTPBearer = MagicMock
            _m.HTTPAuthorizationCredentials = MagicMock


# ---------------------------------------------------------------------------
# apscheduler stubs
# ---------------------------------------------------------------------------
for _name in [
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.interval",
    "apscheduler.triggers.cron",
]:
    if _name not in sys.modules:
        _stub_module(_name)

# discord / telegram stubs
for _name in ["discord", "discord.ext", "discord.ext.commands", "telegram", "telegram.ext"]:
    if _name not in sys.modules:
        _stub_module(_name)

if "discord" in sys.modules:
    discord_mod = sys.modules["discord"]
    discord_mod.Message = MagicMock
    discord_mod.DMChannel = MagicMock
    discord_mod.Client = MagicMock
    discord_mod.Intents = MagicMock()

if "telegram" in sys.modules:
    telegram_mod = sys.modules["telegram"]
    telegram_mod.Update = MagicMock

if "telegram.ext" in sys.modules:
    telegram_ext_mod = sys.modules["telegram.ext"]
    telegram_ext_mod.ApplicationBuilder = MagicMock
    telegram_ext_mod.MessageHandler = MagicMock
    telegram_ext_mod.filters = MagicMock()
    telegram_ext_mod.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)

if "telegram.constants" not in sys.modules:
    telegram_constants_mod = _stub_module("telegram.constants")
    telegram_constants_mod.ChatAction = MagicMock()

# jwt stub — only when PyJWT is not installed. Round-trip the payload so
# /api/auth/login then /api/me still sees the minted claims.
if "jwt" not in sys.modules:
    import json as _json
    jwt_mod = _stub_module("jwt")
    jwt_mod.encode = lambda payload, key, algorithm="HS256": _json.dumps(payload, default=str)
    jwt_mod.decode = lambda token, key=None, algorithms=None, options=None, **kw: _json.loads(token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


def _make_temp_config(tmp_path: Path):
    """Return a HelixConfig pointing at tmp_path as the workspace."""
    from core.config import HelixConfig
    return HelixConfig(workspace_path=str(tmp_path))


class TestZorkUserPermissions:
    """An operator can grant, demote or close a single Zork account."""

    def test_override_beats_the_email_domain(self):
        from core.config import (
            ACCESS_BLOCKED, TIER_STAFF, TIER_STUDENT,
            TiersConfig, UserAccessOverride,
        )
        t = TiersConfig(user_overrides=[
            UserAccessOverride(email="elev@afk.no", access="staff"),
        ])
        assert t.access_for(email="elev@afk.no") == TIER_STAFF
        assert t.access_for(email="annen@afk.no") == TIER_STUDENT

        blocked = TiersConfig(user_overrides=[
            UserAccessOverride(email="ansatt@frid-iks.no", access="blocked"),
        ])
        assert blocked.access_for(email="ansatt@frid-iks.no") == ACCESS_BLOCKED
        assert blocked.access_for(email="kollega@frid-iks.no") == TIER_STAFF

    def test_afk_is_pupil_and_frid_iks_is_staff(self):
        from core.config import TIER_ADMIN, TIER_STAFF, TIER_STUDENT, TiersConfig
        t = TiersConfig()
        assert t.access_for(email="elev@afk.no") == TIER_STUDENT
        assert t.access_for(email="ansatt@frid-iks.no") == TIER_STAFF
        assert t.access_for(email="ansatt@test.no") == TIER_STAFF
        assert t.access_for(email="admin@test.no") == TIER_ADMIN

    def test_moderator_and_admin_overrides(self):
        from core.config import (
            TIER_ADMIN, TIER_MODERATOR, TIER_STAFF, TiersConfig,
            UserAccessOverride, is_staff_like,
        )
        t = TiersConfig(user_overrides=[
            UserAccessOverride(email="leder@frid-iks.no", access="moderator"),
            UserAccessOverride(email="it@frid-iks.no", access="admin"),
        ])
        assert t.access_for(email="leder@frid-iks.no") == TIER_MODERATOR
        assert t.access_for(email="it@frid-iks.no") == TIER_ADMIN
        assert is_staff_like(TIER_MODERATOR)
        assert is_staff_like(TIER_ADMIN)
        assert is_staff_like(TIER_STAFF)
        assert not is_staff_like("student")

    def test_unknown_domain_is_admitted_only_with_an_override(self):
        from core.config import TIER_STAFF, TiersConfig, UserAccessOverride
        t = TiersConfig()
        assert t.access_for(email="x@example.com") is None
        t.user_overrides = [UserAccessOverride(email="x@example.com", access="staff")]
        assert t.access_for(email="x@example.com") == TIER_STAFF

    def test_loadtest_emails_are_stress_accounts(self):
        from web.app import _is_stress_account
        assert _is_stress_account("loadtest-p01-n00@afk.no")
        assert _is_stress_account("loadtest-s02-n07@frid.no")
        assert not _is_stress_account("elev@afk.no")
        assert not _is_stress_account("ansatt@frid.no")


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestDevLoginPassword:
    """First sign-in chooses a password. Stress accounts skip it."""

    def _client(self, tmp_path, monkeypatch):
        import importlib
        import core.dev_passwords as pw
        import web.app as app_mod

        importlib.reload(app_mod)
        cfg = _make_temp_config(tmp_path)
        cfg.web.dev_login = True
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(pw, "STORE_PATH", tmp_path / "dev-passwords.json")
        return _RealTestClient(app_mod.app, raise_server_exceptions=False)

    def test_first_login_without_password_is_refused(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.post("/api/auth/dev-login", json={"email": "ansatt@frid-iks.no"})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "set_password"

    def test_first_login_sets_password_and_later_requires_it(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        body = {
            "email": "ansatt@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        }
        r = client.post("/api/auth/dev-login", json=body)
        assert r.status_code == 200
        assert r.json().get("token")

        bad = client.post("/api/auth/dev-login", json={
            "email": "ansatt@frid-iks.no", "password": "wrong-password",
        })
        assert bad.status_code == 401
        assert bad.json()["detail"]["code"] == "bad_password"

        ok = client.post("/api/auth/dev-login", json={
            "email": "ansatt@frid-iks.no", "password": "correct-horse",
        })
        assert ok.status_code == 200

    def test_confirm_must_match_on_first_login(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.post("/api/auth/dev-login", json={
            "email": "elev@afk.no",
            "password": "correct-horse",
            "password_confirm": "other-horse1",
        })
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "confirm_mismatch"

    def test_stress_account_skips_password(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.post("/api/auth/dev-login", json={"email": "loadtest-p01-n00@afk.no"})
        assert r.status_code == 200
        assert r.json().get("token")

    def test_admin_can_reset_password(self, tmp_path, monkeypatch):
        import web.app as app_mod

        client = self._client(tmp_path, monkeypatch)
        client.post("/api/auth/dev-login", json={
            "email": "ansatt@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        })
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": "admin", "name": "Admin"},
        )
        gone = client.delete(
            "/api/permissions/ansatt@frid-iks.no/password",
            headers={"Authorization": "Bearer validtoken"},
        )
        assert gone.status_code == 200
        again = client.post("/api/auth/dev-login", json={"email": "ansatt@frid-iks.no"})
        assert again.status_code == 403
        assert again.json()["detail"]["code"] == "set_password"

    def test_first_login_appears_on_the_users_list(self, tmp_path, monkeypatch):
        import web.app as app_mod

        client = self._client(tmp_path, monkeypatch)
        r = client.post("/api/auth/dev-login", json={
            "email": "ny@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        })
        assert r.status_code == 200
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": "admin", "name": "Admin"},
        )
        listed = client.get("/api/permissions", headers={"Authorization": "Bearer validtoken"})
        assert listed.status_code == 200
        people = {p["email"]: p for p in listed.json()["people"]}
        assert people["ny@frid-iks.no"]["has_password"] is True

    def test_status_says_when_a_password_must_be_chosen(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        fresh = client.get("/api/auth/dev-login/status", params={"email": "ansatt@frid-iks.no"})
        assert fresh.status_code == 200
        assert fresh.json()["set_password"] is True

        client.post("/api/auth/dev-login", json={
            "email": "ansatt@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        })
        again = client.get("/api/auth/dev-login/status", params={"email": "ansatt@frid-iks.no"})
        assert again.json()["set_password"] is False

        stress = client.get(
            "/api/auth/dev-login/status",
            params={"email": "loadtest-p01-n00@afk.no"},
        )
        assert stress.json()["set_password"] is False

    def test_creating_a_person_issues_a_ten_character_password(self, tmp_path, monkeypatch):
        from core.config import save_config as real_save, load_config as real_load
        from core import config as config_mod
        import web.app as app_mod
        import string

        client = self._client(tmp_path, monkeypatch)
        cfg_path = tmp_path / "config.json"
        cfg = _make_temp_config(tmp_path)
        cfg.web.dev_login = True
        real_save(cfg, cfg_path)
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(app_mod, "save_config", lambda c: real_save(c, cfg_path))
        monkeypatch.setattr(app_mod, "get_config", lambda: real_load(cfg_path, True))
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": "admin", "name": "Admin"},
        )

        created = client.put(
            "/api/permissions",
            json={"email": "Ny@Frid.no", "access": "staff", "issue_password": True},
            headers={"Authorization": "Bearer validtoken"},
        )
        assert created.status_code == 200
        body = created.json()
        issued = body["issued_password"]
        assert body["issued_email"] == "ny@frid.no"
        assert len(issued) == 10
        assert all(c in string.ascii_letters + string.digits for c in issued)
        assert any(c.isalpha() for c in issued) and any(c.isdigit() for c in issued)
        people = {p["email"]: p for p in body["people"]}
        assert people["ny@frid.no"]["has_password"] is True
        assert issued not in str(people["ny@frid.no"])

        again = client.put(
            "/api/permissions",
            json={"email": "ny@frid.no", "access": "moderator", "issue_password": True},
            headers={"Authorization": "Bearer validtoken"},
        )
        assert again.status_code == 200
        assert "issued_password" not in again.json()

        login = client.post("/api/auth/dev-login", json={
            "email": "ny@frid.no", "password": issued,
        })
        assert login.status_code == 200
        assert login.json().get("token")

        listed = client.get("/api/permissions", headers={"Authorization": "Bearer validtoken"})
        assert listed.status_code == 200
        assert "issued_password" not in listed.json()

        role_only = client.put(
            "/api/permissions",
            json={"email": "kollega@frid.no", "access": "staff"},
            headers={"Authorization": "Bearer validtoken"},
        )
        assert role_only.status_code == 200
        assert "issued_password" not in role_only.json()

        stress = client.put(
            "/api/permissions",
            json={
                "email": "loadtest-p01-n00@afk.no",
                "access": "staff",
                "issue_password": True,
            },
            headers={"Authorization": "Bearer validtoken"},
        )
        assert stress.status_code == 200
        assert "issued_password" not in stress.json()


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestConsoleLogin:
    """The operator panel is a named account, not a shared password."""

    def _client(self, tmp_path, monkeypatch, *, entra=False, overrides=None):
        import importlib
        import core.dev_passwords as pw
        import web.app as app_mod
        from core.config import UserAccessOverride

        importlib.reload(app_mod)
        cfg = _make_temp_config(tmp_path)
        cfg.web.dev_login = not entra
        if entra:
            cfg.entra.enabled = True
            cfg.entra.client_id = "cid"
            cfg.entra.tenant_id = "tid"
        if overrides:
            cfg.tiers.user_overrides = [
                UserAccessOverride(email=email, access=access)
                for email, access in overrides
            ]
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(pw, "STORE_PATH", tmp_path / "dev-passwords.json")
        return _RealTestClient(app_mod.app, raise_server_exceptions=False), pw

    def test_shared_password_does_not_open_the_panel(self, tmp_path, monkeypatch):
        client, _pw = self._client(tmp_path, monkeypatch)
        r = client.post("/api/auth/login", json={"password": "the-old-shared-one"})
        assert r.status_code == 400

    def test_staff_cannot_open_the_panel(self, tmp_path, monkeypatch):
        client, _pw = self._client(tmp_path, monkeypatch)
        client.post("/api/auth/dev-login", json={
            "email": "ansatt@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        })
        r = client.post("/api/auth/login", json={
            "email": "ansatt@frid-iks.no", "password": "correct-horse",
        })
        assert r.status_code == 403

    def test_cannot_register_a_new_account_on_the_panel(self, tmp_path, monkeypatch):
        client, pw = self._client(
            tmp_path, monkeypatch,
            overrides=[("ny@frid-iks.no", "admin")],
        )
        r = client.post("/api/auth/login", json={
            "email": "ny@frid-iks.no",
            "password": "correct-horse",
            "password_confirm": "correct-horse",
        })
        assert r.status_code == 403
        assert pw.get_hash("ny@frid-iks.no") is None

    def test_moderator_with_a_password_gets_in(self, tmp_path, monkeypatch):
        client, pw = self._client(
            tmp_path, monkeypatch,
            overrides=[("leder@frid-iks.no", "moderator")],
        )
        pw.set_password("leder@frid-iks.no", "correct-horse")
        r = client.post("/api/auth/login", json={
            "email": "leder@frid-iks.no", "password": "correct-horse",
        })
        assert r.status_code == 200
        me = client.get(
            "/api/me",
            headers={"Authorization": "Bearer " + r.json()["token"]},
        )
        assert me.status_code == 200
        assert me.json()["console"] is True
        assert me.json()["console_role"] == "moderator"
        assert me.json()["email"] == "leder@frid-iks.no"
        assert me.json()["tier"] == "moderator"

    def test_admin_with_a_password_gets_in(self, tmp_path, monkeypatch):
        client, pw = self._client(
            tmp_path, monkeypatch,
            overrides=[("it@frid-iks.no", "admin")],
        )
        pw.set_password("it@frid-iks.no", "correct-horse")
        r = client.post("/api/auth/login", json={
            "email": "it@frid-iks.no", "password": "correct-horse",
        })
        assert r.status_code == 200
        me = client.get(
            "/api/me",
            headers={"Authorization": "Bearer " + r.json()["token"]},
        )
        assert me.status_code == 200
        assert me.json()["console"] is True
        assert me.json()["console_role"] == "admin"
        assert me.json()["role"] == "admin"
        assert me.json()["email"] == "it@frid-iks.no"

    def test_login_still_works_if_old_entra_flag_is_on(self, tmp_path, monkeypatch):
        client, pw = self._client(
            tmp_path, monkeypatch, entra=True,
            overrides=[("leder@frid-iks.no", "admin")],
        )
        pw.set_password("leder@frid-iks.no", "correct-horse")
        r = client.post("/api/auth/login", json={
            "email": "leder@frid-iks.no", "password": "correct-horse",
        })
        assert r.status_code == 200

    def test_panel_login_form_is_an_account_not_a_shared_password(self):
        page = Path("web/static/index.html").read_text(encoding="utf-8")
        assert "Admin password" not in page
        assert "Sign in with Microsoft" not in page
        assert "You cannot create an account here" in page
        assert "loginEmail" in page

    def test_chat_login_is_email_and_password_not_microsoft(self):
        page = Path("web/static/chat.html").read_text(encoding="utf-8")
        assert "Logg inn med Microsoft" not in page
        assert "msBtn" not in page
        assert 'id="devEmail"' in page
        assert 'id="devPassword"' in page


class TestDevPasswordStore:
    def test_hash_is_not_the_password(self, tmp_path):
        from core.dev_passwords import hash_password, verify_password
        stored = hash_password("correct-horse")
        assert "correct-horse" not in stored
        assert stored.startswith("pbkdf2$")
        assert verify_password("correct-horse", stored)
        assert not verify_password("wrong-password", stored)

    def test_generated_password_is_ten_letters_and_digits(self):
        from core.dev_passwords import generate_password
        import string
        alphabet = string.ascii_letters + string.digits
        seen = {generate_password() for _ in range(8)}
        assert len(seen) == 8
        for pw in seen:
            assert len(pw) == 10
            assert all(c in alphabet for c in pw)
            assert any(c.isalpha() for c in pw)
            assert any(c.isdigit() for c in pw)


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestModeratorConsole:
    """Moderators may open the operator panel, but only a subset of it."""

    _CLAIMS = {
        "sub": "dev:leder@frid-iks.no",
        "email": "leder@frid-iks.no",
        "role": "user",
        "tier": "moderator",
        "name": "Leder",
    }

    def _client(self, tmp_path, monkeypatch, claims=None):
        from core.config import HelixConfig, UserAccessOverride, save_config as real_save, load_config as real_load
        from core import config as config_mod
        import importlib
        import web.app as app_mod
        import core.dev_passwords as pw

        importlib.reload(app_mod)
        cfg_path = tmp_path / "config.json"
        cfg = HelixConfig(workspace_path=str(tmp_path))
        cfg.tiers.user_overrides = [
            UserAccessOverride(email="elev@afk.no", access="student"),
            UserAccessOverride(email="ansatt@frid.no", access="staff"),
        ]
        real_save(cfg, cfg_path)
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(app_mod, "save_config", lambda c: real_save(c, cfg_path))
        monkeypatch.setattr(app_mod, "get_config", lambda: real_load(cfg_path, True))
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: dict(claims or self._CLAIMS) if token == "tok" else None,
        )
        monkeypatch.setattr(pw, "STORE_PATH", tmp_path / "dev-passwords.json")
        sm = MagicMock()
        sm.list_sessions = AsyncMock(return_value=[])
        monkeypatch.setattr(app_mod, "_session_manager", sm)
        return _RealTestClient(app_mod.app, raise_server_exceptions=False)

    def test_me_offers_the_console(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/api/me", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        body = r.json()
        assert body["console"] is True
        assert body["console_role"] == "moderator"
        assert body["tier"] == "moderator"

    def test_staff_are_not_offered_the_console(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        r = client.get("/api/me", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        assert r.json()["console"] is False
        sessions = client.get("/api/sessions", headers={"Authorization": "Bearer tok"})
        assert sessions.status_code == 403
        deleted = client.get("/api/sessions/deleted", headers={"Authorization": "Bearer tok"})
        assert deleted.status_code == 403

    def test_can_read_status_insights_skills_sessions(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        assert client.get("/api/status", headers=h).status_code == 200
        assert client.get("/api/insights", headers=h).status_code == 200
        assert client.get("/api/skills", headers=h).status_code == 200
        assert client.get("/api/sessions", headers=h).status_code == 200
        assert client.get("/api/sessions/deleted", headers=h).status_code == 200

    def test_cannot_create_a_skill_or_open_config(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        assert client.post(
            "/api/skills", json={"name": "secret"}, headers=h,
        ).status_code == 403
        assert client.get("/api/config", headers=h).status_code == 403
        assert client.get("/api/logs/audit", headers=h).status_code == 403
        assert client.post(
            "/api/insights/promote",
            json={"skill": "entra-azure", "title": "x"},
            headers=h,
        ).status_code == 403
        assert client.get("/api/crons", headers=h).status_code == 403

    def test_admin_can_list_crons(self, tmp_path, monkeypatch):
        h = {"Authorization": "Bearer tok"}
        admin = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:it@frid.no", "email": "it@frid.no",
            "role": "user", "tier": "admin", "name": "IT",
        })
        listed = admin.get("/api/crons", headers=h)
        assert listed.status_code == 200
        assert listed.json() == []
        staff = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        assert staff.get("/api/crons", headers=h).status_code == 403

    def test_users_list_is_afk_only(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/api/permissions", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        body = r.json()
        emails = {p["email"] for p in body["people"]}
        assert "elev@afk.no" in emails
        assert "ansatt@frid.no" not in emails
        assert body["stress"] == []
        assert body["overrides"] == []
        assert body["defaults"]["staff_domains"] == []

        assert client.put(
            "/api/permissions",
            json={"email": "elev@afk.no", "access": "staff"},
            headers={"Authorization": "Bearer tok"},
        ).status_code == 403

    def test_desk_chat_is_for_the_panel_not_ordinary_staff(self, tmp_path, monkeypatch):
        import core.desk_chat as desk
        monkeypatch.setattr(desk, "STORE_PATH", tmp_path / "desk-chat.jsonl")
        monkeypatch.setattr(desk, "READ_PATH", tmp_path / "desk-chat-read.json")
        staff = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        assert staff.get("/api/desk-chat", headers={"Authorization": "Bearer tok"}).status_code == 403
        mod = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        empty = mod.get("/api/desk-chat", headers=h)
        assert empty.status_code == 200
        assert empty.json()["messages"] == []
        assert empty.json()["unread"] == 0
        assert empty.json()["you"] == "leder@frid-iks.no"
        posted = mod.post("/api/desk-chat", json={"text": "Word krasjer på rom 12"}, headers=h)
        assert posted.status_code == 200
        body = posted.json()
        assert body["text"] == "Word krasjer på rom 12"
        assert body["name"] == "Leder"
        assert body["email"] == "leder@frid-iks.no"
        assert body["role"] == "moderator"
        listed = mod.get("/api/desk-chat", headers=h)
        assert listed.json()["messages"][0]["text"] == "Word krasjer på rom 12"
        assert listed.json()["unread"] == 0

    def test_unread_is_other_people_until_you_open_the_room(self, tmp_path, monkeypatch):
        import core.desk_chat as desk
        monkeypatch.setattr(desk, "STORE_PATH", tmp_path / "desk-chat.jsonl")
        monkeypatch.setattr(desk, "READ_PATH", tmp_path / "desk-chat-read.json")
        mod = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        desk.post("PC på rom 12", name="Ada", email="ada@frid.no", role="moderator")
        waiting = mod.get("/api/desk-chat", headers=h)
        assert waiting.json()["unread"] == 1
        marked = mod.post("/api/desk-chat/read", headers=h)
        assert marked.status_code == 200
        assert marked.json()["unread"] == 0
        after = mod.get("/api/desk-chat", headers=h)
        assert after.json()["unread"] == 0
        desk.post("Jeg tar den", name="Ada", email="ada@frid.no", role="moderator")
        again = mod.get("/api/desk-chat", headers=h)
        assert again.json()["unread"] == 1

    def test_reporter_can_read_and_reply_on_their_own_report(self, tmp_path, monkeypatch):
        import core.desk_tickets as tickets
        monkeypatch.setattr(tickets, "STORE_PATH", tmp_path / "desk-tickets.json")
        tickets.create(
            "issue", "Annen sak", "Ikke Leders.",
            name="Ada", email="ada@frid.no", role="moderator",
        )
        mod = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        filed = mod.post("/api/desk-tickets", json={
            "kind": "issue",
            "subject": "Word krasjer",
            "description": "Rom 12, hver gang de lagrer.",
        }, headers=h)
        assert filed.status_code == 200
        mine = filed.json()
        assert mine["status"] == "new"
        listed = mod.get("/api/desk-tickets", headers=h)
        assert listed.status_code == 200
        subjects = [t["subject"] for t in listed.json()["tickets"]]
        assert subjects == ["Word krasjer"]
        assert listed.json()["inbox"] is False
        replied = mod.post(
            f"/api/desk-tickets/{mine['id']}/replies",
            json={"text": "Skjer også i Excel."},
            headers=h,
        )
        assert replied.status_code == 200
        assert replied.json()["status"] == "new"
        assert replied.json()["replies"][0]["text"] == "Skjer også i Excel."
        other = tickets.list_all()
        ada = next(t for t in other if t["subject"] == "Annen sak")
        assert mod.post(
            f"/api/desk-tickets/{ada['id']}/replies",
            json={"text": "hei"},
            headers=h,
        ).status_code == 403
        assert mod.patch(
            f"/api/desk-tickets/{mine['id']}",
            json={"status": "needs_context"},
            headers=h,
        ).status_code == 403
        assert mod.get("/api/desk-tickets", headers=h).json()["tickets"][0]["status"] == "new"
        assert mod.patch(
            f"/api/desk-tickets/{ada['id']}",
            json={"status": "finished"},
            headers=h,
        ).status_code == 403

    def test_reporter_is_notified_when_admin_replies_or_changes_status(self, tmp_path, monkeypatch):
        import core.desk_tickets as tickets
        monkeypatch.setattr(tickets, "STORE_PATH", tmp_path / "desk-tickets.json")
        mod = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        filed = mod.post("/api/desk-tickets", json={
            "kind": "issue",
            "subject": "Word krasjer",
            "description": "Rom 12.",
        }, headers=h).json()
        assert filed["updated"] <= filed["reporter_seen"] + 0.01
        tickets.reply(
            filed["id"], "Send et skjermbilde.",
            name="Operator", role="operator", staff=True,
        )
        after_reply = mod.get("/api/desk-tickets", headers=h).json()["tickets"][0]
        assert after_reply["updated"] > after_reply["reporter_seen"] + 0.01
        read = mod.post(f"/api/desk-tickets/{filed['id']}/read", headers=h)
        assert read.status_code == 200
        assert read.json()["reporter_seen"] >= read.json()["updated"] - 0.01
        tickets.set_status(filed["id"], "needs_context")
        after_status = mod.get("/api/desk-tickets", headers=h).json()["tickets"][0]
        assert after_status["status"] == "needs_context"
        assert after_status["updated"] > after_status["reporter_seen"] + 0.01
        other = tickets.create(
            "issue", "Annen sak", "Ikke Leders.",
            name="Ada", email="ada@frid.no", role="moderator",
        )
        assert mod.post(
            f"/api/desk-tickets/{other['id']}/read",
            headers=h,
        ).status_code == 403

    def test_operator_can_reply_and_set_status(self, tmp_path, monkeypatch):
        import core.desk_tickets as tickets
        monkeypatch.setattr(tickets, "STORE_PATH", tmp_path / "desk-tickets.json")
        entry = tickets.create(
            "suggestion", "Ny snarvei", "En knapp for å resette MFA.",
            name="Leder", email="leder@frid-iks.no", role="moderator",
        )
        op = self._client(tmp_path, monkeypatch, claims={
            "sub": "admin", "role": "admin", "name": "Operator",
        })
        h = {"Authorization": "Bearer tok"}
        listed = op.get("/api/desk-tickets", headers=h)
        assert listed.status_code == 200
        assert listed.json()["new"] == 1
        assert listed.json()["tickets"][0]["subject"] == "Ny snarvei"
        replied = op.post(
            f"/api/desk-tickets/{entry['id']}/replies",
            json={"text": "Vi tar den denne uken."},
            headers=h,
        )
        assert replied.status_code == 200
        assert replied.json()["status"] == "new"
        assert replied.json()["replies"][0]["text"] == "Vi tar den denne uken."
        closed = op.patch(
            f"/api/desk-tickets/{entry['id']}",
            json={"status": "finished"},
            headers=h,
        )
        assert closed.json()["status"] == "finished"
        assert op.get("/api/desk-tickets", headers=h).json()["new"] == 0

    def test_inbox_screenshots_are_console_only_and_scoped(self, tmp_path, monkeypatch):
        from io import BytesIO
        import core.desk_tickets as tickets
        monkeypatch.setattr(tickets, "STORE_PATH", tmp_path / "desk-tickets.json")
        monkeypatch.setattr(tickets, "ASSETS_DIR", tmp_path / "desk-ticket-assets")
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        mod = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        uploaded = mod.post(
            "/api/desk-tickets/assets",
            files={"file": ("shot.png", BytesIO(png), "image/png")},
            headers=h,
        )
        assert uploaded.status_code == 200
        name = uploaded.json()["name"]
        filed = mod.post("/api/desk-tickets", json={
            "kind": "issue",
            "subject": "Word krasjer",
            "description": "Se skjermbildet.",
            "images": [name],
        }, headers=h)
        assert filed.status_code == 200
        assert filed.json()["images"] == [name]
        fetched = mod.get(f"/api/desk-tickets/assets/{name}", headers=h)
        assert fetched.status_code == 200
        assert fetched.content.startswith(b"\x89PNG")
        shot = mod.post(
            "/api/desk-tickets/assets",
            files={"file": ("reply.png", BytesIO(png), "image/png")},
            headers=h,
        ).json()["name"]
        replied = mod.post(
            f"/api/desk-tickets/{filed.json()['id']}/replies",
            json={"text": "", "images": [shot]},
            headers=h,
        )
        assert replied.status_code == 200
        assert replied.json()["replies"][0]["images"] == [shot]
        import web.app as app_mod
        app_mod._TICKET_UPLOADS.clear()
        orphan = tickets.save_image(png, ".png")
        after_restart = mod.post(
            f"/api/desk-tickets/{filed.json()['id']}/replies",
            json={"text": "her er et skjermbilde", "images": [orphan]},
            headers=h,
        )
        assert after_restart.status_code == 200
        assert orphan in after_restart.json()["replies"][-1]["images"]
        empty = mod.post(
            f"/api/desk-tickets/{filed.json()['id']}/replies",
            json={"text": ""},
            headers=h,
        )
        assert empty.status_code == 400
        assert mod.get("/api/desk-tickets/assets/not-a-file.png", headers=h).status_code == 404
        ada = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ada@frid.no", "email": "ada@frid.no",
            "role": "user", "tier": "moderator", "name": "Ada",
        })
        assert ada.get(
            f"/api/desk-tickets/assets/{name}",
            headers=h,
        ).status_code == 404
        staff = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        assert staff.get(
            f"/api/desk-tickets/assets/{name}",
            headers=h,
        ).status_code == 403
        assert staff.post(
            "/api/desk-tickets/assets",
            files={"file": ("shot.png", BytesIO(png), "image/png")},
            headers=h,
        ).status_code == 403

    def test_desk_websocket_refuses_staff(self, tmp_path, monkeypatch):
        staff = self._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        with staff.websocket_connect("/ws/desk") as ws:
            ws.send_json({"token": "tok"})
            frame = ws.receive_json()
        assert "error" in frame

    def test_can_reset_afk_password_but_not_frid(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        afk = client.delete("/api/permissions/elev@afk.no/password", headers=h)
        assert afk.status_code == 200
        assert "ansatt@frid.no" not in [p["email"] for p in afk.json()["people"]]
        frid = client.delete("/api/permissions/ansatt@frid.no/password", headers=h)
        assert frid.status_code == 403


# ---------------------------------------------------------------------------
# Fix 3 — File upload sanitization
# ---------------------------------------------------------------------------

class TestFileSanitization:
    """Regression tests for core/file_handler.py path-traversal fix."""

    def setup_method(self):
        import importlib
        import core.file_handler as fh
        importlib.reload(fh)
        self.fh = fh

    def test_basename_stripped(self):
        name = self.fh._sanitize_filename("../../etc/passwd")
        assert "/" not in name
        assert ".." not in name
        assert name == "passwd"

    def test_windows_separator_stripped(self):
        name = self.fh._sanitize_filename("..\\..\\windows\\system32\\evil.exe")
        assert "\\" not in name

    def test_plain_filename_unchanged(self):
        name = self.fh._sanitize_filename("report.pdf")
        assert name == "report.pdf"

    def test_empty_filename_fallback(self):
        name = self.fh._sanitize_filename("")
        assert name == "upload"

    def test_collision_handling(self, tmp_path):
        """When the target file already exists a UUID suffix is appended."""
        import core.file_handler as fh

        existing = tmp_path / "file.txt"
        existing.write_bytes(b"old")

        result = fh._resolve_collision(existing)
        assert result != existing
        assert result.suffix == ".txt"
        assert result.parent == tmp_path

    def test_save_file_sanitizes(self, tmp_path, monkeypatch):
        import core.file_handler as fh
        monkeypatch.setattr(fh, "UPLOAD_DIR", tmp_path)

        saved = fh.save_file("../../evil.txt", b"data")
        assert saved is not None
        # Must be inside the upload dir — no traversal
        assert saved.parent == tmp_path
        assert saved.name == "evil.txt"
        assert saved.read_bytes() == b"data"

    def test_save_file_collision(self, tmp_path, monkeypatch):
        import core.file_handler as fh
        monkeypatch.setattr(fh, "UPLOAD_DIR", tmp_path)

        first = fh.save_file("dup.txt", b"first")
        second = fh.save_file("dup.txt", b"second")
        assert first is not None
        assert second is not None
        assert first != second
        assert second.read_bytes() == b"second"


# ---------------------------------------------------------------------------
# Fix 4 — Context engine new vs resumed session
# ---------------------------------------------------------------------------

class TestContextEngine:
    """Regression tests for core/context_engine.py new-session guard."""

    def test_new_session_returns_content(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        soul_file = tmp_path / "SOUL.md"
        soul_file.write_text("I am Helix.")

        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            prompt = build_system_prompt(is_new_session=True)

        assert "I am Helix." in prompt
        assert len(prompt) > 0

    def test_resumed_session_returns_empty(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        (tmp_path / "SOUL.md").write_text("I am Helix.")

        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            prompt = build_system_prompt(is_new_session=False)

        assert prompt == ""

    def test_default_is_new_session(self, tmp_path):
        """Default behaviour (no arg) should load bootstrap — new-session assumed."""
        cfg = _make_temp_config(tmp_path)
        (tmp_path / "AGENTS.md").write_text("Agent instructions.")

        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            prompt = build_system_prompt()

        assert "Agent instructions." in prompt


# ---------------------------------------------------------------------------
# Per-tier system prompt (see ROLES.md)
# ---------------------------------------------------------------------------

class TestTieredPrompt:
    """Staff and students share a core and diverge after it."""

    def _workspace(self, tmp_path):
        for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
                     "HEARTBEAT.md", "MEMORY.md", "TOOLS.md", "STAFF.md",
                     "STUDENT.md", "MODERATOR.md", "ADMIN.md"):
            (tmp_path / name).write_text(f"marker for {name}")
        return _make_temp_config(tmp_path)

    def _prompt(self, tmp_path, tier):
        cfg = self._workspace(tmp_path)
        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            return build_system_prompt(is_new_session=True, tier=tier)

    def test_shared_core_reaches_both(self, tmp_path):
        for tier in ("staff", "student"):
            prompt = self._prompt(tmp_path, tier)
            assert "marker for AGENTS.md" in prompt
            assert "marker for SOUL.md" in prompt

    def test_student_prompt_omits_operator_files(self, tmp_path):
        prompt = self._prompt(tmp_path, "student")
        assert "marker for STUDENT.md" in prompt
        for excluded in ("USER.md", "TOOLS.md", "HEARTBEAT.md", "MEMORY.md",
                         "IDENTITY.md", "STAFF.md", "MODERATOR.md", "ADMIN.md"):
            assert f"marker for {excluded}" not in prompt, (
                f"{excluded} must not reach a pupil's prompt"
            )

    def test_staff_prompt_keeps_everything(self, tmp_path):
        prompt = self._prompt(tmp_path, "staff")
        assert "marker for STAFF.md" in prompt
        assert "marker for TOOLS.md" in prompt
        assert "marker for USER.md" in prompt
        assert "marker for STUDENT.md" not in prompt
        assert "marker for ADMIN.md" not in prompt
        assert "marker for MODERATOR.md" not in prompt

    def test_staff_chat_does_not_get_the_operator_notes(self, tmp_path):
        """USER.md is the operator. A colleague at /chat is someone else."""
        cfg = self._workspace(tmp_path)
        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            prompt = build_system_prompt(
                is_new_session=True, tier="staff", channel="web",
                caller_name="Ny Ansatt", caller_email="nyansatt@frid.no",
            )
        assert "marker for USER.md" not in prompt
        assert "Ny Ansatt" in prompt
        assert "nyansatt@frid.no" in prompt
        assert "never speak it" in prompt
        assert "Access: staff" in prompt
        assert "Your home directory is" not in prompt
        assert "Do not write facts about this person into memory files" in prompt
        assert "filesystem path" in prompt
        assert "HOW TO ANSWER IN THIS CHAT" in prompt
        assert prompt.index("## AGENTS.md") < prompt.index("Today is")
        assert prompt.index("Today is") < prompt.index("HOW TO ANSWER IN THIS CHAT")

    def test_admin_and_moderator_are_named_in_the_prompt(self, tmp_path):
        """Otherwise the model cannot tell a colleague from an operator."""
        cfg = self._workspace(tmp_path)
        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            admin = build_system_prompt(
                is_new_session=True, tier="admin", channel="web",
                caller_name="Jorgen", caller_email="jorgenthin@frid-iks.no",
            )
            mod = build_system_prompt(
                is_new_session=True, tier="moderator", channel="web",
                caller_name="Leder", caller_email="leder@frid-iks.no",
            )
            pupil = build_system_prompt(
                is_new_session=True, tier="student", channel="web",
                caller_name="Elev Elevsen", caller_email="elev@afk.no",
            )
        assert "Access: admin" in admin
        assert "marker for ADMIN.md" in admin
        assert "marker for STAFF.md" in admin
        assert "jorgenthin@frid-iks.no" in admin
        assert "Access: moderator" in mod
        assert "marker for MODERATOR.md" in mod
        assert "Access: pupil" in pupil
        assert "signed-in colleague" not in pupil
        assert "Access: admin" not in pupil
        assert "Access: staff" not in pupil

    def test_unknown_tier_falls_back_to_staff(self, tmp_path):
        """Internal channels pass no tier and must keep the full context."""
        assert self._prompt(tmp_path, "nonsense") == self._prompt(tmp_path, "staff")

    def test_missing_tier_file_is_logged_as_an_error(self, tmp_path, caplog):
        """Losing STUDENT.md silently would drop the scope rules entirely."""
        cfg = self._workspace(tmp_path)
        (tmp_path / "STUDENT.md").unlink()
        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            with caplog.at_level(logging.ERROR, logger="helix.context"):
                build_system_prompt(is_new_session=True, tier="student")
        assert any("STUDENT.md" in r.message for r in caplog.records)


class TestTieredTools:
    """A pupil's agent is offered no tools at all."""

    def test_student_gets_no_tools_whatsoever(self):
        from core.cli_backend import TIER_TOOLS
        student = TIER_TOOLS["student"].split()
        assert student == [], (
            "A pupil gets no tools. Read was here once, on the assumption that the "
            "subprocess cwd confined it to the workspace — it does not, and a pupil "
            "could read config.json, the operator's notes, the staff-only Azure "
            "procedures and other pupils' conversations. Their guides are inlined "
            "into the prompt and their screenshots travel inside the message, so "
            "there is nothing here to restore."
        )

    def test_staff_keeps_the_write_tools_but_never_bash(self):
        from core.cli_backend import TIER_TOOLS
        staff = TIER_TOOLS["staff"].split()
        assert "Write" in staff and "Edit" in staff
        assert "Bash" not in staff, "advice-only is not tier-dependent"
        assert TIER_TOOLS["admin"] == TIER_TOOLS["staff"]
        assert TIER_TOOLS["moderator"] == TIER_TOOLS["staff"]

    def test_unknown_tier_gets_the_staff_set(self):
        from core.cli_backend import TIER_TOOLS
        assert TIER_TOOLS.get("nonsense", TIER_TOOLS["staff"]) == TIER_TOOLS["staff"]

    def test_staff_chat_is_not_offered_write(self):
        """Write is how a /chat session created memory/ and then leaked C:\\Users\\..."""
        from core.cli_backend import tools_for
        chat = tools_for("staff", "web").split()
        assert "Write" not in chat and "Edit" not in chat
        assert "Read" in chat
        assert "WebSearch" not in chat and "WebFetch" not in chat
        assert "Write" in tools_for("staff", "cron").split()
        assert "WebSearch" in tools_for("staff", "cron").split()
        assert tools_for("student", "web") == ""

    def test_helix_folders_are_stripped_from_chat_text(self):
        from core.cli_backend import redact_internal_paths
        leaked = f"Filen ligger i {Path.home() / '.helix' / 'workspace'}"
        out = redact_internal_paths(leaked)
        assert ".helix" not in out
        assert "(internal)" in out


class TestPupilKnowledgeIsInlined:
    """With no file tools, anything a pupil needs must already be in the prompt."""

    def test_pupil_prompt_includes_the_network_and_school_guides(self):
        from core.context_engine import bootstrap_files, PUPIL_KNOWLEDGE
        files = bootstrap_files("student")
        for skill in PUPIL_KNOWLEDGE:
            assert skill in files, f"{skill} must reach a pupil, who cannot read it"

    def test_the_azure_material_is_never_inlined_for_a_pupil(self):
        """It holds the cadm convention, BitLocker access and the PIM procedure."""
        from core.context_engine import bootstrap_files
        files = bootstrap_files("student")
        assert not any("entra-azure" in f for f in files)
        assert not any(f.endswith("/SKILL.md") and "visma-inschool" in f for f in files)
        assert "skills/visma-inschool/PUPIL.md" in files

    def test_staff_still_read_skills_from_disk(self):
        """Staff keep file tools, so inlining their whole knowledge base would be waste."""
        from core.context_engine import bootstrap_files
        assert not any(f.startswith("skills/") for f in bootstrap_files("staff"))

    def test_pupil_prompt_uses_the_pupil_visma_guide(self, tmp_path):
        """The staff VIS file points at entra-azure. That must not reach a pupil."""
        cfg = TestTieredPrompt()._workspace(tmp_path)
        visma = tmp_path / "skills" / "visma-inschool"
        visma.mkdir(parents=True)
        (visma / "SKILL.md").write_text(
            "Ask which role the user holds. See skills/entra-azure/SKILL.md",
            encoding="utf-8",
        )
        (visma / "PUPIL.md").write_text(
            "Logg inn med Feide.",
            encoding="utf-8",
        )
        net = tmp_path / "skills" / "frid-nettverk"
        net.mkdir(parents=True)
        (net / "SKILL.md").write_text("FRID-iot", encoding="utf-8")
        with patch("core.context_engine.get_config", return_value=cfg):
            from core.context_engine import build_system_prompt
            prompt = build_system_prompt(is_new_session=True, tier="student")
        assert "Logg inn med Feide" in prompt
        assert "FRID-iot" in prompt
        assert "entra-azure" not in prompt
        assert "which role the user holds" not in prompt

    def test_pupil_prompt_says_there_are_no_files(self):
        """An agent that thinks it can look something up will promise to go and look."""
        from core.context_engine import build_system_prompt
        prompt = build_system_prompt(is_new_session=True, tier="student")
        assert "no tools" in prompt.lower() or "no access to files" in prompt.lower()

    def test_wifi_skill_defers_device_until_download(self):
        """Pupils drop off if they are interviewed before they have something to do."""
        skill = Path("workspace-template/skills/frid-nettverk/SKILL.md").read_text(
            encoding="utf-8"
        )
        student = Path("workspace-template/STUDENT.md").read_text(encoding="utf-8")
        collapsed = " ".join(skill.lower().split())
        assert "clicked download" in collapsed
        assert "never ask them to open the network list" in collapsed
        assert "koble deg først til wifi-nettverket" in collapsed
        assert "Pupils do not have FRID-nett" not in skill
        assert "press **FRID-nett**" in skill
        assert "friddeploy" in skill
        assert "password **`friddeploy`**" in skill
        assert "friddeploy" in student
        assert "**FRID-nett**" in student
        assert "Sjekk denne PC-en" in student
        assert "**Skaff printer**" in student
        assert "ZorkHelper.command" in student
        assert "## Scoping questions" not in skill
        assert "browser reports" in student.lower()
        assert "list the networks they see" in student.lower()

    def test_wifi_windows_guide_has_the_real_clicks_and_screenshots(self):
        """The portal PDF is the Windows source — vague 'install the cert' is how we used to invent steps."""
        import re
        skill = Path("workspace-template/skills/frid-nettverk/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "Start QuickConnect" in skill
        assert "ArubaQuickConnect.exe" in skill
        assert "ClearPass QuickConnect" in skill
        names = re.findall(r"asset:([A-Za-z0-9._-]+)", skill)
        assert "wifi-last-ned-20260910.png" in names
        assert "wifi-start-quickconnect-20260910.png" in names
        assets = Path("workspace-template/assets")
        missing = [
            n for n in names
            if not (assets / n).is_file() or (assets / n).stat().st_size < 100
        ]
        assert missing == []

    def test_every_pupil_turn_repeats_the_wifi_device_deferral(self):
        """Resumed sessions keep the old system prompt; the marker rides every turn."""
        from core.context_engine import STUDENT_TURN_MARKER
        marker = STUDENT_TURN_MARKER.lower()
        assert "onboard.frid-iks.no" in marker
        assert "friddeploy" in STUDENT_TURN_MARKER
        assert "!skolestart!" not in STUDENT_TURN_MARKER
        assert "hvilke nettverk" in marker
        assert "last ned" in marker
        assert "frid-nett" in marker


class TestSharedCoreIsActuallyShared:
    """The two shared files must hold nothing a pupil has to be told to ignore.

    STUDENT.md used to carry a section cancelling the operator material in
    AGENTS.md — a session-startup list naming files a pupil cannot open, a routing
    table pointing at staff-only guides, an instruction to write findings back to
    the knowledge base. Negation loses to length, which is the same failure that
    produced the maths answer. These tests assert absence rather than override.
    """

    # Each marker is a phrase that only makes sense to an agent with tools, files
    # or a knowledge base, paired with why it must not reach a pupil.
    OPERATOR_MARKERS = {
        "Session startup": "names files a pupil's agent cannot open",
        "skills/": "routes to a knowledge base a pupil cannot reach",
        "entra-azure": "the staff-only Azure material, by name",
        "windows-endpoint": "a routing target a pupil cannot read",
        "capture what you learn": "invites writing pupil data into shared files",
        "MEMORY.md": "memory machinery, not a pupil's business",
        "commands/": "slash commands are staff-only",
        "copyable block": "a pupil has no rights to run anything",
        "assets/": "a pupil cannot list it, and may only see the images their "
                   "own guides cite",
    }

    def _workspace(self):
        from core.config import get_config
        return Path(get_config().workspace_path)

    def _shared_text(self):
        ws = self._workspace()
        return "\n".join(
            (ws / name).read_text(encoding="utf-8")
            for name in ("AGENTS.md", "SOUL.md")
        )

    def test_the_shared_files_assume_no_tools(self):
        shared = self._shared_text().lower()
        for marker, why in self.OPERATOR_MARKERS.items():
            assert marker.lower() not in shared, (
                f"{marker!r} is in the shared core but {why} — it belongs in "
                f"OPERATIONS.md"
            )

    def test_the_rules_that_hold_for_everyone_survived_the_move(self):
        """A shared core trimmed to nothing would pass the test above."""
        shared = self._shared_text()
        for kept in ("never report that a change has happened",
                     "Never invent specifics",
                     "one question at a time",
                     "asset:",
                     "Personal data"):
            assert kept.lower() in shared.lower(), (
                f"{kept!r} applies to every conversation and must stay shared"
            )

    def test_operations_reaches_staff_and_internal_work_only(self):
        from core.context_engine import bootstrap_files
        assert "OPERATIONS.md" in bootstrap_files("staff")
        assert "OPERATIONS.md" in bootstrap_files("moderator")
        assert "OPERATIONS.md" in bootstrap_files("admin")
        assert "OPERATIONS.md" not in bootstrap_files("student")
        # Heartbeats and cron pass no tier and need the working method.
        assert "OPERATIONS.md" in bootstrap_files("nonsense")

    def test_the_operator_material_landed_somewhere(self):
        """Guard against the split having been a deletion."""
        operations = (self._workspace() / "OPERATIONS.md").read_text(
            encoding="utf-8").lower()
        for marker in self.OPERATOR_MARKERS:
            assert marker.lower() in operations, (
                f"{marker!r} left the shared core without arriving in OPERATIONS.md"
            )

    def test_the_pupil_file_no_longer_cancels_the_core(self):
        student = (self._workspace() / "STUDENT.md").read_text(encoding="utf-8")
        for negation in ("Ignore the session-startup list",
                         "tells you to capture what you learn",
                         "Things that do not apply here"):
            assert negation not in student, (
                f"{negation!r} is cancelling something that should now be absent"
            )


class TestAgentName:
    """One name for both audiences, and it is never volunteered as an introduction."""

    def _prompt(self, tier):
        from core.context_engine import build_system_prompt
        return build_system_prompt(is_new_session=True, tier=tier)

    def test_both_tiers_are_told_the_same_name(self):
        from core.config import get_config
        from core.context_engine import _agent_name
        name = _agent_name(get_config())
        assert f"Your name is {name}" in self._prompt("staff")
        assert f"Your name is {name}" in self._prompt("student")

    def test_a_blank_display_name_falls_back_to_the_agent_id(self, monkeypatch):
        from core.config import get_config
        from core.context_engine import _agent_name
        cfg = get_config()
        monkeypatch.setattr(cfg, "agent_name", "")
        monkeypatch.setattr(cfg, "agent_id", "helix")
        assert _agent_name(cfg) == "Helix"

    def test_the_greeting_is_in_the_shared_core(self):
        """A greeting that names the agent is the thing we do not want."""
        from pathlib import Path
        from core.config import get_config
        agents = (Path(get_config().workspace_path) / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        assert "Hei! Hva kan jeg hjelpe deg med i dag?" in agents
        assert "Never introduce yourself" in agents
        for prompt in (self._prompt("staff"), self._prompt("student")):
            assert "Hei! Hva kan jeg hjelpe deg med i dag?" in prompt

    def test_the_pupil_header_does_not_promise_a_filesystem(self):
        """It would contradict the scope note, which says there are no tools."""
        prompt = self._prompt("student")
        for plumbing in ("Your home directory is",
                         "Always read from and write to these locations",
                         "read them carefully"):
            assert plumbing not in prompt, (
                f"{plumbing!r} tells a toolless agent it can reach files"
            )

    def test_staff_keep_the_workspace_directions(self):
        prompt = self._prompt("staff")
        assert "Your home directory is" in prompt
        assert "Always read from and write to these locations" in prompt

    def test_staff_chat_is_not_told_the_machine_paths(self):
        """A colleague at /chat quoted those paths back. They are not theirs."""
        from core.context_engine import build_system_prompt
        prompt = build_system_prompt(
            is_new_session=True, tier="staff", channel="web",
            caller_name="Ny Ansatt", caller_email="nyansatt@frid.no",
        )
        assert "Your home directory is" not in prompt
        assert "Do not write facts about this person into memory files" in prompt


class TestDeskChatStore:
    def test_a_message_is_kept_and_read_back(self, tmp_path, monkeypatch):
        from core import desk_chat
        monkeypatch.setattr(desk_chat, "STORE_PATH", tmp_path / "desk-chat.jsonl")
        monkeypatch.setattr(desk_chat, "READ_PATH", tmp_path / "desk-chat-read.json")
        first = desk_chat.post("PC på rom 12 starter ikke", name="Ada", email="ada@frid.no")
        second = desk_chat.post("Jeg tar den", name="Bo", email="bo@frid.no")
        rows = desk_chat.history()
        assert [r["text"] for r in rows] == [
            "PC på rom 12 starter ikke",
            "Jeg tar den",
        ]
        assert rows[0]["id"] == first["id"]
        assert rows[1]["email"] == "bo@frid.no"
        assert second["name"] == "Bo"

    def test_empty_file_is_an_empty_room(self, tmp_path, monkeypatch):
        from core import desk_chat
        monkeypatch.setattr(desk_chat, "STORE_PATH", tmp_path / "missing.jsonl")
        assert desk_chat.history() == []

    def test_unread_skips_your_own_messages(self, tmp_path, monkeypatch):
        from core import desk_chat
        monkeypatch.setattr(desk_chat, "STORE_PATH", tmp_path / "desk-chat.jsonl")
        monkeypatch.setattr(desk_chat, "READ_PATH", tmp_path / "desk-chat-read.json")
        desk_chat.post("PC på rom 12 starter ikke", name="Ada", email="ada@frid.no", role="moderator")
        desk_chat.post("Jeg tar den", name="Bo", email="bo@frid.no", role="moderator")
        assert desk_chat.unread_count("ada@frid.no") == 1
        assert desk_chat.unread_count("bo@frid.no") == 1
        desk_chat.mark_read("ada@frid.no")
        assert desk_chat.unread_count("ada@frid.no") == 0
        desk_chat.post("Ferdig", name="Bo", email="bo@frid.no", role="moderator")
        assert desk_chat.unread_count("ada@frid.no") == 1
        assert desk_chat.unread_count("bo@frid.no") == 1


class TestDeskTickets:
    def test_create_list_reply_and_status(self, tmp_path, monkeypatch):
        from core import desk_tickets
        monkeypatch.setattr(desk_tickets, "STORE_PATH", tmp_path / "tickets.json")
        issue = desk_tickets.create(
            "issue", "Printeren", "2. etasje står.",
            name="Ada", email="ada@frid.no", role="moderator",
        )
        idea = desk_tickets.create(
            "suggestion", "Mørk modus på /chat", "Elevene har spurt.",
            name="Bo", email="bo@frid.no", role="moderator",
        )
        rows = desk_tickets.list_all()
        assert {r["id"] for r in rows} == {idea["id"], issue["id"]}
        assert desk_tickets.new_count() == 2
        replied = desk_tickets.reply(
            issue["id"], "Jeg tar printeren.",
            name="Operator", role="operator", staff=True,
        )
        assert replied["status"] == "new"
        assert replied["replies"][0]["text"] == "Jeg tar printeren."
        assert desk_tickets.new_count() == 2
        desk_tickets.set_status(issue["id"], "waiting_response")
        waiting = desk_tickets.reply(
            issue["id"], "Kan du sende et skjermbilde?",
            name="Operator", role="operator", staff=True,
        )
        assert waiting["status"] == "in_progress"
        assert desk_tickets.new_count() == 1
        closed = desk_tickets.set_status(issue["id"], "finished")
        assert closed["status"] == "finished"
        refused = desk_tickets.set_status(idea["id"], "refused")
        assert refused["status"] == "refused"

    def test_reporter_is_notified_on_status_or_reply_not_on_filing(self, tmp_path, monkeypatch):
        from core import desk_tickets
        monkeypatch.setattr(desk_tickets, "STORE_PATH", tmp_path / "tickets.json")
        issue = desk_tickets.create(
            "issue", "Printeren", "2. etasje står.",
            name="Ada", email="ada@frid.no", role="moderator",
        )
        assert desk_tickets.is_unseen_for_reporter(issue) is False
        desk_tickets.set_status(issue["id"], "in_progress")
        row = desk_tickets.get(issue["id"])
        assert desk_tickets.is_unseen_for_reporter(row) is True
        seen = desk_tickets.mark_reporter_seen(issue["id"], "ada@frid.no", "moderator")
        assert desk_tickets.is_unseen_for_reporter(seen) is False
        desk_tickets.reply(
            issue["id"], "Vi tar den.",
            name="Operator", role="operator", staff=True,
        )
        row = desk_tickets.get(issue["id"])
        assert desk_tickets.is_unseen_for_reporter(row) is True
        desk_tickets.reply(
            issue["id"], "Takk",
            name="Ada", email="ada@frid.no", role="moderator",
        )
        row = desk_tickets.get(issue["id"])
        assert desk_tickets.is_unseen_for_reporter(row) is False
        assert desk_tickets.mark_reporter_seen(issue["id"], "bo@frid.no", "moderator") is None

    def test_screenshots_persist_and_image_only_reply_is_allowed(self, tmp_path, monkeypatch):
        from core import desk_tickets
        monkeypatch.setattr(desk_tickets, "STORE_PATH", tmp_path / "tickets.json")
        monkeypatch.setattr(desk_tickets, "ASSETS_DIR", tmp_path / "ticket-assets")
        name = desk_tickets.save_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ".png")
        issue = desk_tickets.create(
            "issue", "Skjermbilde", "Se vedlegg.",
            name="Ada", email="ada@frid.no", role="moderator",
            images=[name],
        )
        assert issue["images"] == [name]
        assert desk_tickets.image_path(name) is not None
        assert desk_tickets.ticket_for_image(name)["id"] == issue["id"]
        shot = desk_tickets.save_image(b"\x89PNG\r\n\x1a\n" + b"\x01" * 8, ".png")
        replied = desk_tickets.reply(
            issue["id"], "  ",
            name="Operator", role="operator", staff=True,
            images=[shot],
        )
        assert replied["replies"][0]["text"] == ""
        assert replied["replies"][0]["images"] == [shot]
        try:
            desk_tickets.reply(issue["id"], "  ", name="Ada")
            assert False, "expected ValueError"
        except ValueError as e:
            assert "text or image" in str(e)

    def test_empty_subject_is_refused(self, tmp_path, monkeypatch):
        from core import desk_tickets
        monkeypatch.setattr(desk_tickets, "STORE_PATH", tmp_path / "tickets.json")
        try:
            desk_tickets.create("issue", "  ", "body", name="Ada")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_old_done_status_reads_as_finished(self, tmp_path, monkeypatch):
        from core import desk_tickets
        path = tmp_path / "tickets.json"
        monkeypatch.setattr(desk_tickets, "STORE_PATH", path)
        path.write_text(
            '[{"id":"a1","kind":"issue","subject":"x","description":"y",'
            '"status":"done","name":"Ada","email":"","role":"","ts":1,"updated":1,"replies":[]}]\n',
            encoding="utf-8",
        )
        rows = desk_tickets.list_all()
        assert rows[0]["status"] == "finished"

    def test_old_new_status_stays_new(self, tmp_path, monkeypatch):
        from core import desk_tickets
        path = tmp_path / "tickets.json"
        monkeypatch.setattr(desk_tickets, "STORE_PATH", path)
        path.write_text(
            '[{"id":"a1","kind":"issue","subject":"x","description":"y",'
            '"status":"new","name":"Ada","email":"","role":"","ts":1,"updated":1,"replies":[]}]\n',
            encoding="utf-8",
        )
        rows = desk_tickets.list_all()
        assert rows[0]["status"] == "new"


def _app_zip_bytes(name="run.bat", body="echo hi"):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, body)
    return buf.getvalue()


class TestAppsCatalog:
    """Operator-panel download shelf. Helix stores files; it never runs them."""

    def _mod(self, tmp_path, monkeypatch):
        from core import apps as helix_apps
        monkeypatch.setattr(helix_apps, "STORE_DIR", tmp_path / "helix-apps")
        monkeypatch.setattr(helix_apps, "SEED_DIR", tmp_path / "no-seed")
        return helix_apps

    def test_put_list_download_replace_delete(self, tmp_path, monkeypatch):
        helix_apps = self._mod(tmp_path, monkeypatch)
        first = helix_apps.put_app(
            name="Skoleprogrammer",
            data=_app_zip_bytes(),
            filename="Skoleprogrammer.zip",
            description="Check Office on a pupil PC",
            platforms="windows,mac",
            uploaded_by="ada@frid-iks.no",
        )
        assert first["id"] == "skoleprogrammer"
        assert first["filename"] == "Skoleprogrammer.zip"
        assert first["platforms"] == ["windows", "mac"]
        listed = helix_apps.list_apps()
        assert listed[0]["id"] == "skoleprogrammer"
        path = helix_apps.file_path(first["id"], first["filename"])
        assert path is not None and path.is_file()
        assert path.read_bytes().startswith(b"PK")
        updated = helix_apps.put_app(
            name="Skoleprogrammer",
            data=_app_zip_bytes("Les meg.txt", "ny"),
            filename="Skoleprogrammer.zip",
            app_id="skoleprogrammer",
            uploaded_by="bo@frid-iks.no",
        )
        assert updated["id"] == "skoleprogrammer"
        assert helix_apps.file_path("skoleprogrammer", "Skoleprogrammer.zip").read_bytes().startswith(b"PK")
        assert helix_apps.delete_app("skoleprogrammer") is True
        assert helix_apps.list_apps() == []
        assert helix_apps.file_path("skoleprogrammer") is None

    def test_put_from_path_copies_msi_without_loading_payload(self, tmp_path, monkeypatch):
        helix_apps = self._mod(tmp_path, monkeypatch)
        src = tmp_path / "VikenPrint.msi"
        src.write_bytes(b"\xd0\xcf\x11\xe0" + b"printer" * 20)
        row = helix_apps.put_app_from_path(
            name="printer",
            src=src,
            description="Viken Print / YSoft SafeQ",
            platforms="windows",
            app_id="printer",
            uploaded_by="local-copy",
        )
        assert row["id"] == "printer"
        assert row["name"] == "printer"
        assert row["filename"] == "VikenPrint.msi"
        dest = helix_apps.file_path("printer", "VikenPrint.msi")
        assert dest is not None and dest.is_file()
        assert dest.read_bytes() == src.read_bytes()
        assert dest != src

    def test_adopts_printer_msi_already_on_disk(self, tmp_path, monkeypatch):
        helix_apps = self._mod(tmp_path, monkeypatch)
        dest = tmp_path / "helix-apps" / "printer"
        dest.mkdir(parents=True)
        msi = dest / "VikenPrint.msi"
        msi.write_bytes(b"\xd0\xcf\x11\xe0" + b"printer" * 20)
        listed = helix_apps.list_apps()
        row = next(item for item in listed if item["id"] == "printer")
        assert row["filename"] == "VikenPrint.msi"
        assert row["size"] == msi.stat().st_size
        path = helix_apps.file_path("printer", "VikenPrint.msi")
        assert path is not None and path.is_file()

    def test_rejects_path_traversal_and_non_zip_payload(self, tmp_path, monkeypatch):
        helix_apps = self._mod(tmp_path, monkeypatch)
        try:
            helix_apps.safe_filename("../evil.zip")
        except ValueError:
            raise AssertionError("basename zip should be allowed")
        assert helix_apps.safe_filename(r"..\evil.zip") == "evil.zip"
        try:
            helix_apps.put_app(name="Nope", data=b"not a zip", filename="tool.zip")
            assert False, "expected ValueError"
        except ValueError as e:
            assert "zip" in str(e)
        try:
            helix_apps.slugify("???")
            assert False, "expected ValueError"
        except ValueError:
            pass
        assert helix_apps.file_path("../etc") is None
        assert helix_apps.get_app("not a real id") is None

    def test_seed_copies_once_and_delete_sticks(self, tmp_path, monkeypatch):
        from core import apps as helix_apps
        seed = tmp_path / "seed" / "demo"
        payload = seed / "payload"
        payload.mkdir(parents=True)
        (payload / "run.bat").write_text("@echo off\n", encoding="utf-8")
        (seed / "manifest.json").write_text(json.dumps({
            "id": "demo",
            "name": "Demo tool",
            "filename": "Demo.zip",
            "description": "seeded",
            "platforms": ["windows"],
        }), encoding="utf-8")
        monkeypatch.setattr(helix_apps, "STORE_DIR", tmp_path / "helix-apps")
        monkeypatch.setattr(helix_apps, "SEED_DIR", tmp_path / "seed")
        rows = helix_apps.list_apps()
        assert [r["id"] for r in rows] == ["demo"]
        assert rows[0]["uploaded_by"] == "seed"
        assert helix_apps.file_path("demo", "Demo.zip").is_file()
        assert helix_apps.delete_app("demo") is True
        assert helix_apps.list_apps() == []
        assert helix_apps.file_path("demo") is None


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestConsoleApps:
    """Moderators download. Admins put files on the shelf. Staff cannot."""

    def test_moderator_downloads_and_cannot_upload(self, tmp_path, monkeypatch):
        from core import apps as helix_apps
        monkeypatch.setattr(helix_apps, "STORE_DIR", tmp_path / "helix-apps")
        monkeypatch.setattr(helix_apps, "SEED_DIR", tmp_path / "no-seed")
        helix_apps.put_app(
            name="Tool",
            data=_app_zip_bytes(),
            filename="tool.zip",
            platforms="windows",
        )
        mod = TestModeratorConsole()._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        listed = mod.get("/api/apps", headers=h)
        assert listed.status_code == 200
        assert listed.json()["apps"][0]["id"] == "tool"
        dl = mod.get("/api/apps/tool/download", headers=h)
        assert dl.status_code == 200
        assert dl.content.startswith(b"PK")
        assert "attachment" in (dl.headers.get("content-disposition") or "").lower()
        posted = mod.post(
            "/api/apps",
            files={"file": ("other.zip", _app_zip_bytes(), "application/zip")},
            data={"name": "Other"},
            headers=h,
        )
        assert posted.status_code == 403
        deleted = mod.delete("/api/apps/tool", headers=h)
        assert deleted.status_code == 403

    def test_admin_can_upload_and_staff_cannot_see_the_shelf(self, tmp_path, monkeypatch):
        from core import apps as helix_apps
        monkeypatch.setattr(helix_apps, "STORE_DIR", tmp_path / "helix-apps")
        monkeypatch.setattr(helix_apps, "SEED_DIR", tmp_path / "no-seed")
        admin = TestAdminApiIntegration()._make_client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer validtoken"}
        created = admin.post(
            "/api/apps",
            files={"file": ("tool.zip", _app_zip_bytes(), "application/zip")},
            data={
                "name": "Office checker",
                "description": "Check Office",
                "platforms": "windows,mac",
                "run_hint": "Unpack and run windows_run.bat as administrator.",
            },
            headers=h,
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["id"] == "office-checker"
        assert body["platforms"] == ["windows", "mac"]
        listed = admin.get("/api/apps", headers=h)
        assert listed.status_code == 200
        assert listed.json()["apps"][0]["filename"] == "tool.zip"
        gone = admin.delete("/api/apps/office-checker", headers=h)
        assert gone.status_code == 200
        assert helix_apps.list_apps() == []
        staff = TestModeratorConsole()._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        assert staff.get("/api/apps", headers={"Authorization": "Bearer tok"}).status_code == 403

    def test_panel_offers_apps_to_moderators(self):
        page = Path("web/static/index.html").read_text(encoding="utf-8")
        assert "{ id: 'apps',     label: 'Apps'" in page
        assert "'apps'" in page.split("moderatorPages:")[1].split("]")[0]
        assert "currentPage === 'apps'" in page
        assert "downloadApp(app)" in page
        assert "runShelfApp(app)" in page
        assert "isHelperApp(app)" in page
        assert "School/work Programs" in page
        assert "shelfApps()" in page
        assert "appDisplayName(app)" in page
        assert "startPcCheck()" in page
        assert "startFridNett()" in page
        assert "startHelperInstall" in page
        assert "kind: 'install'" in page
        assert "kind: 'scan'" in page
        assert "Zork helper is needed to run the listed apps" in page
        assert "ZorkHelper.cmd" in page
        assert "ZorkHelper.command" in page
        assert "?os=mac" in page
        assert "zorkhelper://wake" in page
        assert "isFridNettInstall" in page
        assert "wakeHelperApp('fridnett')" in page
        assert "loopback-network" in page
        assert "helperLoopbackDenied" in page
        assert "allowDownload: false" in page
        assert "helperIsInstalled()" in page
        assert "waitForLoopbackDecision" in page
        assert "['skoleprogrammer']" in page
        assert "id: 'printer'" in page
        assert "['printer']" in page
        assert "Get printer" in page
        assert "FRID-nett and printer sit at the top" in page


class TestPcHelper:
    """Tiny launcher plus a read-only scan. No arbitrary remote commands."""

    def test_launcher_is_tiny_and_not_skoleprogrammer(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="https://zork.example")
        cmd = pc_helper.launcher_cmd(row["base_url"], row["id"], row["secret"])
        assert cmd.startswith("@echo off")
        assert "tray launcher" in cmd
        assert "EncodedCommand" in cmd
        assert "/bootstrap" in Path("core/pc_helper_launch.ps1").read_text(encoding="utf-8")
        assert "net session" not in cmd
        assert "Skoleprogrammer.ps1" not in cmd
        assert row["id"] not in pc_helper.scan_script()
        script = pc_helper.scan_script()
        assert "param(" in script
        assert "UploadString" in script
        assert "Invoke-Expression" not in script
        assert "-EncodedCommand" not in script
        assert "items       = @($items)" not in script
        assert "ToArray()" in script
        assert "Microsoft.Update.Session" in script
        assert "IsInstalled=0" in script
        assert "Get-AppxPackage -AllUsers -Name 'MSTeams'" in script
        assert "ms-teams" in script
        assert "Get-Process" in script
        tray = pc_helper.tray_script()
        assert "NotifyIcon" in tray
        assert "/v1/ping" in tray
        assert "Get-PingBody" in tray
        assert "Refresh-WifiStatus" in tray
        assert "WifiCheckedAt" in tray
        assert "Test-SchoolFridSsid" in tray
        assert "Test-FridIotSsid" in tray
        assert "Test-CaptiveOffline" in tray
        assert "install-script.ps1" in tray
        assert "Save-HttpToFile" in tray
        assert "Get-HelperHealth" in tray
        assert "helper_api_rev" in tray
        assert "helper_ready" in tray
        assert "install_seed_ok" in tray
        assert "install_cache_ok" in tray
        assert "progress  = Get-LocalProgress" in tray
        assert "install-busy.flag" in tray
        assert "Drain-OpenUrl" in tray
        assert "open-url.txt" in tray
        assert "zork-helper-install.ps1" in tray
        assert "Start-InboxFridNett" in tray
        assert "Resolve-InboxSession" in tray
        assert "Test-SessionTokenUsable" in tray
        assert "offline-local-session-token" in tray
        assert "op -eq 'fridnett'" in tray
        assert "--new-window" in tray
        assert "msedge.exe" in tray
        assert "ZorkHelperEdge" in tray
        assert "ProcessStartInfo" in tray
        assert "gjesteportal" in tray
        assert "--remote-debugging-port" in tray
        assert "Get-NetConnectionProfile" in tray
        assert "netsh wlan show interfaces" not in tray
        ping_block = tray.split("GET\\s+/v1/ping")[1].split("POST\\s+/v1/job")[0]
        assert "Refresh-WifiStatus" not in ping_block
        assert "Get-PingBody" in ping_block
        assert "/v1/wake" in tray
        assert "18792" in tray
        assert "zorkhelper" in tray
        assert "URL Protocol" in tray
        assert "Test-JobAllowed" in tray
        assert "New-ZorkIcon" in tray
        assert "GraphicsPath" in tray
        assert "AddClosedCurve" in tray
        assert "New-Object System.Drawing.Font 'Segoe UI'" not in tray
        assert "SystemIcons" in tray
        assert "ApplicationContext" in tray
        assert "Hide-ZorkConsole" in tray
        assert "GetApartmentState" in tray
        assert "Run($script:AppContext)" in tray
        assert "exit 1" not in tray
        install_job = tray.split("function Start-InstallJob")[1].split("function Register-Device")[0]
        assert "[Diagnostics.ProcessWindowStyle]::Normal" in install_job
        assert "runas" in install_job
        assert "zork-elevate-" in install_job
        assert "JobFile" in install_job
        assert "DoneFile" in install_job
        assert "FRID-iot" in install_job
        assert "fridnett" in tray
        assert "Test-FridNettOnly" in tray
        assert "Sjekker wifi" in tray
        assert "Done! Du er koblet til riktig nettverk" in tray
        assert "AfterInstallScan" in tray
        assert "Test-PrinterOnly" in tray
        assert "-not (Test-PrinterOnly" in tray
        assert "BusyStartedTick" in tray
        assert "-not $script:SkipScan" not in tray
        assert "Test-InstallStillRunning" in tray
        assert "zork-printer-" in tray
        assert "CreateNoWindow" in install_job
        assert "zork-elevate-" in install_job
        finish = tray.split("function Finish-BusyIfNeeded")[1].split("function")[0]
        install_finish = finish.split("} else {")[0]
        assert "BusyDone" in install_finish
        assert "HasExited" in install_finish
        assert "18000" not in install_finish
        drain = tray.split("function Drain-Incoming")[1].split("function Handle-LocalClient")[0]
        assert "BusyKind -eq 'install'" in drain
        inst = pc_helper.install_script()
        assert "JobFile" in inst
        assert "DoneFile" in inst
        assert "Laster ned Windows-oppdateringer" in inst
        assert "Installerer Windows-oppdateringer" in inst
        assert "kode $code" not in inst
        assert "ferdig (kode" not in inst
        assert "-Activity ''" in inst
        assert "$needTool.Count -gt 0 -or $wanted.Count -gt 0" not in inst
        assert "sketchup" in inst
        assert "fridnett" in inst
        assert "skoleprogrammer" in inst
        assert "printer" in inst
        assert "Install-FridNett" in inst
        assert "Install-Printer" in inst
        assert "Stop-ZorkScanJobs" in inst
        assert "zork-scan-" in inst
        assert "AllocConsole" in inst
        assert "SetForegroundWindow" in inst
        assert "/tool/fridnett" in inst
        assert "/tool/printer" in inst
        mac = pc_helper.scan_mac_script()
        assert "softwareupdate -l" in mac
        assert "softwareupdate -i" not in mac
        assert "Microsoft Word.app" in mac
        assert "macupdate" in mac
        assert "osascript" not in mac
        assert "eval" not in mac
        launch_mac = pc_helper.launcher_mac("https://zork.example", row["id"], row["secret"])
        assert launch_mac.startswith("#!/bin/bash")
        assert "script-mac" in launch_mac
        assert "EncodedCommand" not in launch_mac
        assert row["id"] in launch_mac
        assert "PrinterOk" in inst
        assert "Done! Printer er installert." in inst
        assert "Klarte ikke a installere printer." in inst
        assert "Test-MsiOk" in inst
        assert "Show-PrinterBar" in inst
        assert "Show-PrinterWindow" in inst
        assert "Write-Progress" in inst
        assert "Laster ned printer... " in inst
        assert "3010" in inst
        assert "msiexec.exe" in inst
        assert "Connect-FridIot" in inst
        assert "Test-SsidInRange" in inst
        assert "Invoke-Netsh" in inst
        assert "System32\\netsh.exe" in inst
        assert "WaitForExit" in inst
        assert "'wlan', 'show', 'networks'" in inst
        assert "'wlan', 'disconnect'" in inst
        assert "hex>465249442d696f74" in inst
        assert "enableRandomization>false" in inst
        assert "Prover a koble til FRID-iot likevel" in inst
        assert "Kobler til FRID-iot pa nytt med riktig passord" in inst
        assert "Hvis FRID ber om brukernavn og passord" in inst
        assert "IT Helpdesk i 1. etasje" in inst
        assert "& netsh" not in inst
        assert "Done! Du er koblet til riktig nettverk" in inst
        assert "Get-SsidMatch $wifi.Ssid 'FRID'" in inst
        assert "Open-FridOnboardLogin" in inst
        assert "Wait-FridQuickConnectProcess" in inst
        assert "CreateProcessWithTokenW" in inst
        assert "GetShellWindow" in inst
        assert "Restart-HelperTray" in inst
        assert "install-busy.flag" in inst
        assert "fridOnly" in inst
        assert "ZorkUnelevate" in inst
        assert "--remote-debugging-port" in inst
        assert "mdps_status.php" in inst
        assert "ZorkEdgeCdp" in inst
        assert "ClickStartQuickConnect" in inst
        assert "remote-allow-origins" in inst
        assert "Start-EdgeOnboard" in inst
        assert "Start-EdgeWithArgs" in inst
        assert "Wait-ZorkEdgeReady" in inst
        assert "ok-nocdp" in inst
        assert "Ensure-ZorkDefenderExclusions" in inst
        assert "Get-MpPreference" in inst
        assert "Add-MpPreference" in inst
        assert "Windows-sikkerhet: la til unntak for Zork Helper." in inst
        assert "Windows-sikkerhet lot ikke helper legge til unntak" in inst
        assert "Edge-start:" in inst
        assert "msedge.exe" in inst
        assert "--new-window" in inst
        assert "--user-data-dir" in inst
        assert "ZorkHelperEdge" in inst
        assert "ProcessStartInfo" in inst
        assert "gjesteportal.frid-iks.no" in inst
        assert "frid_provisioning.php" in inst
        assert "Hvis Start QuickConnect ikke klikkes automatisk" in inst
        assert "Hvis vinduet ikke vises" in inst
        assert "open-url.txt" in inst
        assert "--app=" not in inst
        assert "--window-size=700,700" not in inst
        assert "Set-CaptiveOffline" in inst
        assert "SkipHelixHttp" in inst
        assert "progress.txt" in inst
        assert "captive.flag" in inst
        assert "HttpWebRequest" in inst
        assert "Timeout = 2500" in inst
        assert "gjesteportal.frid-iks.no/onboard/frid_provisioning.php" in inst
        assert 't.Contains("viken")' not in inst
        assert "microsoftonline" in inst
        assert "microsoft edge" in inst
        assert "netsh.exe" in inst
        assert "FRID-iot" in inst
        assert "keyMaterial>friddeploy" in inst
        assert "ClickButtons" in inst
        assert "BM_CLICK" in inst
        assert "ArubaQuickConnect" not in pc_helper.scan_script()
        scan = pc_helper.scan_script()
        assert "SketchUp" not in scan
        assert "sketchup" not in scan
        assert "reboot  = [bool]$Reboot" in scan or "reboot =" in scan
        assert "Item 'winupdate'" in scan
        assert "Item 'printer'" in scan
        assert "SafeQ6" in scan
        assert "($pendingReboot -and ($n -eq 0))" not in scan
        assert "Update-HelperTray" in scan
        assert "Invoke-Expression" not in tray
        opener = pc_helper.open_script()
        assert "127.0.0.1:18792" in opener
        assert "zork-helper-tray.ps1" in opener
        assert "WindowStyle Hidden" in opener
        assert "Get-InboxOp" in opener
        assert "fridnett" in opener
        assert "Invoke-Expression" not in opener
        assert pc_helper.HELPER_PROTOCOL == "zorkhelper"
        install = pc_helper.install_script()
        assert "Microsoft.Teams" in install
        assert "Invoke-Expression" not in install
        assert "IsInstalled=0" in install
        boot = pc_helper.bootstrap_script()
        assert "Write-Progress" in boot
        assert "Loading..." in boot
        assert "Send-BootProgress" in boot
        assert "/install-script" in boot
        assert "zork-helper-install.ps1" in boot
        assert "install-script.ps1" in boot
        assert "HasExited" not in boot
        assert "/v1/ping" in boot
        assert "/open" in boot
        assert "zorkhelper" in boot
        assert "Update-HelperTray" in inst
        assert boot.index("Starter Zork Helper ved klokken") < boot.index("Sjekker programmer")
        assert "Send-LocalJob" in boot
        assert "AllowEmptyString" in boot
        assert "__DEFAULT_JOB_ITEMS__" in Path("core/pc_helper_bootstrap.ps1").read_text(encoding="utf-8")
        assert "-JobKind" in Path("core/pc_helper_launch.ps1").read_text(encoding="utf-8")
        assert "__JOB_KIND__" in Path("core/pc_helper_launch.ps1").read_text(encoding="utf-8")
        assert "IsNullOrWhiteSpace($jobItems)" in Path("core/pc_helper_launch.ps1").read_text(encoding="utf-8")
        assert "-JobItems $jobItems" not in Path("core/pc_helper_launch.ps1").read_text(encoding="utf-8")
        printer_boot = pc_helper.bootstrap_script(job_kind="install", job_items=["printer"])
        assert "$JobItems = 'printer'" in printer_boot
        assert "$JobKind = 'install'" in printer_boot
        assert "__DEFAULT_JOB_ITEMS__" not in printer_boot
        install_cmd = pc_helper.launcher_cmd(
            row["base_url"], row["id"], row["secret"],
            job_kind="install", job_items=["fridnett"],
        )
        marker = "-EncodedCommand "
        encoded = install_cmd.split(marker, 1)[1].split("\r\n", 1)[0].strip()
        decoded = base64.b64decode(encoded).decode("utf-16le")
        assert "-JobKind" in decoded
        assert "install" in decoded
        assert "fridnett" in decoded
        assert pc_helper.set_session_intent(row["id"], "install", ["fridnett"])["intent"] == {
            "kind": "install", "items": ["fridnett"],
        }
        assert pc_helper.session_intent(pc_helper.get(row["id"])) == ("install", ["fridnett"])
        assert "Invoke-Expression" not in boot
        assert "kan ta et minutt" in pc_helper.scan_script()

    def test_wrong_secret_cannot_post_a_result(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        assert pc_helper.set_result(row["id"], "nope-secret-value-xx", {
            "host": "PC1", "items": [{"id": "office", "name": "Office", "found": True}],
        }) is None
        assert pc_helper.get(row["id"])["status"] == "waiting"

    def test_result_is_clipped_to_known_fields(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        out = pc_helper.set_result(row["id"], row["secret"], {
            "host": "KLASSE-PC",
            "os": "Windows 11",
            "admin": True,
            "eval": "should-not-run",
            "items": [
                {"id": "office", "name": "Office", "found": True, "ok": True, "version": "16", "note": "nb-NO"},
            ],
        })
        assert out["status"] == "done"
        assert "eval" not in out["result"]
        assert out["result"]["items"][0]["id"] == "office"
        assert out["result"]["items"][0]["reboot"] is False

    def test_result_keeps_windows_update_reboot_flag(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        out = pc_helper.set_result(row["id"], row["secret"], {
            "host": "PC1",
            "items": [{
                "id": "winupdate", "name": "Windows Update",
                "found": True, "ok": False, "reboot": True,
                "note": "Omstart venter etter oppdateringer",
            }],
        })
        assert out["result"]["items"][0]["reboot"] is True

    def test_progress_can_mark_install_on_a_finished_scan(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        pc_helper.set_result(row["id"], row["secret"], {
            "host": "PC1",
            "items": [{"id": "winupdate", "name": "Windows Update", "found": True, "ok": False}],
        })
        installing = pc_helper.add_progress(row["id"], row["secret"], "UAC", activity="installing")
        assert installing["status"] == "done"
        assert installing["activity"] == "installing"
        stopped = pc_helper.add_progress(row["id"], row["secret"], "avbrutt", activity="")
        assert stopped["activity"] == ""
        pc_helper.add_progress(row["id"], row["secret"], "UAC", activity="installing")
        done = pc_helper.add_progress(row["id"], row["secret"], "Installasjon ferdig.")
        assert done["activity"] == ""

    def test_scan_script_does_not_wrap_the_item_list_the_way_ps51_throws(self):
        from core import pc_helper
        script = pc_helper.scan_script()
        assert script.startswith("\ufeff")
        assert "@($items)" not in script.replace(" ", "")
        assert "[object[]]$rawItems" in script or "[object[]]" in script

    def test_result_accepts_powershell_numbered_item_objects(self):
        from core import pc_helper
        pc_helper.reset_store()
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        out = pc_helper.set_result(row["id"], row["secret"], {
            "host": "PC1",
            "items": {"0": {"id": "office", "name": "Office", "found": True, "ok": True}},
        })
        assert out["status"] == "done"
        assert out["result"]["items"][0]["id"] == "office"

    def test_presence_and_allowlisted_install_job(self):
        from core import pc_helper
        pc_helper.reset_store()
        assert pc_helper.presence_for("ada@frid-iks.no")["online"] is False
        row = pc_helper.create_session(owner="ada@frid-iks.no", base_url="http://localhost")
        waiting = pc_helper.queue_job(
            owner="ada@frid-iks.no", session_id=row["id"],
            kind="install", items=["teams", "rm -rf /", "winupdate"],
        )
        assert waiting["via"] == "waiting"
        assert pc_helper.get(row["id"])["activity"] != "installing"
        assert pc_helper.queue_job(
            owner="ada@frid-iks.no", session_id=row["id"],
            kind="install", items=["nope"],
        ) is None
        device = pc_helper.register_device(row["id"], row["secret"], "KLASSE-PC")
        assert device and device["id"] and device["secret"]
        beat = pc_helper.heartbeat(device["id"], device["secret"])
        assert beat["ok"] is True
        assert pc_helper.presence_for("ada@frid-iks.no")["online"] is True
        job = pc_helper.take_job(device["id"], device["secret"])
        assert job["job"]["kind"] == "install"
        assert job["job"]["items"] == ["teams", "winupdate"]
        assert pc_helper.normalize_install_items(["fridnett", "rm -rf /"]) == ["fridnett"]
        assert "rm" not in str(job)
        assert pc_helper.take_job(device["id"], device["secret"])["job"] is None
        assert pc_helper.heartbeat(device["id"], "wrong-secret-value-xx") is None
        parked = pc_helper.create_session(owner="bo@frid-iks.no", base_url="http://localhost")
        assert pc_helper.queue_job(
            owner="bo@frid-iks.no", session_id=parked["id"],
            kind="install", items=["teams"],
        )["via"] == "waiting"
        assert pc_helper.mark_connected(parked["id"], parked["secret"])
        device2 = pc_helper.register_device(parked["id"], parked["secret"], "ANNEN-PC")
        assert pc_helper.take_job(device2["id"], device2["secret"])["job"] is None
        other = pc_helper.create_session(owner="other@frid-iks.no", base_url="http://localhost")
        assert pc_helper.queue_job(
            owner="other@frid-iks.no", session_id=row["id"], kind="scan",
        ) is None
        assert pc_helper.queue_job(
            owner="ada@frid-iks.no", session_id=other["id"], kind="scan",
        ) is None


    def test_fridnett_seed_is_a_windows_program(self, tmp_path, monkeypatch):
        from core import pc_helper
        from core import apps as helix_apps
        monkeypatch.setattr(helix_apps, "STORE_DIR", tmp_path / "helix-apps")
        monkeypatch.setattr(helix_apps, "SEED_DIR", Path("apps-seed"))
        assert "fridnett" in pc_helper.INSTALLABLE
        assert "skoleprogrammer" in pc_helper.INSTALLABLE
        assert "printer" in pc_helper.INSTALLABLE
        assert pc_helper.normalize_install_items(["skoleprogrammer", "rm -rf /"]) == ["skoleprogrammer"]
        found = pc_helper.fridnett_file()
        assert found is not None
        row, path = found
        assert path.is_file()
        assert path.read_bytes()[:2] == b"MZ"
        assert "frid" in (row.get("id") or "").lower() or "frid" in (row.get("name") or "").lower()
        staff = Path("workspace-template/STAFF.md").read_text(encoding="utf-8")
        assert "**FRID-nett**" in staff
        assert "Zork Helper joins **FRID-iot**" in staff
        assert "login window" in staff
        assert "already on the wifi named" in staff
        assert "says Done" in staff
        assert "**Skaff printer**" in staff
        assert "macOS updates" in staff
        assert "ZorkHelper.command" in staff
        assert "that button is hidden" not in staff
        assert "password **friddeploy**" not in staff
        assert "Apper på enheten" in staff


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestConsoleHelper:
    def test_moderator_check_flow_staff_ok_pupils_ok(self, tmp_path, monkeypatch):
        from core import pc_helper
        pc_helper.reset_store()
        mod = TestModeratorConsole()._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        created = mod.post("/api/helper/sessions", headers=h)
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        assert created.json()["status"] == "waiting"
        assert "secret" not in created.json()
        assert created.json().get("handoff")
        assert created.json().get("local_port") == 18792
        status0 = mod.get(f"/api/helper/sessions/{sid}", headers=h)
        assert "handoff" not in status0.json()
        assert "secret" not in status0.json()
        launcher = mod.get(f"/api/helper/sessions/{sid}/launcher", headers=h)
        assert launcher.status_code == 200
        body = launcher.text
        assert "EncodedCommand" in body
        assert "attachment" in (launcher.headers.get("content-disposition") or "").lower()
        assert "ZorkHelper.cmd" in (launcher.headers.get("content-disposition") or "")
        row = pc_helper.get(sid)
        secret = row["secret"]
        script = mod.get(
            f"/api/helper/{sid}/script",
            headers={"X-Helper-Secret": secret},
        )
        assert script.status_code == 200
        assert "Skoleprogrammer.ps1" not in script.text
        assert "Microsoft 365" in script.text
        posted = mod.post(
            f"/api/helper/{sid}/result",
            headers={"X-Helper-Secret": secret},
            json={"data": {
                "host": "ELEV-PC",
                "os": "Windows 11",
                "admin": True,
                "items": [{"id": "office", "name": "Office", "found": True, "ok": True, "note": "nb-NO"}],
            }},
        )
        assert posted.status_code == 200
        status = mod.get(f"/api/helper/sessions/{sid}", headers=h)
        assert status.status_code == 200
        assert status.json()["status"] == "done"
        assert status.json()["result"]["host"] == "ELEV-PC"
        staff = TestModeratorConsole()._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        own = staff.post("/api/helper/sessions", headers=h)
        assert own.status_code == 200, own.text
        pupil = TestModeratorConsole()._client(tmp_path, monkeypatch, claims={
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        own_pupil = pupil.post("/api/helper/sessions", headers=h)
        assert own_pupil.status_code == 200, own_pupil.text
        assert pupil.get("/api/helper/presence", headers=h).status_code == 200
        assert pupil.get(
            f"/api/helper/sessions/{sid}", headers=h,
        ).status_code == 404
        assert mod.post(
            f"/api/helper/{sid}/result",
            headers={"X-Helper-Secret": "wrong-secret-value-1"},
            json={"data": {"host": "x", "items": []}},
        ).status_code == 404

    def test_chat_page_can_start_the_helper(self):
        page = Path("web/static/chat.html").read_text(encoding="utf-8")
        assert "startPcCheck()" in page
        assert "ZorkHelper.cmd" in page
        assert "ZorkHelper.command" in page
        assert "isHelperPc" in page
        assert "isMacPc" in page
        assert 'id="cronPulse"' in page
        assert "startCronPulse" in page
        assert "/api/crons" in page
        assert "last run succeeded" in page
        assert "cronPulseMore" in page
        assert 'me.console_role === "admin"' in page
        assert "pcopenmac" in page
        assert "?os=mac" in page
        assert 'id="pcHelperUpdateChip"' not in page
        assert 'id="pcHelperUpdateBtn"' not in page
        assert "ensureHelperReadyForRun" in page
        assert "waitForHelperSessionTerminal" in page
        assert 'id="pcFridNettBtn"' in page
        assert "startFridNett" in page
        assert "pcfridnett" in page
        assert 'id="pcPrinterBtn"' in page
        assert "startPrinter" in page
        assert "pcprinter" in page
        assert 'id="cronPulse"' in page
        assert "startCronPulse" in page
        assert "/api/crons" in page
        assert "last run succeeded" in page
        assert "cronPulseMore" in page
        assert 'me.console_role === "admin"' in page
        assert "Skaff printer" in page
        assert "Get printer" in page
        assert "pcprinterfail" in page
        assert "helperIsPrinterFailed" in page
        assert "printerDownloadProgress" in page
        assert "pc-bar" in page
        assert "pcprinterdl" in page
        assert "pcprinteruacnote" in page
        assert "pcprinterstart" in page
        assert "pcprinterstartnote" in page
        assert "tellAgentPrinterFinished" in page
        assert "laster ned printer" in page
        headline = page.split("function helperHeadline")[1].split("function paintHelperCard")[0]
        printer_head = headline.split("if(helperPrinterJob){")[1].split("if(helperFridJob){")[0]
        assert 't("pcprinteruac")' in printer_head
        assert 't("pcprinterstart")' in printer_head
        assert "if(installing && line)" not in printer_head
        assert "return { text: line" not in printer_head
        frid_head = headline.split("if(helperFridJob){")[1].split("if(installing){")[0]
        assert 't("pcfridnettswitch")' in frid_head
        assert "sjekker wifi" in frid_head
        paint = page.split("function paintHelperCard")[1].split("function formatScanForAgent")[0]
        assert "pcprinteruacnote" in paint
        assert "pcprinterstartnote" in paint
        assert "pcfridnettswitch" in paint
        assert "pc-bar-busy" in paint
        deliver = page.split("async function deliverHelperJob")[1].split("async function dispatchHelperJob")[0]
        assert "askLocalHelper" in deliver
        assert "pingLocalHelper" not in deliver
        startp = page.split("async function startPrinter")[1].split("async function startPcCheck")[0]
        assert "stopWifiWatch" in startp
        assert "ensureHelperReadyForRun" in startp
        assert "if(helperSession && helperSession.id)" not in startp
        startf = page.split("async function startFridNett")[1].split("function tellAgentFridNettAlreadyOnFrid")[0]
        assert "stopWifiWatch" in startf
        assert "ensureHelperReadyForRun" in startf
        assert "if(helperSession && helperSession.id)" not in startf
        assert "helperPrinterJob || helperFridJob" in page
        assert '["printer"]' in page
        assert "openFridCertificateLogin" not in page
        assert "Last ned sertifikat" not in page
        assert 'id="certChip"' not in page
        assert 'id="certBtn"' not in page
        assert "pclastned" not in page
        assert "<iframe" not in page.lower()
        assert "looksLikeWifiAsk" in page
        assert '["fridnett"]' in page
        assert 'kind: "install"' in page
        assert "!helperFridJob" in page
        assert "offerFridNettCard" in page
        assert "onFridNetwork" in page
        assert "startWifiWatch" in page
        assert "helperWifiKnown" in page
        assert "helperWifiProbed" in page
        assert "isWindowsPc" in page
        assert "pcfridnettdone" in page
        assert "pcfridnettswitch" in page
        assert "helperLocalProgress" in page
        assert "data.progress" in page
        assert "helperFridJob ? 2500" in page
        assert "waitForHelperPing" in page
        assert "helperWifiProbed && !onFridNetwork()" in page
        assert "isSchoolFridSsid" in page
        assert "helperOnSchoolFrid" in page
        assert "helperAlreadyOnThisPc" in page
        assert "Hvis FRID ber om brukernavn/passord da, be dem gå til IT Helpdesk i 1. etasje." in page
        assert "waitForLoopbackDecision" in page
        assert "helperSeenThisTab" in page
        assert "HELPER_MIN_API_REV" in page
        assert "helperLooksUpdated" in page
        assert "rememberHelperHealth" in page
        assert "waitForHelperSessionTerminal(session.id, 20000)" in page
        assert "probeHelperWifi" in page
        assert "FRID[-_ ]?iot" in page
        assert "const show = !isPupil" not in page
        assert "if(isPupil || helperBusy)" not in page
        assert "if(isPupil || helperInstallBusy" not in page
        assert "Zork Helper kobler til FRID-iot" in page
        assert "Zork Helper joins FRID-iot" in page
        assert "Koble til FRID-iot med passordet friddeploy først" not in page
        assert "Connect to FRID-iot with the password friddeploy first" not in page
        assert "echo: false" in page
        assert "requestHelperInstall" in page
        assert "pingLocalHelper" in page
        assert "127.0.0.1" in page
        assert "/v1/ping" in page
        assert "zorkhelper://wake" in page
        assert "loopback-network" in page
        assert "local-network-access" in page
        assert 'id="loopbackNotice"' in page
        assert "pcloopbackbody" in page
        assert 'return "blocked"' in page
        assert "pc-spinner" in page
        assert "helperLastTold" in page
        assert "startHelperFromCard" not in page
        assert "pcelapsed" not in page
        assert "pcnotray" not in page
        assert "pcstall" not in page
        assert "Har ventet" not in page
        assert "Start Zork Helper" not in page
        assert "ikke forlat siden" not in page
        check = page.split("async function startPcCheck")[1].split("async function helperInstallItems")[0]
        assert "allowDownload: true" in check
        assert "dispatchHelperJob" in check
        dispatch = page.split("async function dispatchHelperJob")[1].split("async function startPcCheck")[0]
        assert "helperIsInstalled" not in dispatch
        assert "helperAlreadyOnThisPc" in dispatch
        assert "waitForLoopbackDecision" in page
        assert dispatch.find("wakeHelperApp") < dispatch.rfind("downloadHelperLauncher")
        assert "isFridNettJob" in dispatch
        assert 'wakeHelperApp("fridnett")' in dispatch
        assert "waitForLocalHelper" in page
        assert "8000" in dispatch
        install = page.split("async function helperInstallItems")[1].split("async function pollHelper")[0]
        assert "downloadHelperLauncher" not in install
        assert "allowDownload" in install
        assert "skipPreflight" in install
        assert "ensureHelperReadyForRun" in install
        assert "dispatchHelperJob" in install
        assert "helperToldAgent = false" not in install
        assert '["sketchup"]' in install
        assert "pcsketchup" in page
        assert "pcsketchupnote" in page
        assert "requestSketchupInstall" in page
        assert "pcinstall" in page
        assert "winget" in page
        assert "Ikke gi winget" in page
        assert "pcreboot" in page
        assert "pcrebootshort" in page
        assert "pcinstalldone" in page
        assert "helperHeadline" in page
        assert "pc-log" not in page.split("function paintHelperCard")[1].split("function formatScanForAgent")[0]
        assert "needsReboot" in page
        assert "!row.reboot" in page
        dispatch_braces = dispatch.count("{") - dispatch.count("}")
        assert dispatch_braces == 0, "dispatchHelperJob is missing a closing brace — that kills the login page"

    def test_chat_page_javascript_parses(self, tmp_path):
        import re
        import shutil
        import subprocess
        html = Path("web/static/chat.html").read_text(encoding="utf-8")
        scripts = re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.I | re.S,
        )
        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        f = tmp_path / "chat.js"
        f.write_text("\n".join(scripts), encoding="utf-8")
        checked = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
        panel = Path("web/static/index.html").read_text(encoding="utf-8")
        panel_scripts = re.findall(
            r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", panel, flags=re.I | re.S,
        )
        pf = tmp_path / "panel.js"
        pf.write_text("\n".join(panel_scripts), encoding="utf-8")
        panel_checked = subprocess.run([node, "--check", str(pf)], capture_output=True, text=True)
        assert panel_checked.returncode == 0, panel_checked.stderr

    def test_tray_device_and_consented_install(self, tmp_path, monkeypatch):
        from core import pc_helper
        pc_helper.reset_store()
        mod = TestModeratorConsole()._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        assert mod.get("/api/helper/presence", headers=h).json()["online"] is False
        created = mod.post("/api/helper/sessions", headers=h, json={})
        sid = created.json()["id"]
        secret = pc_helper.get(sid)["secret"]
        tray = mod.get(f"/api/helper/{sid}/tray", headers={"X-Helper-Secret": secret})
        assert tray.status_code == 200
        assert "NotifyIcon" in tray.text
        assert "/v1/ping" in tray.text
        assert "18792" in tray.text
        assert "zorkhelper" in tray.text
        opener = mod.get(f"/api/helper/{sid}/open", headers={"X-Helper-Secret": secret})
        assert opener.status_code == 200
        assert "18792" in opener.text
        boot = mod.get(f"/api/helper/{sid}/bootstrap", headers={"X-Helper-Secret": secret})
        assert boot.status_code == 200
        assert "Write-Progress" in boot.text
        assert "Loading..." in boot.text
        inst = mod.get(f"/api/helper/{sid}/install-script", headers={"X-Helper-Secret": secret})
        assert inst.status_code == 200
        assert "Microsoft.Teams" in inst.text
        assert "Invoke-Expression" not in inst.text
        mac_launch = mod.get(f"/api/helper/sessions/{sid}/launcher?os=mac", headers=h)
        assert mac_launch.status_code == 200
        assert "ZorkHelper.command" in (mac_launch.headers.get("content-disposition") or "")
        assert "script-mac" in mac_launch.text
        assert "EncodedCommand" not in mac_launch.text
        assert "softwareupdate -i" not in mac_launch.text
        mac_scan = mod.get(
            f"/api/helper/{sid}/script-mac",
            headers={"X-Helper-Secret": secret},
        )
        assert mac_scan.status_code == 200
        assert "softwareupdate -l" in mac_scan.text
        assert "softwareupdate -i" not in mac_scan.text
        assert "Microsoft Word.app" in mac_scan.text
        assert "macupdate" in mac_scan.text
        assert "osascript" not in mac_scan.text
        reg = mod.post(
            f"/api/helper/{sid}/device",
            headers={"X-Helper-Secret": secret},
            json={"hostname": "KLASSE-PC"},
        )
        assert reg.status_code == 200
        did, dsecret = reg.json()["id"], reg.json()["secret"]
        assert mod.post(
            f"/api/helper/device/{did}/heartbeat",
            headers={"X-Helper-Secret": dsecret},
        ).status_code == 200
        assert mod.get("/api/helper/presence", headers=h).json()["online"] is True
        queued = mod.post(
            f"/api/helper/sessions/{sid}/enqueue",
            headers=h,
            json={"kind": "install", "items": ["teams", "curl http://evil", "winupdate"]},
        )
        assert queued.status_code == 200
        assert pc_helper.get(sid)["activity"] == "installing"
        job = mod.get(
            f"/api/helper/device/{did}/job",
            headers={"X-Helper-Secret": dsecret},
        )
        assert job.status_code == 200
        assert job.json()["job"]["kind"] == "install"
        assert job.json()["job"]["items"] == ["teams", "winupdate"]
        staff = TestModeratorConsole()._client(tmp_path, monkeypatch, claims={
            "sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
            "role": "user", "tier": "staff",
        })
        assert staff.post(
            f"/api/helper/sessions/{sid}/enqueue",
            headers=h,
            json={"kind": "scan"},
        ).status_code == 404
        pupil = TestModeratorConsole()._client(tmp_path, monkeypatch, claims={
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        assert pupil.get("/api/helper/presence", headers=h).status_code == 200

    def test_someone_else_cannot_read_the_session(self, tmp_path, monkeypatch):
        from core import pc_helper
        pc_helper.reset_store()
        mod = TestModeratorConsole()._client(tmp_path, monkeypatch)
        h = {"Authorization": "Bearer tok"}
        sid = mod.post("/api/helper/sessions", headers=h).json()["id"]
        admin = TestAdminApiIntegration()._make_client(tmp_path, monkeypatch)
        other = admin.get(
            f"/api/helper/sessions/{sid}",
            headers={"Authorization": "Bearer validtoken"},
        )
        assert other.status_code == 404


class TestKnowledgeCoverage:
    """Where people ask, against what has actually been written down.

    A synthetic workspace throughout, so filling in a real skill file cannot
    break these — the mechanism is under test, not today's contents.
    """

    def _skill(self, root, skill_id, description, body=""):
        d = root / "skills" / skill_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: {description}\n---\n\n{body}",
            encoding="utf-8",
        )
        return d

    # ---- coverage -------------------------------------------------------

    def test_a_template_stub_is_not_a_procedure(self, tmp_path):
        """`### <Short problem title>` ships in the skill template unfilled."""
        from core.knowledge import skill_coverage
        self._skill(tmp_path, "visma-enterprise", "HR", (
            "## Verified procedures\n\n"
            "### <Short problem title>\n**Symptom:** what the user reports\n\n"
            "### Registrere egenmelding\n1. Meny\n2. Ansatt\n"
        ))
        facts = skill_coverage(tmp_path)["visma-enterprise"]
        assert facts["procedures"] == 1, "the angle-bracket stub must not count"

    def test_a_subsection_outside_the_verified_section_is_not_a_procedure(self, tmp_path):
        from core.knowledge import skill_coverage
        self._skill(tmp_path, "m365-office", "Outlook", (
            "## Scoping questions\n\n### Which client?\n\n"
            "## Verified local knowledge\n\n_(Add confirmed findings.)_\n"
        ))
        assert skill_coverage(tmp_path)["m365-office"]["procedures"] == 0

    def test_verified_prose_with_org_anchors_counts_as_documented(self, tmp_path):
        """frid-nettverk's shape: no ### entries, but real hostnames and screenshots.

        Counting only verified subsections would call the best-documented file in
        the workspace empty.
        """
        from core.knowledge import skill_coverage
        self._skill(tmp_path, "frid-nettverk", "wifi", (
            "## Getting online\n\nGo to <https://onboard.frid-iks.no/> and download\n"
            "the certificate.\n\n![Skjermbilde](asset:onboarding-1.png)\n"
        ))
        facts = skill_coverage(tmp_path)["frid-nettverk"]
        assert facts["procedures"] == 0
        assert facts["anchors"] >= 2
        assert facts["documented"] is True

    def test_generic_advice_with_no_org_specifics_is_undocumented(self, tmp_path):
        from core.knowledge import skill_coverage
        self._skill(tmp_path, "windows-endpoint", "Windows devices", (
            "## Scoping questions\n\n- One device or many?\n\n"
            "## Verified local knowledge\n\n_(Add confirmed findings here.)_\n"
        ))
        assert skill_coverage(tmp_path)["windows-endpoint"]["documented"] is False

    def test_a_question_in_an_empty_skill_lacks_a_premade_answer(self, tmp_path):
        from core.knowledge import lacks_premade_answer
        self._skill(tmp_path, "m365-office", "Outlook Word Excel Teams")
        self._skill(tmp_path, "frid-nettverk", "wifi", (
            "## Getting online\n\nGo to <https://onboard.frid-iks.no/>.\n"
        ))
        assert lacks_premade_answer(
            "Word viser en feil når jeg lagrer", workspace=tmp_path
        ) == "m365-office"
        assert lacks_premade_answer("hei", workspace=tmp_path) is None
        assert lacks_premade_answer(
            "wifi virker ikke", workspace=tmp_path
        ) is None

    def test_praise_and_guide_edits_are_not_knowledge_gaps(self, tmp_path):
        from core.knowledge import lacks_premade_answer, looks_like_support_question
        self._skill(tmp_path, "windows-endpoint", "Windows devices printers projectors")
        self._skill(tmp_path, "m365-office", "Outlook Word Excel Teams OneNote")
        praise = (
            "Supert. nå må vi separere de neste stegene basert på om de "
            "bruker windows eller mac. jeg gir deg stegene for windows først, "
            "men du må huske å la det være plass til mac stegene etterpå også."
        )
        assert looks_like_support_question(praise) is False
        assert lacks_premade_answer(praise, workspace=tmp_path) is None
        assert lacks_premade_answer("Supert.", workspace=tmp_path) is None
        assert looks_like_support_question("Supert. Word krasjer når jeg lagrer") is True
        assert lacks_premade_answer(
            "Supert. Word krasjer når jeg lagrer", workspace=tmp_path
        ) == "m365-office"
        assert lacks_premade_answer(
            "Printeren skriver ikke ut", workspace=tmp_path
        ) == "windows-endpoint"
        assert lacks_premade_answer(
            "Projektoren i klasserommet er død", workspace=tmp_path
        ) == "windows-endpoint"
        assert lacks_premade_answer(
            "OneNote synkroniserer ikke", workspace=tmp_path
        ) == "m365-office"

    # ---- classification -------------------------------------------------

    def test_a_new_skill_is_classifiable_from_its_frontmatter_alone(self, tmp_path):
        """The point of deriving vocabulary: adding a skill needs no code change."""
        from core.knowledge import classify_topic
        self._skill(tmp_path, "smartboard", "Smartboard projectors in classrooms")
        assert classify_topic("projectors in the classroom are dead", workspace=tmp_path) == "smartboard"

    def test_a_hyphenated_compound_still_matches(self, tmp_path):
        """"SharePoint-området" is one token to a word regex, hiding the only clue."""
        from core.knowledge import classify_topic
        self._skill(tmp_path, "m365-office", "Outlook Teams SharePoint")
        self._skill(tmp_path, "entra-azure", "Sign-in and licences")
        assert classify_topic(
            "En bruker mangler tilgang til et delt SharePoint-område",
            workspace=tmp_path,
        ) == "m365-office"

    def test_norwegian_inflection_matches_the_stem(self, tmp_path):
        from core.knowledge import classify_topic
        self._skill(tmp_path, "frid-nettverk", "wifi and account activation")
        for phrasing in ("hvordan aktiverer jeg kontoen",
                         "jeg vil aktivere kontoen min",
                         "aktiveringen feilet"):
            assert classify_topic(phrasing, workspace=tmp_path) == "frid-nettverk", phrasing

    def test_a_stem_and_its_inflection_do_not_score_twice(self, tmp_path):
        """Holding "konto" and "kontoen" let one domain win on spelling alone."""
        from core.knowledge import vocabulary
        self._skill(tmp_path, "entra-azure", "konto kontoen kontoer lisens lisenser")
        terms = vocabulary(tmp_path)["entra-azure"]
        assert "konto" in terms
        assert "kontoen" not in terms and "kontoer" not in terms
        assert "lisenser" not in terms

    def test_a_tie_goes_to_the_pupils_own_guide(self, tmp_path):
        """MFA means the activation guide to a pupil, the reset procedure to staff."""
        from core.knowledge import classify_topic
        self._skill(tmp_path, "frid-nettverk", "Authenticator setup")
        self._skill(tmp_path, "entra-azure", "Authenticator resets")
        assert classify_topic("authenticator", tier="student", workspace=tmp_path) == "frid-nettverk"

    def test_nothing_recognisable_classifies_as_nothing(self, tmp_path):
        from core.knowledge import classify_topic
        self._skill(tmp_path, "m365-office", "Outlook and Teams")
        assert classify_topic("hei", workspace=tmp_path) is None
        assert classify_topic("", workspace=tmp_path) is None

    def test_no_skills_directory_is_survivable(self, tmp_path):
        from core.knowledge import classify_topic, skill_coverage
        assert skill_coverage(tmp_path) == {}
        assert classify_topic("wifi", workspace=tmp_path) is None

    def test_the_matching_skill_is_prepended_to_the_message(self, tmp_path):
        from core.knowledge import with_matching_skill
        self._skill(tmp_path, "frid-nettverk", "wifi", "Certificate first.\n")
        out = with_matching_skill("wifi virker ikke", workspace=tmp_path)
        assert out.startswith("[Skill file already included: skills/frid-nettverk/SKILL.md.")
        assert "Certificate first." in out
        assert out.endswith("wifi virker ikke")

    def test_a_greeting_is_not_wrapped_in_a_skill(self, tmp_path):
        from core.knowledge import with_matching_skill
        self._skill(tmp_path, "frid-nettverk", "wifi", "Certificate first.\n")
        assert with_matching_skill("hei", workspace=tmp_path) == "hei"

    def test_a_short_follow_up_is_not_wrapped_in_a_different_skill(self, tmp_path):
        """"en elev" is Visma InSchool. Mid-MFA-reset that would ask for bruker@afk.no."""
        from core.knowledge import FOLLOW_UP_MIN_TOKENS, classify_topic, with_matching_skill
        self._skill(tmp_path, "visma-inschool", "school pupil timetable",
                    "Logg inn med bruker@afk.no\n")
        self._skill(tmp_path, "entra-azure", "MFA sign-in licence",
                    "Search Azure by name.\n")
        assert classify_topic("en elev", workspace=tmp_path) == "visma-inschool"
        assert "bruker@afk.no" in with_matching_skill("en elev", workspace=tmp_path)
        assert with_matching_skill(
            "en elev", workspace=tmp_path, min_tokens=FOLLOW_UP_MIN_TOKENS,
        ) == "en elev"

    def test_staff_mfa_reset_classifies_as_entra(self, tmp_path):
        from core.knowledge import classify_topic
        self._skill(tmp_path, "frid-nettverk", "wifi Authenticator setup")
        self._skill(tmp_path, "entra-azure", "Entra ID MFA sign-in")
        self._skill(tmp_path, "visma-inschool", "school pupil timetable")
        assert classify_topic("Noen trenger MFA-reset", workspace=tmp_path) == "entra-azure"

    def test_a_bad_skill_id_does_not_leave_the_skills_folder(self, tmp_path):
        from core.knowledge import skill_body
        assert skill_body("../etc", workspace=tmp_path) is None
        assert skill_body("frid-nettverk/../../x", workspace=tmp_path) is None

    # ---- the report -----------------------------------------------------

    def test_undocumented_demand_sorts_to_the_top(self, tmp_path):
        from core.knowledge import coverage_report
        self._skill(tmp_path, "entra-azure", "sign-in",
                    "## Verified procedures\n\n### Resette MFA\n1. Open\n")
        self._skill(tmp_path, "windows-endpoint", "devices", "## Scoping\n\n- One?\n")
        self._skill(tmp_path, "m365-office", "Outlook", "## Scoping\n\n- Which client?\n")
        rows = coverage_report({"entra-azure": 50, "windows-endpoint": 3}, tmp_path)

        assert rows[0]["skill"] == "windows-endpoint", (
            "3 questions with nothing written down outrank 50 that are documented"
        )
        assert rows[0]["undocumented_demand"] == 3
        # Documented, so busy is fine — it is not work waiting to be done.
        assert next(r for r in rows if r["skill"] == "entra-azure")["undocumented_demand"] == 0
        # Nobody asked, but an empty file should be visible before it is urgent.
        assert any(r["skill"] == "m365-office" and r["asked"] == 0 for r in rows)
        assert all(r["file"] == f"skills/{r['skill']}/SKILL.md" for r in rows)

    def test_aliases_are_spelled_the_way_people_type_them(self):
        """ASCII-folded aliases can never match, and fail silently.

        "loenn", "fravaer" and "traadloes" were all here once, matching nothing.
        """
        from core.knowledge import ALIASES
        for skill_id, expected in (
            ("visma-enterprise", "lønn"),
            ("visma-inschool", "fravær"),
            ("frid-nettverk", "trådløs"),
            ("entra-azure", "låst"),
        ):
            assert expected in ALIASES[skill_id], (
                f"{expected!r} missing from {skill_id} — check it was not "
                f"ASCII-folded, which matches nothing a Norwegian user types"
            )


class TestPupilCliInvocation:
    """A toolless tier needs the streaming input path, or images cannot arrive."""

    def _cmd(self, tier, images=None):
        import asyncio
        from core import cli_backend

        captured = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class Proc:
                returncode = 0

                async def communicate(self, input=None):
                    captured["stdin"] = input
                    payload = {
                        "type": "result",
                        "result": "ok",
                        "session_id": "sid",
                        "modelUsage": {},
                    }
                    return (json.dumps(payload).encode(), b"")

            return Proc()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                cli_backend._invoke(
                    "m", "sys", "hei", ["--session-id", "x"], 30, tier, images
                )
            )
        return captured

    def test_pupil_call_offers_no_tools_and_streams(self):
        captured = self._cmd("student")
        cmd = captured["cmd"]
        assert "--allowedTools" not in cmd, "a pupil gets --tools '' instead"
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "stream-json" in cmd

    def test_staff_call_keeps_the_plain_json_path(self):
        cmd = self._cmd("staff")["cmd"]
        assert "--allowedTools" in cmd
        assert "--input-format" not in cmd

    def test_staff_chat_invoke_does_not_offer_write(self):
        import asyncio
        from core import cli_backend
        captured = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class Proc:
                returncode = 0

                async def communicate(self, input=None):
                    payload = {
                        "type": "result",
                        "result": "ok",
                        "session_id": "sid",
                        "modelUsage": {},
                    }
                    return (json.dumps(payload).encode(), b"")

            return Proc()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                cli_backend._invoke(
                    "m", "sys", "hei", ["--session-id", "x"], 30, "staff",
                    channel="web",
                )
            )
        tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
        assert "Write" not in tools.split()
        assert "Edit" not in tools.split()
        assert "WebSearch" not in tools.split()
        offered = captured["cmd"][captured["cmd"].index("--tools") + 1]
        assert "Bash" not in offered.split(",")
        assert "WebSearch" not in offered.split(",")
        assert captured["cmd"][captured["cmd"].index("--effort") + 1] == "low"

    def test_pupil_image_is_base64_in_the_message(self, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
        captured = self._cmd("student", [png])
        payload = json.loads(captured["stdin"].decode())
        blocks = payload["message"]["content"]
        assert blocks[0]["type"] == "image"
        assert blocks[0]["source"]["media_type"] == "image/png"
        assert blocks[-1]["type"] == "text"

    def test_an_unreadable_image_does_not_lose_the_question(self, tmp_path):
        """A pupil who pastes something odd should still get an answer to their words."""
        captured = self._cmd("student", [tmp_path / "gone.png"])
        payload = json.loads(captured["stdin"].decode())
        blocks = payload["message"]["content"]
        assert [b["type"] for b in blocks] == ["text"]

    def test_empty_system_prompt_is_not_written_to_a_file(self):
        """A resumed turn must not replace the cached bootstrap with a blank file."""
        import asyncio
        from core import cli_backend

        captured = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class Proc:
                returncode = 0

                async def communicate(self, input=None):
                    payload = {
                        "type": "result",
                        "result": "ok",
                        "session_id": "sid",
                        "modelUsage": {},
                    }
                    return (json.dumps(payload).encode(), b"")

            return Proc()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                cli_backend._invoke(
                    "m", "", "hei", ["--resume", "x"], 30, "staff", None
                )
            )
        assert "--system-prompt-file" not in captured["cmd"]

    def test_live_staff_call_uses_stream_json_output_not_input(self):
        import asyncio
        from core import cli_backend

        captured = {}

        async def on_text(delta):
            pass

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)

            class Stdin:
                def write(self, data):
                    captured["stdin"] = data
                async def drain(self):
                    pass
                def close(self):
                    pass

            class Stream:
                def __init__(self, data):
                    self._data = data
                    self._sent = False
                async def read(self, n=-1):
                    if self._sent:
                        return b""
                    self._sent = True
                    return self._data

            payload = (
                json.dumps({
                    "type": "result",
                    "result": "ok",
                    "session_id": "sid",
                    "modelUsage": {},
                }) + "\n"
            ).encode()

            class Proc:
                returncode = 0
                stdin = Stdin()
                stdout = Stream(payload)
                stderr = Stream(b"")
                async def wait(self):
                    return 0
                def kill(self):
                    pass

            return Proc()

        with patch("asyncio.create_subprocess_exec", fake_exec):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                cli_backend._invoke(
                    "m", "sys", "hei", ["--session-id", "x"], 30, "staff",
                    None, on_text=on_text,
                )
            )
        cmd = captured["cmd"]
        assert "--allowedTools" in cmd
        assert "--input-format" not in cmd
        assert "--include-partial-messages" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--effort" not in cmd


class TestCliTurnTelemetry:
    def test_usage_keeps_turn_timing_from_the_result_object(self):
        from core.cli_backend import _usage_from
        usage = _usage_from({
            "num_turns": 3,
            "duration_ms": 4120,
            "duration_api_ms": 3800,
            "modelUsage": {"claude-sonnet-4-6": {
                "inputTokens": 10, "outputTokens": 4, "cacheReadInputTokens": 2,
            }},
        }, "claude-sonnet-4-6")
        assert usage["num_turns"] == 3
        assert usage["duration_ms"] == 4120
        assert usage["duration_api_ms"] == 3800
        assert usage["input_tokens"] == 10


class TestChatTokenStreaming:
    """ /chat must show tokens as they arrive, not after claude.exe exits. """

    def test_text_delta_is_extracted(self):
        from core.cli_backend import _event_text_delta, _event_starts_tool_use
        obj = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hei"},
            },
        }
        assert _event_text_delta(obj) == "Hei"
        assert not _event_starts_tool_use(obj)

    def test_tool_use_start_is_detected(self):
        from core.cli_backend import _event_starts_tool_use, _event_tool_name
        obj = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "name": "Read"},
            },
        }
        assert _event_starts_tool_use(obj)
        assert _event_tool_name(obj) == "Read"

    @pytest.mark.anyio
    async def test_web_yields_tokens_without_duplicating_the_result(self):
        from core.agent_loop import AgentLoop

        sid = str(uuid.uuid4())
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": sid,
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "student",
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()

        async def fake_call(**kwargs):
            on_text = kwargs.get("on_text")
            if on_text:
                await on_text("He")
                await on_text("i")
            return "Hei", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"):
            chunks = []
            async for chunk in AgentLoop(sm).run("web", "dev:x|1", "hei"):
                chunks.append(chunk)

        assert chunks.count("He") == 1
        assert chunks.count("i") == 1
        assert "Hei" not in chunks
        assistant = [
            c.args for c in sm.append_message.await_args_list if c.args[1] == "assistant"
        ]
        assert assistant == [(sid, "assistant", "Hei")]

    @pytest.mark.anyio
    async def test_web_yields_tool_status_without_saving_it(self):
        from core.agent_loop import AgentLoop

        sid = str(uuid.uuid4())
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": sid,
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "staff",
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()

        async def fake_call(**kwargs):
            on_tool = kwargs.get("on_tool")
            if on_tool:
                await on_tool("Read")
            return "svar", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"), \
             patch("core.agent_loop.with_matching_skill", lambda msg, tier="", **kwargs: msg), \
             patch("core.agent_loop.classify_topic", return_value=None):
            chunks = []
            async for chunk in AgentLoop(sm).run("web", "dev:x|1", "hei"):
                chunks.append(chunk)

        assert {"type": "status", "kind": "read"} in chunks
        assert {"type": "status", "kind": "think"} in chunks
        assert {"type": "status", "kind": "rules"} in chunks
        assistant = [
            c.args for c in sm.append_message.await_args_list if c.args[1] == "assistant"
        ]
        assert assistant == [(sid, "assistant", "svar")]


    @pytest.mark.anyio
    async def test_web_wait_phases_follow_skill_then_reply(self):
        """The /chat bubble must name the step the agent is on, not a timer."""
        from core.agent_loop import AgentLoop

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": str(uuid.uuid4()),
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "student",
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()

        async def fake_call(**kwargs):
            on_text = kwargs.get("on_text")
            if on_text:
                await on_text("ok")
            return "ok", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"), \
             patch("core.agent_loop.classify_topic", return_value="frid-nettverk"):
            chunks = []
            async for chunk in AgentLoop(sm).run("web", "dev:x|1", "wifi"):
                chunks.append(chunk)

        kinds = [c["kind"] for c in chunks if isinstance(c, dict) and c.get("type") == "status"]
        assert kinds[:3] == ["read", "think", "rules"]
        assert "respond" in kinds
        assert kinds.index("rules") < kinds.index("respond")

    @pytest.mark.anyio
    async def test_web_skips_rules_when_no_skill_matches(self):
        from core.agent_loop import AgentLoop

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": str(uuid.uuid4()),
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "student",
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()

        async def fake_call(**kwargs):
            return "svar", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"), \
             patch("core.agent_loop.classify_topic", return_value=None):
            chunks = []
            async for chunk in AgentLoop(sm).run("web", "dev:x|1", "hei"):
                chunks.append(chunk)

        kinds = [c["kind"] for c in chunks if isinstance(c, dict) and c.get("type") == "status"]
        assert kinds[:2] == ["read", "think"]
        assert "rules" not in kinds

    @pytest.mark.anyio
    async def test_staff_sees_the_skill_the_pupil_does_not(self):
        from core.agent_loop import AgentLoop

        seen = {}

        async def _run(tier):
            sm = MagicMock()
            sm.get_or_create = AsyncMock(return_value={
                "session_id": str(uuid.uuid4()),
                "channel": "web",
                "peer": "dev:x|1",
                "model": "claude-sonnet-4-6",
                "claude_session_id": None,
                "tier": tier,
            })
            sm.pop_pending_context = AsyncMock(return_value=None)
            sm.append_message = AsyncMock()
            sm.update_activity = AsyncMock()
            sm.set_claude_session_id = AsyncMock()

            async def fake_call(**kwargs):
                seen[tier] = kwargs["user_message"]
                return "svar", "sid", {}

            with patch("core.agent_loop.call_claude", fake_call), \
                 patch("core.agent_loop.build_system_prompt",
                       lambda is_new_session, tier, **kwargs: f"SYS:{tier}"), \
                 patch("core.agent_loop.with_matching_skill",
                       lambda msg, tier="", **kwargs: f"SKILL\n{msg}"):
                async for _ in AgentLoop(sm).run("web", "dev:x|1", "wifi"):
                    pass

        await _run("staff")
        await _run("student")
        assert "SKILL\n" in seen["staff"]
        assert seen["student"].startswith("[Denne meldingen er fra en ELEV.")
        assert "SKILL\n" not in seen["student"]

    @pytest.mark.anyio
    async def test_staff_does_not_rewrap_the_same_skill_on_a_follow_up(self):
        from core.agent_loop import AgentLoop

        sid = str(uuid.uuid4())
        session = {
            "session_id": sid,
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "staff",
        }
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value=session)
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()
        seen = []

        async def fake_call(**kwargs):
            seen.append(kwargs["user_message"])
            return "svar", "claude-sid", {}

        loop = AgentLoop(sm)
        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"), \
             patch("core.agent_loop.classify_topic",
                   return_value="entra-azure"), \
             patch("core.agent_loop.with_matching_skill",
                   lambda msg, tier="", **kwargs: f"SKILL\n{msg}"):
            async for _ in loop.run("web", "dev:x|1", "Noen trenger MFA-reset"):
                pass
            session["claude_session_id"] = "claude-sid"
            async for _ in loop.run(
                "web", "dev:x|1", "hvordan gjør jeg steg to for MFA-reset"
            ):
                pass

        assert seen[0].startswith("SKILL\n") or "SKILL\n" in seen[0]
        assert "SKILL\n" not in seen[1]
        assert "steg to" in seen[1]


class TestHelixYearlyClaudeToken:
    """Helix may use a setup-token from the secret store. It must not inherit
    a user-wide CLAUDE_CODE_OAUTH_TOKEN, and an API key must not outrank it."""

    def _env(self, monkeypatch, secret, extra=None):
        from core import cli_backend
        monkeypatch.setattr(cli_backend, "sync_credentials", lambda: True)
        for key, value in (extra or {}).items():
            monkeypatch.setenv(key, value)
        with patch("security.secrets.get_secret", return_value=secret):
            return cli_backend._build_env()

    def test_secret_is_what_the_subprocess_sees(self, monkeypatch):
        env = self._env(
            monkeypatch,
            "sk-ant-oat01-helix",
            {"CLAUDE_CODE_OAUTH_TOKEN": "stale-user", "ANTHROPIC_API_KEY": "sk-user"},
        )
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-helix"
        assert "ANTHROPIC_API_KEY" not in env

    def test_without_a_secret_a_user_wide_token_does_not_pin_helix(self, monkeypatch):
        env = self._env(
            monkeypatch, None, {"CLAUDE_CODE_OAUTH_TOKEN": "stale-user"}
        )
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_auto_memory_is_forced_off(self, monkeypatch):
        """Claude Code auto-memory is not gated by --allowedTools. A pupil can trip it."""
        env = self._env(
            monkeypatch, None, {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}
        )
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


class TestReadableSubjects:
    """Insights brackets are phrases, reused when the problem is the same."""

    def test_label_keeps_the_question(self):
        from core.subjects import labelize
        assert labelize("hei") == ""
        label = labelize("hei, authenticator gir meg ikke bekreftelseskode")
        assert "authenticator" in label.lower()
        assert "bekreftelseskode" in label.lower()
        assert not label.lower().startswith("hei")

    def test_a_new_phrasing_joins_the_existing_bracket(self, tmp_path):
        from core.subjects import remember_subject
        path = tmp_path / "subjects.json"
        first = remember_subject(
            "Jeg får ikke bekreftelseskode i Authenticator", path=path
        )
        second = remember_subject(
            "authenticator gir ikke kode", path=path
        )
        assert first == second
        assert "authenticator" in first.lower()

    def test_unrelated_problems_stay_apart(self, tmp_path):
        from core.subjects import remember_subject
        path = tmp_path / "subjects.json"
        wifi = remember_subject("hvordan logger jeg på FRID-nettverket", path=path)
        payroll = remember_subject("hvor registrerer jeg egenmelding", path=path)
        assert wifi != payroll

    def test_missing_label_names_the_gap(self, tmp_path):
        from core.subjects import missing_label
        path = tmp_path / "subjects.json"
        label = missing_label("Word viser en feil når jeg lagrer", "m365-office", path=path)
        assert label.lower().startswith("missing ")
        assert "word" in label.lower()
        again = missing_label("feil i Word når jeg lagrer", "m365-office", path=path)
        assert again == label

    def test_praise_is_stripped_from_the_heading(self, tmp_path):
        from core.subjects import labelize
        assert labelize("Supert.") == ""
        heading = labelize("Supert. Word krasjer når jeg lagrer")
        assert heading.lower().startswith("word")
        assert "supert" not in heading.lower()


class TestOperatorAccountStaysOffTheChat:
    """Claude Code injects the operator's oauth email into every session.
    Helix must strip it from its own config dir and never touch ~/.claude."""

    def test_helix_profile_is_stripped_personal_is_not(self, tmp_path, monkeypatch):
        from core import cli_backend
        helix = tmp_path / "helix-claude"
        personal = tmp_path / "user-claude"
        helix.mkdir()
        personal.mkdir()
        payload = {
            "oauthAccount": {
                "emailAddress": "operator@example.com",
                "displayName": "Operator",
                "organizationName": "operator@example.com's Organization",
                "accountUuid": "keep-me",
            }
        }
        (helix / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
        (personal / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(cli_backend, "HELIX_CLAUDE_CONFIG", helix)
        monkeypatch.setattr(cli_backend, "USER_CLAUDE_CONFIG", personal)
        assert cli_backend.sanitize_helix_oauth_profile() is True
        helix_acct = json.loads((helix / ".claude.json").read_text(encoding="utf-8"))["oauthAccount"]
        personal_acct = json.loads((personal / ".claude.json").read_text(encoding="utf-8"))["oauthAccount"]
        assert helix_acct["emailAddress"] == ""
        assert helix_acct["displayName"] == ""
        assert helix_acct["accountUuid"] == "keep-me"
        assert personal_acct["emailAddress"] == "operator@example.com"

    def test_already_clean_is_a_no_op(self, tmp_path, monkeypatch):
        from core import cli_backend
        helix = tmp_path / "helix-claude"
        helix.mkdir()
        (helix / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": ""}}), encoding="utf-8"
        )
        monkeypatch.setattr(cli_backend, "HELIX_CLAUDE_CONFIG", helix)
        assert cli_backend.sanitize_helix_oauth_profile() is False


class TestClaudeConcurrencyGate:
    """A holiday spike must wait in line, not launch 100 claude.exe processes."""

    def test_in_flight_turns_stay_at_the_cap(self, monkeypatch):
        import asyncio
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 2)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0)
        cli_backend.reset_claude_gate()
        current = 0
        peak = 0

        async def fake_invoke(*args, **kwargs):
            nonlocal current, peak
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.05)
            current -= 1
            return ("ok", "sid", {})

        monkeypatch.setattr(cli_backend, "_invoke", fake_invoke)

        async def burst():
            await asyncio.gather(*[
                cli_backend.call_claude(
                    model="m", system="s", user_message="u",
                    is_new_session=True, tier="student",
                )
                for _ in range(6)
            ])

        asyncio.run(burst())
        assert peak == 2
        cli_backend.reset_claude_gate()

    def test_waiters_are_told_their_place(self, monkeypatch):
        import asyncio
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 1)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0)
        cli_backend.reset_claude_gate()
        hold = asyncio.Event()
        first_started = asyncio.Event()
        got_place = asyncio.Event()
        seen = []

        async def fake_invoke(*args, **kwargs):
            first_started.set()
            await hold.wait()
            return ("ok", "sid", {})

        monkeypatch.setattr(cli_backend, "_invoke", fake_invoke)

        async def on_wait(position, waiting):
            seen.append(position)
            if position == 1:
                got_place.set()

        async def run():
            first = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
            ))
            await first_started.wait()
            second = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
                on_wait=on_wait,
            ))
            await asyncio.wait_for(got_place.wait(), timeout=1)
            assert seen[0] == 1
            hold.set()
            await asyncio.gather(first, second)
            assert seen[-1] == 0

        asyncio.run(run())
        cli_backend.reset_claude_gate()

    def test_cancelled_waiter_leaves_the_line(self, monkeypatch):
        import asyncio
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 1)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0)
        cli_backend.reset_claude_gate()
        hold = asyncio.Event()
        first_started = asyncio.Event()
        got_place = asyncio.Event()

        async def fake_invoke(*args, **kwargs):
            first_started.set()
            await hold.wait()
            return ("ok", "sid", {})

        monkeypatch.setattr(cli_backend, "_invoke", fake_invoke)

        async def on_wait(position, waiting):
            if position == 1:
                got_place.set()

        async def run():
            first = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
            ))
            await first_started.wait()
            waiter = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
                on_wait=on_wait,
            ))
            await asyncio.wait_for(got_place.wait(), timeout=1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            q = cli_backend._claude_queue()
            assert q.waiters == []
            assert q.in_flight == 1
            hold.set()
            await first
            assert q.in_flight == 0

        asyncio.run(run())
        cli_backend.reset_claude_gate()

    def test_failed_cli_does_not_retry_during_a_blast(self, monkeypatch):
        import asyncio
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 2)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0)
        cli_backend.reset_claude_gate()
        calls = {"n": 0}

        async def fake_once(*args, **kwargs):
            calls["n"] += 1
            cli_backend._claude_queue().waiters.append(cli_backend._WaitTicket())
            raise RuntimeError("cli exit 1")

        monkeypatch.setattr(cli_backend, "_call_claude_once", fake_once)

        async def boom():
            await cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
            )

        with pytest.raises(RuntimeError, match="cli exit 1"):
            asyncio.run(boom())
        assert calls["n"] == 1
        cli_backend.reset_claude_gate()

    def test_grants_are_paced(self, monkeypatch):
        import asyncio
        import time
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 3)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0.05)
        cli_backend.reset_claude_gate()
        starts = []

        async def fake_invoke(*args, **kwargs):
            starts.append(time.monotonic())
            return ("ok", "sid", {})

        monkeypatch.setattr(cli_backend, "_invoke", fake_invoke)

        async def burst():
            await asyncio.gather(*[
                cli_backend.call_claude(
                    model="m", system="s", user_message="u",
                    is_new_session=True, tier="student",
                )
                for _ in range(3)
            ])

        asyncio.run(burst())
        starts.sort()
        gaps = [starts[i] - starts[i - 1] for i in range(1, len(starts))]
        assert all(g >= 0.04 for g in gaps), gaps
        cli_backend.reset_claude_gate()

    def test_repeated_failures_open_a_cooldown(self):
        from core.cli_backend import ClaudeQueue

        q = ClaudeQueue(20)
        for _ in range(4):
            q.note_failure()
        assert q._cool_until == 0.0
        q.note_failure()
        assert q._cool_until > time.monotonic()

    def test_quota_rejection_is_detected(self):
        from core.cli_backend import _quota_from_cli_output

        blob = (
            '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
            '"resetsAt":1787241600,"rateLimitType":"five_hour",'
            '"overageDisabledReason":"out_of_credits"}}'
        )
        assert _quota_from_cli_output(blob) == 1787241600
        staff = (
            '{"is_error":true,"duration_api_ms":0,"oauthAccount":{"email":"x"},'
            '"usage":{"output_tokens":0},"result":""}'
        )
        assert _quota_from_cli_output(staff) == 0
        assert _quota_from_cli_output("claude CLI exit 1: boom") is None
        from core.cli_backend import _login_expired
        assert _login_expired(staff) is False
        assert _login_expired("please run /login") is True

    def test_staff_empty_error_is_quota_not_login(self, monkeypatch):
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 20)
        cli_backend.reset_claude_gate()
        payload = (
            '{"is_error":true,"usage":{"output_tokens":0},'
            '"oauthAccount":{"emailAddress":"x"},"result":""}'
        )
        with pytest.raises(cli_backend.QuotaExhaustedError):
            cli_backend._raise_cli_failure("", payload)
        cli_backend.reset_claude_gate()

    def test_quota_message_names_the_reset_time(self, monkeypatch):
        from datetime import datetime
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo
        from core.cli_backend import quota_user_message

        tz = ZoneInfo("Europe/Oslo")
        monkeypatch.setattr(
            "core.config.get_config",
            lambda: SimpleNamespace(timezone="Europe/Oslo"),
        )
        when = datetime(2026, 8, 20, 20, 0, tzinfo=tz)
        msg = quota_user_message(int(when.timestamp()))
        assert msg.startswith("Jeg er ikke i stand til å svare på flere spørsmål før")
        assert "kl. 20:00" in msg
        assert msg.endswith("hvis du trenger svar før det kan du snakke med IT-avdelingen")

    def test_cli_session_limit_phrase_becomes_the_clock(self):
        from core.cli_backend import _quota_from_cli_output, quota_user_message

        blob = (
            '{"is_error":true,"api_error_status":429,'
            '"usage":{"output_tokens":0},'
            '"result":"You\'ve hit your session limit · resets 6pm (Europe/Oslo)"}'
        )
        ts = _quota_from_cli_output(blob)
        assert ts and ts > 0
        msg = quota_user_message(ts)
        assert msg.startswith("Jeg er ikke i stand til å svare på flere spørsmål før")
        assert "kl. 18:00" in msg
        assert "kvoten fylles opp igjen" not in msg
        assert msg.endswith("hvis du trenger svar før det kan du snakke med IT-avdelingen")

    def test_quota_fails_the_line_without_launching_more(self, monkeypatch):
        import asyncio
        from core import cli_backend

        monkeypatch.setattr(cli_backend, "_claude_limit", lambda: 1)
        monkeypatch.setattr(cli_backend, "_claude_gap", lambda: 0)
        cli_backend.reset_claude_gate()
        invokes = {"n": 0}

        async def fake_invoke(*args, **kwargs):
            invokes["n"] += 1
            cli_backend._claude_queue().note_quota(int(time.time()) + 3600)
            raise cli_backend.QuotaExhaustedError(int(time.time()) + 3600)

        monkeypatch.setattr(cli_backend, "_invoke", fake_invoke)

        async def run():
            first = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
            ))
            waiter = asyncio.create_task(cli_backend.call_claude(
                model="m", system="s", user_message="u",
                is_new_session=True, tier="student",
            ))
            with pytest.raises(cli_backend.QuotaExhaustedError):
                await first
            with pytest.raises(cli_backend.QuotaExhaustedError):
                await waiter

        asyncio.run(run())
        assert invokes["n"] == 1
        cli_backend.reset_claude_gate()


class TestQueueStatusEvents:
    """Place-in-line is a live status event, not a chat message."""

    @pytest.mark.anyio
    async def test_web_yields_queue_dict_not_transcript(self):
        from core.agent_loop import AgentLoop

        sid = str(uuid.uuid4())
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": sid,
            "channel": "web",
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": "student",
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()

        async def fake_call(**kwargs):
            on_wait = kwargs.get("on_wait")
            if on_wait:
                await on_wait(3, 12)
                await on_wait(1, 8)
                await on_wait(0, 0)
            return "svar", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"):
            chunks = []
            async for chunk in AgentLoop(sm).run("web", "dev:x|1", "hei"):
                chunks.append(chunk)

        assert {"type": "queue", "position": 3, "waiting": 12} in chunks
        assert {"type": "queue", "position": 1, "waiting": 8} in chunks
        assert {"type": "queue", "position": 0, "waiting": 0} in chunks
        assert "svar" in chunks
        assistant = [
            c.args for c in sm.append_message.await_args_list if c.args[1] == "assistant"
        ]
        assert assistant == [(sid, "assistant", "svar")]


class TestAgentLoopTierWiring:
    """The tier stored on the session must reach both the prompt and the CLI."""

    async def _run(self, tier, channel="web", message="hei", client_os=""):
        from core.agent_loop import AgentLoop

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": str(uuid.uuid4()),
            "channel": channel,
            "peer": "dev:x|1",
            "model": "claude-sonnet-4-6",
            "claude_session_id": None,
            "tier": tier,
        })
        sm.pop_pending_context = AsyncMock(return_value=None)
        sm.append_message = AsyncMock()
        sm.update_activity = AsyncMock()
        sm.set_claude_session_id = AsyncMock()
        self._sm = sm

        seen = {}

        async def fake_call(**kwargs):
            seen.update(kwargs)
            return "svar", "sid", {}

        with patch("core.agent_loop.call_claude", fake_call), \
             patch("core.agent_loop.build_system_prompt",
                   lambda is_new_session, tier, **kwargs: f"SYS:{tier}"):
            async for _chunk in AgentLoop(sm).run(
                channel, "dev:x|1", message, client_os=client_os
            ):
                pass
        return seen

    @pytest.mark.anyio
    async def test_student_session_gets_student_prompt_and_tools(self):
        seen = await self._run("student")
        assert seen["tier"] == "student"
        assert seen["system"] == "SYS:student"

    @pytest.mark.anyio
    async def test_staff_session_gets_staff_prompt_and_tools(self):
        seen = await self._run("staff")
        assert seen["tier"] == "staff"
        assert seen["system"] == "SYS:staff"

    @pytest.mark.anyio
    async def test_untiered_web_session_falls_to_student(self):
        """A row predating the tier column must not be handed the staff prompt."""
        seen = await self._run(None)
        assert seen["tier"] == "student"

    @pytest.mark.anyio
    async def test_internal_channels_keep_full_context(self):
        """Heartbeats and cron jobs have no user behind them."""
        for channel in ("cron", "discord", "queue"):
            seen = await self._run(None, channel=channel)
            assert seen["tier"] == "staff", channel

    @pytest.mark.anyio
    async def test_student_turn_carries_the_scope_marker(self):
        """The rule has to travel with the message; the prompt alone did not hold."""
        from core.context_engine import STUDENT_TURN_MARKER
        seen = await self._run("student")
        assert STUDENT_TURN_MARKER in seen["user_message"]
        assert seen["user_message"].endswith("hei")

    @pytest.mark.anyio
    async def test_student_turn_includes_browser_os(self):
        seen = await self._run("student", client_os="Windows")
        assert "Windows PC" in seen["user_message"]
        assert seen["user_message"].endswith("hei")

    @pytest.mark.anyio
    async def test_junk_os_hint_is_dropped(self):
        seen = await self._run("student", client_os="<script>mac</script>")
        assert "script" not in seen["user_message"]
        assert "Windows PC" not in seen["user_message"]

    @pytest.mark.anyio
    async def test_pupil_screenshot_is_sent_inline_not_as_a_path(self):
        """A pupil has no Read tool, so a path marker would be a dead reference."""
        message = (
            "[Attached image: C:\\shots\\a.png]\n"
            "[Attached image: C:\\shots\\b.png]\n"
            "her er feilmeldingen"
        )
        seen = await self._run("student", message=message)
        assert [p.name for p in seen["images"]] == ["a.png", "b.png"]
        assert "Attached image" not in seen["user_message"]
        assert "her er feilmeldingen" in seen["user_message"]

    @pytest.mark.anyio
    async def test_staff_screenshot_stays_a_path(self):
        """Staff open the file themselves, which is how a screenshot becomes an asset."""
        message = "[Attached image: C:\\shots\\a.png]\nse her"
        seen = await self._run("staff", message=message)
        assert seen["images"] == []
        assert "Attached image" in seen["user_message"]

    @pytest.mark.anyio
    async def test_pupil_transcript_keeps_the_original_message(self):
        """The chat bubble shows what the pupil wrote, markers and marker text included."""
        message = "[Attached image: C:\\shots\\a.png]\nhjelp"
        await self._run("student", message=message)
        stored = [
            call.args for call in self._sm.append_message.await_args_list
            if call.args[1] == "user"
        ]
        assert stored and stored[0][2] == message

    @pytest.mark.anyio
    async def test_staff_turn_carries_the_role_not_the_pupil_marker(self):
        from core.context_engine import STUDENT_TURN_MARKER, turn_marker
        seen = await self._run("staff")
        assert STUDENT_TURN_MARKER not in seen["user_message"]
        assert turn_marker("staff", "web") in seen["user_message"]
        assert seen["user_message"].endswith("hei")

    @pytest.mark.anyio
    async def test_admin_and_moderator_turns_name_their_access(self):
        from core.context_engine import turn_marker
        admin = await self._run("admin")
        mod = await self._run("moderator")
        assert turn_marker("admin", "web") in admin["user_message"]
        assert turn_marker("moderator", "web") in mod["user_message"]
        assert "ROLE: admin" in admin["user_message"]
        assert "ROLE: moderator" in mod["user_message"]

    @pytest.mark.anyio
    async def test_internal_work_is_not_told_it_is_talking_to_staff(self):
        """A heartbeat is not a person. A ROLE line there would be a lie."""
        from core.context_engine import turn_marker
        seen = await self._run("staff", channel="heartbeat")
        assert turn_marker("staff", "web") not in seen["user_message"]
        assert seen["user_message"] == "hei"

    @pytest.mark.anyio
    async def test_marker_is_not_stored_in_the_transcript(self):
        """Otherwise our plumbing appears inside the pupil's own chat bubble."""
        from core.context_engine import STUDENT_TURN_MARKER
        await self._run("student")
        stored = [c.args[2] for c in self._sm.append_message.await_args_list
                  if c.args[1] == "user"]
        assert stored == ["hei"]
        assert all(STUDENT_TURN_MARKER not in s for s in stored)

        from core.context_engine import turn_marker
        await self._run("admin")
        stored = [c.args[2] for c in self._sm.append_message.await_args_list
                  if c.args[1] == "user"]
        assert stored == ["hei"]
        assert all(turn_marker("admin", "web") not in s for s in stored)


# ---------------------------------------------------------------------------
# Fix 1 — Version constant
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_constant_exists(self):
        import main
        assert hasattr(main, "VERSION")

    def test_version_is_string(self):
        import main
        assert isinstance(main.VERSION, str)

    def test_version_not_old_hardcode(self):
        import main
        assert main.VERSION != "1.0.0"

    def test_version_is_valid_semver(self):
        import re
        import main
        assert re.match(r"^\d+\.\d+\.\d+$", main.VERSION), f"VERSION '{main.VERSION}' is not valid semver"


# ---------------------------------------------------------------------------
# Fix 2 — Snapshot route uses correct call_claude kwargs
# ---------------------------------------------------------------------------

class TestSnapshotRoute:
    """Regression: snapshot must call call_claude with the correct signature."""

    @pytest.mark.anyio
    async def test_snapshot_correct_kwargs(self, tmp_path):
        """call_claude must receive user_message=, model=, system=, is_new_session=."""
        import web.app as app_mod

        session_stub = {
            "session_id": str(uuid.uuid4()),
            "channel": "web",
            "peer": "admin",
        }
        session_manager = MagicMock()
        session_manager.get_or_create = AsyncMock(return_value=session_stub)
        session_manager.async_read_transcript = AsyncMock(return_value=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ])

        agent_loop = MagicMock()
        cfg = _make_temp_config(tmp_path)

        captured_kwargs: dict = {}

        async def fake_call_claude(**kwargs):
            captured_kwargs.update(kwargs)
            return ("Summary text", "fake-session-id", {})

        with (
            patch("web.app._session_manager", session_manager),
            patch("web.app._agent_loop", agent_loop),
            patch("web.app.get_config", return_value=cfg),
            patch("core.cli_backend.call_claude", fake_call_claude),
        ):
            await app_mod.take_snapshot("web", "admin")

        # Verify no legacy kwargs were passed
        assert "prompt" not in captured_kwargs, "Legacy kwarg 'prompt=' must not be used"
        assert "session_id" not in captured_kwargs, "Legacy kwarg 'session_id=' must not be used"
        # Verify correct kwargs present
        assert "user_message" in captured_kwargs
        assert "model" in captured_kwargs
        assert "is_new_session" in captured_kwargs
        assert captured_kwargs["is_new_session"] is False


# ---------------------------------------------------------------------------
# Session flows (using in-memory SQLite via our aiosqlite stub)
# ---------------------------------------------------------------------------

class TestSessionFlows:
    """Tests for session create, switch, and compact using in-memory SQLite."""

    def _make_sm(self, tmp_path: Path):
        """Return a SessionManager with path helpers redirected to tmp_path."""
        import core.session_manager as sm_mod
        from core.session_manager import SessionManager

        def _db(agent_id):
            p = tmp_path / agent_id
            p.mkdir(parents=True, exist_ok=True)
            return p / "sessions.db"

        def _tp(agent_id, session_id):
            p = tmp_path / agent_id
            p.mkdir(parents=True, exist_ok=True)
            return p / f"{session_id}.jsonl"

        sm_mod._db_path = _db
        sm_mod._transcript_path = _tp

        return SessionManager(agent_id="test-agent")

    @pytest.mark.anyio
    async def test_create_session(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            session = await sm.get_or_create("discord", "user123")
            await sm.stop()
        assert "session_id" in session
        assert session["channel"] == "discord"
        assert session["peer"] == "user123"

    @pytest.mark.anyio
    async def test_pupil_web_chat_opens_on_the_fast_model(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            pupil = await sm.get_or_create("web", "elev|1", tier="student")
            staff = await sm.get_or_create("web", "ansatt|1", tier="staff")
            other = await sm.get_or_create("discord", "user123")
            await sm.stop()
        assert pupil["model"] == cfg.models.chat_id
        assert staff["model"] == cfg.models.default_id
        assert other["model"] == cfg.models.default_id
        assert pupil["model"] != staff["model"]

    @pytest.mark.anyio
    async def test_same_channel_peer_returns_same_session(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            s1 = await sm.get_or_create("discord", "user123")
            s2 = await sm.get_or_create("discord", "user123")
            await sm.stop()
        assert s1["session_id"] == s2["session_id"]

    @pytest.mark.anyio
    async def test_reset_session_removes_row(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            await sm.get_or_create("discord", "resetme")
            await sm.reset_session("discord", "resetme")
            sessions = await sm.list_sessions()
            await sm.stop()
        assert not any(s["peer"] == "resetme" for s in sessions)

    @pytest.mark.anyio
    async def test_reset_session_deletes_the_transcript(self, tmp_path):
        """A deleted chat must not leave a file the model can pick up again."""
        import core.session_manager as sm_mod
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            session = await sm.get_or_create("web", "elev|cid", tier="student")
            sid = session["session_id"]
            await sm.append_message(sid, "user", "wifi")
            live = sm_mod._transcript_path("test-agent", sid)
            leftover = live.with_suffix(".jsonl.archived")
            leftover.write_text("{}\n", encoding="utf-8")
            assert live.exists()
            await sm.reset_session("web", "elev|cid")
            sessions = await sm.list_sessions()
            await sm.stop()
        assert not any(s["peer"] == "elev|cid" for s in sessions)
        assert not live.exists()
        assert not leftover.exists()

    @pytest.mark.anyio
    async def test_pupils_are_purged_on_the_shorter_clock(self, tmp_path):
        """A pupil's conversation goes at 5 days; staff keep theirs to 30."""
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            pupil = await sm.get_or_create("web", "elev|a", tier="student")
            staff = await sm.get_or_create("web", "ansatt|b", tier="staff")
            legacy = await sm.get_or_create("web", "gammel|c")  # predates tiers
            await self._age(sm, [pupil, staff, legacy], days=10)

            removed = await sm.purge_older_than(30, student_days=5)
            left = {s["peer"] for s in await sm.list_sessions()}
            await sm.stop()

        assert removed == 1
        assert left == {"ansatt|b", "gammel|c"}

    @pytest.mark.anyio
    async def test_a_pupil_inside_the_window_is_left_alone(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            pupil = await sm.get_or_create("web", "elev|a", tier="student")
            await self._age(sm, [pupil], days=3)
            removed = await sm.purge_older_than(30, student_days=5)
            await sm.stop()
        assert removed == 0

    @pytest.mark.anyio
    async def test_everyone_is_purged_on_one_clock_when_no_pupil_window(self, tmp_path):
        """student_days=0 must mean "same as everyone", not "never delete"."""
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            pupil = await sm.get_or_create("web", "elev|a", tier="student")
            staff = await sm.get_or_create("web", "ansatt|b", tier="staff")
            await self._age(sm, [pupil, staff], days=40)
            removed = await sm.purge_older_than(30, student_days=0)
            await sm.stop()
        assert removed == 2

    @pytest.mark.anyio
    async def test_keeping_a_pupil_conversation_resets_its_clock(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            pupil = await sm.get_or_create("web", "elev|a", tier="student")
            await self._age(sm, [pupil], days=10)
            await sm.touch(pupil["session_id"])
            removed = await sm.purge_older_than(30, student_days=5)
            await sm.stop()
        assert removed == 0

    async def _age(self, sm, sessions: list, days: int) -> None:
        """Backdate last_active so the retention sweep sees an old conversation."""
        from core.session_manager import _now_ts
        stale = _now_ts() - days * 86400
        for s in sessions:
            await sm._db.execute(
                "UPDATE sessions SET last_active=? WHERE session_id=?",
                (stale, s["session_id"]),
            )
        await sm._db.commit()

    @pytest.mark.anyio
    async def test_compact_sets_pending_context(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            session = await sm.get_or_create("discord", "compact_user")
            sid = session["session_id"]
            await sm.compact_session(sid, "Compaction summary here.")
            ctx = await sm.pop_pending_context(sid)
            await sm.stop()
        assert ctx is not None
        assert "Compaction summary here." in ctx

    @pytest.mark.anyio
    async def test_pop_pending_context_clears_it(self, tmp_path):
        cfg = _make_temp_config(tmp_path)
        sm = self._make_sm(tmp_path)
        with patch("core.session_manager.get_config", return_value=cfg):
            await sm.start()
            session = await sm.get_or_create("discord", "ctx_user")
            sid = session["session_id"]
            await sm.compact_session(sid, "Summary.")
            await sm.pop_pending_context(sid)
            ctx2 = await sm.pop_pending_context(sid)
            await sm.stop()
        assert ctx2 is None


# ---------------------------------------------------------------------------
# Slash command routing
# ---------------------------------------------------------------------------

class TestEmployeeCommandScope:
    """FRID employees get own-session commands only. Skills and shared state stay
    with the operator and with people given the admin access role.
    """

    def test_scope_by_tier(self):
        from channels.slash_commands import command_permitted, command_scope_for
        assert command_scope_for("student") is None
        assert command_scope_for("staff") == "employee"
        assert command_scope_for("moderator") == "employee"
        assert command_scope_for("admin") == "all"
        assert command_scope_for("staff", operator=True) == "all"
        assert command_permitted("/status", "employee")
        assert command_permitted("/think", "employee")
        assert not command_permitted("/skills", "employee")
        assert not command_permitted("/session", "employee")
        assert not command_permitted("/dokumenter", "employee")
        assert not command_permitted("/nettside", "employee")
        assert not command_permitted("/queue", "employee")
        assert command_permitted("/skills", "all")
        assert command_permitted("/nettside", "all")
        assert not command_permitted("/status", None)

    @pytest.mark.anyio
    async def test_employee_cannot_create_a_skill(self, tmp_path):
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash(
                "/skill new triage flow", "web", "ansatt",
                MagicMock(), MagicMock(), send, scope="employee",
            )

        assert result is True
        assert not (tmp_path / "skills" / "triage-flow" / "SKILL.md").exists()
        assert any("ikke tilgjengelig" in s for s in sent)

    @pytest.mark.anyio
    async def test_employee_help_lists_only_own_session_commands(self, tmp_path):
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            await handle_slash(
                "/help", "web", "ansatt",
                MagicMock(), MagicMock(), send, scope="employee",
            )

        combined = "\n".join(sent)
        assert "/status" in combined
        assert "/new" in combined
        assert "/skill" not in combined
        assert "/session" not in combined
        assert "/queue" not in combined


class TestSlashCommands:
    """Tests for channels/slash_commands.py routing logic."""

    def _make_session_manager(self):
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": str(uuid.uuid4()),
            "channel": "discord",
            "peer": "user1",
            "agent_id": "helix",
            "model": "claude-sonnet-4-6",
            "last_active": 1_700_000_000.0,
            "compacted": 0,
            "token_count": 0,
            "context_window": 0,
            "max_output_tokens": 0,
        })
        sm.list_sessions = AsyncMock(return_value=[])
        sm.reset_session = AsyncMock()
        sm.set_model = AsyncMock()
        sm.read_transcript = MagicMock(return_value=[])
        sm.async_read_transcript = AsyncMock(return_value=[])
        return sm

    def _make_agent_loop(self):
        loop = MagicMock()
        loop.run_heartbeat = AsyncMock(return_value=None)
        return loop

    @pytest.mark.anyio
    async def test_status_handled(self, tmp_path):
        responses = []

        async def send(text):
            responses.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            handled = await handle_slash(
                "/status", "discord", "user1",
                self._make_session_manager(), self._make_agent_loop(), send
            )

        assert handled is True
        assert responses, "Expected at least one response message"
        combined = "\n".join(responses)
        assert "Model" in combined or "Session" in combined

    @pytest.mark.anyio
    async def test_new_handled(self, tmp_path):
        responses = []

        async def send(text):
            responses.append(text)

        sm = self._make_session_manager()
        cfg = _make_temp_config(tmp_path)

        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            handled = await handle_slash(
                "/new", "discord", "user1",
                sm, self._make_agent_loop(), send
            )

        assert handled is True
        sm.reset_session.assert_awaited_once_with("discord", "user1")

    @pytest.mark.anyio
    async def test_model_switch(self, tmp_path):
        responses = []

        async def send(text):
            responses.append(text)

        sm = self._make_session_manager()
        cfg = _make_temp_config(tmp_path)

        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            handled = await handle_slash(
                "/model sonnet", "discord", "user1",
                sm, self._make_agent_loop(), send
            )

        assert handled is True
        sm.set_model.assert_awaited_once()

    @pytest.mark.anyio
    async def test_unknown_command_handled(self, tmp_path):
        responses = []

        async def send(text):
            responses.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            handled = await handle_slash(
                "/doesnotexist", "discord", "user1",
                self._make_session_manager(), self._make_agent_loop(), send
            )

        assert handled is True
        assert any("Unknown" in r or "unknown" in r for r in responses)


# ---------------------------------------------------------------------------
# Integration tests — snapshot route via TestClient
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestSnapshotIntegration:
    """Integration tests for the snapshot endpoint via FastAPI TestClient.

    The real app layer is exercised; external deps (Claude API) are mocked.
    """

    def _make_client(self, tmp_path, monkeypatch):
        """Return a TestClient with all external deps patched."""
        import importlib
        import web.app as app_mod

        # Reload so fresh module state is used for this test
        importlib.reload(app_mod)

        TestClient = _RealTestClient

        cfg = _make_temp_config(tmp_path)

        session_stub = {
            "session_id": str(uuid.uuid4()),
            "channel": "web",
            "peer": "admin",
        }
        session_manager = MagicMock()
        session_manager.get_or_create = AsyncMock(return_value=session_stub)
        session_manager.async_read_transcript = AsyncMock(return_value=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ])

        agent_loop = MagicMock()

        monkeypatch.setattr(app_mod, "_session_manager", session_manager)
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)

        # Patch call_claude inside the snapshot route
        async def fake_call_claude(**kwargs):
            return ("Snapshot summary text.", "fake-sid", {})

        monkeypatch.setattr("core.cli_backend.call_claude", fake_call_claude)

        # Patch _verify_token so requests are treated as authenticated
        monkeypatch.setattr(app_mod, "_decode_token", lambda token: {"sub": "admin", "role": "admin", "name": "Admin"})

        client = TestClient(app_mod.app, raise_server_exceptions=False)
        return client

    def test_snapshot_returns_200(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch)
        resp = client.post(
            "/api/sessions/web/admin/snapshot",
            headers={"Authorization": "Bearer validtoken"},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("status") == "ok"

    def test_snapshot_not_500(self, tmp_path, monkeypatch):
        """Regression: snapshot route must not return 500 under normal conditions."""
        client = self._make_client(tmp_path, monkeypatch)
        resp = client.post(
            "/api/sessions/web/admin/snapshot",
            headers={"Authorization": "Bearer validtoken"},
        )
        assert resp.status_code != 500, f"Snapshot returned 500: {resp.text}"

    def test_snapshot_empty_session_returns_empty_status(self, tmp_path, monkeypatch):
        """When session has no messages, should return empty status not 500."""
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        TestClient = _RealTestClient

        cfg = _make_temp_config(tmp_path)
        session_stub = {"session_id": str(uuid.uuid4()), "channel": "web", "peer": "admin"}
        session_manager = MagicMock()
        session_manager.get_or_create = AsyncMock(return_value=session_stub)
        session_manager.async_read_transcript = AsyncMock(return_value=[])  # empty
        agent_loop = MagicMock()

        monkeypatch.setattr(app_mod, "_session_manager", session_manager)
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(app_mod, "_decode_token", lambda token: {"sub": "admin", "role": "admin", "name": "Admin"})

        client = TestClient(app_mod.app, raise_server_exceptions=False)
        resp = client.post(
            "/api/sessions/web/admin/snapshot",
            headers={"Authorization": "Bearer validtoken"},
        )
        assert resp.status_code == 200
        assert resp.json().get("status") == "empty"


# ---------------------------------------------------------------------------
# Integration tests — slash command flow via adapter boundary
# ---------------------------------------------------------------------------

class TestSlashCommandAdapterBoundary:
    """Exercise slash commands through the full handle_slash interface
    (adapter boundary), not just internal logic.

    These tests verify that when a message arrives at the channel boundary
    as a slash command string, the correct side effects occur end-to-end.
    """

    def _make_sm(self):
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={
            "session_id": str(uuid.uuid4()),
            "channel": "discord",
            "peer": "user42",
            "agent_id": "helix",
            "model": "claude-sonnet-4-6",
            "last_active": 1_700_000_000.0,
            "compacted": 0,
            "token_count": 0,
            "context_window": 0,
            "max_output_tokens": 0,
        })
        sm.list_sessions = AsyncMock(return_value=[])
        sm.reset_session = AsyncMock()
        sm.set_model = AsyncMock()
        sm.read_transcript = MagicMock(return_value=[])
        sm.async_read_transcript = AsyncMock(return_value=[])
        return sm

    def _make_loop(self):
        loop = MagicMock()
        loop.run_heartbeat = AsyncMock(return_value="HEARTBEAT_OK")
        return loop

    @pytest.mark.anyio
    async def test_help_command_returns_response(self, tmp_path):
        """Adapter receives /help → handle_slash returns True and sends text."""
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/help", "discord", "user42",
                                        self._make_sm(), self._make_loop(), send)

        assert result is True
        assert sent, "Expected /help to produce at least one response"
        combined = "\n".join(sent)
        assert "/new" in combined or "Commands" in combined

    @pytest.mark.anyio
    async def test_new_resets_session_via_adapter(self, tmp_path):
        """/new coming through adapter boundary must reset the session."""
        sent = []

        async def send(text):
            sent.append(text)

        sm = self._make_sm()
        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/new", "discord", "user42",
                                        sm, self._make_loop(), send)

        assert result is True
        sm.reset_session.assert_awaited_once_with("discord", "user42")
        assert sent, "Expected a confirmation message"

    @pytest.mark.anyio
    async def test_unknown_command_via_adapter(self, tmp_path):
        """/bogus should be intercepted, return True, and report unknown."""
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/boguscmd", "discord", "user42",
                                        self._make_sm(), self._make_loop(), send)

        assert result is True
        assert any("Unknown" in s or "unknown" in s for s in sent)

    @pytest.mark.anyio
    async def test_non_slash_message_falls_through(self, tmp_path):
        """A plain message is NOT a slash command and handle_slash is not called
        at the adapter boundary — this test verifies handle_slash itself
        still returns False for agent-handled slash commands (boundary contract)."""
        # /think is an agent-handled command — handle_slash returns False
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/think deeply", "discord", "user42",
                                        self._make_sm(), self._make_loop(), send)

        # Agent-handled commands return False so the adapter routes them onward
        assert result is False

    @pytest.mark.anyio
    async def test_skill_new_creates_workspace_skill(self, tmp_path):
        sent = []

        async def send(text):
            sent.append(text)

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/skill new triage flow", "discord", "user42",
                                        self._make_sm(), self._make_loop(), send)

        assert result is True
        assert (tmp_path / "skills" / "triage-flow" / "SKILL.md").exists()
        assert any("Skill created" in s for s in sent)

    @pytest.mark.anyio
    async def test_skills_lists_installed_skills(self, tmp_path):
        sent = []

        async def send(text):
            sent.append(text)

        from core.skill_manager import create_skill
        create_skill(str(tmp_path), "incident triage", description="triage incidents")

        cfg = _make_temp_config(tmp_path)
        with patch("channels.slash_commands.get_config", return_value=cfg):
            from channels.slash_commands import handle_slash
            result = await handle_slash("/skills", "discord", "user42",
                                        self._make_sm(), self._make_loop(), send)

        assert result is True
        assert any("incident-triage" in s for s in sent)


# ---------------------------------------------------------------------------
# Unit tests — debounce key scoping
# ---------------------------------------------------------------------------

class TestDebounceKeyScoping:
    def test_discord_debounce_key_is_chat_scoped(self):
        from channels.discord_adapter import DiscordAdapter

        adapter = DiscordAdapter(handler=None, auth=None, agent_loop=None, session_manager=None)

        guild_message_a = MagicMock()
        guild_message_a.channel.id = 111
        guild_message_b = MagicMock()
        guild_message_b.channel.id = 222

        key_a = adapter._debounce_key(guild_message_a, "user42", is_dm=False)
        key_b = adapter._debounce_key(guild_message_b, "user42", is_dm=False)
        dm_key = adapter._debounce_key(guild_message_a, "user42", is_dm=True)

        assert key_a != key_b
        assert key_a != dm_key
        assert key_b != dm_key

    def test_telegram_debounce_key_is_chat_scoped(self):
        from channels.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter(handler=None, auth=None, agent_loop=None, session_manager=None)

        update_a = MagicMock()
        update_a.effective_chat.id = 1001
        update_b = MagicMock()
        update_b.effective_chat.id = 2002

        key_a = adapter._debounce_key(update_a, 12345)
        key_b = adapter._debounce_key(update_b, 12345)

        assert key_a != key_b


# ---------------------------------------------------------------------------
# Unit tests — skill manager
# ---------------------------------------------------------------------------

class TestSkillManager:
    def test_create_list_read_write_skill(self, tmp_path):
        from core.skill_manager import create_skill, list_skills, read_skill, write_skill

        created = create_skill(str(tmp_path), "Weekly Review", description="Summarize the week")
        assert created["id"] == "weekly-review"
        assert created["path"] == "skills/weekly-review/SKILL.md"

        skills = list_skills(str(tmp_path))
        assert len(skills) == 1
        assert skills[0]["id"] == "weekly-review"

        loaded = read_skill(str(tmp_path), "weekly-review")
        assert "Weekly Review" in loaded["content"]

        saved = write_skill(str(tmp_path), "weekly-review", "# Weekly Review\n\nUpdated")
        assert saved["description"] == "Updated"

    def test_invalid_skill_id_rejected(self, tmp_path):
        from core.skill_manager import read_skill

        with pytest.raises(ValueError):
            read_skill(str(tmp_path), "../bad")


# ---------------------------------------------------------------------------
# Integration tests — queue and workspace file APIs
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestAdminApiIntegration:
    def _make_client(self, tmp_path, monkeypatch, queue_manager=None):
        import importlib
        import web.app as app_mod

        importlib.reload(app_mod)

        TestClient = _RealTestClient
        cfg = _make_temp_config(tmp_path)

        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(app_mod, "_decode_token", lambda token: {"sub": "admin", "role": "admin", "name": "Admin"})
        monkeypatch.setattr(app_mod, "_session_manager", MagicMock())
        monkeypatch.setattr(app_mod, "_agent_loop", MagicMock())
        monkeypatch.setattr(app_mod, "_queue_manager", queue_manager)

        return TestClient(app_mod.app, raise_server_exceptions=False)

    def test_queue_endpoints(self, tmp_path, monkeypatch):
        queue_manager = MagicMock()
        queue_manager.list_tasks = AsyncMock(return_value=[{"id": "abc123", "status": "pending"}])
        queue_manager.queue_depth = AsyncMock(return_value={"pending": 1})
        queue_manager.add_task = AsyncMock(return_value={"id": "abc123", "status": "pending", "prompt": "do thing"})
        queue_manager.clear_done = AsyncMock(return_value=2)
        queue_manager.cancel_task = AsyncMock(return_value=True)
        queue_manager.get_task = AsyncMock(return_value={"id": "abc123", "status": "pending"})

        client = self._make_client(tmp_path, monkeypatch, queue_manager=queue_manager)
        headers = {"Authorization": "Bearer validtoken"}

        list_resp = client.get("/api/queue", headers=headers)
        assert list_resp.status_code == 200
        assert list_resp.json()["depth"] == {"pending": 1}

        create_resp = client.post("/api/queue", json={"prompt": "do thing"}, headers=headers)
        assert create_resp.status_code == 200
        queue_manager.add_task.assert_awaited_once_with("do thing", "web", "admin", None)

        get_resp = client.get("/api/queue/abc123", headers=headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == "abc123"

        cancel_resp = client.delete("/api/queue/abc123", headers=headers)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        clear_resp = client.delete("/api/queue/done", headers=headers)
        assert clear_resp.status_code == 200
        assert clear_resp.json()["cleared"] == 2

    def test_workspace_file_crud_endpoints(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch, queue_manager=MagicMock())
        headers = {"Authorization": "Bearer validtoken"}

        create_resp = client.post(
            "/api/files/create",
            json={"path": "notes/test.md", "content": "hello"},
            headers=headers,
        )
        assert create_resp.status_code == 200
        created_path = tmp_path / "notes" / "test.md"
        assert created_path.exists()
        assert created_path.read_text() == "hello"

        read_resp = client.get("/api/files/read?path=notes%2Ftest.md", headers=headers)
        assert read_resp.status_code == 200
        assert read_resp.json()["content"] == "hello"

        write_resp = client.put(
            "/api/files/write",
            json={"path": "notes/test.md", "content": "updated"},
            headers=headers,
        )
        assert write_resp.status_code == 200
        assert created_path.read_text() == "updated"

        mkdir_resp = client.post(
            "/api/files/mkdir",
            json={"path": "notes/subdir"},
            headers=headers,
        )
        assert mkdir_resp.status_code == 200
        assert (tmp_path / "notes" / "subdir").is_dir()

        tree_resp = client.get("/api/files/tree", headers=headers)
        assert tree_resp.status_code == 200
        assert tree_resp.json()["type"] == "dir"

        delete_file_resp = client.delete("/api/files/delete?path=notes%2Ftest.md", headers=headers)
        assert delete_file_resp.status_code == 200
        assert not created_path.exists()

        delete_dir_resp = client.delete("/api/files/delete?path=notes%2Fsubdir", headers=headers)
        assert delete_dir_resp.status_code == 200
        assert not (tmp_path / "notes" / "subdir").exists()

    def test_skills_api_endpoints(self, tmp_path, monkeypatch):
        client = self._make_client(tmp_path, monkeypatch, queue_manager=MagicMock())
        headers = {"Authorization": "Bearer validtoken"}

        list_resp = client.get("/api/skills", headers=headers)
        assert list_resp.status_code == 200
        assert list_resp.json()["skills"] == []

        create_resp = client.post(
            "/api/skills",
            json={"name": "Triage Flow", "description": "Help with ticket triage"},
            headers=headers,
        )
        assert create_resp.status_code == 200
        created = create_resp.json()
        assert created["id"] == "triage-flow"
        assert (tmp_path / "skills" / "triage-flow" / "SKILL.md").exists()

        get_resp = client.get("/api/skills/triage-flow", headers=headers)
        assert get_resp.status_code == 200
        assert "Triage Flow" in get_resp.json()["content"]

        save_resp = client.put(
            "/api/skills/triage-flow",
            json={"content": "# Triage Flow\n\nUpdated"},
            headers=headers,
        )
        assert save_resp.status_code == 200
        assert (tmp_path / "skills" / "triage-flow" / "SKILL.md").read_text() == "# Triage Flow\n\nUpdated"

        list_resp_2 = client.get("/api/skills", headers=headers)
        assert list_resp_2.status_code == 200
        assert len(list_resp_2.json()["skills"]) == 1

        (tmp_path / "assets").mkdir(exist_ok=True)
        (tmp_path / "assets" / "a.png").write_bytes(b"\x89PNG")
        (tmp_path / "skills" / "triage-flow" / "SKILL.md").write_text(
            "![x](asset:a.png)", encoding="utf-8"
        )
        tmpl = tmp_path / "repo-template"
        import core.workspace_sync as ws
        monkeypatch.setattr(ws, "TEMPLATE_DIR", tmpl)
        copy_resp = client.post("/api/skills/triage-flow/to-template", headers=headers)
        assert copy_resp.status_code == 200
        body = copy_resp.json()
        assert body["assets"] == ["a.png"]
        assert (tmpl / "skills" / "triage-flow" / "SKILL.md").exists()
        assert (tmpl / "assets" / "a.png").read_bytes().startswith(b"\x89PNG")

    def test_permissions_api_sets_an_override(self, tmp_path, monkeypatch):
        from core.config import HelixConfig, save_config as real_save, load_config as real_load
        from core import config as config_mod
        import web.app as app_mod

        cfg_path = tmp_path / "config.json"
        real_save(HelixConfig(workspace_path=str(tmp_path / "ws")), cfg_path)
        client = self._make_client(tmp_path, monkeypatch)
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(app_mod, "save_config", lambda c: real_save(c, cfg_path))
        monkeypatch.setattr(app_mod, "get_config", lambda: real_load(cfg_path, True))

        headers = {"Authorization": "Bearer validtoken"}
        listed = client.get("/api/permissions", headers=headers)
        assert listed.status_code == 200
        assert "frid.no" in listed.json()["defaults"]["staff_domains"]

        saved = client.put(
            "/api/permissions",
            json={"email": "Elev@AFK.no", "access": "blocked"},
            headers=headers,
        )
        assert saved.status_code == 200
        people = {p["email"]: p for p in saved.json()["people"]}
        assert people["elev@afk.no"]["override"] == "blocked"
        assert people["elev@afk.no"]["effective"] == "blocked"

        pupil = client.put(
            "/api/permissions",
            json={"email": "elev@afk.no", "access": "student"},
            headers=headers,
        )
        assert pupil.status_code == 200
        assert "elev@afk.no" not in [
            p["email"] for p in pupil.json()["people"] if p.get("override")
        ]

        mod = client.put(
            "/api/permissions",
            json={"email": "leder@frid-iks.no", "access": "moderator"},
            headers=headers,
        )
        people = {p["email"]: p for p in mod.json()["people"]}
        assert people["leder@frid-iks.no"]["override"] == "moderator"

        admin = client.put(
            "/api/permissions",
            json={"email": "it@frid-iks.no", "access": "admin"},
            headers=headers,
        )
        people = {p["email"]: p for p in admin.json()["people"]}
        assert people["it@frid-iks.no"]["override"] == "admin"

        probe = client.put(
            "/api/permissions",
            json={"email": "loadtest-p01-n00@afk.no", "access": "blocked"},
            headers=headers,
        )
        assert probe.status_code == 200
        body = probe.json()
        assert "loadtest-p01-n00@afk.no" not in [p["email"] for p in body["people"]]
        stress = {p["email"]: p for p in body["stress"]}
        assert stress["loadtest-p01-n00@afk.no"]["override"] == "blocked"

        gone = client.delete("/api/permissions/elev@afk.no", headers=headers)
        assert gone.status_code == 200
        still = [p["email"] for p in gone.json()["people"] if p.get("override")]
        assert "elev@afk.no" not in still


class TestEntraChatsFollowEmailLogin:
    def test_timestamp_match_reattaches_the_conversation(self):
        from web.app import entra_rekey_plan
        email = "jorgenthin@frid-iks.no"
        events = [{"event": "chat_started", "sender_id": email, "ts": 1000.0}]
        sessions = [{
            "session_id": "sid-1",
            "channel": "web",
            "peer": "entra:abc-oid|conv-1111-1111-1111-111111111111",
            "created_at": 990.0,
            "last_active": 1005.0,
        }]
        plan = entra_rekey_plan(email, events, sessions)
        assert plan == [("sid-1", f"dev:{email}|conv-1111-1111-1111-111111111111")]

    def test_does_not_give_someone_else_the_chat(self):
        from web.app import entra_rekey_plan
        events = [{"event": "chat_started", "sender_id": "elev@afk.no", "ts": 1000.0}]
        sessions = [{
            "session_id": "sid-1",
            "channel": "web",
            "peer": "entra:abc-oid|conv-1111-1111-1111-111111111111",
            "created_at": 990.0,
            "last_active": 1005.0,
        }]
        assert entra_rekey_plan("jorgenthin@frid-iks.no", events, sessions) == []

    def test_sole_real_user_gets_orphaned_entra_chats(self):
        from web.app import entra_rekey_plan
        email = "jorgenthin@frid-iks.no"
        events = [
            {"event": "chat_started", "sender_id": email, "ts": 1.0},
            {"event": "chat_started", "sender_id": "loadtest-p01-n00@test.no", "ts": 2.0},
        ]
        sessions = [{
            "session_id": "sid-1",
            "channel": "web",
            "peer": "entra:abc-oid|conv-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "created_at": 99999.0,
            "last_active": 99999.0,
        }]
        plan = entra_rekey_plan(email, events, sessions)
        assert plan == [("sid-1", f"dev:{email}|conv-aaaa-aaaa-aaaa-aaaaaaaaaaaa")]


class TestMicrosoftSignInIsGone:
    """Entra SSO is not a login path. Old URLs must not redirect into Microsoft."""

    @pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
    def test_microsoft_endpoints_are_gone(self, tmp_path, monkeypatch):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(app_mod, "get_config", lambda: _make_temp_config(tmp_path))
        client = _RealTestClient(app_mod.app, raise_server_exceptions=False)
        assert client.get("/api/auth/microsoft/enabled").json() == {"enabled": False}
        assert client.get("/api/auth/microsoft/login").status_code == 410
        assert client.get("/api/auth/microsoft/callback").status_code == 410


@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestPublicPages:
    """The public hostname is the chat. Operators use /admin."""

    def test_root_sends_people_to_chat(self, tmp_path, monkeypatch):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(app_mod, "get_config", lambda: _make_temp_config(tmp_path))
        client = _RealTestClient(app_mod.app, raise_server_exceptions=False)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers.get("location") == "/chat"

    def test_admin_is_the_operator_panel(self, tmp_path, monkeypatch):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(app_mod, "get_config", lambda: _make_temp_config(tmp_path))
        client = _RealTestClient(app_mod.app, raise_server_exceptions=False)
        r = client.get("/admin")
        assert r.status_code == 200
        assert "Zorken Admin" in r.text
        assert "Operator panel" in r.text

    def test_chat_is_the_sign_in_page(self, tmp_path, monkeypatch):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(app_mod, "get_config", lambda: _make_temp_config(tmp_path))
        client = _RealTestClient(app_mod.app, raise_server_exceptions=False)
        r = client.get("/chat")
        assert r.status_code == 200
        assert 'data-t="title"' in r.text
        assert "Zorken" in r.text
        assert "window.HELIX_FLAGS" in r.text


# ---------------------------------------------------------------------------
# Integration tests — WebSocket auth (connect with good/bad token)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestWebSocketAuth:
    """Integration tests for WebSocket auth handshake.

    Uses FastAPI's TestClient which supports ws:// connections via the
    starlette websocket test helpers.
    """

    def _make_app_client(self, tmp_path, monkeypatch, good_token="good-token"):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        TestClient = _RealTestClient

        # Patch _decode_token: accept only good_token. ws_chat needs the claims
        # (it derives the per-user session key from "sub"), so this is the
        # auth gate rather than the old boolean _verify_token.
        monkeypatch.setattr(
            app_mod,
            "_decode_token",
            lambda token: ({"sub": "admin", "role": "admin", "name": "Admin"}
                           if token == good_token else None),
        )

        agent_loop = MagicMock()

        async def fake_run(channel, peer, message, **kwargs):
            yield "pong"

        agent_loop.run = fake_run

        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)
        monkeypatch.setattr(app_mod, "_session_manager", MagicMock())

        return TestClient(app_mod.app)

    def test_ws_valid_token_connects(self, tmp_path, monkeypatch):
        """WebSocket with a valid auth token should connect successfully."""
        client = self._make_app_client(tmp_path, monkeypatch)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "good-token"})
            data = ws.receive_json()
            assert data.get("status") == "connected"

    def test_ws_bad_token_rejected(self, tmp_path, monkeypatch):
        """WebSocket with a wrong token should be rejected (error or close)."""
        client = self._make_app_client(tmp_path, monkeypatch)
        rejected = False
        try:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"token": "wrong-token"})
                data = ws.receive_json()
                # Server sends {"error": "Unauthorized"} then closes
                if "error" in data:
                    rejected = True
                else:
                    # Connection may have closed without explicit error message
                    rejected = False
        except Exception:
            # WebSocketDisconnect or similar — also counts as rejected
            rejected = True
        assert rejected, "Expected bad token to be rejected"

    def test_ws_missing_token_rejected(self, tmp_path, monkeypatch):
        """WebSocket with no token field should be rejected."""
        client = self._make_app_client(tmp_path, monkeypatch)
        rejected = False
        try:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({})  # no token key
                data = ws.receive_json()
                if "error" in data:
                    rejected = True
        except Exception:
            rejected = True
        assert rejected, "Expected missing token to be rejected"


# ---------------------------------------------------------------------------
# Integration tests — commands are staff-only (see ROLES.md)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_REAL_FASTAPI, reason="fastapi not installed")
class TestCommandTierGate:
    """Slash commands belong to staff.

    A student's leading slash must carry no meaning: the message goes to the
    agent as ordinary text, so a pupil typing "/hjelp" gets help rather than a
    notice about a command surface they cannot use.
    """

    def _client(self, monkeypatch, claims: dict):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: dict(claims) if token == "tok" else None,
        )

        seen = {"slash": [], "agent": [], "kwargs": {}}

        async def fake_slash(message, channel, peer, sm, al, send_fn, **kwargs):
            seen["slash"].append(message)
            await send_fn("kommando kjørt")
            return True

        async def fake_run(channel, peer, message, **kwargs):
            seen["agent"].append(message)
            seen["kwargs"] = kwargs
            yield "svar"

        agent_loop = MagicMock()
        agent_loop.run = fake_run
        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={"session_id": "s"})
        sm.async_read_transcript = AsyncMock(return_value=[])
        sm.context_status = AsyncMock(return_value={
            "blocked": False, "percent": 0, "window": 0, "used": 0,
        })
        monkeypatch.setattr(app_mod, "handle_slash", fake_slash)
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)
        monkeypatch.setattr(app_mod, "_session_manager", sm)

        return _RealTestClient(app_mod.app), seen

    def _send(self, monkeypatch, claims: dict, message: str, extra=None) -> dict:
        client, seen = self._client(monkeypatch, claims)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            assert ws.receive_json().get("status") == "connected"
            payload = {"message": message}
            if extra:
                payload.update(extra)
            ws.send_json(payload)
            # Drain until the turn ends so the server has finished routing.
            for _ in range(10):
                if ws.receive_json().get("done"):
                    break
        return seen

    def test_staff_slash_runs_the_command(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "role": "user", "tier": "staff"},
            "/status",
        )
        assert seen["slash"] == ["/status"]
        assert seen["agent"] == []

    def test_staff_cannot_run_skill_commands(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "role": "user", "tier": "staff"},
            "/skills",
        )
        assert seen["slash"] == []
        assert seen["agent"] == []

    def test_moderator_cannot_run_skill_commands(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:leder@frid-iks.no", "role": "user", "tier": "moderator"},
            "/skill new foo",
        )
        assert seen["slash"] == []
        assert seen["agent"] == []

    def test_admin_can_run_skill_commands(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {
                "sub": "dev:it@frid-iks.no", "email": "it@frid-iks.no",
                "role": "admin", "tier": "admin",
            },
            "/skills",
        )
        assert seen["slash"] == ["/skills"]
        assert seen["agent"] == []

    def test_staff_cannot_reach_nettside(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "role": "user", "tier": "staff"},
            "/nettside https://example.com",
        )
        assert seen["slash"] == []
        assert seen["agent"] == []

    def test_student_slash_reaches_the_agent_as_text(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "role": "user", "tier": "student"},
            "/status",
        )
        assert seen["slash"] == []
        assert seen["agent"] == ["/status"]

    def test_browser_os_reaches_the_agent(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "role": "user", "tier": "student"},
            "wifi",
            extra={"os": "macOS"},
        )
        assert seen["kwargs"].get("client_os") == "macOS"

    def test_junk_os_is_not_forwarded(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "role": "user", "tier": "student"},
            "wifi",
            extra={"os": "not-an-os"},
        )
        assert seen["kwargs"].get("client_os") == ""

    def test_student_cannot_reach_nettside(self, monkeypatch):
        """The /nettside branch fetches a URL server-side — students must miss it."""
        seen = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "role": "user", "tier": "student"},
            "/nettside https://example.com",
        )
        assert seen["slash"] == []
        assert seen["agent"] == ["/nettside https://example.com"]

    def test_token_without_tier_is_treated_as_student(self, monkeypatch):
        """Tokens minted before tiers existed must lose commands, not keep them."""
        seen = self._send(
            monkeypatch,
            {"sub": "dev:gammel@frid.no", "role": "user"},
            "/status",
        )
        assert seen["slash"] == []
        assert seen["agent"] == ["/status"]

    def test_operator_keeps_commands(self, monkeypatch):
        seen = self._send(
            monkeypatch,
            {"sub": "admin", "role": "admin", "tier": "staff"},
            "/status",
        )
        assert seen["slash"] == ["/status"]

    def test_staff_override_gives_a_pupil_commands(self, monkeypatch):
        from core.config import HelixConfig, UserAccessOverride

        cfg = HelixConfig()
        cfg.tiers.user_overrides = [
            UserAccessOverride(email="elev@afk.no", access="staff"),
        ]
        client, seen = self._client(monkeypatch, {
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        import web.app as app_mod
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            assert ws.receive_json().get("status") == "connected"
            ws.send_json({"message": "/status"})
            for _ in range(10):
                if ws.receive_json().get("done"):
                    break
        assert seen["slash"] == ["/status"]

    def test_blocked_person_cannot_open_chat(self, monkeypatch):
        from core.config import HelixConfig, UserAccessOverride

        cfg = HelixConfig()
        cfg.tiers.user_overrides = [
            UserAccessOverride(email="elev@afk.no", access="blocked"),
        ]
        client, _ = self._client(monkeypatch, {
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        import web.app as app_mod
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            frame = ws.receive_json()
        assert frame.get("code") == "forbidden"
        assert "Zorken" in frame.get("error", "")

    def test_staff_override_gives_a_pupil_commands(self, monkeypatch):
        from core.config import HelixConfig, UserAccessOverride

        cfg = HelixConfig()
        cfg.tiers.user_overrides = [
            UserAccessOverride(email="elev@afk.no", access="staff"),
        ]
        client, seen = self._client(monkeypatch, {
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        import web.app as app_mod
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            assert ws.receive_json().get("status") == "connected"
            ws.send_json({"message": "/status"})
            for _ in range(10):
                if ws.receive_json().get("done"):
                    break
        assert seen["slash"] == ["/status"]

    def test_blocked_person_cannot_open_chat(self, monkeypatch):
        from core.config import HelixConfig, UserAccessOverride

        cfg = HelixConfig()
        cfg.tiers.user_overrides = [
            UserAccessOverride(email="elev@afk.no", access="blocked"),
        ]
        client, _ = self._client(monkeypatch, {
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "role": "user", "tier": "student",
        })
        import web.app as app_mod
        monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            frame = ws.receive_json()
        assert frame.get("code") == "forbidden"
        assert "Zorken" in frame.get("error", "")


class TestChatAuditRecords:
    """Every conversation leaves a record the IT desk can read later.

    The point is the insight, not the surveillance: who asked, what it was
    about, and whether they were pushing at the edges of what a pupil may ask.
    """

    def _client(self, monkeypatch, claims: dict, reply: str = "svar",
                history: list = None):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: dict(claims) if token == "tok" else None,
        )

        async def fake_run(channel, peer, message, **kwargs):
            yield reply

        agent_loop = MagicMock()
        agent_loop.run = fake_run
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={"session_id": "s1"})
        sm.async_read_transcript = AsyncMock(return_value=history or [])
        sm.context_status = AsyncMock(return_value={"blocked": False})
        monkeypatch.setattr(app_mod, "_session_manager", sm)

        recorded = []
        audit = MagicMock()
        audit.log = lambda event, **fields: recorded.append((event, fields))
        monkeypatch.setattr(app_mod, "_audit", audit)

        return _RealTestClient(app_mod.app), recorded

    def _send(self, monkeypatch, claims, message, reply="svar",
              history: list = None) -> list:
        client, recorded = self._client(monkeypatch, claims, reply, history)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            ws.send_json({"message": message})
            for _ in range(10):
                if ws.receive_json().get("done"):
                    break
        return recorded

    def test_conversation_start_is_recorded_with_who_and_what(self, monkeypatch):
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "name": "Elev Elevsen", "role": "user", "tier": "student"},
            "Jeg får ikke koblet meg til wifi på skolen",
        )
        started = [f for e, f in recorded if e == "chat_started"]
        assert len(started) == 1
        rec = started[0]
        assert rec["sender_id"] == "elev@afk.no"
        assert rec["domain"] == "afk.no"
        assert rec["tier"] == "student"
        assert "wifi" in rec["content_preview"].lower()

    def test_the_record_carries_the_topic_it_was_about(self, monkeypatch):
        """Resolved from the whole message here, not from the 60-char title later."""
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "Jeg får ikke koblet mobilen min til skolens wifi",
        )
        rec = [f for e, f in recorded if e == "chat_started"][0]
        assert rec["topic"] == "frid-nettverk", (
            "the network guide is what a pupil asking about wifi needs; a blank "
            "topic here means the coverage view cannot see the demand"
        )

    def test_only_the_first_message_starts_a_conversation(self, monkeypatch):
        client, recorded = self._client(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
        )
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            for text in ("wifi virker ikke", "og passordet også"):
                ws.send_json({"message": text})
                for _ in range(10):
                    if ws.receive_json().get("done"):
                        break
        assert len([e for e, _ in recorded if e == "chat_started"]) == 1

    def test_returning_to_an_existing_conversation_is_not_a_new_one(self, monkeypatch):
        """A reconnect replays history — it must not double-count the question."""
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "og hva med telefonen?",
            history=[{"role": "user", "content": "wifi virker ikke"}],
        )
        assert not [e for e, _ in recorded if e == "chat_started"]

    def test_entra_sign_in_is_recorded_by_email_not_object_id(self, monkeypatch):
        """The subject is an opaque Entra id — an audit record naming it is useless."""
        recorded = self._send(
            monkeypatch,
            {"sub": "entra:3f2b1c00-0000-0000-0000-abcdef123456",
             "email": "elev.elevsen@afk.no", "role": "user", "tier": "student"},
            "wifi virker ikke",
        )
        rec = [f for e, f in recorded if e == "chat_started"][0]
        assert rec["sender_id"] == "elev.elevsen@afk.no"
        assert rec["domain"] == "afk.no"

    def test_blocked_command_is_recorded(self, monkeypatch):
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "/nettside https://example.com",
        )
        blocked = [f for e, f in recorded if e == "chat_command_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["command"] == "/nettside"

    def test_refusal_is_recorded_with_what_they_asked(self, monkeypatch):
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "Kan du løse denne mattoppgaven for meg?",
            reply="Jeg kan bare hjelpe med IT på skolen. Spør læreren din om matte.",
        )
        refusals = [f for e, f in recorded if e == "chat_scope_refusal"]
        assert len(refusals) == 1
        assert "mattoppgave" in refusals[0]["content_preview"].lower()
        assert "jeg kan bare hjelpe med it" in refusals[0]["reply"].lower()

    def test_an_undocumented_question_logs_the_conversation(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
             "name": "Ada", "role": "user", "tier": "staff"},
            "Word viser en feil når jeg lagrer dokumentet",
            reply="Jeg har ikke en lokal prosedyre for den Word-feilen.",
        )
        gaps = [f for e, f in recorded if e == "chat_knowledge_gap"]
        assert len(gaps) == 1
        rec = gaps[0]
        assert rec["topic"] == "m365-office"
        assert rec["label"].lower().startswith("missing ")
        assert "word" in rec["label"].lower()
        assert rec["session_id"] == "s1"
        roles = [m["role"] for m in rec["messages"]]
        assert roles == ["user", "assistant"]
        assert "Word" in rec["messages"][0]["content"]
        assert "lokal prosedyre" in rec["messages"][1]["content"]

    def test_praising_the_agent_is_not_a_knowledge_gap(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid-iks.no", "email": "ansatt@frid-iks.no",
             "name": "Ada", "role": "user", "tier": "staff"},
            "Supert. nå må vi separere de neste stegene basert på om de "
            "bruker windows eller mac.",
        )
        assert not [e for e, _ in recorded if e == "chat_knowledge_gap"]

    def test_a_documented_question_is_not_a_knowledge_gap(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "Jeg får ikke koblet meg til wifi på skolen",
        )
        assert not [e for e, _ in recorded if e == "chat_knowledge_gap"]

    def test_a_later_turn_can_be_the_undocumented_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        client, recorded = self._client(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
             "role": "user", "tier": "staff"},
        )
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            for text in ("hei", "Word viser en feil når jeg lagrer"):
                ws.send_json({"message": text})
                for _ in range(10):
                    if ws.receive_json().get("done"):
                        break
        gaps = [f for e, f in recorded if e == "chat_knowledge_gap"]
        assert len(gaps) == 1
        asked = [m["content"] for m in gaps[0]["messages"] if m["role"] == "user"]
        assert asked[0] == "hei"
        assert "Word" in asked[1]

    def test_two_undocumented_turns_log_the_conversation_once(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        client, recorded = self._client(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
             "role": "user", "tier": "staff"},
        )
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            for text in ("Word viser en feil", "og Excel krasjer også"):
                ws.send_json({"message": text})
                for _ in range(10):
                    if ws.receive_json().get("done"):
                        break
        assert len([e for e, _ in recorded if e == "chat_knowledge_gap"]) == 1

    def test_a_scope_refusal_is_not_a_knowledge_gap(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.subjects.CATALOG_PATH", tmp_path / "subjects.json")
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
             "role": "user", "tier": "student"},
            "Kan du løse denne mattoppgaven for meg?",
            reply="Jeg kan bare hjelpe med IT på skolen. Spør læreren din om matte.",
        )
        assert not [e for e, _ in recorded if e == "chat_knowledge_gap"]

    def test_staff_reply_that_mentions_the_phrase_is_not_a_refusal(self, monkeypatch):
        """Staff have no scope to break, so the phrase means nothing for them."""
        recorded = self._send(
            monkeypatch,
            {"sub": "dev:ansatt@frid.no", "email": "ansatt@frid.no",
             "role": "user", "tier": "staff"},
            "Hva svarer agenten elevene?",
            reply="Den sier: Jeg kan bare hjelpe med IT på skolen.",
        )
        assert not [e for e, _ in recorded if e == "chat_scope_refusal"]


class TestChatDeleteWipesAndNotes:
    """Deleting a /chat conversation wipes it; Insights keeps a short note."""

    def _delete(self, monkeypatch, messages, *, claims=None, peer=None, cid=None):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        cid = cid or "11111111-2222-3333-4444-555555555555"
        claims = claims or {
            "sub": "dev:elev@afk.no", "email": "elev@afk.no",
            "name": "Elev", "role": "user", "tier": "student",
        }
        peer = peer or f"dev:elev@afk.no|{cid}"

        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: dict(claims) if token == "tok" else None,
        )

        sm = MagicMock()
        sm.list_sessions = AsyncMock(return_value=[
            {"session_id": "s1", "channel": "web", "peer": peer},
        ] if messages is not None else [])
        sm.async_read_transcript = AsyncMock(return_value=messages or [])
        sm.reset_session = AsyncMock()
        monkeypatch.setattr(app_mod, "_session_manager", sm)

        recorded = []
        audit = MagicMock()
        audit.log = lambda event, **fields: recorded.append((event, fields))
        monkeypatch.setattr(app_mod, "_audit", audit)

        client = _RealTestClient(app_mod.app)
        r = client.delete(
            f"/api/me/conversations/{cid}",
            headers={"Authorization": "Bearer tok"},
        )
        return r, recorded, sm, peer

    def test_delete_records_a_brief_and_destroys_the_session(self, monkeypatch):
        r, recorded, sm, peer = self._delete(monkeypatch, [
            {"role": "user", "content": "wifi virker ikke"},
            {"role": "assistant", "content": "Sjekk sertifikatet. " + ("steg. " * 80)},
            {"role": "user", "content": "og på telefonen?"},
        ])
        assert r.status_code == 200
        sm.reset_session.assert_awaited_once_with("web", peer)
        deleted = [f for e, f in recorded if e == "chat_deleted"]
        assert len(deleted) == 1
        note = deleted[0]
        assert note["sender_id"] == "elev@afk.no"
        assert note["turns"] == 2
        assert "wifi" in note["summary"]
        assert "telefonen" in note["summary"]
        assert "Sjekk sertifikatet" not in note["summary"]
        assert note["asks"] == ["wifi virker ikke", "og på telefonen?"]
        assert note["flagged"] is False

    def test_every_user_ask_is_kept_on_the_note(self, monkeypatch):
        msgs = []
        for i in range(8):
            msgs.append({"role": "user", "content": f"spørsmål {i} om wifi"})
            msgs.append({"role": "assistant", "content": "sjekk dette"})
        r, recorded, _, _ = self._delete(monkeypatch, msgs)
        assert r.status_code == 200
        note = [f for e, f in recorded if e == "chat_deleted"][0]
        assert note["turns"] == 8
        assert note["asks"][-1] == "spørsmål 7 om wifi"
        assert "spørsmål 7" not in note["summary"]

    def test_homework_that_was_refused_is_flagged(self, monkeypatch):
        r, recorded, _, _ = self._delete(monkeypatch, [
            {"role": "user", "content": "skriv et dikt om våren"},
            {"role": "assistant", "content": "Jeg kan bare hjelpe med IT på skolen."},
        ])
        assert r.status_code == 200
        note = [f for e, f in recorded if e == "chat_deleted"][0]
        assert note["flagged"] is True
        assert note["flag_reason"] == "refused"
        assert "dikt" in note["summary"]

    def test_homework_the_model_answered_is_still_flagged(self, monkeypatch):
        """A pupil who got an answer and then deleted the chat must still show."""
        r, recorded, _, _ = self._delete(monkeypatch, [
            {"role": "user", "content": "skriv et dikt om våren"},
            {"role": "assistant", "content": "Her er et dikt: solen stiger..."},
        ])
        assert r.status_code == 200
        note = [f for e, f in recorded if e == "chat_deleted"][0]
        assert note["flagged"] is True
        assert note["flag_reason"] == "off_topic"

    def test_empty_chat_is_wiped_without_a_note(self, monkeypatch):
        r, recorded, sm, _ = self._delete(monkeypatch, None)
        assert r.status_code == 200
        assert not [e for e, _ in recorded if e == "chat_deleted"]
        sm.reset_session.assert_not_awaited()


class TestPupilRetentionWindow:
    """A pupil is told the clock that will actually delete their conversation."""

    def _app(self, monkeypatch, tier: str):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": f"dev:x@y.no", "role": "user", "tier": tier},
        )
        return app_mod

    def _me(self, monkeypatch, tier: str) -> dict:
        app_mod = self._app(monkeypatch, tier)
        r = _RealTestClient(app_mod.app).get(
            "/api/me", headers={"Authorization": "Bearer tok"}
        )
        assert r.status_code == 200
        return r.json()

    def test_pupil_is_quoted_the_short_window(self, monkeypatch):
        assert self._me(monkeypatch, "student")["retention_days"] == 5

    def test_staff_are_quoted_the_long_window(self, monkeypatch):
        assert self._me(monkeypatch, "staff")["retention_days"] == 30

    def test_pupil_window_falls_back_when_unset(self, monkeypatch):
        """0 means "same as everyone else", not "keep pupil chats forever"."""
        app_mod = self._app(monkeypatch, "student")
        cfg = app_mod.get_config()
        monkeypatch.setattr(cfg.session, "student_retention_days", 0)
        assert app_mod._retention_days("student") == cfg.session.retention_days


class TestPupilKeepsTheSolution:
    """Keeping a conversation means something different for a pupil.

    Staff get a handover briefing for the next technician. A pupil gets the fix
    itself, in their own thread, and only pays for a model call when the thread
    is long enough that summarising saves anything.
    """

    def _client(self, monkeypatch, tier: str, messages: list):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "dev:elev@afk.no", "role": "user", "tier": tier},
        )

        calls = {"summarised": [], "touched": [], "appended": []}

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={"session_id": "s1"})
        sm.async_read_transcript = AsyncMock(return_value=messages)
        sm.compact_session = AsyncMock(
            side_effect=lambda sid, summary: calls["summarised"].append(summary)
        )
        sm.touch = AsyncMock(side_effect=lambda sid: calls["touched"].append(sid))
        sm.append_message = AsyncMock(
            side_effect=lambda sid, role, content: calls["appended"].append(content)
        )
        monkeypatch.setattr(app_mod, "_session_manager", sm)
        monkeypatch.setattr(app_mod, "_agent_loop", MagicMock())

        prompts = []

        async def fake_claude(*, model, system, user_message, **kw):
            prompts.append(user_message)
            return "Slik løste du det: 1) last ned sertifikatet 2) koble til FRID", "sid", {}

        monkeypatch.setattr(app_mod, "_audit", None)
        import core.cli_backend as cli
        monkeypatch.setattr(cli, "call_claude", fake_claude)

        client = _RealTestClient(app_mod.app)
        cid = "11111111-2222-3333-4444-555555555555"
        r = client.post(
            f"/api/me/conversations/{cid}/compact",
            headers={"Authorization": "Bearer tok"},
        )
        return r, calls, prompts

    def _thread(self, turns: int, answer_len: int = 400) -> list:
        answer = "Gjør dette: " + "steg. " * (answer_len // 6)
        msgs = []
        for i in range(turns):
            msgs.append({"role": "user", "content": f"spørsmål {i}"})
            msgs.append({"role": "assistant", "content": answer})
        return msgs

    def test_short_pupil_chat_is_kept_without_paying_for_a_summary(self, monkeypatch):
        r, calls, prompts = self._client(monkeypatch, "student", self._thread(2))
        assert r.status_code == 200
        assert r.json()["kept"] == "as_is"
        assert calls["touched"] == ["s1"]
        assert prompts == []
        assert calls["summarised"] == []

    def test_long_pupil_chat_is_summarised_into_the_solution(self, monkeypatch):
        r, calls, prompts = self._client(monkeypatch, "student", self._thread(6))
        assert r.status_code == 200
        body = r.json()
        assert body["kept"] == "summary"
        assert body["in_thread"] is True
        # The pupil prompt asks for the fix only — not the handover briefing.
        assert "KUN løsningen" in prompts[0]
        assert "ruled out" not in prompts[0]
        # And it lands in their own thread, where they can read it.
        assert calls["appended"] and calls["summarised"]

    def test_staff_always_get_the_handover_briefing(self, monkeypatch):
        r, calls, prompts = self._client(monkeypatch, "staff", self._thread(2))
        assert r.status_code == 200
        assert r.json()["in_thread"] is False
        assert "ruled out" in prompts[0]
        # Staff threads are not rewritten with a note.
        assert calls["appended"] == []

    def test_nothing_answered_yet_cannot_be_kept(self, monkeypatch):
        r, calls, prompts = self._client(
            monkeypatch, "student",
            [{"role": "user", "content": "hei"}, {"role": "assistant", "content": "hei!"}],
        )
        assert r.status_code == 409
        assert calls["touched"] == [] and prompts == []


class TestPupilImagesAreNotPermanent:
    """Nothing a pupil or employee sends becomes part of the knowledge base.

    Only the operator and Users-tab admins persist screenshots into assets/.
    Employees' commands are own-session only, so their uploads stay temporary.
    """

    def _send_with_image(
        self, monkeypatch, tmp_path, tier: str, role: str = "user", sub: str = "dev:x@y.no",
    ):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        claims = {"sub": sub, "role": role, "tier": tier}
        if role == "admin" and tier == "admin":
            claims["email"] = "it@frid-iks.no"
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: dict(claims) if token == "tok" else None,
        )

        img = tmp_path / "skjermbilde.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        monkeypatch.setitem(
            app_mod._UPLOADS, "up1",
            {"path": str(img), "owner": sub, "at": time.time()},
        )

        assets = tmp_path / "assets"

        def _assets():
            assets.mkdir(parents=True, exist_ok=True)
            return assets

        monkeypatch.setattr(app_mod, "_assets_dir", _assets)

        seen = []

        async def fake_run(channel, peer, message, **kwargs):
            seen.append(message)
            yield "svar"

        agent_loop = MagicMock()
        agent_loop.run = fake_run
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={"session_id": "s1"})
        sm.async_read_transcript = AsyncMock(return_value=[])
        sm.context_status = AsyncMock(return_value={"blocked": False})
        monkeypatch.setattr(app_mod, "_session_manager", sm)
        monkeypatch.setattr(app_mod, "_audit", None)

        client = _RealTestClient(app_mod.app)
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            ws.send_json({"message": "her er passordfeilen min", "attachments": ["up1"]})
            for _ in range(10):
                if ws.receive_json().get("done"):
                    break
        return seen, assets, img

    def test_pupil_screenshot_never_reaches_the_assets_folder(self, monkeypatch, tmp_path):
        seen, assets, img = self._send_with_image(monkeypatch, tmp_path, "student")
        assert not assets.exists() or list(assets.iterdir()) == [], \
            "a pupil's screenshot was copied into the knowledge base"
        # It still reaches the model — from the temp upload, which expires.
        assert seen and str(img) in seen[0]

    def test_pupil_agent_is_not_told_the_image_is_a_permanent_asset(
        self, monkeypatch, tmp_path
    ):
        seen, _assets, _img = self._send_with_image(monkeypatch, tmp_path, "student")
        assert "documentation assets" not in seen[0]
        assert "asset:" not in seen[0]

    def test_staff_screenshot_is_not_persisted(self, monkeypatch, tmp_path):
        seen, assets, img = self._send_with_image(monkeypatch, tmp_path, "staff")
        assert not assets.exists() or list(assets.iterdir()) == [], \
            "an employee's screenshot was copied into the knowledge base"
        assert seen and str(img) in seen[0]
        assert "documentation assets" not in seen[0]

    def test_admin_screenshot_is_persisted(self, monkeypatch, tmp_path):
        seen, assets, _img = self._send_with_image(
            monkeypatch, tmp_path, "admin", role="admin",
        )
        stored = list(assets.iterdir())
        assert len(stored) == 1
        assert "documentation assets" in seen[0]

    def test_operator_screenshot_is_persisted(self, monkeypatch, tmp_path):
        seen, assets, _img = self._send_with_image(
            monkeypatch, tmp_path, "staff", role="admin", sub="admin",
        )
        stored = list(assets.iterdir())
        assert len(stored) == 1
        assert "documentation assets" in seen[0]

    def test_stale_uploads_are_swept_by_age_on_disk(self, tmp_path, monkeypatch):
        """The in-memory map does not survive a restart; the files do."""
        # core.file_handler is stubbed for the app-level tests above, so load the
        # real module here to exercise the sweep itself.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_real_file_handler", REPO_ROOT / "core" / "file_handler.py"
        )
        fh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fh)

        monkeypatch.setattr(fh, "UPLOAD_DIR", tmp_path)
        old = tmp_path / "orphan.png"
        old.write_bytes(b"x")
        import os
        stale = time.time() - 8 * 3600
        os.utime(old, (stale, stale))
        fresh = tmp_path / "recent.png"
        fresh.write_bytes(b"x")

        assert fh.prune_stale_uploads(6 * 3600) == 1
        assert not old.exists()
        assert fresh.exists()


class TestClientOs:
    def test_hint_beats_user_agent(self):
        from core.client_os import normalize
        assert normalize("iOS", "Mozilla/5.0 (Windows NT 10.0)") == "iOS"

    def test_user_agent_when_hint_is_empty(self):
        from core.client_os import normalize
        assert normalize("", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)") == "macOS"

    def test_junk_hint_falls_back_to_user_agent(self):
        from core.client_os import normalize, prompt_note
        assert normalize("<script>", "Mozilla/5.0 (Linux; Android 14)") == "Android"
        assert "Do not say you cannot see the device" in prompt_note("Windows")
        assert "Windows PC" in prompt_note("Windows")
        assert prompt_note("nope") == ""


class TestPupilWaitNotes:
    """The wait bubble names the step the agent is on, not a rotating guess."""

    def test_chat_ties_notes_to_phases_not_a_timer(self):
        page = Path("web/static/chat.html").read_text(encoding="utf-8")
        assert "waitRead" in page
        assert "waitThink" in page
        assert "waitRules" in page
        assert "waitRespond" in page
        assert "WAIT_LATE_MS = 20000" in page
        assert "Math.random() < 1/3" in page
        assert "applyPhase" in page
        assert "Netflix-pause…" in page
        assert "startWaitNotes(true)" in page
        assert "2400" not in page


class TestChatImageLightbox:
    """Agent screenshots enlarge in the chat, not in a new browser tab."""

    def test_chat_does_not_open_images_in_a_new_tab(self):
        page = Path("web/static/chat.html").read_text(encoding="utf-8")
        assert 'window.open' not in page
        assert 'id="lightbox"' in page
        assert "openLightbox" in page
        assert 'id="lbClose"' in page
        assert 'cursor:zoom-in' in page.replace(" ", "")
        assert "Escape" in page


class TestDeskChatPersonality:
    """Own messages sit on the other side, with a role label and a sent line."""

    def test_team_chat_marks_own_messages(self):
        page = Path("web/static/index.html").read_text(encoding="utf-8")
        assert "desk-msg.mine" in page
        assert "deskRoleLabel" in page
        assert "desk-msg-role" in page
        assert "linear-gradient(90deg, transparent 0%, #3b82f6" in page
        assert "flex-direction: row-reverse" in page


class TestPupilAssetBoundary:
    """A pupil may load the images their own guides cite, and no others.

    The assets folder is the whole knowledge base, including screenshots of Azure
    PIM and the MFA reset procedure — the material pupils are specifically walled
    off from.
    """

    def _client(self, monkeypatch, tmp_path, tier: str):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        workspace = tmp_path / "workspace"
        (workspace / "skills" / "frid-nettverk").mkdir(parents=True)
        (workspace / "skills" / "visma-inschool").mkdir(parents=True)
        (workspace / "skills" / "frid-nettverk" / "SKILL.md").write_text(
            "![wifi](asset:wifi-steg-1.png)", encoding="utf-8"
        )
        (workspace / "skills" / "visma-inschool" / "PUPIL.md").write_text(
            "![konto](asset:kontoaktivering-start.png)", encoding="utf-8"
        )
        (workspace / "skills" / "visma-inschool" / "SKILL.md").write_text(
            "staff only ![azure](asset:azure-pim-aktiver-rollen.png)", encoding="utf-8"
        )

        assets = workspace / "assets"
        assets.mkdir()
        for name in ("wifi-steg-1.png", "kontoaktivering-start.png",
                     "azure-pim-aktiver-rollen.png"):
            (assets / name).write_bytes(b"\x89PNG\r\n\x1a\n")

        cfg = app_mod.get_config()
        monkeypatch.setattr(cfg, "workspace_path", str(workspace))
        monkeypatch.setattr(app_mod, "_assets_dir", lambda: assets)
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "dev:x@y.no", "role": "user", "tier": tier},
        )
        monkeypatch.setattr(app_mod, "_pupil_assets_cache", (0.0, frozenset()))
        return _RealTestClient(app_mod.app)

    def _get(self, client, name: str) -> int:
        return client.get(
            f"/api/assets/{name}", headers={"Authorization": "Bearer tok"}
        ).status_code

    def test_pupil_can_load_an_image_their_guide_cites(self, monkeypatch, tmp_path):
        client = self._client(monkeypatch, tmp_path, "student")
        assert self._get(client, "wifi-steg-1.png") == 200
        assert self._get(client, "kontoaktivering-start.png") == 200

    def test_pupil_cannot_load_the_azure_screenshots(self, monkeypatch, tmp_path):
        client = self._client(monkeypatch, tmp_path, "student")
        assert self._get(client, "azure-pim-aktiver-rollen.png") == 404

    def test_staff_can_load_any_of_them(self, monkeypatch, tmp_path):
        client = self._client(monkeypatch, tmp_path, "staff")
        assert self._get(client, "azure-pim-aktiver-rollen.png") == 200
        assert self._get(client, "wifi-steg-1.png") == 200

    def test_the_allowlist_follows_the_guides_when_they_change(self, monkeypatch, tmp_path):
        import web.app as app_mod
        client = self._client(monkeypatch, tmp_path, "student")
        assert self._get(client, "azure-pim-aktiver-rollen.png") == 404

        # Cite it from a pupil guide and it becomes theirs to load, without a
        # restart — the cache is keyed on the guides' modification time.
        guide = (Path(app_mod.get_config().workspace_path)
                 / "skills" / "frid-nettverk" / "SKILL.md")
        guide.write_text(
            "![wifi](asset:wifi-steg-1.png)\n![x](asset:azure-pim-aktiver-rollen.png)",
            encoding="utf-8",
        )
        import os
        future = time.time() + 10
        os.utime(guide, (future, future))
        assert self._get(client, "azure-pim-aktiver-rollen.png") == 200


class TestTieredRateLimits:
    """The allowance belongs to the person, not to the socket they opened.

    One Claude subscription sits behind every conversation, so a limit that a
    pupil can reset by pressing F5 is not a limit.
    """

    def _limiter(self):
        from security.auth import RateLimiter
        return RateLimiter()

    def test_pupils_get_a_tighter_allowance_than_staff(self):
        from core.config import get_config
        limits = get_config().security.rate_limit
        assert limits.for_tier("student") < limits.for_tier("staff")

    def test_an_unset_pupil_limit_falls_back_to_the_shared_one(self, monkeypatch):
        from core.config import get_config
        limits = get_config().security.rate_limit
        monkeypatch.setattr(limits, "student_per_minute", 0)
        assert limits.for_tier("student") == limits.for_tier("staff")

    def test_pupil_is_stopped_at_the_pupil_limit(self):
        from core.config import get_config
        per_minute, _ = get_config().security.rate_limit.for_tier("student")
        rl = self._limiter()
        for _ in range(per_minute):
            assert rl.check("web:elev", "student") is None
        assert "minute" in (rl.check("web:elev", "student") or "")

    def test_staff_are_not_stopped_at_the_pupil_limit(self):
        from core.config import get_config
        pupil_limit, _ = get_config().security.rate_limit.for_tier("student")
        rl = self._limiter()
        for _ in range(pupil_limit + 1):
            assert rl.check("web:ansatt", "staff") is None

    def test_two_connections_share_one_allowance(self):
        """The whole point: reconnecting must not hand out a fresh allowance."""
        from core.config import get_config
        per_minute, _ = get_config().security.rate_limit.for_tier("student")
        rl = self._limiter()
        for i in range(per_minute):
            # Same person, alternating "sockets" — the key is the person.
            assert rl.check("web:elev", "student") is None
        assert rl.check("web:elev", "student") is not None

    def test_one_pupil_hitting_the_limit_does_not_block_another(self):
        from core.config import get_config
        per_minute, _ = get_config().security.rate_limit.for_tier("student")
        rl = self._limiter()
        for _ in range(per_minute + 2):
            rl.check("web:elev1", "student")
        assert rl.check("web:elev2", "student") is None

    def test_idle_senders_are_forgotten(self):
        """Otherwise a school year of pupils accumulates in memory forever."""
        rl = self._limiter()
        rl.check("web:gone", "student")
        # Pretend the hour window aged out, then force a prune.
        rl._hour_windows["web:gone"][0] -= 7200
        rl._prune(time.time())
        assert "web:gone" not in rl._hour_windows
        assert "web:gone" not in rl._minute_windows


class TestRateLimitIsPerUserOverWebsocket:
    """The websocket handler must consult the shared limiter, not a fresh one."""

    def _client(self, monkeypatch, tier: str):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "dev:elev@afk.no", "email": "elev@afk.no",
                           "role": "user", "tier": tier},
        )

        async def fake_run(channel, peer, message, **kwargs):
            yield "svar"

        agent_loop = MagicMock()
        agent_loop.run = fake_run
        monkeypatch.setattr(app_mod, "_agent_loop", agent_loop)

        sm = MagicMock()
        sm.get_or_create = AsyncMock(return_value={"session_id": "s1"})
        sm.async_read_transcript = AsyncMock(return_value=[])
        sm.context_status = AsyncMock(return_value={"blocked": False})
        monkeypatch.setattr(app_mod, "_session_manager", sm)

        recorded = []
        audit = MagicMock()
        audit.log = lambda event, **f: recorded.append((event, f))
        monkeypatch.setattr(app_mod, "_audit", audit)

        return app_mod, _RealTestClient(app_mod.app), recorded

    def test_allowance_survives_a_reconnect(self, monkeypatch):
        from core.config import get_config
        per_minute, _ = get_config().security.rate_limit.for_tier("student")
        app_mod, client, recorded = self._client(monkeypatch, "student")

        sent = 0
        errors = []
        # Two separate connections, one person. The second must not start over.
        for _ in range(2):
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"token": "tok"})
                while ws.receive_json().get("status") != "connected":
                    pass
                for _ in range(per_minute):
                    ws.send_json({"message": "hei"})
                    sent += 1
                    for _ in range(10):
                        frame = ws.receive_json()
                        if frame.get("error"):
                            errors.append(frame)
                        if frame.get("done") or frame.get("error"):
                            break

        assert sent == per_minute * 2
        assert errors, "a reconnect handed out a fresh allowance"
        assert errors[0]["code"] == "rate_limited"

    def test_hitting_the_limit_is_recorded_once_not_per_attempt(self, monkeypatch):
        from core.config import get_config
        per_minute, _ = get_config().security.rate_limit.for_tier("student")
        app_mod, client, recorded = self._client(monkeypatch, "student")

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": "tok"})
            while ws.receive_json().get("status") != "connected":
                pass
            for _ in range(per_minute + 5):
                ws.send_json({"message": "hei"})
                for _ in range(10):
                    frame = ws.receive_json()
                    if frame.get("done") or frame.get("error"):
                        break

        hits = [f for e, f in recorded if e == "chat_rate_limited"]
        assert len(hits) == 1
        assert hits[0]["sender_id"] == "elev@afk.no"


class TestChatInsights:
    """The counting behind the admin view."""

    def _get(self, monkeypatch, tmp_path, events: list[dict], days: int = 30,
             role: str = "admin"):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)

        log = tmp_path / "audit.log"
        log.write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(app_mod, "_audit_log_path", lambda: log)
        monkeypatch.setattr(
            "core.subjects.CATALOG_PATH", tmp_path / "subjects.json"
        )
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": role} if token == "tok" else None,
        )
        client = _RealTestClient(app_mod.app)
        return client.get(
            f"/api/insights?days={days}",
            headers={"Authorization": "Bearer tok"},
        )

    def _insights(self, monkeypatch, tmp_path, events: list[dict], days: int = 30):
        r = self._get(monkeypatch, tmp_path, events, days)
        assert r.status_code == 200
        return r.json()

    def _started(self, title, *, email="elev@afk.no", domain="afk.no",
                 tier="student", ago_days=0):
        return {
            "ts": time.time() - ago_days * 86400,
            "event": "chat_started",
            "sender_id": email,
            "domain": domain,
            "tier": tier,
            "content_preview": title,
        }

    def test_counts_conversations_and_people(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi virker ikke"),
            self._started("wifi virker ikke igjen"),
            self._started("glemt passord", email="elev2@afk.no"),
        ])
        assert data["totals"]["conversations"] == 3
        assert data["totals"]["people"] == 2

    def test_top_words_surface_the_common_problem(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("Jeg får ikke koblet meg til wifi"),
            self._started("wifi på pc-en min"),
            self._started("Hjelp med wifi og sertifikat"),
            self._started("glemt passordet mitt"),
        ])
        words = {w["key"]: w["count"] for w in data["top_words"]}
        assert words.get("wifi") == 3
        # Filler words carry no signal and must not crowd out the real subject.
        for noise in ("jeg", "ikke", "med", "til", "og", "min", "mitt"):
            assert noise not in words

    def test_topics_are_counted_from_the_stored_label(self, monkeypatch, tmp_path):
        events = [
            self._started("wifi virker ikke") | {"topic": "frid-nettverk"},
            self._started("wifi igjen") | {"topic": "frid-nettverk"},
            self._started("pc-en er treg") | {"topic": "windows-endpoint"},
        ]
        data = self._insights(monkeypatch, tmp_path, events)
        topics = {t["key"]: t["count"] for t in data["by_topic"]}
        assert topics["frid-nettverk"] == 2
        assert topics["windows-endpoint"] == 1

    def test_similar_questions_share_one_readable_subject(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("Jeg får ikke koblet meg til wifi"),
            self._started("wifi på pc-en min"),
            self._started("Hjelp med wifi og sertifikat"),
            self._started("glemt passordet mitt"),
        ])
        subjects = {s["key"]: s["count"] for s in data["by_subject"]}
        wifi = [k for k in subjects if "wifi" in k.lower()]
        assert len(wifi) == 1, subjects
        assert subjects[wifi[0]] == 3
        assert any("passord" in k.lower() for k in subjects)
        for pile in ("jeg", "ikke", "med", "til"):
            assert pile not in subjects

    def test_a_stored_subject_is_kept_even_if_the_title_is_short(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi") | {"subject": "Logge på FRID-nettverket"},
            self._started("koble til nettet") | {"subject": "Logge på FRID-nettverket"},
        ])
        assert data["by_subject"] == [
            {"key": "Logge på FRID-nettverket", "count": 2}
        ]

    def test_every_conversation_is_either_classified_or_counted_as_unclassified(
        self, monkeypatch, tmp_path
    ):
        """An invariant, so this holds whatever the skill files happen to contain."""
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi virker ikke") | {"topic": "frid-nettverk"},
            self._started("hei"),
            self._started("takk for hjelpen"),
        ])
        classified = sum(t["count"] for t in data["by_topic"])
        assert classified + data["unclassified"] == data["totals"]["conversations"]

    def test_coverage_joins_demand_to_what_is_written_down(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi virker ikke") | {"topic": "frid-nettverk"},
        ])
        assert data["coverage"], "the report should list the workspace's skills"
        for row in data["coverage"]:
            assert {"skill", "file", "asked", "procedures", "documented",
                    "undocumented_demand"} <= set(row)
            assert row["file"] == f"skills/{row['skill']}/SKILL.md"
        assert data["totals"]["undocumented"] == 0

    def test_knowledge_gaps_are_the_undocumented_card(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi virker ikke") | {"topic": "frid-nettverk"},
            {"ts": time.time(), "event": "chat_knowledge_gap",
             "sender_id": "ansatt@frid.no", "name": "Ada",
             "tier": "staff",
             "topic": "m365-office",
             "label": "missing Word error solution",
             "content_preview": "Word viser en feil",
             "session_id": "s1",
             "messages": [
                 {"role": "user", "content": "Word viser en feil"},
                 {"role": "assistant", "content": "Jeg har ikke en lokal prosedyre for dette."},
             ]},
            {"ts": time.time() + 1, "event": "chat_knowledge_gap",
             "sender_id": "ansatt@frid.no",
             "tier": "staff",
             "topic": "m365-office",
             "label": "missing Word error solution",
             "session_id": "s1",
             "messages": [
                 {"role": "user", "content": "Word viser en feil"},
                 {"role": "assistant", "content": "Jeg har ikke en lokal prosedyre for dette."},
                 {"role": "user", "content": "den sier filen er ødelagt"},
             ]},
        ])
        assert data["totals"]["undocumented"] == 1
        assert len(data["recent_gaps"]) == 1
        gap = data["recent_gaps"][0]
        assert gap["label"] == "missing Word error solution"
        assert gap["email"] == "ansatt@frid.no"
        assert gap["name"] == "Ada"
        assert len(gap["messages"]) == 3

    def test_refusals_and_blocked_commands_are_counted(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi"),
            {"ts": time.time(), "event": "chat_scope_refusal",
             "sender_id": "elev@afk.no", "name": "Elev Elevsen",
             "tier": "student",
             "content_preview": "skriv et dikt",
             "reply": "Jeg kan bare hjelpe med IT på skolen."},
            {"ts": time.time(), "event": "chat_command_blocked",
             "sender_id": "elev@afk.no", "tier": "student", "command": "/status"},
        ])
        assert data["totals"]["refusals"] == 1
        assert data["totals"]["blocked_commands"] == 1
        assert data["recent_refusals"][0]["asked"] == "skriv et dikt"
        assert data["recent_refusals"][0]["email"] == "elev@afk.no"
        assert data["recent_refusals"][0]["name"] == "Elev Elevsen"
        assert "IT på skolen" in data["recent_refusals"][0]["reply"]

    def test_deleted_chats_surface_the_brief_note(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi"),
            {"ts": time.time(), "event": "chat_deleted",
             "sender_id": "elev@afk.no", "name": "Elev Elevsen",
             "tier": "student", "topic": "",
             "content_preview": "skriv et dikt",
             "summary": "skriv et dikt · og gjør det rimet",
             "turns": 2, "flagged": True, "flag_reason": "off_topic"},
        ])
        assert data["totals"]["deleted"] == 1
        assert data["totals"]["flagged_deleted"] == 1
        row = data["recent_deleted"][0]
        assert row["email"] == "elev@afk.no"
        assert row["name"] == "Elev Elevsen"
        assert "dikt" in row["summary"]
        assert row["flagged"] is True
        assert row["turns"] == 2

    def test_sessions_lists_deleted_chats_for_the_person(self, monkeypatch, tmp_path):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        log = tmp_path / "audit.log"
        log.write_text(json.dumps({
            "ts": time.time(), "event": "chat_deleted",
            "sender_id": "elev@afk.no", "name": "Elev Elevsen",
            "tier": "student", "channel": "web",
            "content_preview": "skriv et dikt",
            "summary": "skriv et dikt · og gjør det rimet",
            "turns": 2, "flagged": True, "flag_reason": "off_topic",
        }) + "\n", encoding="utf-8")
        monkeypatch.setattr(app_mod, "_audit_log_path", lambda: log)
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": "admin"} if token == "tok" else None,
        )
        r = _RealTestClient(app_mod.app).get(
            "/api/sessions/deleted",
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 200
        row = r.json()["deleted"][0]
        assert row["email"] == "elev@afk.no"
        assert "dikt" in row["summary"]
        assert row["flagged"] is True

    def test_events_outside_the_window_are_left_out(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("nytt problem", ago_days=1),
            self._started("gammelt problem", ago_days=45),
        ], days=7)
        assert data["totals"]["conversations"] == 1
        assert [c["title"] for c in data["recent"]] == ["nytt problem"]

    def test_staff_and_pupils_are_told_apart(self, monkeypatch, tmp_path):
        data = self._insights(monkeypatch, tmp_path, [
            self._started("wifi"),
            self._started("azure-tilgang", email="ansatt@frid.no",
                          domain="frid.no", tier="staff"),
        ])
        tiers = {t["key"]: t["count"] for t in data["by_tier"]}
        domains = {d["key"]: d["count"] for d in data["by_domain"]}
        assert tiers == {"student": 1, "staff": 1}
        assert domains == {"afk.no": 1, "frid.no": 1}

    def test_missing_log_is_not_an_error(self, monkeypatch, tmp_path):
        import importlib
        import web.app as app_mod
        importlib.reload(app_mod)
        monkeypatch.setattr(app_mod, "_audit_log_path", lambda: tmp_path / "gone.log")
        monkeypatch.setattr(
            app_mod, "_decode_token", lambda token: {"sub": "admin", "role": "admin"},
        )
        r = _RealTestClient(app_mod.app).get(
            "/api/insights", headers={"Authorization": "Bearer tok"}
        )
        assert r.status_code == 200
        assert r.json()["totals"]["conversations"] == 0

    def test_staff_cannot_read_pupil_emails(self, monkeypatch, tmp_path):
        """The view names pupils, so it stays behind the operator login."""
        r = self._get(monkeypatch, tmp_path, [self._started("wifi")], role="user")
        assert r.status_code == 403


class TestOperatorConsole:
    """Health, workspace drift, Insights drill-down, promote-to-skill."""

    def test_pii_is_stripped_from_a_draft(self):
        from core.skill_promote import strip_pii, draft_block
        text = strip_pii("Ring 99887766 or mail ola@afk.no, fnr 01010112345")
        assert "@" not in text
        assert "99887766" not in text
        assert "01010112345" not in text
        block = draft_block("MFA reset for ola@afk.no", "kontoen til ola@afk.no")
        assert "ola@afk.no" not in block
        assert "### " in block

    def test_promote_appends_a_stub_once(self, tmp_path):
        from core.skill_promote import promote
        skill = tmp_path / "skills" / "windows-endpoint"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "# Windows\n\n## Verified procedures\n", encoding="utf-8"
        )
        first = promote(str(tmp_path), "windows-endpoint", "PC-en er treg")
        assert first["already"] is False
        second = promote(str(tmp_path), "windows-endpoint", "PC-en er treg")
        assert second["already"] is True
        body = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert body.count("### PC-en er treg") == 1

    def test_drift_sees_a_diverged_skill(self, tmp_path):
        from core.workspace_sync import drift_report
        tmpl = tmp_path / "template"
        live = tmp_path / "live"
        (tmpl / "skills" / "a").mkdir(parents=True)
        (live / "skills" / "a").mkdir(parents=True)
        (tmpl / "skills" / "a" / "SKILL.md").write_text("template", encoding="utf-8")
        (live / "skills" / "a" / "SKILL.md").write_text("edited", encoding="utf-8")
        (tmpl / "AGENTS.md").write_text("same", encoding="utf-8")
        (live / "AGENTS.md").write_text("same", encoding="utf-8")
        report = drift_report(workspace=live, template=tmpl)
        assert "skills/a/SKILL.md" in report["diverged"]
        assert report["ok"] is False

    def test_pupil_visma_file_does_not_name_entra(self):
        from pathlib import Path
        pupil = Path("workspace-template/skills/visma-inschool/PUPIL.md").read_text(
            encoding="utf-8"
        )
        staff = Path("workspace-template/skills/visma-inschool/SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "entra-azure" not in pupil
        assert "colleague in the same role" not in pupil
        assert "entra-azure" in staff

    def test_copy_skill_to_template_takes_screenshots(self, tmp_path):
        from core.workspace_sync import copy_skill_to_template
        live = tmp_path / "live"
        tmpl = tmp_path / "template"
        skill = live / "skills" / "visma-inschool"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "staff ![x](asset:tap.png)", encoding="utf-8"
        )
        (skill / "PUPIL.md").write_text(
            "pupil ![x](asset:tap.png)", encoding="utf-8"
        )
        (live / "assets").mkdir()
        (live / "assets" / "tap.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (live / "assets" / "unrelated.png").write_bytes(b"nope")
        result = copy_skill_to_template(
            "visma-inschool", workspace=live, template=tmpl
        )
        assert "skills/visma-inschool/PUPIL.md" in result["copied"]
        assert result["assets"] == ["tap.png"]
        assert result["missing_assets"] == []
        assert (tmpl / "assets" / "tap.png").read_bytes().startswith(b"\x89PNG")
        assert not (tmpl / "assets" / "unrelated.png").exists()

    def test_copy_skill_to_template_lists_missing_images(self, tmp_path):
        from core.workspace_sync import copy_skill_to_template
        live = tmp_path / "live"
        tmpl = tmp_path / "template"
        skill = live / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("![x](asset:gone.png)", encoding="utf-8")
        result = copy_skill_to_template("demo", workspace=live, template=tmpl)
        assert result["missing_assets"] == ["gone.png"]
        assert result["assets"] == []

    def test_copy_skill_to_template_rejects_bad_id(self, tmp_path):
        from core.workspace_sync import copy_skill_to_template
        with pytest.raises(ValueError):
            copy_skill_to_template("../etc", workspace=tmp_path, template=tmp_path / "t")

    def test_pupil_visma_screenshots_are_in_the_template(self):
        import re
        pupil = Path("workspace-template/skills/visma-inschool/PUPIL.md").read_text(
            encoding="utf-8"
        )
        names = re.findall(r"asset:([A-Za-z0-9._-]+)", pupil)
        assert len(set(names)) == 9, names
        assets = Path("workspace-template/assets")
        missing = [
            n for n in names
            if not (assets / n).is_file() or (assets / n).stat().st_size < 100
        ]
        assert missing == []

    def test_auth_status_sees_the_helix_credentials_file(self, tmp_path, monkeypatch):
        from core import ops_health
        cred = tmp_path / ".credentials.json"
        cred.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(ops_health, "CREDENTIALS", cred)
        monkeypatch.setattr(ops_health, "get_secret", lambda key: "")
        status = ops_health.auth_status()
        assert status["credentials_file"] is True
        assert status["oauth_token"] is False
        assert status["ready"] is True

    def test_insights_subject_lists_the_opening_lines(self, monkeypatch, tmp_path):
        import importlib
        import json
        import web.app as app_mod
        importlib.reload(app_mod)
        log = tmp_path / "audit.log"
        now = time.time()
        events = [
            {
                "event": "chat_started", "ts": now - 60,
                "content_preview": "Jeg kommer ikke på wifi",
                "subject": "Jeg kommer ikke på wifi",
                "topic": "frid-nettverk", "tier": "student",
                "sender_id": "elev@afk.no", "name": "Elev",
            },
            {
                "event": "chat_started", "ts": now - 30,
                "content_preview": "MFA reset",
                "subject": "MFA reset",
                "topic": "entra-azure", "tier": "staff",
                "sender_id": "it@frid.no", "name": "IT",
            },
        ]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        monkeypatch.setattr(app_mod, "_audit_log_path", lambda: log)
        monkeypatch.setattr(
            app_mod, "_decode_token",
            lambda token: {"sub": "admin", "role": "admin"} if token == "tok" else None,
        )
        client = _RealTestClient(app_mod.app)
        r = client.get(
            "/api/insights/subject?key=Jeg kommer ikke på wifi",
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 200
        rows = r.json()["conversations"]
        assert len(rows) == 1
        assert rows[0]["topic"] == "frid-nettverk"


class TestCronRunResult:
    """A cron is a debug tool: the last run must show pass/fail, not only a time."""

    def test_ok_keeps_the_agent_text(self):
        from web.app import _cron_outcome
        assert _cron_outcome("50 users all clean") == ("ok", "50 users all clean")

    def test_norwegian_failure_is_an_error(self):
        from web.app import _cron_outcome
        status, text = _cron_outcome(
            "⚠️ Noe gikk galt her, og jeg fikk ikke svart."
        )
        assert status == "error"
        assert "Noe gikk galt" in text

    def test_no_reply_is_silent(self):
        from web.app import _cron_outcome
        assert _cron_outcome("NO_REPLY")[0] == "silent"
        assert _cron_outcome(None)[0] == "silent"
        assert _cron_outcome("")[0] == "silent"

    def test_exception_is_an_error(self):
        from web.app import _cron_outcome
        status, text = _cron_outcome(None, error=RuntimeError("cli exit 1"))
        assert status == "error"
        assert "cli exit 1" in text

    def test_probe_budget_grows_with_queue_depth(self, monkeypatch):
        from web.app import _probe_budget

        monkeypatch.setattr("core.cli_backend._claude_limit", lambda: 20)
        turn50, _ = _probe_budget(50)
        turn150, overall150 = _probe_budget(150)
        assert turn50 == 360
        assert turn150 == 960
        assert overall150 > turn150
        assert overall150 <= 2400

    def test_morning_load_prompt_is_a_50_user_probe(self):
        from web.app import _load_test_n
        assert _load_test_n("run a stress test running 50 concurrent users") == 50
        assert _load_test_n("Morning briefing") is None
        assert _load_test_n("run 50") is None

    def test_concurrency_probe_follows_railway_port(self):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "scripts" / "probe_concurrency.py"
        spec = importlib.util.spec_from_file_location("probe_concurrency_urls", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.helix_urls({}) == (
            "http://127.0.0.1:18791", "ws://127.0.0.1:18791/ws/chat",
        )
        assert mod.helix_urls({"PORT": "8080"}) == (
            "http://127.0.0.1:8080", "ws://127.0.0.1:8080/ws/chat",
        )
        assert mod.helix_urls({
            "PORT": "8080",
            "HELIX_BASE": "https://it-agent.up.railway.app",
        }) == (
            "https://it-agent.up.railway.app",
            "wss://it-agent.up.railway.app/ws/chat",
        )

    def test_wifi_probe_accepts_frid_iot_then_last_ned(self):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "scripts" / "probe_concurrency.py"
        spec = importlib.util.spec_from_file_location("probe_concurrency_score", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        case = {"expect": "wifi"}
        good = (
            "Koble deg først til wifi-nettverket FRID-iot med passordet "
            "friddeploy. Når du er på det nettverket, gå til "
            "https://onboard.frid-iks.no/ og trykk Last ned."
        )
        assert mod.score(case, good, []) == []
        password_only = "Wifi-passordet på skolen er friddeploy."
        assert "wifi_password_not_cert" in mod.score(case, password_only, [])

    @pytest.mark.anyio
    async def test_load_test_cron_stores_the_probe_report(self, tmp_path, monkeypatch):
        from core.config import HelixConfig, CronJob, save_config, load_config
        from core import config as config_mod
        import web.app as app_mod

        cfg_path = tmp_path / "config.json"
        cron = CronJob(
            id="e5700f84", name="morning load", schedule="0 7 * * *",
            prompt="run a stress test running 50 concurrent users",
        )
        cfg = HelixConfig(workspace_path=str(tmp_path / "ws"), crons=[cron])
        save_config(cfg, cfg_path)
        real_load, real_save = config_mod.load_config, config_mod.save_config
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(
            config_mod, "save_config",
            lambda cfg, path=cfg_path: real_save(cfg, cfg_path),
        )
        config_mod._config_cache = None
        config_mod._config_mtime = 0.0

        async def fake_probe(n):
            assert n == 50
            return 1, (
                "Load probe: 50 concurrent /chat users\n"
                "wall clock: 49.2s\n"
                "cross-talk: none\n"
                "3/50 turns had issues\n"
            )

        monkeypatch.setattr(app_mod, "_run_load_probe", fake_probe)
        monkeypatch.setattr(app_mod, "_agent_loop", MagicMock())

        await app_mod._execute_cron("e5700f84")
        saved = load_config(cfg_path, force_reload=True)
        assert saved.crons[0].last_status == "error"
        assert "3/50 turns had issues" in saved.crons[0].last_result
        config_mod._config_cache = None

    def test_old_config_still_loads_without_result_fields(self):
        from core.config import CronJob
        job = CronJob(name="x", schedule="0 8 * * *", prompt="go")
        assert job.last_status is None
        assert job.last_result is None

    @pytest.mark.anyio
    async def test_execute_cron_stores_the_agent_result(self, tmp_path, monkeypatch):
        from core.config import HelixConfig, CronJob, save_config, load_config
        from core import config as config_mod
        import web.app as app_mod

        cfg_path = tmp_path / "config.json"
        cron = CronJob(
            id="abc12345", name="stress", schedule="0 8 * * *", prompt="run 50",
        )
        cfg = HelixConfig(workspace_path=str(tmp_path / "ws"), crons=[cron])
        save_config(cfg, cfg_path)
        real_load, real_save = config_mod.load_config, config_mod.save_config
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(
            config_mod, "save_config",
            lambda cfg, path=cfg_path: real_save(cfg, cfg_path),
        )
        config_mod._config_cache = None
        config_mod._config_mtime = 0.0

        loop = MagicMock()
        loop.run_cron = AsyncMock(return_value="50/50 ok, no mix-ups")
        monkeypatch.setattr(app_mod, "_agent_loop", loop)

        await app_mod._execute_cron("abc12345")
        saved = load_config(cfg_path, force_reload=True)
        assert saved.crons[0].last_status == "ok"
        assert "50/50" in saved.crons[0].last_result
        assert saved.crons[0].last_run
        config_mod._config_cache = None

    @pytest.mark.anyio
    async def test_execute_cron_stores_a_crash(self, tmp_path, monkeypatch):
        from core.config import HelixConfig, CronJob, save_config, load_config
        from core import config as config_mod
        import web.app as app_mod

        cfg_path = tmp_path / "config.json"
        cron = CronJob(
            id="abc12345", name="stress", schedule="0 8 * * *", prompt="run 50",
        )
        cfg = HelixConfig(workspace_path=str(tmp_path / "ws"), crons=[cron])
        save_config(cfg, cfg_path)
        real_load, real_save = config_mod.load_config, config_mod.save_config
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=cfg_path, force_reload=False: real_load(cfg_path, force_reload=True),
        )
        monkeypatch.setattr(
            config_mod, "save_config",
            lambda cfg, path=cfg_path: real_save(cfg, cfg_path),
        )
        config_mod._config_cache = None
        config_mod._config_mtime = 0.0

        loop = MagicMock()
        loop.run_cron = AsyncMock(side_effect=RuntimeError("cli exit 1"))
        monkeypatch.setattr(app_mod, "_agent_loop", loop)

        await app_mod._execute_cron("abc12345")
        saved = load_config(cfg_path, force_reload=True)
        assert saved.crons[0].last_status == "error"
        assert "cli exit 1" in saved.crons[0].last_result
        config_mod._config_cache = None


class TestCronScheduleHealth:
    """A successful run yesterday must not look healthy if today's slot was skipped."""

    def _cron(self, **kwargs):
        from core.config import CronJob
        return CronJob(name="morning", schedule="0 7 * * *", prompt="go", **kwargs)

    def test_previous_fire_is_this_morning_after_seven(self):
        from core.cron_health import previous_fire_time
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        now = datetime(2026, 8, 21, 15, 13, tzinfo=tz)
        due = previous_fire_time("0 7 * * *", now, "Europe/Oslo")
        assert due == datetime(2026, 8, 21, 7, 0, tzinfo=tz)

    def test_previous_fire_is_yesterday_before_seven(self):
        from core.cron_health import previous_fire_time
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        now = datetime(2026, 8, 21, 6, 59, tzinfo=tz)
        due = previous_fire_time("0 7 * * *", now, "Europe/Oslo")
        assert due == datetime(2026, 8, 20, 7, 0, tzinfo=tz)

    def test_missed_when_last_run_was_yesterday(self):
        from core.cron_health import schedule_state
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 7, 1, tzinfo=tz).isoformat(),
        )
        state = schedule_state(
            cron, now=datetime(2026, 8, 21, 15, 0, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert state["missed"] is True
        assert state["expected_at"].startswith("2026-08-21T07:00")

    def test_not_missed_when_it_ran_this_morning(self):
        from core.cron_health import schedule_state
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 21, 7, 2, tzinfo=tz).isoformat(),
        )
        state = schedule_state(
            cron, now=datetime(2026, 8, 21, 15, 0, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert state["missed"] is False

    def test_not_missed_before_todays_slot(self):
        from core.cron_health import schedule_state
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 7, 1, tzinfo=tz).isoformat(),
        )
        state = schedule_state(
            cron, now=datetime(2026, 8, 21, 6, 50, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert state["missed"] is False

    def test_grace_period_right_after_due_time(self):
        from core.cron_health import schedule_state
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 7, 1, tzinfo=tz).isoformat(),
        )
        state = schedule_state(
            cron, now=datetime(2026, 8, 21, 7, 5, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert state["missed"] is False

    def test_paused_job_is_not_missed(self):
        from core.cron_health import schedule_state
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=False,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 7, 1, tzinfo=tz).isoformat(),
        )
        state = schedule_state(
            cron, now=datetime(2026, 8, 21, 15, 0, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert state["missed"] is False

    def test_weekday_schedule_skips_sunday(self):
        from core.cron_health import previous_fire_time
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        # Friday 21 Aug 2026; previous weekday 08:00 is Friday.
        now = datetime(2026, 8, 21, 10, 0, tzinfo=tz)
        due = previous_fire_time("0 8 * * 1-5", now, "Europe/Oslo")
        assert due == datetime(2026, 8, 21, 8, 0, tzinfo=tz)
        sunday = datetime(2026, 8, 23, 10, 0, tzinfo=tz)
        due_sun = previous_fire_time("0 8 * * 1-5", sunday, "Europe/Oslo")
        assert due_sun == datetime(2026, 8, 21, 8, 0, tzinfo=tz)

    def test_catch_up_after_a_late_morning_start(self):
        from core.cron_health import should_catch_up
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 14, 15, tzinfo=tz).isoformat(),
        )
        assert should_catch_up(
            cron, now=datetime(2026, 8, 21, 8, 10, tzinfo=tz), timezone="Europe/Oslo",
        )
        assert should_catch_up(
            cron, now=datetime(2026, 8, 21, 7, 55, tzinfo=tz), timezone="Europe/Oslo",
        )

    def test_no_catch_up_in_the_afternoon(self):
        from core.cron_health import should_catch_up
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 20, 14, 15, tzinfo=tz).isoformat(),
        )
        assert not should_catch_up(
            cron, now=datetime(2026, 8, 21, 15, 20, tzinfo=tz), timezone="Europe/Oslo",
        )

    def test_no_catch_up_when_this_morning_already_ran(self):
        from core.cron_health import should_catch_up
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Oslo")
        cron = self._cron(
            enabled=True,
            last_status="ok",
            last_run=datetime(2026, 8, 21, 7, 56, tzinfo=tz).isoformat(),
        )
        assert not should_catch_up(
            cron, now=datetime(2026, 8, 21, 8, 10, tzinfo=tz), timezone="Europe/Oslo",
        )

